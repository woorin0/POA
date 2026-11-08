"""Minimal POA Stage/Read-only local Unix gateway and Ubuntu24 preflight E2E.

Only synthetic stdin or AF_UNIX health traffic. Never imports original
main.py, CCXT, KIS, trading credentials, operating DB or external networks.
"""
from __future__ import annotations

import ast
import configparser
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from deployment import poa_ubuntu24_preflight as P
from poa_runtime import local_unix_gateway as G

ROOT = Path(__file__).resolve().parents[1]
PARENT = "554ccab03c05e2e4d4a50f75630500704b4654e3"
MAIN = "c5566c4cc2a4ce716f1f0d66260bd60e4f95546f"
WORKER = ROOT / "poa_runtime/local_worker.py"
GATEWAY = ROOT / "poa_runtime/local_unix_gateway.py"
NEW = {
    "poa_runtime/local_worker.py",
    "poa_runtime/local_unix_gateway.py",
    "deployment/poa_ubuntu24_preflight.py",
    "deployment/poa-stage-uds.service.candidate",
    "deployment/poa-readonly-uds.service.candidate",
    "deployment/PR30_MINIMAL_RUNTIME_PREFLIGHT.md",
    "tests/test_pr30_minimal_runtime.py",
    ".github/workflows/pr30_minimal_runtime_ci.yml",
    "tests/test_poa_runtime_roles.py",
    "tests/test_pr_b_sealed_roles.py",
    "tests/test_pr29_independent_reaudit.py",
}


def test_exact_eleven_paths_parent_and_original_financial_assets_preserved():
    modified = set(subprocess.check_output(
        ["git", "diff", "--name-only", PARENT, "HEAD"], cwd=ROOT, text=True
    ).splitlines())
    assert modified == NEW, ("UNAUTHORIZED_CHANGES", sorted(modified ^ NEW))
    protected = (
        "main.py", "run.py", "exchange", "requirements.txt",
        "release_manifest_sha256.json", "deployment/install_once.py",
        "deployment/build_cloud_config.py", "deployment/prepare_ubuntu.py",
        "tests/run_offline.py",
    )
    subprocess.run(["git", "diff", "--exit-code", MAIN, "HEAD", "--", *protected],
                   cwd=ROOT, check=True)


@pytest.mark.parametrize("role,method,path,expected", [
    ("stage", "GET", "/__stage/health", 200),
    ("stage", "HEAD", "/__stage/health", 200),
    ("stage", "POST", "/order", 403),
    ("stage", "POST", "/hedge", 403),
    ("stage", "GET", "/__read/health", 403),
    ("read-only", "GET", "/__read/health", 200),
    ("read-only", "HEAD", "/__read/health", 200),
    ("read-only", "POST", "/order", 403),
    ("read-only", "GET", "/__stage/health", 403),
])
def test_pr30_sealed_worker_executed_in_separate_child(role, method, path, expected):
    request = json.dumps({"method": method, "path": path})
    p = subprocess.run(
        [sys.executable, "-I", "-B", "-S", str(WORKER), role],
        input=request, capture_output=True, text=True, close_fds=True,
        cwd=ROOT, timeout=20,
        env={"PYTHONDONTWRITEBYTECODE":"1", "PYTHONIOENCODING":"utf-8"},
    )
    assert p.returncode == 0, (p.returncode, p.stderr[-800:])
    response = json.loads(p.stdout)
    assert set(response) == {"status", "body"}
    assert response["status"] == expected
    if expected == 200:
        if method == "HEAD":
            assert response["body"] == ""  # Correct ASGI HEAD: zero response body.
        else:
            assert '"orders_enabled":false' in response["body"]
    else:
        assert "DENIED" in response["body"] or "NOT_AUTHORIZED" in response["body"]


@pytest.mark.parametrize("payload,role", [
    ("not-json", "stage"),
    ('{"path":"/order"}', "stage"),
    ('{"path":"/order","method":"POST","trading":true}', "stage"),
    ('{"method":"POST","path":"/order"}', "trading"),
    ('{"method":"GET","path":"http://exchange.invalid/"}', "stage"),
])
def test_worker_bad_input_and_trading_role_fail_closed(payload, role):
    p = subprocess.run([sys.executable, "-I", "-B", "-S", str(WORKER), role],
        input=payload, capture_output=True, text=True, timeout=20,
        cwd=ROOT, close_fds=True,
        env={"PYTHONDONTWRITEBYTECODE":"1", "POA_TRADE_ALLOW":"1",
             "BINANCE_API_SECRET":"SYNTHETIC_CANARY"})
    assert p.returncode != 0
    assert "SYNTHETIC_CANARY" not in p.stdout + p.stderr


@pytest.mark.parametrize("role,method,path,expected", [
    ("stage","GET","/__stage/health",200),
    ("stage","POST","/order",403),
    ("read-only","GET","/__read/health",200),
    ("read-only","POST","/order",403),
])
def test_local_gateway_spawns_real_sealed_worker_without_any_live_sdk(role,method,path,expected):
    status,body = G._worker(role,method,path)
    assert status == expected
    assert b"api_key" not in body.lower()
    assert b"SYNTHETIC_CANARY" not in body


def _client(path,request):
    code = r'''
import socket,sys
sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
sock.settimeout(6)
sock.connect(sys.argv[1])
sock.sendall(sys.argv[2].encode("ascii"))
parts=[]
while True:
    block=sock.recv(4096)
    if not block: break
    parts.append(block)
sock.close()
sys.stdout.buffer.write(b"".join(parts))
'''
    p = subprocess.run([sys.executable,"-I","-B","-S","-c",code,
                        str(path),request],capture_output=True,timeout=15,
                       env={"PYTHONDONTWRITEBYTECODE":"1"})
    assert p.returncode == 0, p.stderr[-300:]
    return p.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="Linux local AF_UNIX integration only")
@pytest.mark.parametrize("role,health", [("stage","/__stage/health"),("read-only","/__read/health")])
def test_real_local_unix_socket_process_health_order_and_malformed_denial(role,health):
    with tempfile.TemporaryDirectory() as parent:
        path = Path(parent)/"poa.sock"
        server = subprocess.Popen(
            [sys.executable,"-I","-B","-S",str(GATEWAY),
             "--role",role,"--unix-socket",str(path)],
            cwd=ROOT, close_fds=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PYTHONIOENCODING":"utf-8","PYTHONDONTWRITEBYTECODE":"1"},
        )
        try:
            for _ in range(60):
                if path.exists() or server.poll() is not None: break
                time.sleep(0.05)
            assert path.exists(), "GATEWAY_START_FAILED"
            response = _client(path, "GET " + health + " HTTP/1.1\r\nHost: local\r\n\r\n")
            assert response.startswith(b"HTTP/1.1 200 OK"), response[:160]
            assert b'"orders_enabled":false' in response
            denied = _client(path, "POST /order HTTP/1.1\r\nHost: local\r\n\r\n")
            assert denied.startswith(b"HTTP/1.1 403 Forbidden"), denied[:160]
            malformed = _client(path, "GET http://external.invalid/ HTTP/1.1\r\n\r\n")
            assert malformed.startswith(b"HTTP/1.1 400 Bad Request")
        finally:
            server.terminate()
            try: server.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill(); server.communicate(timeout=5)


def test_unix_server_never_reuses_existing_socket_or_non_private_parent():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)/"existing.sock"
        path.write_text("DO_NOT_DELETE")
        with pytest.raises(ValueError,match="SOCKET_PATH_ALREADY_EXISTS"):
            G.serve("stage", str(path))
        assert path.read_text() == "DO_NOT_DELETE"
        with pytest.raises(ValueError,match="TRADING_ROLE_DISABLED"):
            G.serve("trading", str(Path(d)/"new.sock"))


def test_gateway_is_sanitized_exec_only_no_production_imports_or_remote_calls():
    for name in ("poa_runtime/local_worker.py","poa_runtime/local_unix_gateway.py"):
        source = (ROOT/name).read_text()
        tree = ast.parse(source)
        imports=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node,ast.ImportFrom):
                imports.append(node.module or "")
        assert not any(i.startswith(("ccxt","pykis","exchange",
            "main","run","pocketbase","sqlite3","requests","httpx")) for i in imports)
    src = GATEWAY.read_text()
    assert 'close_fds=True' in src
    assert '"-I", "-B", "-S"' in src
    assert "shell=True" not in src
    assert 'getattr(socketserver, "UnixStreamServer", socketserver.BaseServer)' in src
    assert "LINUX_ONLY_UNIX_SOCKET" in src
    assert "subprocess.run(" in src


def _facts():
    return {
        "ubuntu_2404": True, "amd64": True, "cpu_count":4,
        "ram_bytes": 8*1024**3, "free_bytes":30*1024**3,
        "systemd_version":255,"cgroup_v2":True,"seccomp_actions":True,
        "unprivileged":True,"inherited_socket_count":0,
        "legacy_process_count":0,"sensitive_env_present":False,
        "docker_socket_present":False,
    }


def test_preflight_pure_positive_facts_only_grant_static_gate():
    report=P.evaluate(_facts())
    assert report["preinstall_static_gate"]=="PASS"
    assert all(x=="PASS" for x in report["checks"].values())
    assert report["operational_ready"]=="BLOCKED"
    assert report["live_exchange_reads"]=="BLOCKED"
    assert report["trading"]=="BLOCKED"
    assert report["install_or_mutation_performed"] is False


@pytest.mark.parametrize("key,value", [
    ("ubuntu_2404", False),("amd64",False),("cpu_count",1),
    ("ram_bytes",1024),("free_bytes",1000),("systemd_version",249),
    ("cgroup_v2",False),("seccomp_actions",False),("unprivileged",False),
    ("inherited_socket_count",-1),("inherited_socket_count",1),
    ("legacy_process_count",-1),("legacy_process_count",1),
    ("sensitive_env_present",True),("docker_socket_present",True),
])
def test_preflight_missing_or_unsafe_host_capability_hard_blocked(key,value):
    facts=_facts(); facts[key]=value
    report=P.evaluate(facts)
    assert report["preinstall_static_gate"]=="BLOCKED"
    assert report["checks"]


@pytest.mark.parametrize("role,slug", [("stage","stage"),("read-only","readonly")])
def test_systemd_unit_is_unstartable_secretless_and_local_only(role,slug):
    raw=(ROOT/f"deployment/poa-{slug if role=='stage' else 'readonly'}-uds.service.candidate").read_text()
    conf=configparser.ConfigParser(strict=True,interpolation=None)
    conf.optionxform=str
    conf.read_file(io.StringIO(raw))
    assert conf.sections()==["Unit","Service"]
    assert not conf.has_section("Install")
    assert conf["Unit"]["RefuseManualStart"]=="yes"
    for name,value in {
        "ExecStartPre":"/usr/bin/false","Restart":"no",
        "NoNewPrivileges":"yes","DynamicUser":"yes",
        "PrivateNetwork":"yes","IPAddressDeny":"any",
        "RestrictAddressFamilies":"AF_UNIX",
        "RuntimeDirectoryMode":"0700","ProtectSystem":"strict",
        "ProtectHome":"yes","CapabilityBoundingSet":"",
        "AmbientCapabilities":"",
    }.items():
        assert conf["Service"][name]==value, name
    assert "local_unix_gateway.py" in conf["Service"]["ExecStart"]
    assert "--role "+role+" " in conf["Service"]["ExecStart"]
    assert "--unix-socket /run/poa-"+slug+"/health.sock" in conf["Service"]["ExecStart"]
    for forbidden in ("EnvironmentFile", "PassEnvironment","LoadCredential",
                      "BindPaths","JoinsNamespaceOf","Sockets"):
        assert forbidden not in conf["Service"]
    assert "main:app" not in raw and "systemctl enable" not in raw


def test_doc_keeps_all_operational_blockers_explicit():
    doc=(ROOT/"deployment/PR30_MINIMAL_RUNTIME_PREFLIGHT.md").read_text()
    for key in (
        "PR31_VERIFIED_TEST_ONLY=PASS",
        "PRODUCTION_READY=BLOCKED","ORIGINAL_MAIN_EXTERNAL_PROCESS=UNCONTROLLED",
        "ACTUAL_VULTR_HOST=UNVERIFIED","DB_RESTORE=NOT_PERFORMED",
        "READ_ONLY_KEY_ATTESTATION=UNVERIFIED","LIVE_ORDERS=0",
        "FINANCE_AND_DB_SOURCE=UNCHANGED",
    ):
        assert key in doc
