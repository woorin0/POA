"""Offline Mock regression for isolated dedicated read-key interface.

Runs with the original immutable POA code, never a real key or exchange.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from audit_artifacts.readonly_credentials import isolated_broker as B
from exchange.verified_pnl import ledger
from exchange.verified_pnl.order_fill_read import ReadHost, ReadRejected

ROOT = Path(__file__).resolve().parents[2]
PRODUCT = "ad88384b8b6030c1109843b09b6761395b3f2d1c"
TREE = "663c82e1134f592beb9c8bc652daccc7b2185975"
VENUES = ("UPBIT", "BYBIT", "BINANCE", "OKX")


def vault(venue, key="dedicated-read-key", secret="dedicated-read-secret"):
    v = {
        f"{B.PREFIX}{venue}_KEY": key,
        f"{B.PREFIX}{venue}_SECRET": secret,
    }
    if venue == "OKX":
        v[f"{B.PREFIX}{venue}_PASSPHRASE"] = "dedicated-read-passphrase"
    return v


def blocked(code, call, *args, **kwargs):
    with pytest.raises(B.ReadOnlyBlocked) as err:
        call(*args, **kwargs)
    assert str(err.value) == code
    assert err.value.code == code


def test_immutable_real_product_source_is_tested_without_product_modification():
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == PRODUCT
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip() == TREE
    assert subprocess.check_output(
        ["git", "diff", "--name-only"], cwd=ROOT, text=True).strip() == ""


@pytest.mark.parametrize("venue", VENUES)
def test_dedicated_vault_has_exact_fields_and_never_falls_back(venue):
    key = B.load_dedicated_key(vault(venue), venue)
    assert key.venue == venue
    host = ReadHost(B.OfflineReadHostSettings(key))
    assert host.credential(venue) == (
        "dedicated-read-key", "dedicated-read-secret",
        "dedicated-read-passphrase" if venue == "OKX" else None,
    )
    for other in VENUES:
        if other != venue:
            with pytest.raises(ReadRejected, match="FILL_CREDENTIAL_UNAVAILABLE"):
                host.credential(other)


@pytest.mark.parametrize("venue", VENUES)
def test_trading_key_name_present_in_isolated_vault_is_denied(venue):
    old = vault(venue)
    old[venue + "_KEY"] = "trading-key"
    blocked("READ_VAULT_FIELDS_INVALID", B.load_dedicated_key, old, venue)


@pytest.mark.parametrize("venue", VENUES)
def test_absent_isolated_vault_never_loads_trading_key(venue):
    blocked("READ_VAULT_FIELDS_INVALID", B.load_dedicated_key, {}, venue)
    blocked("READ_VAULT_FIELDS_INVALID", B.load_dedicated_key,
            {venue + "_KEY": "trade-key", venue + "_SECRET": "trade-secret"}, venue)


@pytest.mark.parametrize("venue", VENUES)
def test_partial_key_material_fails_closed(venue):
    partial = vault(venue)
    del partial[f"{B.PREFIX}{venue}_SECRET"]
    blocked("READ_VAULT_FIELDS_INVALID", B.load_dedicated_key, partial, venue)


@pytest.mark.parametrize("invalid", [None, "", "key with space", "line\nbreak", "한글", 123])
def test_untrusted_value_rejected_without_diagnostic_secret(invalid):
    raw = vault("BYBIT")
    raw[f"{B.PREFIX}BYBIT_KEY"] = invalid
    blocked("READ_CREDENTIAL_UNAVAILABLE", B.load_dedicated_key, raw, "BYBIT")


def test_okx_passphrase_is_mandatory_and_not_inferred():
    raw = vault("OKX")
    del raw[f"{B.PREFIX}OKX_PASSPHRASE"]
    blocked("READ_VAULT_FIELDS_INVALID", B.load_dedicated_key, raw, "OKX")
    raw[f"{B.PREFIX}OKX_PASSPHRASE"] = ""
    blocked("READ_CREDENTIAL_UNAVAILABLE", B.load_dedicated_key, raw, "OKX")


@pytest.mark.parametrize("venue", ["BITGET", "UPBIT ", "binance", "", None])
def test_only_supported_venues_have_the_new_read_contract(venue):
    blocked("READ_VENUE_UNSUPPORTED", B.load_dedicated_key, {}, venue)


@pytest.mark.parametrize("venue", VENUES)
def test_actual_ledger_reference_is_key_based_not_venue_account_based(venue):
    assert B.account_ref(venue, "read-key") == ledger.identity(venue, "read-key")
    assert B.account_ref(venue, "read-key") != ledger.identity(venue, "trading-key")


@pytest.mark.parametrize("venue", VENUES)
def test_separate_key_fails_closed_on_unproven_venue_account_binding(venue):
    read = B.load_dedicated_key(vault(venue), venue)
    original = ledger.identity(venue, "trading-key")
    decision = B.plan_separate_read(read, original,
       trading_access_fingerprint=hashlib.sha256(b"trading-key").hexdigest())
    assert decision.separated
    assert decision.authorized is False
    assert decision.original_account_ref == original
    assert decision.read_account_ref != original
    assert decision.status == "READ_EXTERNAL_ACCOUNT_BINDING_AND_PERMISSIONS_UNVERIFIED"
    blocked("READ_OPERATIONAL_ACTIVATION_BLOCKED",
            B.require_operational_authorization, decision)


@pytest.mark.parametrize("venue", VENUES)
def test_original_trade_key_reuse_is_forbidden_even_if_identical_account_reference(venue):
    read = B.load_dedicated_key(vault(venue, key="trading-key"), venue)
    original = ledger.identity(venue, "trading-key")
    blocked("READ_TRADING_KEY_REUSE", B.plan_separate_read, read, original)


@pytest.mark.parametrize("venue", VENUES)
def test_trading_fingerprint_denies_mislabelled_read_key(venue):
    read = B.load_dedicated_key(vault(venue), venue)
    blocked("READ_TRADING_KEY_REUSE", B.plan_separate_read, read,
            ledger.identity(venue, "other-account"),
            trading_access_fingerprint=hashlib.sha256(read.access.encode()).hexdigest())


@pytest.mark.parametrize("invalid", ["", "a"*63, "f"*65, "z"*64, None, 123])
def test_invalid_original_account_binding_ref_denied(invalid):
    read = B.load_dedicated_key(vault("UPBIT"), "UPBIT")
    blocked("READ_ORIGINAL_SCOPE_INVALID", B.plan_separate_read, read, invalid)


def test_invalid_trading_fingerprint_is_denied():
    read = B.load_dedicated_key(vault("BYBIT"), "BYBIT")
    blocked("READ_TRADE_FINGERPRINT_INVALID", B.plan_separate_read, read,
            ledger.identity("BYBIT", "trading-key"), trading_access_fingerprint="bad")


@pytest.mark.parametrize("venue", VENUES)
def test_real_readhost_rejects_original_trade_key_account_binding_before_client_use(venue):
    read = B.load_dedicated_key(vault(venue), venue)
    original = ledger.identity(venue, "trading-key")
    host = ReadHost(B.OfflineReadHostSettings(read))
    with pytest.raises(ReadRejected, match="FILL_ACCOUNT_MISMATCH"):
        host._client({"exchange": venue, "market_type": "spot", "account": original})
    assert host.clients == {}


def test_no_activation_flags_or_self_attestation_can_enable_live_reader():
    read = B.load_dedicated_key(vault("BINANCE"), "BINANCE")
    decision = B.plan_separate_read(read, ledger.identity("BINANCE", "trade"))
    blocked("READ_OPERATIONAL_ACTIVATION_BLOCKED",
            B.require_operational_authorization, decision)
    blocked("READ_BINDING_INVALID", B.require_operational_authorization, True)
    blocked("READ_BINDING_INVALID", B.require_operational_authorization,
            {"approved": True, "permissions": ["read"]})


def test_sensitive_material_is_not_in_repr_or_error_messages():
    access, secret = "distinct-access-MARKER", "distinct-secret-MARKER"
    read = B.load_dedicated_key(vault("OKX", access, secret), "OKX")
    host = ReadHost(B.OfflineReadHostSettings(read))
    for obj in (read, host.settings):
        for forbidden in (access, secret, "dedicated-read-passphrase"):
            assert forbidden not in repr(obj)
    bad = vault("OKX", access, "bad secret with space")
    try:
        B.load_dedicated_key(bad, "OKX")
    except B.ReadOnlyBlocked as exc:
        for forbidden in (access, secret, "bad secret with space"):
            assert forbidden not in str(exc)
    else:
        raise AssertionError("bad secret incorrectly accepted")


def test_integration_does_not_write_operating_store_or_import_order_server():
    import sys
    assert "main" not in sys.modules
    assert B.require_operational_authorization.__module__.endswith("isolated_broker")

@pytest.mark.parametrize("venue", VENUES)
def test_unmodified_ccxt_client_uses_dedicated_key_in_mock_without_http(venue):
    read = B.load_dedicated_key(vault(venue), venue)
    host = ReadHost(B.OfflineReadHostSettings(read))
    client = host._client({
        "exchange": venue, "market_type": "spot",
        "account": ledger.identity(venue, read.access),
    })
    assert client.apiKey == read.access
    assert client.secret == read.secret
    assert client.password == read.passphrase
    assert client.enableRateLimit is True
    assert host.clients[(venue, "spot")] is client
    # No REST calls: client is configured locally only.
