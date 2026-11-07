"""P0 offline boundary audit: no live keys, networks, orders or operating DB.

Independent checks run against immutable PR #11 test-merge product. Mock only
the HTTP transport; do not replace ReadHost._http or its URL/TLS/status policy.
Real exchange privileges, production order interlocks and production DB restore
remain explicitly UNVERIFIED until evidence is obtained outside GitHub CI.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from exchange.verified_pnl import migrations
from exchange.verified_pnl import order_fill_read as R

ROOT = Path(__file__).resolve().parents[1]
PIN = "ad88384b8b6030c1109843b09b6761395b3f2d1c"
TREE = "663c82e1134f592beb9c8bc652daccc7b2185975"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def test_exact_clean_integrated_product_checkout():
    assert git("rev-parse", "HEAD") == PIN
    assert git("rev-parse", "HEAD^{tree}") == TREE
    assert git("diff", "--name-only") == ""
    assert git("diff", "--cached", "--name-only") == ""


def test_trade_execution_is_present_but_audit_imports_no_entrypoint():
    """A CI offline test is not a production order kill switch."""
    main = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    attributes = {
        n.func.attr for n in ast.walk(main)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert {"market_buy", "market_sell", "create_order"} <= attributes
    assert "main" not in sys.modules
    print("P0_PRODUCTION_ORDER_INTERLOCK=UNVERIFIED; TRADING_ENTRYPOINT_PRESENT=YES")


def test_readhost_source_restricts_transport_and_ccxt_signer_to_get():
    tree = ast.parse((ROOT / "exchange/verified_pnl/order_fill_read.py").read_text(encoding="utf-8"))
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ReadHost"]
    assert len(classes) == 1
    methods = {n.name: n for n in classes[0].body if isinstance(n, ast.FunctionDef)}
    assert "_http" in methods and "_observe" in methods
    reqs = [n for n in ast.walk(methods["_http"]) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "request"]
    assert len(reqs) == 1
    assert isinstance(reqs[0].args[0], ast.Constant) and reqs[0].args[0].value == "GET"
    signs = [n for n in ast.walk(methods["_observe"]) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "sign"]
    assert len(signs) == 1
    assert isinstance(signs[0].args[2], ast.Constant) and signs[0].args[2].value == "GET"
    forbidden = {"create_order", "cancel_order", "edit_order", "place_order",
                 "market_buy", "market_sell", "submit_order", "create_market_order"}
    called = {n.func.attr for n in ast.walk(classes[0])
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (forbidden & called)
    print("P0_GET_ONLY_SOURCE_BOUNDARY=PASS; EXCHANGE_KEY_PERMISSIONS=UNVERIFIED")


def test_missing_or_incomplete_credentials_fail_closed():
    for exchange in ("UPBIT", "BINANCE", "BYBIT", "OKX"):
        host = R.ReadHost(SimpleNamespace())
        with pytest.raises(R.ReadRejected, match="FILL_CREDENTIAL_UNAVAILABLE"):
            host.credential(exchange)
    host = R.ReadHost(SimpleNamespace(OKX_KEY="synthetic-k", OKX_SECRET="synthetic-s"))
    with pytest.raises(R.ReadRejected, match="FILL_CREDENTIAL_UNAVAILABLE"):
        host.credential("OKX")


def test_mismatched_account_ref_cannot_create_client():
    host = R.ReadHost(SimpleNamespace(BINANCE_KEY="synthetic-k",
                                      BINANCE_SECRET="synthetic-s",
                                      BINANCE_PASSPHRASE=None))
    with pytest.raises(R.ReadRejected, match="FILL_ACCOUNT_MISMATCH"):
        host._client(dict(exchange="BINANCE", market_type="spot", account="wrong-account"))


class FakeResponse:
    def __init__(self, status=200, body=b'{"ok":true}', headers=None):
        self.status = status
        self.body = body
        self.offset = 0
        self.headers = {"content-type": "application/json"}
        self.headers.update({k.lower(): v for k, v in (headers or {}).items()})

    def getheader(self, name):
        return self.headers.get(name.lower())

    def read1(self, size):
        data = self.body[self.offset:self.offset + size]
        self.offset += len(data)
        return data


def fake_connection(monkeypatch, response):
    seen = []
    class Connection:
        def __init__(self, host, port, *, timeout, context):
            assert port == 443 and timeout == 5
            assert context.check_hostname
            seen.append(["init", host])
        def set_debuglevel(self, level):
            assert level == 0
        def request(self, method, path, *, headers):
            seen.append(["request", method, path, headers.get("Accept")])
        def getresponse(self):
            return response
        def close(self):
            seen.append(["close"])
    monkeypatch.setattr(R.http.client, "HTTPSConnection", Connection)
    return seen


@pytest.mark.parametrize("exchange,url", [
    ("BINANCE", "https://api.binance.com/api/v3/order?symbol=BTCUSDT"),
    ("BINANCE", "https://fapi.binance.com/fapi/v1/userTrades?symbol=BTCUSDT"),
    ("BYBIT", "https://api.bybit.com/v5/order/history?category=linear"),
    ("OKX", "https://www.okx.com/api/v5/trade/order?instId=BTC-USDT"),
])
def test_allowed_https_get_uses_only_fake_transport(monkeypatch, exchange, url):
    seen = fake_connection(monkeypatch, FakeResponse())
    result = R.ReadHost(SimpleNamespace())._http(
        exchange, {"url": url, "method": "GET", "body": None, "headers": {}})
    assert result == {"ok": True}
    assert seen[0][0] == "init"
    assert seen[1][0:2] == ["request", "GET"]
    assert seen[-1][0] == "close"


@pytest.mark.parametrize("url,method,body", [
    ("http://api.binance.com/api/v3/order", "GET", None),
    ("https://evil.example/api/v3/order", "GET", None),
    ("https://api.bybit.com/v5/order/history", "GET", None),
    ("https://api.binance.com/api/v3/order", "POST", None),
    ("https://api.binance.com/api/v3/order", "DELETE", None),
    ("https://api.binance.com/api/v3/order", "GET", "{}"),
    ("https://api.binance.com/api/v3/order/extra", "GET", None),
    ("https://api.binance.com/api/v3/order#fragment", "GET", None),
    ("https://user:password@api.binance.com/api/v3/order", "GET", None),
    ("https://api.binance.com:8443/api/v3/order", "GET", None),
])
def test_disallowed_transport_is_rejected_before_any_network(monkeypatch, url, method, body):
    touches = []
    def forbidden(*args, **kwargs):
        touches.append(1)
        raise AssertionError("Unexpected attempted HTTPS connection")
    monkeypatch.setattr(R.http.client, "HTTPSConnection", forbidden)
    with pytest.raises(R.ReadRejected, match="FILL_GET_REJECTED"):
        R.ReadHost(SimpleNamespace())._http(
            "BINANCE", {"url": url, "method": method, "body": body})
    assert not touches


@pytest.mark.parametrize("status,expected", [
    (401, "FILL_AUTH_REJECTED"),
    (403, "FILL_PERMISSION_REJECTED"),
    (418, "FILL_RATE_LIMITED"),
    (429, "FILL_RATE_LIMITED"),
    (302, "FILL_REDIRECT_REJECTED"),
    (500, "FILL_TEMPORARY_UNAVAILABLE"),
])
def test_real_readhost_rejects_http_error_without_retry(monkeypatch, status, expected):
    seen = fake_connection(monkeypatch, FakeResponse(status=status))
    with pytest.raises(R.ReadRejected, match=expected):
        R.ReadHost(SimpleNamespace())._http(
            "BINANCE", {"url": "https://api.binance.com/api/v3/order",
                        "method": "GET", "body": None})
    assert len([item for item in seen if item[0] == "request"]) == 1
    assert seen[-1][0] == "close"


@pytest.mark.parametrize("body,headers,expected", [
    (b'{"a":1,"a":2}', {}, "FILL_RESPONSE_INVALID"),
    (b'{"a":NaN}', {}, "FILL_RESPONSE_INVALID"),
    (b'{"ok":true}', {"Content-Type": "text/plain"}, "FILL_RESPONSE_INVALID"),
    (b'{"ok":true}', {"Content-Length": "1048577"}, "FILL_RESPONSE_TOO_LARGE"),
])
def test_malformed_authenticated_response_fails_closed(monkeypatch, body, headers, expected):
    seen = fake_connection(monkeypatch, FakeResponse(body=body, headers=headers))
    with pytest.raises(R.ReadRejected, match=expected):
        R.ReadHost(SimpleNamespace())._http(
            "BINANCE", {"url": "https://api.binance.com/api/v3/order",
                        "method": "GET", "body": None})
    assert len([x for x in seen if x[0] == "request"]) == 1


def test_real_isolation_hook_rejects_network_without_network_call():
    # sys.audit invokes the installed hook directly; it never initiates a socket.
    with pytest.raises(RuntimeError, match="OFFLINE_NETWORK_DISABLED"):
        sys.audit("socket.connect", None, ("127.0.0.1", 443))


def test_real_legacy_snapshot_preflight_preserves_db_bytes(tmp_path):
    db = tmp_path / "existing_verified_jobs.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE verified_jobs (id TEXT PRIMARY KEY,state TEXT,data TEXT)")
        conn.execute("INSERT INTO verified_jobs VALUES ('old-1','PENDING','{}')")
    original = hashlib.sha256(db.read_bytes()).hexdigest()
    snapshot = migrations.preflight(db)
    assert snapshot["integrity"] == "ok"
    assert snapshot["tables"]["verified_jobs"]["count"] == 1
    assert hashlib.sha256(db.read_bytes()).hexdigest() == original
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO verified_jobs VALUES ('mutate','SENT','{}')")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == original


def test_mock_backup_restore_keeps_original_rows(tmp_path):
    src = tmp_path / "original.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    with sqlite3.connect(src) as conn:
        conn.execute("CREATE TABLE states(id TEXT PRIMARY KEY, status TEXT, wire BLOB)")
        conn.execute("INSERT INTO states VALUES (?,?,?)", ("delivery-1", "UNKNOWN", b"fixed-wire"))
    original = hashlib.sha256(src.read_bytes()).hexdigest()
    with sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True) as read:
        with sqlite3.connect(backup) as out:
            read.backup(out)
    with sqlite3.connect(backup) as check:
        assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert check.execute("SELECT * FROM states").fetchall() == [
            ("delivery-1", "UNKNOWN", b"fixed-wire")]
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original


def test_backup_requires_quiesced_writers_even_on_synthetic_db(tmp_path):
    destination = tmp_path / "unapproved-backup.sqlite3"
    with pytest.raises(ValueError, match="writer_quiescence_required"):
        migrations.verify_backup(SimpleNamespace(db_path=str(tmp_path / "original.sqlite3")),
                                 destination, writer_quiesced=False)
    assert not destination.exists()


def test_ci_has_no_live_exchange_credentials():
    for exchange in ("UPBIT", "BYBIT", "BINANCE", "OKX"):
        for suffix in ("KEY", "SECRET", "PASSPHRASE"):
            assert not os.environ.get(exchange + "_" + suffix), "LIVE_CREDENTIAL_EXPOSED"
    assert not os.environ.get("DISCORD_WEBHOOK_URL"), "REAL_WEBHOOK_EXPOSED"
    print("P0_REAL_EXCHANGE_API_READ=UNVERIFIED; API_KEY_PERMISSION_SCOPE=UNVERIFIED; REAL_DB_RESTORE=UNVERIFIED")
