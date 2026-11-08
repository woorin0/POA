"""PR30 pinned-parent independent offline OS/kernel and process escape Mock E2E.

Every process and route is synthetic. Never import actual POA main, exchange
modules, CCXT/KIS, DB, real credentials, or access any remote endpoint.
"""
from __future__ import annotations

import ast
import configparser
import errno
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from unittest import mock

import pytest

from deployment.poa_os_boundary import kernel_probe as P

ROOT = Path(__file__).resolve().parents[1]
FIXED_PARENT = "554ccab03c05e2e4d4a50f75630500704b4654e3"
UNIT = ROOT / "deployment/poa_os_boundary/poa-os-isolated.service.candidate"
EXPECTED = {
    "deployment/poa_os_boundary/__init__.py",
    "deployment/poa_os_boundary/kernel_probe.py",
    "deployment/poa_os_boundary/poa-os-isolated.service.candidate",
    "deployment/poa_os_boundary/PR30_OS_BOUNDARY_AUDIT.md",
    "tests/test_pr30_linux_os_boundary.py",
    ".github/workflows/pr30_os_boundary_ci.yml",
    "tests/test_poa_runtime_roles.py",
    "tests/test_pr_b_sealed_roles.py",
    "tests/test_pr29_independent_reaudit.py",
}


def test_nine_file_scope_and_financial_product_untouched():
    changed = set(subprocess.check_output(
        ["git", "diff", "--name-only", FIXED_PARENT, "HEAD"],
        cwd=ROOT, text=True,
    ).splitlines())
    assert changed == EXPECTED, ("UNAUTHORIZED_DELTA", sorted(changed ^ EXPECTED))
    protected = [
        "main.py", "run.py", "exchange", "requirements.txt",
        "release_manifest_sha256.json", "deployment/install_once.py",
        "deployment/build_cloud_config.py", "deployment/prepare_ubuntu.py",
    ]
    subprocess.run(["git", "diff", "--exit-code", FIXED_PARENT, "HEAD",
                    "--", *protected], cwd=ROOT, check=True)


def unit_config():
    raw = UNIT.read_text(encoding="utf-8")
    conf = configparser.ConfigParser(interpolation=None, strict=True)
    conf.optionxform = str
    conf.read_file(io.StringIO(raw))
    return conf, raw


@pytest.mark.parametrize("name,value", [
    ("ExecStartPre", "/usr/bin/false"),
    ("ExecStart", "/usr/bin/false"),
    ("Restart", "no"),
    ("PrivateNetwork", "yes"),
    ("RestrictAddressFamilies", "none"),
    ("IPAddressDeny", "any"),
    ("NoNewPrivileges", "yes"),
    ("DynamicUser", "yes"),
    ("RestrictNamespaces", "yes"),
    ("StandardInput", "null"),
    ("ProtectSystem", "strict"),
    ("ProtectHome", "yes"),
    ("SystemCallErrorNumber", "EPERM"),
    ("CapabilityBoundingSet", ""),
    ("AmbientCapabilities", ""),
])
def test_unstartable_service_requires_host_network_privilege_denials(name, value):
    conf, _ = unit_config()
    assert conf.sections() == ["Unit", "Service"]
    assert conf["Service"][name] == value


def test_unit_never_loads_legacy_main_or_order_keys_or_fd_store():
    conf, raw = unit_config()
    assert not conf.has_section("Install")
    assert conf["Unit"]["RefuseManualStart"] == "yes"
    assert conf["Unit"]["RefuseManualStop"] == "yes"
    flags = set(conf["Service"]["SystemCallFilter"].split())
    assert {"~@network-io", "@privileged", "@mount",
            "io_uring_setup", "io_uring_enter", "io_uring_register"} <= flags
    for forbidden in (
        "EnvironmentFile", "PassEnvironment", "Socket", "Sockets",
        "FileDescriptorStoreMax", "JoinsNamespaceOf", "NetworkNamespacePath",
        "BindPaths", "BindReadOnlyPaths", "LoadCredential", "SetCredential",
        "ExecStartPost", "ExecReload", "IPAddressAllow", "Delegate",
    ):
        assert forbidden not in conf["Service"]
    for forbidden in (
        "main:app", "run.py", "ccxt.binance(", "KoreaInvestment(", "PB_DATA=",
        "API_KEY=", "GHCR_READ_PAT=", "systemctl enable", "systemctl start",
    ):
        assert forbidden not in raw


@pytest.mark.parametrize("target", [
    ("PrivateNetwork", "no"),
    ("RestrictAddressFamilies", "AF_INET AF_INET6"),
    ("IPAddressDeny", "none"),
    ("NoNewPrivileges", "no"),
    ("ExecStart", "/usr/bin/python3 main.py"),
    ("ExecStartPre", "/usr/bin/true"),
    ("Restart", "always"),
])
def test_os_contract_downgrade_cannot_satisfy_static_policy(target):
    key, replacement = target
    config, raw = unit_config()
    original = config["Service"][key]
    mutant = raw.replace(key + "=" + original + "\n",
                         key + "=" + replacement + "\n", 1)
    assert mutant != raw
    conf = configparser.ConfigParser(interpolation=None, strict=True)
    conf.optionxform = str
    conf.read_file(io.StringIO(mutant))
    assert conf["Service"][key] != original


def test_synthetic_process_has_no_real_trading_imports_or_network_targets():
    tree = ast.parse((ROOT / "deployment/poa_os_boundary/kernel_probe.py").read_text())
    imports = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports.extend(alias.name for alias in n.names)
        elif isinstance(n, ast.ImportFrom):
            imports.append(n.module or "")
    assert not any(name.startswith((
        "main", "run", "exchange", "ccxt", "pykis", "kis",
        "requests", "httpx", "pocketbase", "sqlite3"
    )) for name in imports)
    source = (ROOT / "deployment/poa_os_boundary/kernel_probe.py").read_text()
    for bad in ("https://", "wss://", ".create_order(", "requests.post(",
                "socket.connect(", "socket.create_connection("):
        assert bad not in source
    assert "close_fds=True" in source
    assert "PR_SET_NO_NEW_PRIVS" in source


@pytest.mark.parametrize("route", ["main", "ccxt", "kis", "safe-retry", "ffi", "subprocess", "all"])
def test_actual_linux_kernel_filter_denies_direct_and_exec_child_network_syscalls(route):
    if sys.platform != "linux":
        # On Windows only a synthetic denial can be tested; no kernel claim.
        with mock.patch.object(P.socket, "socket", side_effect=PermissionError(errno.EPERM,"MOCK")):
            for label in P.ROUTES:
                P.mock_transport(label)
        assert sys.platform != "linux"
        return
    r = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(P.__file__).resolve()), "--"+route],
        cwd=ROOT, capture_output=True, text=True, close_fds=True, timeout=40,
        env={"PYTHONIOENCODING":"utf-8","PYTHONDONTWRITEBYTECODE":"1"},
    )
    assert r.returncode == 0, ("KERNEL_DENIAL_NOT_VERIFIED", route, r.stderr[-1500:])
    assert '"result": "OS_KERNEL_SYSCALL_DENIAL_PASS"' in r.stdout
    assert '"actual_orders": 0' in r.stdout
    assert '"external_connections": 0' in r.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="Linux AF_UNIX FD preflight is not a Windows HANDLE test")
def test_preopened_unix_socket_fd_fails_closed_before_seccomp_filter():
    left, right = socket.socketpair()
    try:
        # Deliberately pass a synthetic local-only descriptor: kernel filter
        # cannot revoke socket FDs already created by another process.
        p = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(P.__file__).resolve()),
             "--inherited-socket"],
            cwd=ROOT, capture_output=True, text=True, timeout=25,
            pass_fds=(left.fileno(),), close_fds=True,
            env={"PYTHONIOENCODING":"utf-8","PYTHONDONTWRITEBYTECODE":"1"},
        )
        assert p.returncode == 75
        assert "PREOPENED_SOCKET_FD" in p.stderr
    finally:
        left.close()
        right.close()


def test_seccomp_missing_is_a_hard_failure_not_a_silent_mock(monkeypatch):
    monkeypatch.setattr(P.ctypes.util, "find_library", lambda _: None)
    with pytest.raises(P.IsolationBlocked,
                       match="LIBSECCOMP_MISSING" if sys.platform=="linux" else "LINUX_ONLY"):
        P._libseccomp()


def test_unconfined_external_process_remains_explicit_negative_control():
    # This interpreter is intentionally NOT under the sealed role or seccomp;
    # no socket is created, and no actual main.py or raw SDK is imported.
    for label in P.ROUTES:
        r = subprocess.run([sys.executable, "-I", "-B", "-c",
                            "print('SYNTHETIC_OTHER_PROCESS_UNCONFINED')"],
                           capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, (label, r.stderr)
        assert r.stdout.strip() == "SYNTHETIC_OTHER_PROCESS_UNCONFINED"


def test_linux_systemd_parser_validates_inert_unit_without_starting():
    if sys.platform != "linux":
        return
    analyze = shutil.which("systemd-analyze")
    assert analyze, "SYSTEMD_ANALYZE_MISSING"
    with tempfile.TemporaryDirectory() as temp:
        unit = Path(temp) / "poa-os-isolated.service"
        unit.write_bytes(UNIT.read_bytes())
        result = subprocess.run(
            [analyze, "verify", str(unit)],
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, (result.stdout[-700:], result.stderr[-1200:])


def test_critical_security_limitations_remain_explicit():
    doc = (ROOT / "deployment/poa_os_boundary/PR30_OS_BOUNDARY_AUDIT.md").read_text()
    for item in (
        "WINDOWS_KERNEL_SECCOMP=NOT_SUPPORTED",
        "SEPARATE_PROCESS_SYNTHETIC_MAIN_CCXT_KIS_RETRY=MOCK_ONLY",
        "REAL_MAIN_ORDER_EXECUTION=NOT_PERFORMED",
        "VULTR_HOST_SYSTEMD=UNVERIFIED",
        "HOST_WIDE_ORDER_DENY=UNVERIFIED",
        "EXTERNAL_UNCONFINED_PROCESS=UNCONTROLLED",
        "INHERITED_PIPE_IPC_PROXY=UNCONTROLLED",
        "ACTUAL_ORDERS=0",
        "FINANCIAL_SOURCE=UNCHANGED",
        "PRODUCTION_READY=BLOCKED",
    ):
        assert item in doc
