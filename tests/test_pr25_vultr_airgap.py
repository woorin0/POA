"""Independent POA PR25→26 Vultr Ubuntu OS/network privilege containment E2E.

No production startup, real S3/cloud, live exchange client, order, DB, key,
systemd activation, server login, socket connection or network packet.

Windows verifies parser/Mock contracts; Ubuntu adds real libseccomp kernel
denial and inherited subprocess syscalls. Actual Vultr systemd is UNVERIFIED.
"""
from __future__ import annotations

import ast
import configparser
import errno
import io
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
from unittest import mock

import pytest

from deployment.vultr_airgap import seccomp_rehearsal as S

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deployment/vultr_airgap/poa-airgap.service.candidate"
PR25_SHA = "db343f2cda151fd396f58b5d8d0544bb7e5c5e6f"
PERMITTED_FILES = {
    "deployment/vultr_airgap/__init__.py",
    "deployment/vultr_airgap/poa-airgap.service.candidate",
    "deployment/vultr_airgap/seccomp_rehearsal.py",
    "deployment/vultr_airgap/PR25_VULTR_AIRGAP_CONTRACT.md",
    "tests/test_pr25_vultr_airgap.py",
    ".github/workflows/pr25_vultr_airgap_ci.yml",
}


def unit_config():
    data = UNIT.read_text(encoding="utf-8")
    c = configparser.ConfigParser(interpolation=None, strict=True)
    c.optionxform = str
    c.read_file(io.StringIO(data))
    return c, data


def service():
    c, data = unit_config()
    assert c.sections() == ["Unit", "Service"]
    return c["Service"]


def test_real_pr25_product_and_database_bytes_unchanged():
    diff = subprocess.check_output(["git", "diff", "--name-only", PR25_SHA, "HEAD"],
                                   cwd=ROOT, text=True).splitlines()
    assert set(diff) == PERMITTED_FILES
    assert subprocess.check_output(["git", "status", "--porcelain"],
                                   cwd=ROOT, text=True).strip() == ""


@pytest.mark.parametrize("name,value", [
    ("ExecStartPre", "/usr/bin/false"),
    ("ExecStart", "/usr/bin/false"),
    ("Restart", "no"),
    ("WorkingDirectory", "/"),
    ("PrivateNetwork", "yes"),
    ("RestrictAddressFamilies", "none"),
    ("IPAddressDeny", "any"),
    ("SystemCallArchitectures", "native"),
    ("NoNewPrivileges", "yes"),
    ("DynamicUser", "yes"),
    ("PrivateDevices", "yes"),
    ("PrivateTmp", "yes"),
    ("ProtectHome", "yes"),
    ("ProtectSystem", "strict"),
    ("RestrictNamespaces", "yes"),
    ("LockPersonality", "yes"),
    ("RestrictSUIDSGID", "yes"),
    ("MemoryDenyWriteExecute", "yes"),
    ("StandardInput", "null"),
])
def test_inert_systemd_os_network_and_privilege_contract(name, value):
    assert service().get(name) == value


def test_systemd_network_syscalls_and_io_uring_blocked_by_contract():
    s = service()
    spec = s["SystemCallFilter"].split()
    assert spec[0] == "~@network-io"
    assert "@privileged" in spec
    assert "@mount" in spec
    assert {"io_uring_setup", "io_uring_enter", "io_uring_register"} <= set(spec)
    assert s["SystemCallErrorNumber"] == "EPERM"
    assert s["CapabilityBoundingSet"] == ""
    assert s["AmbientCapabilities"] == ""


def test_candidate_has_no_install_target_secrets_socket_activation_or_network_bridge():
    c, raw = unit_config()
    assert not c.has_section("Install")
    s = c["Service"]
    for forbidden in ("EnvironmentFile", "Sockets", "JoinsNamespaceOf",
                      "NetworkNamespacePath", "BindPaths", "BindReadOnlyPaths",
                      "RootDirectory", "RootImage", "User", "Group", "PassEnvironment"):
        assert forbidden not in s
    assert s["Environment"] == "POA_STAGE_ONLY=1 PYTHONDONTWRITEBYTECODE=1"
    for unexpected in ("KIS", "AWS_ACCESS_KEY_ID", "GHCR_READ_PAT",
                       "BINANCE_KEY=", "BYBIT_KEY=", "UPBIT_KEY=",
                       "OKX_KEY=", "BITGET_KEY=", "/root/db/pb_data",
                       "poa.service", "main:app", "systemctl enable"):
        assert unexpected not in raw
    assert "/usr/bin/false" in s["ExecStart"]
    assert "RefuseManualStart=yes" in raw


def test_no_python_main_ccxt_kis_or_db_import_in_rehearsal():
    tree = ast.parse((ROOT/"deployment/vultr_airgap/seccomp_rehearsal.py").read_text())
    imports = []
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Import):
            imports.extend(alias.name for alias in stmt.names)
        elif isinstance(stmt, ast.ImportFrom):
            imports.append(stmt.module or "")
    assert not any(n.startswith(("ccxt", "httpx", "requests", "exchange",
                                 "main", "sqlite3", "pocketbase")) for n in imports)


@pytest.mark.parametrize("name", ["ccxt", "kis", "ffi", "subprocess", "all"])
def test_linux_child_actual_kernel_seccomp_rejects_all_socket_creation(name):
    if sys.platform == "linux":
        p = subprocess.run(
            [sys.executable, "-I", "-B", str(S.__file__), "--"+name],
            cwd=ROOT, capture_output=True, text=True, timeout=25,
            env={"PYTHONDONTWRITEBYTECODE":"1","PYTHONIOENCODING":"utf-8"},
            close_fds=True,
        )
        assert p.returncode == 0, ("KERNEL_SECCOMP_GATE_BLOCKED", p.stderr[:300])
        assert "LINUX_SECCOMP_KERNEL_DENY_PASS" in p.stdout
        assert '"api_calls": 0' in p.stdout
        assert '"live_orders": 0' in p.stdout
        if name == "subprocess":
            assert '"subprocess_inherits": true' in p.stdout
    else:
        # Windows cannot run Linux seccomp; explicitly ONLY an in-process
        # mocked denial, never misrepresent it as a real host security proof.
        def rejected_socket(*args, **kwargs):
            raise PermissionError(errno.EPERM, "WINDOWS_SIMULATED_DENY")
        with mock.patch.object(S.socket, "socket", side_effect=rejected_socket):
            if name in ("ccxt", "all"):
                assert S.fake_ccxt_direct_create_order() == "KERNEL_SOCKET_DENIED"
            if name in ("kis", "all"):
                assert S.fake_kis_direct_post_order() == "KERNEL_SOCKET_DENIED"
        assert sys.platform != "linux"


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_mock_transport_denial_fails_closed_without_any_connect(family):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EPERM, "SYNTHETIC_SOCKET_DENIED")
    with mock.patch.object(S.socket, "socket", side_effect=denied) as patched:
        assert S.deny_socket_probe(family) == "KERNEL_SOCKET_DENIED"
        patched.assert_called_once_with(family, socket.SOCK_STREAM, 0)


def test_synthetic_socket_creation_succeeding_is_rejected_without_connect():
    class DummySocket:
        def close(self):
            pass
    with mock.patch.object(S.socket, "socket", return_value=DummySocket()):
        with pytest.raises(AssertionError, match="SOCKET_CREATED_WITHOUT_KERNEL_DENIAL"):
            S.deny_socket_probe(socket.AF_INET)


def test_linux_seccomp_fail_closed_if_library_missing(monkeypatch):
    monkeypatch.setattr(S.ctypes.util, "find_library", lambda _: None)
    if sys.platform == "linux":
        with pytest.raises(S.SandboxUnavailable, match="LIBSECCOMP_NOT_PRESENT"):
            S._libseccomp()
    else:
        with pytest.raises(S.SandboxUnavailable, match="LINUX_ONLY"):
            S._libseccomp()


def test_no_real_network_targets_or_executable_live_order_flows():
    text = (ROOT/"deployment/vultr_airgap/seccomp_rehearsal.py").read_text()
    for disallowed in ("https://", "wss://", "requests.post(",
                       "ccxt.binance(", "KoreaInvestment(", ".create_order(",
                       "socket.create_connection(", "socket.connect("):
        assert disallowed not in text
    assert "SOCKET_CREATED_WITHOUT_KERNEL_DENIAL" in text
    assert "close_fds=True" in text


def test_ubuntu_systemd_unit_parser_when_available():
    if sys.platform != "linux":
        # Windows: equivalent strict text contract is asserted above.
        return
    tool = __import__("shutil").which("systemd-analyze")
    if tool is None:
        pytest.fail("SYSTEMD_ANALYZE_MISSING_LINUX_GATE_BLOCKED")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)/"poa-airgap.service"
        target.write_bytes(UNIT.read_bytes())
        res = subprocess.run([tool, "verify", str(target)],
                             capture_output=True, text=True, timeout=20)
        assert res.returncode == 0, (res.stdout[-700:], res.stderr[-1200:])


def test_no_runtime_install_or_live_order_approval_is_created():
    c, text = unit_config()
    assert c["Service"]["ExecStartPre"] == "/usr/bin/false"
    assert c["Service"]["ExecStart"] == "/usr/bin/false"
    assert "ExecStart=/usr/bin/python" not in text
    assert "systemctl start" not in text
    assert "systemctl enable" not in text
    assert "EnvironmentFile=" not in text


def test_documented_limitations_and_inherited_socket_fd_risk():
    doc = (ROOT/"deployment/vultr_airgap/PR25_VULTR_AIRGAP_CONTRACT.md").read_text()
    for required in (
        "VULTR_HOST_VERIFIED=NO", "LIVE_ORDERS=NOT_EXECUTED",
        "INHERITED_SOCKET_FD=UNVERIFIED", "ACTUAL_SYSTEMD_EXECUTION=NOT_PERFORMED",
        "WINDOWS_SECCOMP=MOCK_ONLY", "RAW_CCXT=SIMULATED_TRANSPORT_ONLY",
        "OPERATING_READY=BLOCKED",
    ):
        assert required in doc
