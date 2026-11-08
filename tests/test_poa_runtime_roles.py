"""POA PR A minimal Stage/Read-only/Trading role integration Mock E2E.

No actual main.py import, order call, real database, third-party SDK,
cloud, network or live key. Historical product identity is checked in
the inherited PR23 tests; all new surfaces stay inert.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from poa_runtime.roles import (
    Role, RoleBlocked, asgi_for, authorize_live_read,
    deny_order_execution, readonly_preflight, require_role,
)
from poa_stage.read_credentials import (
    PREFIX, VENUES, ReadGateBlocked, historical_ref, require_proven_live_read,
)

ROOT=Path(__file__).resolve().parents[1]
BASE="c5566c4cc2a4ce716f1f0d66260bd60e4f95546f"
EXPECTED={
    "poa_stage/__init__.py","poa_stage/gateway.py",
    "poa_stage/read_credentials.py",
    "poa_runtime/__init__.py","poa_runtime/roles.py",
    "poa_runtime/readonly_gateway.py","poa_runtime/trade_gate.py",
    "poa_runtime/__main__.py","tests/test_vultr_stage.py",
    "tests/test_poa_runtime_roles.py",
    "deployment/poa-stage-only.service.candidate",
    "deployment/poa-readonly-only.service.candidate",
    "deployment/poa-trading-disabled.service.candidate",
    "deployment/PR_A_VULTR_MINIMAL_INTEGRATION.md",
    ".github/workflows/vultr_role_integration_a.yml",
}


def drive(coro):
    """Inert ASGI callbacks must complete without an event loop/socketpair."""
    try:
        coro.send(None)
    except StopIteration:
        return
    else:
        coro.close()
        raise AssertionError("UNEXPECTED_ASYNC_IO_OR_EXTERNAL_BOUNDARY")


def asgi_request(app, method="POST", path="/order", protocol="http"):
    sent=[]
    inputs=[]
    async def receive():
        inputs.append("BODY_READ")
        return {"type":"http.request","body":b'{"api_secret":"CANARY"}'}
    async def send(x):
        sent.append(x)
    scope={"type":protocol,"path":path,"method":method,"headers":[],
           "query_string":b"secret=NOT_FOR_LOG"}
    drive(app(scope,receive,send))
    if protocol=="websocket":
        assert sent==[{"type":"websocket.close","code":4403,"reason":"READ_ONLY_NO_WS"}]
        assert inputs==[]
        return None
    assert len(sent)==2
    assert inputs==[]
    assert sent[0]["type"]=="http.response.start"
    assert sent[1]["type"]=="http.response.body"
    assert b"CANARY" not in sent[1]["body"]
    return sent[0]["status"],sent[1]["body"]


@pytest.mark.parametrize("mode",list(Role))
def test_roles_are_exact_and_never_activate_real_app(mode):
    if mode is Role.TRADING:
        with pytest.raises(RoleBlocked,match="POA_RUNTIME_TRADING_DISABLED"):
            asgi_for(mode)
    else:
        assert callable(asgi_for(mode))


@pytest.mark.parametrize("invalid",[None,False,0,"trading","STAGE",object(),{}])
def test_role_mismatch_and_coercion_cannot_activate(invalid):
    with pytest.raises(RoleBlocked,match="POA_ROLE_UNRECOGNIZED"):
        asgi_for(invalid)


@pytest.mark.parametrize("mode",list(Role))
def test_central_execution_guard_always_denies_even_if_role_selected(mode):
    with pytest.raises(RoleBlocked) as e:
        deny_order_execution(role=mode,approved=True,ci=True)
    assert e.value.code=="POA_RUNTIME_TRADING_DISABLED"


@pytest.mark.parametrize("requested",["stage","read-only","trading"])
def test_no_environment_override_for_order_gate(monkeypatch,requested):
    monkeypatch.setenv("POA_RUNTIME_ROLE",requested)
    monkeypatch.setenv("POA_APPROVE_LIVE_ORDERS","true")
    monkeypatch.setenv("POA_TRADE_ALLOW","1")
    with pytest.raises(RoleBlocked,match="POA_RUNTIME_TRADING_DISABLED"):
        deny_order_execution()


@pytest.mark.parametrize("expected",list(Role))
def test_roles_do_not_alias_each_other(expected):
    for role in Role:
        if role is expected:
            assert require_role(role,expected)
        else:
            with pytest.raises(RoleBlocked,match="POA_ROLE_MISMATCH"):
                require_role(role,expected)


@pytest.mark.parametrize("method,path",[
    ("GET","/order"),("POST","/order"),("POST","/"),
    ("POST","/hedge"),("POST","/price"),("GET","/price"),
    ("POST","/api/orders"),("PUT","/order"),("DELETE","/"),
    ("GET","/fill"),("POST","/__read/health"),
    ("GET","//order"),("HEAD","/hedge"),("PATCH","/"),
])
def test_read_only_asgi_denies_all_external_order_routes(method,path):
    app=asgi_for(Role.READ_ONLY)
    status,raw=asgi_request(app,method,path)
    assert status==403
    if method=="HEAD":
        assert raw==b""
    else:
        assert raw==b'{"detail":"POA_READ_ONLY_NOT_AUTHORIZED"}'


@pytest.mark.parametrize("method",["GET","HEAD"])
def test_read_only_health_cannot_claim_key_or_db_ready(method):
    status,raw=asgi_request(asgi_for(Role.READ_ONLY),method,"/__read/health")
    assert status==200
    if method=="HEAD":
        assert raw==b""
    else:
        assert json.loads(raw)=={
            "role":"READ_ONLY","orders_enabled":False,
            "exchange_reads_enabled":False,"db_attached":False}


def test_read_only_websocket_closed_without_receiving_body():
    assert asgi_request(asgi_for(Role.READ_ONLY),protocol="websocket")==None


def test_read_only_lifespan_never_loads_trading_module():
    events=iter(["lifespan.startup","lifespan.shutdown"])
    outputs=[]
    async def receive():
        return {"type":next(events)}
    async def send(x):
        outputs.append(x["type"])
    drive(asgi_for(Role.READ_ONLY)({"type":"lifespan"},receive,send))
    assert outputs==["lifespan.startup.complete","lifespan.shutdown.complete"]


def _vault(venue):
    v={PREFIX+venue+"_KEY":"NEW_READ_ACCESS_"+venue,
       PREFIX+venue+"_SECRET":"SYNTHETIC_SECRET_"+venue}
    if venue=="OKX":
        v[PREFIX+venue+"_PASSPHRASE"]="SYNTHETIC_PASSPHRASE"
    return v


@pytest.mark.parametrize("venue",sorted(VENUES))
def test_explicit_dedicated_read_key_plan_does_not_activate_reads(venue):
    old_access="ORIGINAL_TRADING_ACCESS_"+venue
    old_ref=historical_ref(venue,old_access)
    plan=readonly_preflight(_vault(venue),venue,old_ref,
        trading_key_fingerprint=hashlib.sha256(old_access.encode()).hexdigest())
    assert plan.venue==venue
    assert plan.old_trading_ref==old_ref
    assert plan.new_read_ref!=old_ref
    assert plan.authorized is False
    assert plan.gate=="READ_EXTERNAL_ACCOUNT_BINDING_AND_PERMISSIONS_UNVERIFIED"
    with pytest.raises(RoleBlocked,match="POA_READ_ONLY_VENUE_PROOF_MISSING"):
        authorize_live_read(plan)
    with pytest.raises(ReadGateBlocked,match="READ_PRODUCTION_BINDING_UNVERIFIED"):
        require_proven_live_read(plan)


@pytest.mark.parametrize("venue",sorted(VENUES))
def test_readonly_credential_reuse_cannot_be_authorized(venue):
    v=_vault(venue)
    with pytest.raises(ReadGateBlocked,match="READ_TRADING_KEY_REUSE"):
        readonly_preflight(v,venue,historical_ref(venue,v[PREFIX+venue+"_KEY"]))


@pytest.mark.parametrize("venue",["BITGET","KIS","NYSE","NASDAQ",""])
def test_unsupported_venues_cannot_be_labeled_read_verified(venue):
    with pytest.raises(ReadGateBlocked,match="READ_VENUE_UNSUPPORTED"):
        readonly_preflight({},venue,"0"*64)


@pytest.mark.parametrize("extra",["BINANCE_KEY","UPBIT_KEY","DISCORD_WEBHOOK_URL","PASSWORD"])
def test_readonly_vault_rejects_legacy_trading_and_webhook_secrets(extra):
    v=_vault("BINANCE")
    v[extra]="CANARY_CREDENTIAL"
    with pytest.raises(ReadGateBlocked,match="READ_VAULT_FIELDS_INVALID"):
        readonly_preflight(v,"BINANCE",historical_ref("BINANCE","OLD_TRADE"))


@pytest.mark.parametrize("mode",["stage","read-only","trading"])
def test_role_preflight_cli_never_starts_network_or_trading(mode):
    environment={**os.environ,
                 "BINANCE_KEY":"LEGACY_CANARY_TRADE",
                 "BINANCE_SECRET":"LEGACY_CANARY_SECRET",
                 "POA_RUNTIME_ROLE":"trading",
                 "POA_TRADE_ALLOW":"1"}
    p=subprocess.run([sys.executable,"-B","-m","poa_runtime","--role",mode],
        cwd=ROOT,env=environment,capture_output=True,text=True,timeout=15)
    assert "LEGACY_CANARY_SECRET" not in p.stdout+p.stderr
    if mode=="trading":
        assert p.returncode==77
        assert "POA_RUNTIME_TRADING_DISABLED" in p.stderr
    else:
        assert p.returncode==0
        info=json.loads(p.stdout)
        assert info["trading_allowed"] is False
        assert info["live_read_allowed"] is False
        assert info["db_attached"] is False
        assert info["server_started"] is False


def test_role_app_import_uses_no_legacy_product_modules():
    code="""import sys
from poa_runtime.roles import Role,asgi_for
from poa_runtime.readonly_gateway import app
asgi_for(Role.STAGE)
asgi_for(Role.READ_ONLY)
assert not any(n=='main' or n.startswith('exchange') or
               n.startswith('ccxt') or n.startswith('pocketbase') or
               n=='sqlite3' for n in sys.modules)
print('NO_PRODUCT_IMPORT=PASS')
"""
    p=subprocess.run([sys.executable,"-B","-c",code],cwd=ROOT,
        capture_output=True,text=True,timeout=15,check=True)
    assert p.stdout.strip()=="NO_PRODUCT_IMPORT=PASS"


def test_203_concurrent_trade_requests_always_denied():
    def attack(_):
        try:
            deny_order_execution(request={"BTCUSDT":"LONG"})
        except RoleBlocked as error:
            return error.code
        return "BYPASS"
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(attack,range(203)))
    assert results==["POA_RUNTIME_TRADING_DISABLED"]*203


@pytest.mark.parametrize("path",[
    "deployment/poa-stage-only.service.candidate",
    "deployment/poa-readonly-only.service.candidate",
    "deployment/poa-trading-disabled.service.candidate",
])
def test_inert_candidate_units_require_new_independent_authorization(path):
    src=(ROOT/path).read_text()
    assert "ExecStartPre=/usr/bin/false" in src
    assert "NoNewPrivileges=yes" in src
    assert "ProtectSystem=strict" in src
    assert "PrivateNetwork=yes" in src
    assert "EnvironmentFile=" not in src
    assert "main:app" not in src and "run.py" not in src
    assert "systemctl enable" not in src


def test_original_poa_product_finance_and_db_sources_are_unchanged():
    changed=set(subprocess.check_output(["git","diff","--name-only",BASE,"HEAD"],
        text=True,cwd=ROOT).splitlines())
    assert changed==EXPECTED
    assert not any(x.startswith(("exchange/","poa_runtime/../")) for x in changed)
    assert not any(x in ("main.py","run.py","requirements.txt",
        "release_manifest_sha256.json") for x in changed)


def test_operational_blockers_are_not_marked_pass():
    doc=(ROOT/"deployment/PR_A_VULTR_MINIMAL_INTEGRATION.md").read_text()
    for label in (
        "TRADING_ROLE_ACTIVATION=BLOCKED",
        "LIVE_READ_AUTHORIZATION=BLOCKED",
        "LEGACY_MAIN_INTERNAL_SINK_GUARD=NOT_INTEGRATED",
        "VULTR_HOST_TEST=NOT_PERFORMED",
        "DB_RESTORE=NOT_VERIFIED",
        "PRODUCTION_READY=BLOCKED",
    ):
        assert label in doc
