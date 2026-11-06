"""Independent offline security and historical-source checks on immutable PR4 code.

Executed only under release_checks.isolation (socket DNS/connection forbidden).
The audit helper is taken from PR7 and imported as a pure module. Tests never
start POA, place orders, load real API keys, or post Discord messages.
"""
from __future__ import annotations

import ast
import copy
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import types
from unittest.mock import patch

import pytest
import requests
import fill_confirmation_contracts as F
from exchange.verified_pnl.order_fill_read import ReadHost, ReadRejected, validate
from exchange.verified_pnl import order_fill_confirmation as C

PRODUCT_SHA = "bf6e66e259c7ebbd5099e78b24089caa94936ec9"
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "poa_readonly_audit_v2.py"
MARKETS = [
    ("UPBIT", False), ("BYBIT", False), ("BYBIT", True),
    ("BINANCE", False), ("BINANCE", True), ("OKX", False), ("OKX", True),
]


@pytest.fixture(scope="module")
def audit():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == PRODUCT_SHA
    spec = importlib.util.spec_from_file_location("offline_readonly_phase3", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.EXPECTED_SHA == PRODUCT_SHA
    return module


def baseline(r, exchange, future, audit, *, side="sell", fee=False):
    qty = "0.2" if exchange == "OKX" and future else "1"
    expected_total = "42" if exchange == "OKX" and future else "210"
    expected_qty = "0.4" if exchange == "OKX" and future else "2"
    item = {
        "exchange": exchange,
        "market": "linear" if future else "spot",
        "symbol": r.info.unified_symbol,
        "side": side,
        "order_id": r.oid,
        "reference_source": "exchange_statement",
        "reference_fills": [
            {"price": "100", "quantity_base": qty},
            {"price": "110", "quantity_base": qty},
        ],
        "reference_quantity": expected_qty,
        "reference_total": expected_total,
    }
    if future:
        item["is_close"] = r.info.is_close
    if exchange == "OKX" and future:
        item["contract_size"] = "0.1"
    if fee:
        item["reference_fees"] = {"USDT": "0.2"}
    audit.require_order(item)
    return item


def observation(r):
    request = {
        "info": vars(r.info),
        "result": {"id": r.oid},
        "source_account_ref": r.scope["account"],
    }
    assert type(r.host) is ReadHost
    proof = json.loads(r.host.observe(request).encoded)
    assert validate(proof)
    return proof, C.render(C.make_payload(proof)).data


@pytest.mark.parametrize("exchange,future", MARKETS)
def test_native_venue_response_matches_standalone_independent_oracle(
    tmp_path, monkeypatch, audit, exchange, future,
):
    r = F.setup(tmp_path, monkeypatch, exchange, future, side="sell", count=2, base="SOL")
    proof, embed = observation(r)
    item = baseline(r, exchange, future, audit)
    assert audit.compare_proof_to_reference(item, proof, embed) == []
    assert r.calls, "authenticated native GET transport fixture was not used"
    assert r.posts == [], "standalone auditor must never POST to Discord"


@pytest.mark.parametrize("exchange,future", MARKETS)
def test_missing_native_rows_or_wrong_scope_cannot_be_accepted(
    tmp_path, monkeypatch, audit, exchange, future,
):
    r = F.setup(tmp_path, monkeypatch, exchange, future, side="sell", count=2, base="SOL")
    proof, embed = observation(r)
    item = baseline(r, exchange, future, audit)
    damaged = copy.deepcopy(item)
    damaged["reference_fills"][0]["price"] = "101"
    assert "source fill rows differ" in audit.compare_proof_to_reference(damaged, proof, embed)
    for scope_field, value in (("order_id", "other-order"), ("symbol", "BTC/USDT"),
                               ("side", "buy"), ("market", "spot" if future else "linear")):
        candidate = dict(item)
        candidate[scope_field] = value
        assert any("scope mismatch" in x for x in audit.compare_proof_to_reference(candidate, proof, embed))
    assert r.posts == []


@pytest.mark.parametrize("exchange,future", MARKETS)
def test_native_finality_rejected_before_any_preview(tmp_path, monkeypatch, exchange, future):
    r = F.setup(tmp_path, monkeypatch, exchange, future, side="sell", count=2)
    order = r.state["order"]
    if exchange == "UPBIT":
        order["state"] = "wait"
    elif exchange == "BYBIT":
        order["orderStatus"] = "New"
    elif exchange == "BINANCE":
        order["status"] = "PARTIALLY_FILLED"
    else:
        order["state"] = "live"
    with pytest.raises(ReadRejected):
        r.host.observe({"info": vars(r.info), "result": {"id": r.oid},
                        "source_account_ref": r.scope["account"]})
    assert r.posts == []


@pytest.mark.parametrize("exchange", ["BYBIT", "BINANCE", "OKX"])
@pytest.mark.parametrize("failure", [
    "FILL_AUTH_REJECTED", "FILL_PERMISSION_REJECTED",
    "FILL_RATE_LIMITED", "FILL_TIMEOUT", "FILL_TEMPORARY_UNAVAILABLE",
])
def test_all_authenticated_read_failures_are_fail_closed_and_secret_safe(
    tmp_path, monkeypatch, capsys, exchange, failure,
):
    r = F.setup(tmp_path, monkeypatch, exchange, False, side="buy", count=2)
    def denied(*args, **kwargs):
        raise ReadRejected(failure)
    monkeypatch.setattr(r.host, "_http", denied)
    with pytest.raises(ReadRejected) as err:
        r.host.observe({"info": vars(r.info), "result": {"id": r.oid},
                        "source_account_ref": r.scope["account"]})
    assert err.value.code == failure
    output = capsys.readouterr()
    assert "synthetic-key" not in output.out + output.err
    assert "synthetic-" + "s" * 64 not in output.out + output.err
    assert r.posts == []


def test_fees_and_negative_rebates_are_checked_independently(tmp_path, monkeypatch, audit):
    r = F.setup(tmp_path, monkeypatch, "BYBIT", True, side="sell", count=2, base="SOL")
    proof, embed = observation(r)
    item = baseline(r, "BYBIT", True, audit, fee=True)
    item["reference_position"] = "롱 청산"
    audit.require_order(item)
    assert audit.compare_proof_to_reference(item, proof, embed) == []
    wrong_fee = dict(item, reference_fees={"USDT": "0.1"})
    assert "independent fee summary differs or is unverified" in audit.compare_proof_to_reference(wrong_fee, proof, embed)
    wrong_position = dict(item, reference_position="숏 청산")
    assert "independent futures position mismatch" in audit.compare_proof_to_reference(wrong_position, proof, embed)
    rebate = dict(item, reference_fees={"USDT": "-0.1"})
    audit.require_order(rebate)
    assert "independent fee summary differs or is unverified" in audit.compare_proof_to_reference(rebate, proof, embed)
    unknown_fees = dict(proof, fees=None)
    assert "independent fee summary differs or is unverified" in audit.compare_proof_to_reference(item, unknown_fees, embed)
    bad_instrument = dict(proof, instrument="ETHUSDT")
    assert "native instrument mismatch" in audit.compare_proof_to_reference(item, bad_instrument, embed)


def test_disallow_unsafe_audit_file_and_non_private_fixtures(tmp_path, audit):
    path = tmp_path / "manifest.json"
    path.write_text('{"orders": []}', encoding="utf-8")
    path.chmod(0o600)
    assert audit.read_private_manifest(path) == {"orders": []}
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(ValueError, match="mode 0600"):
            audit.read_private_manifest(path)
        path.chmod(0o600)
    symlink = tmp_path / "symlink.json"
    try:
        symlink.symlink_to(path)
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(ValueError, match="symlink"):
            audit.read_private_manifest(symlink)
    big = tmp_path / "oversized.json"
    big.write_bytes(b"x" * 2_000_001)
    big.chmod(0o600)
    with pytest.raises(ValueError, match="at most 2 MB"):
        audit.read_private_manifest(big)
    if hasattr(os, "mkfifo"):
        pipe = tmp_path / "manifest.fifo"
        os.mkfifo(pipe)
        with pytest.raises(ValueError, match="regular file"):
            audit.read_private_manifest(pipe)


def test_endpoint_methods_and_credential_transport_are_blocked_offline(audit):
    called = []
    def stub(conn, method, target, *args, **kwargs):
        called.append((conn.host, method, target))
        return "SYNTHETIC_NO_IO"
    with patch.object(http.client.HTTPSConnection, "request", stub), \
         patch.object(http.client.HTTPConnection, "request", stub), \
         patch.object(requests.sessions.Session, "request", stub):
        audit.limit_network_to_exchange_gets()
        for host, endpoints in audit.READ_ENDPOINTS.items():
            for p in endpoints:
                fake = types.SimpleNamespace(host=host, port=443)
                assert http.client.HTTPSConnection.request(fake, "GET", p) == "SYNTHETIC_NO_IO"
        for method in ("POST", "PUT", "DELETE", "PATCH", "CONNECT", "HEAD"):
            with pytest.raises(RuntimeError, match="BLOCKED"):
                http.client.HTTPSConnection.request(types.SimpleNamespace(host="api.upbit.com", port=443), method, "/v1/order")
        for host, target, port in [
            ("evil.example", "/v1/order", 443),
            ("api.upbit.com", "/v1/orders", 443),
            ("api.upbit.com", "//evil.example/v1/order", 443),
            ("api.upbit.com", "https://api.upbit.com/v1/order", 443),
            ("api.bybit.com", "/v5/order/create", 443),
            ("www.okx.com", "/api/v5/trade/order", 80),
            ("fapi.binance.com", "/fapi/v1/order/submit", 443),
        ]:
            with pytest.raises(RuntimeError, match="BLOCKED"):
                http.client.HTTPSConnection.request(types.SimpleNamespace(host=host, port=port), "GET", target)
        with pytest.raises(RuntimeError, match="PLAINTEXT"):
            http.client.HTTPConnection.request(types.SimpleNamespace(host="api.upbit.com"), "GET", "/v1/order")
        with pytest.raises(RuntimeError, match="REQUESTS_TRANSPORT"):
            requests.sessions.Session().request("GET", "https://api.upbit.com/v1/order")
    assert len(called) == sum(map(len, audit.READ_ENDPOINTS.values()))


def test_security_guard_and_forbidden_business_imports_are_product_unchanged(audit):
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)
    assert "main" not in imported
    assert "exchange.verified_pnl.outbox" not in imported
    assert "exchange.verified_pnl.dispatcher" not in imported
    assert "exchange.verified_pnl.delivery_store" not in imported
    assert "post" not in calls and "put" not in calls and "delete" not in calls
    assert "ReadHost(st)" in source
    assert "limit_network_to_exchange_gets()" in source
    assert not Path(ROOT / ".env").is_file(), "CI must not contain real .env"
    assert os.environ.get("POA_EXCHANGE_FILL_CONFIRMATION_ENABLED", "").lower() in ("", "false", "0")
