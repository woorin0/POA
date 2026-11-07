"""Vultr Phase 0-1 *product-adjacent* stage safety E2E.

Never runs main.py or invokes any real exchange order function. The ASGI stage
gateway has NO delegation to financial, order, retry, PocketBase or Discord
modules. A separate offline dedicated-key view is compared against the real
product's ReadHost account-ref trust check with no HTTP execution.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from poa_stage.gateway import app
from poa_stage.read_credentials import (
    VENUES, PREFIX, DedicatedReadKey, OfflineReadHostSettings,
    ReadGateBlocked, historical_ref, load_explicit_read_key,
    require_proven_live_read, stage_read_plan,
)

ROOT = Path(__file__).resolve().parents[1]


def request(method="POST", path="/order", *, incoming=None):
    sent=[]
    receive_calls=[]
    async def receive():
        receive_calls.append(True)
        return {"type": "http.request", "body": incoming or b'{"password":"fake"}',
                "more_body": False}
    async def send(message):
        sent.append(message)
    asyncio.run(app({
        "type":"http", "http_version":"1.1",
        "path":path,"method":method,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"password=DO_NOT_LOG",
    },receive,send))
    assert len(sent)==2
    assert sent[0]["type"]=="http.response.start"
    assert sent[1]["type"]=="http.response.body"
    assert all(b"DO_NOT_LOG" not in str(m).encode() for m in sent)
    return sent[0]["status"],sent[1]["body"],receive_calls


@pytest.mark.parametrize("method,path", [
    ("POST","/order"),("POST","/"),("POST","/hedge"),
    ("POST","/price"),("POST","/ip"),
    ("GET","/order"),("PUT","/order"),("DELETE","/"),
    ("PATCH","/hedge"),("POST","/__stage/health"),
    ("POST","/order%2f"),("POST","//order"),
    ("POST","/anything"),("GET","/"),("GET","/price"),
    ("HEAD","/order"),("POST","/order/"),
])
def test_no_order_or_auxiliary_route_can_execute(method,path):
    status,body,called=request(method,path,incoming=b'{"symbol":"BTCUSDT","amount":100}')
    assert status==403
    if method=="HEAD":
        assert body==b""
    else:
        assert body==b'{"detail":"POA_STAGE_ORDER_DENIED"}'
    assert not called, "denied requests must not inspect webhook payload"


@pytest.mark.parametrize("method",["GET","HEAD"])
def test_stage_health_is_inert_and_never_reports_trading_readiness(method):
    status,body,called=request(method,"/__stage/health")
    assert status==200
    assert not called
    if method=="GET":
        data=json.loads(body)
        assert data=={"state":"STAGE_ONLY","orders_enabled":False,
                      "read_enabled":False,"db_attached":False}
    else:
        assert body==b""


def test_ws_upgrade_always_denied_without_reading_message():
    sent=[]
    async def receive():
        raise AssertionError("must not receive live websocket data")
    async def send(message):
        sent.append(message)
    asyncio.run(app({"type":"websocket","path":"/order","headers":[]},receive,send))
    assert sent==[{"type":"websocket.close","code":4403,"reason":"STAGE_ONLY"}]


def test_lifespan_starts_no_product_service_and_shuts_down():
    events=iter(["lifespan.startup","lifespan.shutdown"])
    sent=[]
    async def receive():
        return {"type":next(events)}
    async def send(x):
        sent.append(x["type"])
    asyncio.run(app({"type":"lifespan"},receive,send))
    assert sent==["lifespan.startup.complete","lifespan.shutdown.complete"]


def test_stage_gateway_subprocess_does_not_import_trade_or_db_code_even_with_legacy_keys():
    env={**os.environ,
         "BINANCE_KEY":"FAKE_TRADE_KEY",
         "BINANCE_SECRET":"FAKE_TRADE_SECRET",
         "DISCORD_WEBHOOK_URL":"https://discord.com/api/webhooks/000/FAKE",
         "POA_STAGE_AUTHORIZED":"1"}
    check=r"""import json,sys
import poa_stage.gateway
assert not any(n=='main' or n.startswith('exchange') or n.startswith('pocketbase')
               or n.startswith('ccxt') or n=='sqlite3' for n in sys.modules)
print('STAGE_NO_PRODUCT_IMPORT=PASS')
"""
    child=subprocess.run([sys.executable,"-B","-c",check],
                        cwd=ROOT,env=env,check=True,capture_output=True,
                        timeout=15,text=True)
    assert child.stdout.strip()=="STAGE_NO_PRODUCT_IMPORT=PASS"
    assert "FAKE_TRADE_SECRET" not in child.stdout+child.stderr


def test_stage_service_unit_is_inert_and_separate_from_live_units():
    text=(ROOT/"deployment/poa-stage-only.service.candidate").read_text()
    assert "ExecStartPre=/usr/bin/false" in text
    assert "NoNewPrivileges=yes" in text
    assert "ProtectSystem=strict" in text
    assert "PrivateNetwork=yes" in text
    assert "ExecStart=" in text and "poa_stage.gateway:app" in text
    assert "main:app" not in text and "run.py" not in text
    assert "poa.service" not in text
    assert "PrivateTmp=yes" in text


def fake_vault(venue):
    d={PREFIX+venue+"_KEY":"READ_ACCESS_"+venue,
       PREFIX+venue+"_SECRET":"READ_SECRET_"+venue}
    if venue=="OKX":
        d[PREFIX+venue+"_PASSPHRASE"]="READ_PASSPHRASE_OKX"
    return d


@pytest.mark.parametrize("venue",sorted(VENUES))
def test_dedicated_key_plan_does_not_authorize_real_exchange_read(venue):
    key=load_explicit_read_key(fake_vault(venue),venue)
    ref=historical_ref(venue,"TRADING_ACCESS_"+venue)
    plan=stage_read_plan(key,ref,trading_key_fingerprint=
                         hashlib.sha256(("TRADING_ACCESS_"+venue).encode()).hexdigest())
    assert not plan.authorized
    assert plan.old_trading_ref==ref
    assert plan.new_read_ref==historical_ref(venue,key.access)
    assert plan.new_read_ref!=plan.old_trading_ref
    assert plan.gate=="READ_EXTERNAL_ACCOUNT_BINDING_AND_PERMISSIONS_UNVERIFIED"
    assert key.access not in repr(key)
    assert key.secret not in repr(key)
    with pytest.raises(ReadGateBlocked) as err:
        require_proven_live_read(plan)
    assert err.value.code=="READ_PRODUCTION_BINDING_UNVERIFIED"


@pytest.mark.parametrize("venue",sorted(VENUES))
def test_exact_separate_vault_contract_and_no_key_fallback(venue):
    vault=fake_vault(venue)
    assert load_explicit_read_key(vault,venue).venue==venue
    for field in vault:
        missing=dict(vault)
        del missing[field]
        with pytest.raises(ReadGateBlocked, match="READ_VAULT_FIELDS_INVALID"):
            load_explicit_read_key(missing,venue)
    with pytest.raises(ReadGateBlocked, match="READ_VAULT_FIELDS_INVALID"):
        load_explicit_read_key({**vault,"BINANCE_TRADING_KEY":"forbidden"},venue)
    with pytest.raises(ReadGateBlocked, match="READ_VAULT_FIELDS_INVALID"):
        load_explicit_read_key({**vault,"UPBIT_KEY":"legacy-trade-key"},venue)


@pytest.mark.parametrize("venue",sorted(VENUES))
def test_original_trading_key_reuse_rejected_even_with_different_secret(venue):
    key=load_explicit_read_key(fake_vault(venue),venue)
    with pytest.raises(ReadGateBlocked,match="READ_TRADING_KEY_REUSE"):
        stage_read_plan(key,historical_ref(venue,key.access))
    with pytest.raises(ReadGateBlocked,match="READ_TRADING_KEY_REUSE"):
        stage_read_plan(key,historical_ref(venue,"OTHER_TRADE_KEY"),
          trading_key_fingerprint=hashlib.sha256(key.access.encode()).hexdigest())


@pytest.mark.parametrize("venue",["BITGET","KIS","KRX","NASDAQ","NYSE","AMEX","any"])
def test_unverified_venue_has_no_read_key_production_path(venue):
    with pytest.raises(ReadGateBlocked,match="READ_VENUE_UNSUPPORTED"):
        load_explicit_read_key({},venue)


@pytest.mark.parametrize("bad",["",None,12,"READ KEY","DUMMY\nKEY","한글KEY"])
def test_bad_or_missing_read_credentials_fail_closed(bad):
    vault=fake_vault("BINANCE")
    vault[PREFIX+"BINANCE_KEY"]=bad
    with pytest.raises(ReadGateBlocked,match="READ_CREDENTIAL_UNAVAILABLE"):
        load_explicit_read_key(vault,"BINANCE")


def test_real_product_ledger_uses_same_immutable_original_reference_formula():
    from exchange.verified_pnl.ledger import identity
    for venue in VENUES:
        assert identity(venue,"ORIGINAL_TRADE_KEY") == historical_ref(venue,"ORIGINAL_TRADE_KEY")


def test_real_readhost_rejects_different_read_key_with_original_trading_account_identity():
    from exchange.verified_pnl.order_fill_read import ReadHost, ReadRejected
    venue="BYBIT"
    key=load_explicit_read_key(fake_vault(venue),venue)
    settings=OfflineReadHostSettings(key)
    host=ReadHost(settings)
    assert host.credential(venue)==(key.access,key.secret,None)
    assert not hasattr(settings,"UPBIT_KEY")
    assert not hasattr(settings,"BINANCE_KEY")
    original=historical_ref(venue,"TRADING_KEY_NOT_READ_KEY")
    scope={"exchange":venue,"market_type":"spot","account":original}
    with pytest.raises(ReadRejected) as err:
        host._client(scope)
    assert str(err.value)=="FILL_ACCOUNT_MISMATCH" or err.value.code=="FILL_ACCOUNT_MISMATCH"
    assert not host.clients


def test_original_product_trade_paths_not_modified_in_stage_branch():
    # Immutable protected files checked again by workflow manifest+git checks.
    # Stage code must not have any bridge to those paths.
    source=(ROOT/"poa_stage/gateway.py").read_text()
    assert "from exchange" not in source
    assert "import main" not in source
    assert "import ccxt" not in source
    assert "create_order(" not in source
    assert "get_bot(" not in source
    assert "sqlite3" not in source


def test_read_key_errors_never_contain_secrets():
    secret="SECRET_VALUE_MUST_NOT_LOG_123"
    vault={PREFIX+"BINANCE_KEY":"BREAD",PREFIX+"BINANCE_SECRET":secret}
    key=load_explicit_read_key(vault,"BINANCE")
    with pytest.raises(ReadGateBlocked) as error:
        require_proven_live_read(stage_read_plan(key,historical_ref("BINANCE","OTRADE")))
    assert secret not in str(error.value)
