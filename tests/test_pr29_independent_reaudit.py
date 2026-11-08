"""Independent PR #29 adversarial audit of the sealed, inert Stage/Read-only boundary.

No original POA main/exchange modules are imported or executed, and no real
network, account keys, operating DB, KIS or exchange SDK is used. All attacks
are disposable interpreters with *synthetic* client stubs.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PARENT = "89797bd6214011e7b0e3a6f9ab1e96b845773f45"

BASE_CHILD = r'''
import importlib, sys
from poa_runtime.roles import Role
from poa_runtime.sealed_boundary import SealedRoleDenied, assert_seal_intact, seal_role
role = Role(sys.argv[1])
app_module = "poa_runtime.sealed_stage" if role is Role.STAGE else "poa_runtime.sealed_readonly"
app = importlib.import_module(app_module).app
assert assert_seal_intact(role)
async def receive():
    raise AssertionError("UNEXPECTED_BODY_ACCESS")
seen=[]
async def send(event): seen.append(event)
def drive():
    coro = app({"type":"http","method":"POST","path":"/order","headers":[]},receive,send)
    try: coro.send(None)
    except StopIteration: return
    else:
        coro.close()
        raise AssertionError("UNEXPECTED_AWAIT_OR_IO")
drive()
assert seen[0]["status"] == 403
'''


def child(script, role="stage", *, pass_fds=()):
    env = {**os.environ, "POA_RUNTIME_ROLE": "trading",
           "POA_TRADE_ALLOW": "1", "BINANCE_SECRET": "SYNTHETIC_CANARY"}
    p = subprocess.run(
        [sys.executable, "-B", "-c", script, role],
        cwd=ROOT, env=env, capture_output=True, text=True,
        close_fds=True, pass_fds=pass_fds, timeout=30,
    )
    assert "SYNTHETIC_CANARY" not in p.stdout + p.stderr
    return p


@pytest.mark.parametrize("role", ["stage", "read-only"])
@pytest.mark.parametrize("target", ["ccxt", "exchange.safe_retry", "exchange.stock.kis", "main"])
def test_sys_modules_poisoned_cache_fail_closed_at_next_asgi_boundary(role, target):
    script = BASE_CHILD + r'''
import types
fake = types.ModuleType(sys.argv[2])
fake.create_order = lambda *args: "SYNTHETIC_ONLY"
sys.modules[sys.argv[2]] = fake
# A cached module can bypass Python's import finder before the next
# request: do NOT mislabel this as globally blocked.
assert importlib.import_module(sys.argv[2]) is fake
assert fake.create_order("BTCUSDT") == "SYNTHETIC_ONLY"
try:
    drive()
except SealedRoleDenied as exc:
    assert exc.code == "POA_SEALED_ROLE_MODULE_CACHE_TAMPERED"
else:
    raise AssertionError("POISONED_MODULE_WAS_NOT_REJECTED")
print("CACHE_BYPASS_DETECTED_NEXT_REQUEST actual_orders=0")
'''
    # The tested module name is a literal synthetic stub, not a real SDK.
    p = child(script.replace("sys.argv[2]", repr(target)), role=role)
    assert p.returncode == 0, p.stderr[-1400:]
    assert "CACHE_BYPASS_DETECTED_NEXT_REQUEST" in p.stdout


@pytest.mark.parametrize("role", ["stage", "read-only"])
def test_sys_meta_path_finder_removal_is_rejected_at_next_request(role):
    script = BASE_CHILD + '''
sys.meta_path.pop(0)
try:
    drive()
except SealedRoleDenied as exc:
    assert exc.code == "POA_SEALED_ROLE_IMPORT_POLICY_TAMPERED"
else:
    raise AssertionError("IMPORT_POLICY_TAMPER_WAS_NOT_REJECTED")
print("META_PATH_REMOVAL_DETECTED actual_orders=0")
'''
    p=child(script,role=role)
    assert p.returncode == 0, p.stderr[-1200:]
    assert "META_PATH_REMOVAL_DETECTED" in p.stdout


def test_audit_hook_native_side_effects_still_blocked():
    script = BASE_CHILD + '''
import os, socket, subprocess
for attack in (
    lambda: socket.socket(),
    lambda: os.system("echo NEVER_EXECUTE"),
    lambda: subprocess.run([sys.executable,"-I","-c","pass"]),
):
    try:
        attack()
    except SealedRoleDenied as exc:
        assert exc.code == "POA_SEALED_ROLE_SIDE_EFFECT_DENIED"
    else:
        raise AssertionError("AUDIT_HOOK_EXECUTION_BYPASS")
print("NATIVE_PYTHON_EVENTS_DENIED actual_orders=0")
'''
    p=child(script)
    assert p.returncode == 0, p.stderr[-1200:]
    assert "NATIVE_PYTHON_EVENTS_DENIED" in p.stdout


@pytest.mark.skipif(sys.platform!="linux", reason="Windows inherited socket HANDLE kernel check UNVERIFIED")
@pytest.mark.parametrize("kind", ["unix-socketpair", "inet-unconnected"])
def test_linux_inherited_socket_fd_rejected_before_role_seal(kind):
    if kind=="unix-socketpair":
        sock, peer = socket.socketpair()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        peer = None
    try:
        script = '''
import sys
from poa_runtime.roles import Role
from poa_runtime.sealed_boundary import SealedRoleDenied, seal_role
try:
    seal_role(Role.STAGE)
except SealedRoleDenied as exc:
    assert exc.code == "POA_SEALED_ROLE_INHERITED_SOCKET_FD"
else:
    raise AssertionError("INHERITED_FD_ACCEPTED")
print("INHERITED_FD_REJECTED actual_orders=0")
'''
        p = child(script,pass_fds=(sock.fileno(),))
        assert p.returncode == 0, p.stderr[-1200:]
        assert "INHERITED_FD_REJECTED" in p.stdout
    finally:
        sock.close()
        if peer:peer.close()


def test_os_pipe_and_external_process_are_explicit_negative_controls():
    # os.write on a preopened pipe does not emit a CPython audit event. A
    # compromised process may write through inherited transport/IPC handles.
    script = BASE_CHILD + '''
import os
reader, writer = os.pipe()
try:
    assert os.write(writer, b"SYNTHETIC_PIPE_ONLY") == len(b"SYNTHETIC_PIPE_ONLY")
    assert os.read(reader,128) == b"SYNTHETIC_PIPE_ONLY"
finally:
    os.close(reader)
    os.close(writer)
print("PIPE_HANDLE_OUTSIDE_AUDIT actual_orders=0")
'''
    p=child(script)
    assert p.returncode == 0, p.stderr[-1200:]
    assert "PIPE_HANDLE_OUTSIDE_AUDIT" in p.stdout
    # A *different process* is necessarily outside the Stage/Read-only
    # interpreter policy. Never import actual legacy product or raw SDK.
    independent=subprocess.run([sys.executable,"-I","-B","-c",
        "print('SYNTHETIC_MAIN_SAFE_RETRY_CCXT_KIS_OTHER_PROCESS')"],
        capture_output=True,text=True,timeout=15,check=True)
    assert independent.stdout.strip()=="SYNTHETIC_MAIN_SAFE_RETRY_CCXT_KIS_OTHER_PROCESS"


def test_full_interpreter_code_object_and_pre_import_spoof_are_not_security_claims():
    # Untrusted code running inside the interpreter defeats this Python
    # defense-in-depth boundary; these are explicit synthetic negative controls.
    script = BASE_CHILD + '''
marker=[]
exec(compile("marker.append('SYNTHETIC_ONLY')", "<forged>", "exec"),{"marker":marker})
assert marker == ["SYNTHETIC_ONLY"]
print("FORGED_CODE_OBJECT_OUTSIDE_FILENAME_FILTER actual_orders=0")
'''
    p=child(script)
    assert p.returncode == 0, p.stderr[-1200:]
    assert "FORGED_CODE_OBJECT_OUTSIDE_FILENAME_FILTER" in p.stdout


def test_original_pr29_code_lacks_new_cache_and_fd_tripwires():
    original = subprocess.check_output(
        ["git", "show", PARENT+":poa_runtime/sealed_boundary.py"],
        cwd=ROOT, text=True)
    assert "def assert_seal_intact" not in original
    assert "def _reject_inherited_socket_fds" not in original
    current = (ROOT/"poa_runtime/sealed_boundary.py").read_text()
    assert "def assert_seal_intact" in current
    assert "def _reject_inherited_socket_fds" in current


def test_exact_audit_file_scope_finance_and_main_unchanged():
    approved = {
        "poa_runtime/sealed_boundary.py",
        "poa_runtime/sealed_stage.py",
        "poa_runtime/sealed_readonly.py",
        "tests/test_poa_runtime_roles.py",
        "tests/test_pr_b_sealed_roles.py",
        "tests/test_pr29_independent_reaudit.py",
        "deployment/PR29_INDEPENDENT_REAUDIT.md",
        ".github/workflows/pr29_independent_reaudit.yml",
    "poa_runtime/local_worker.py",
    "poa_runtime/local_unix_gateway.py",
    "deployment/poa_ubuntu24_preflight.py",
    "deployment/poa-stage-uds.service.candidate",
    "deployment/poa-readonly-uds.service.candidate",
    "deployment/PR30_MINIMAL_RUNTIME_PREFLIGHT.md",
    "tests/test_pr30_minimal_runtime.py",
    ".github/workflows/pr30_minimal_runtime_ci.yml",
    }
    actual=set(subprocess.check_output(
        ["git","diff","--name-only",PARENT,"HEAD"],cwd=ROOT,text=True).splitlines())
    assert actual==approved, ("UNAUTHORIZED_SCOPE", sorted(approved^actual))
    protected = [
        "main.py", "run.py", "exchange", "requirements.txt",
        "release_manifest_sha256.json", "deployment/install_once.py",
        "deployment/build_cloud_config.py", "deployment/prepare_ubuntu.py",
    ]
    subprocess.run(["git","diff","--exit-code",PARENT,"HEAD","--",*protected],
                   cwd=ROOT,check=True)


def test_unresolved_host_controls_are_not_mislabeled_as_pass():
    doc=(ROOT/"deployment/PR29_INDEPENDENT_REAUDIT.md").read_text()
    for label in (
        "CACHE_INJECTION_BEFORE_NEXT_REQUEST=UNCONTROLLED",
        "BEFORE_SEAL_MODULE_SPOOF=UNCONTROLLED",
        "ARBITRARY_EXEC_CODE=UNCONTROLLED",
        "INHERITED_NON_SOCKET_IPC=UNCONTROLLED",
        "WINDOWS_INHERITED_HANDLES=UNVERIFIED",
        "LEGACY_MAIN_OTHER_PROCESS=UNCONTROLLED",
        "RAW_CCXT_KIS_OTHER_PROCESS=UNCONTROLLED",
        "VULTR_OS_POLICY=UNVERIFIED",
        "FINANCIAL_SOURCE=UNCHANGED",
        "ACTUAL_ORDERS=0",
        "PRODUCTION_READY=BLOCKED",
    ):
        assert label in doc
