"""Integration B adversarial OFFLINE Mock E2E for sealed Stage and Read-only.

Every test uses a disposable interpreter; neither legacy POA code, actual
SDKs, operating DB, keys nor live orders are executed.
"""
from __future__ import annotations

import configparser
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PR_A = "a0faaf2b266722d535c666a1068a8f4c987b37e2"
B_FILES = {
    "poa_runtime/sealed_boundary.py",
    "poa_runtime/sealed_stage.py",
    "poa_runtime/sealed_readonly.py",
    "deployment/poa-stage-sealed.service.candidate",
    "deployment/poa-readonly-sealed.service.candidate",
    "deployment/PR_B_SEALED_ROLE_CONTRACT.md",
    "tests/test_pr_b_sealed_roles.py",
    "tests/test_poa_runtime_roles.py",
    ".github/workflows/pr_b_sealed_roles_ci.yml",
}

CHILD = r'''
import importlib, os, runpy, socket, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from poa_runtime.roles import Role
from poa_runtime.sealed_boundary import SealedRoleDenied, active_role, seal_role

role = Role(sys.argv[1])
module = "poa_runtime.sealed_stage" if role is Role.STAGE else "poa_runtime.sealed_readonly"
app = importlib.import_module(module).app
assert callable(app) and active_role() is role
assert seal_role(role) is role
try:
    seal_role(Role.READ_ONLY if role is Role.STAGE else Role.STAGE)
except SealedRoleDenied as exc:
    assert exc.code == "POA_SEALED_ROLE_CONFLICT"
else:
    raise AssertionError("ROLE_SWITCH_ALLOWED")

# Actual product entrypoint names are blocked *before import or initialization*.
for name in ("main", "run", "exchange", "exchange.pexchange",
             "exchange.safe_retry", "exchange.stock.kis",
             "exchange.binance", "exchange.bitget", "exchange.bybit",
             "exchange.okx", "exchange.upbit", "ccxt", "ccxtpro",
             "pocketbase", "sqlite3", "pykis"):
    try:
        importlib.import_module(name)
    except SealedRoleDenied as exc:
        assert exc.code == "POA_SEALED_ROLE_PRODUCT_IMPORT_DENIED"
    else:
        raise AssertionError("IMPORT_ESCAPED:" + name)

# Synthetic no-network probes for raw CCXT/KIS transport, subprocess and
# unexpected worker restart paths. Fail if *any* API reaches a side effect.
for label, operation in (
    ("SOCKET", lambda: socket.socket()),
    ("SPAWN", lambda: subprocess.run([sys.executable, "-c", "pass"])),
    ("SHELL", lambda: os.system("echo SYNTHETIC_ONLY")),
):
    try:
        operation()
    except SealedRoleDenied as exc:
        assert exc.code == "POA_SEALED_ROLE_SIDE_EFFECT_DENIED"
    else:
        raise AssertionError(label + "_ESCAPED")

# Simulate runpy/loader invoking code labeled with the real legacy file
# paths; only synthetic code is compiled, never any actual POA finance code.
for path in (Path("main.py"), Path("run.py"),
             Path("exchange/safe_retry.py"),
             Path("exchange/stock/kis.py"),
             Path("exchange/pexchange.py")):
    marker = []
    code = compile("marker.append('SIDE_EFFECT')", str(path.resolve()), "exec")
    try:
        exec(code, {"marker": marker})
    except SealedRoleDenied as exc:
        assert exc.code == "POA_SEALED_ROLE_PRODUCT_EXEC_DENIED"
    else:
        raise AssertionError("EXEC_ESCAPED:" + str(path))
    assert marker == []

# Existing immutable ASGI role surface: no webhook/body parsing/SDK/DB.
result = []
payload_read = []
async def receive():
    payload_read.append(True)
    return {"type": "http.request", "body": b"SYNTHETIC_SECRET"}
async def send(msg):
    result.append(msg)
coro = app({"type":"http", "method":"POST","path":"/order",
            "headers":[]}, receive, send)
try:
    coro.send(None)
except StopIteration:
    pass
else:
    coro.close()
    raise AssertionError("ASGI_EXTERNAL_IO")
assert result[0]["status"] == 403 and not payload_read
assert b"SYNTHETIC_SECRET" not in result[-1]["body"]

def attempt(_):
    try:
        importlib.import_module("exchange.safe_retry")
    except SealedRoleDenied as exc:
        return exc.code
    return "ORDER_PATH_ESCAPE"
with ThreadPoolExecutor(max_workers=12) as pool:
    outcomes = list(pool.map(attempt, range(203)))
assert outcomes == ["POA_SEALED_ROLE_PRODUCT_IMPORT_DENIED"] * 203
print("SEALED_ROLE_MOCK_203_PASS role=" + role.value + " actual_orders=0")
'''


@pytest.mark.parametrize("role", ["stage", "read-only"])
def test_real_child_interpreter_seals_all_legacy_product_imports_and_side_effects(role):
    env = {**os.environ, "POA_RUNTIME_ROLE": "trading",
           "POA_APPROVE_LIVE_ORDERS": "true",
           "BINANCE_SECRET": "SYNTHETIC_SECRET"}
    result = subprocess.run(
        [sys.executable, "-B", "-c", CHILD, role], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=35, close_fds=True,
    )
    assert result.returncode == 0, (result.stdout[-500:], result.stderr[-1200:])
    assert "SEALED_ROLE_MOCK_203_PASS" in result.stdout
    assert "actual_orders=0" in result.stdout
    assert "SYNTHETIC_SECRET" not in result.stdout + result.stderr


def test_preloaded_legacy_module_is_refused_before_seal():
    code = """
import sys, types
sys.modules['exchange'] = types.ModuleType('exchange')
from poa_runtime.roles import Role
from poa_runtime.sealed_boundary import seal_role, SealedRoleDenied
try: seal_role(Role.STAGE)
except SealedRoleDenied as exc:
    assert exc.code == 'POA_SEALED_ROLE_PRELOADED_PRODUCT'
else: raise AssertionError('PRELOADED_PRODUCT_BYPASS')
print('PRELOADED_BLOCKED')
"""
    p = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr[-800:]
    assert "PRELOADED_BLOCKED" in p.stdout


@pytest.mark.parametrize("role", ["trading", None, "stage", 0, False])
def test_untyped_or_trading_role_cannot_seal(role):
    from poa_runtime.roles import Role
    from poa_runtime.sealed_boundary import SealedRoleDenied, seal_role
    with pytest.raises(SealedRoleDenied, match="POA_SEALED_ROLE_TRADING_DISABLED"):
        seal_role(Role.TRADING if role == "trading" else role)


@pytest.mark.parametrize("unit,entry", [
    ("deployment/poa-stage-sealed.service.candidate", "poa_runtime.sealed_stage:app"),
    ("deployment/poa-readonly-sealed.service.candidate", "poa_runtime.sealed_readonly:app"),
])
def test_new_systemd_candidates_are_unstartable_and_secretless(unit, entry):
    raw = (ROOT / unit).read_text(encoding="utf-8")
    conf = configparser.ConfigParser(strict=True, interpolation=None)
    conf.read_file(io.StringIO(raw))
    assert conf.sections() == ["Unit", "Service"]
    assert not conf.has_section("Install")
    assert conf["Unit"]["RefuseManualStart"] == "yes"
    for name, value in {
        "ExecStartPre": "/usr/bin/false", "Restart": "no",
        "DynamicUser": "yes", "NoNewPrivileges": "yes",
        "PrivateNetwork": "yes", "IPAddressDeny": "any",
        "RestrictAddressFamilies": "AF_UNIX",
        "ProtectSystem": "strict", "ProtectHome": "yes",
        "CapabilityBoundingSet": "", "AmbientCapabilities": "",
    }.items():
        assert conf["Service"][name] == value, name
    assert entry in conf["Service"]["ExecStart"]
    for forbidden in ("EnvironmentFile", "PassEnvironment", "BindPaths",
                      "JoinsNamespaceOf", "Sockets"):
        assert forbidden not in conf["Service"]
    assert "main:app" not in raw and "systemctl enable" not in raw
    assert "API_KEY=" not in raw


def test_strict_pr_b_delta_and_immutable_main_product():
    changed = set(subprocess.check_output(
        ["git", "diff", "--name-only", PR_A, "HEAD"], cwd=ROOT, text=True
    ).splitlines())
    assert changed == B_FILES, ("UNEXPECTED_PR_B_DIFF", sorted(changed ^ B_FILES))
    protected = ["main.py", "run.py", "exchange", "requirements.txt",
                 "release_manifest_sha256.json", "deployment/install_once.py",
                 "deployment/build_cloud_config.py", "deployment/prepare_ubuntu.py"]
    subprocess.run(["git", "diff", "--exit-code", PR_A, "HEAD", "--", *protected],
                   cwd=ROOT, check=True)


def test_explicit_not_operational_boundaries():
    doc = (ROOT / "deployment/PR_B_SEALED_ROLE_CONTRACT.md").read_text()
    for required in (
        "LEGACY_MAIN_SAME_PROCESS_IMPORT=DENIED",
        "LEGACY_MAIN_OTHER_PROCESS=UNCONTROLLED",
        "RAW_CCXT_OUTSIDE_SEALED_ROLE=UNCONTROLLED",
        "NATIVE_PROCESS_COMPROMISE=UNVERIFIED",
        "VULTR_HOST_OS_ENFORCEMENT=UNVERIFIED",
        "READ_ONLY_EXCHANGE_ACCESS=BLOCKED",
        "DB_RESTORE=NOT_PERFORMED",
        "ACTUAL_ORDERS=0",
        "PRODUCTION_READY=BLOCKED",
    ):
        assert required in doc
