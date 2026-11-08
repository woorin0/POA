"""Dedicated read-key planner for Vultr Phase 0-1 STAGING ONLY.

Shares PR15's historical POA identity policy without copying or rewriting
any product ledger. No key discovery from environment, files, .env, DB or
trading Settings; an explicit *separate vault mapping* is mandatory.
A key hash is NOT an exchange account ID. No live reading/permission grant
is authorized by this module; PR16-22 evidence was offline only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass

VENUES = frozenset(("UPBIT", "BINANCE", "BYBIT", "OKX"))
PREFIX = "POA_FILL_READ_ONLY_"


class ReadGateBlocked(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _deny(code):
    raise ReadGateBlocked(code)


def _hexsha(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _secret(value):
    return (type(value) is str and 1 <= len(value) <= 4096
            and value.isascii() and value.isprintable()
            and not any(ch.isspace() for ch in value))


@dataclass(frozen=True, repr=False)
class DedicatedReadKey:
    venue: str
    access: str
    secret: str
    passphrase: str | None

    def __repr__(self):
        return "DedicatedReadKey(<redacted>)"


@dataclass(frozen=True)
class ReadPlan:
    venue: str
    old_trading_ref: str
    new_read_ref: str
    authorized: bool
    gate: str


def historical_ref(venue, access):
    """Byte-identical formula to exchange.verified_pnl.ledger.identity."""
    if venue not in VENUES or type(access) is not str:
        _deny("READ_IDENTITY_INVALID")
    return hashlib.sha256(json.dumps((venue, access), ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()


def load_explicit_read_key(vault: Mapping[str, str], venue: str):
    if venue not in VENUES:
        _deny("READ_VENUE_UNSUPPORTED")
    if not isinstance(vault, Mapping):
        _deny("READ_VAULT_INVALID")
    fields = {PREFIX + venue + "_KEY", PREFIX + venue + "_SECRET"}
    if venue == "OKX":
        fields.add(PREFIX + venue + "_PASSPHRASE")
    # Do not accidentally accept trading-key fields or other venue's keys.
    if set(vault) != fields:
        _deny("READ_VAULT_FIELDS_INVALID")
    if any(not _secret(vault[field]) for field in fields):
        _deny("READ_CREDENTIAL_UNAVAILABLE")
    return DedicatedReadKey(venue,
        vault[PREFIX + venue + "_KEY"],
        vault[PREFIX + venue + "_SECRET"],
        vault.get(PREFIX + venue + "_PASSPHRASE"))


def stage_read_plan(key: DedicatedReadKey, original_trading_ref: str,
                    *, trading_key_fingerprint: str | None = None):
    if type(key) is not DedicatedReadKey or key.venue not in VENUES:
        _deny("READ_KEY_INVALID")
    if not _hexsha(original_trading_ref):
        _deny("READ_ORIGINAL_SCOPE_INVALID")
    new_ref = historical_ref(key.venue, key.access)
    if hmac.compare_digest(new_ref, original_trading_ref):
        _deny("READ_TRADING_KEY_REUSE")
    if trading_key_fingerprint is not None:
        if not _hexsha(trading_key_fingerprint):
            _deny("READ_TRADE_FINGERPRINT_INVALID")
        if hmac.compare_digest(hashlib.sha256(key.access.encode()).hexdigest(), trading_key_fingerprint):
            _deny("READ_TRADING_KEY_REUSE")
    return ReadPlan(key.venue, original_trading_ref, new_ref, False,
        "READ_EXTERNAL_ACCOUNT_BINDING_AND_PERMISSIONS_UNVERIFIED")


def require_proven_live_read(plan: ReadPlan):
    # PR16's observer/approver/witness signatures were synthetic.
    # Never upgrade signed Mock assertions to venue attested identity.
    if type(plan) is not ReadPlan or plan.authorized is not False:
        _deny("READ_BINDING_INVALID")
    _deny("READ_PRODUCTION_BINDING_UNVERIFIED")


class OfflineReadHostSettings:
    """Pure settings view for an injected Mock ReadHost, no trading credentials.

    Does not grant network or operational access. The real product ReadHost
    refuses alternate-key identity with historic trading-key account refs.
    """
    __slots__ = ("_key",)

    def __init__(self, key: DedicatedReadKey):
        if type(key) is not DedicatedReadKey or key.venue not in VENUES:
            _deny("READ_KEY_INVALID")
        object.__setattr__(self, "_key", key)

    def __getattr__(self, name):
        key = object.__getattribute__(self, "_key")
        known = {
            key.venue + "_KEY": key.access,
            key.venue + "_SECRET": key.secret,
            key.venue + "_PASSPHRASE": key.passphrase,
        }
        if name not in known:
            raise AttributeError(name)
        return known[name]

    def __repr__(self):
        return "OfflineReadHostSettings(<redacted>)"
