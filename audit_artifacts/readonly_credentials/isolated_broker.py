"""Offline-only dedicated read-credential boundary.

NEVER activated in production; existing trade-key-derived account references
cannot be silently re-bound to distinct read-key identities.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Mapping

VENUES = frozenset(("UPBIT", "BYBIT", "BINANCE", "OKX"))
PREFIX = "POA_FILL_READ_ONLY_"


class ReadOnlyBlocked(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class DedicatedReadKey:
    venue: str
    access: str
    secret: str
    passphrase: str | None = None

    def __repr__(self):
        return "DedicatedReadKey(<redacted>)"


def account_ref(venue, access):
    """Equal to POA's ledger.identity(venue, access), NOT a venue account ID."""
    if venue not in VENUES or type(access) is not str:
        raise ReadOnlyBlocked("READ_IDENTITY_INVALID")
    return hashlib.sha256(
        json.dumps([venue, access], ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _valid_secret(value):
    return (type(value) is str and 1 <= len(value) <= 4096
            and value.isascii() and value.isprintable()
            and not any(c.isspace() for c in value))


def load_dedicated_key(vault: Mapping[str, str], venue: str) -> DedicatedReadKey:
    """Explicitly supplied isolated read vault; no automatic config or trade-key fallback."""
    if venue not in VENUES:
        raise ReadOnlyBlocked("READ_VENUE_UNSUPPORTED")
    if not isinstance(vault, Mapping):
        raise ReadOnlyBlocked("READ_VAULT_INVALID")
    expected = {PREFIX + venue + "_KEY", PREFIX + venue + "_SECRET"}
    if venue == "OKX":
        expected.add(PREFIX + venue + "_PASSPHRASE")
    if set(vault) != expected:
        raise ReadOnlyBlocked("READ_VAULT_FIELDS_INVALID")
    if any(not _valid_secret(vault[k]) for k in expected):
        raise ReadOnlyBlocked("READ_CREDENTIAL_UNAVAILABLE")
    return DedicatedReadKey(
        venue, vault[PREFIX + venue + "_KEY"], vault[PREFIX + venue + "_SECRET"],
        vault.get(PREFIX + venue + "_PASSPHRASE"),
    )


@dataclass(frozen=True)
class ReadBindingDecision:
    venue: str
    original_account_ref: str
    read_account_ref: str
    separated: bool
    authorized: bool
    status: str


def plan_separate_read(key: DedicatedReadKey, original_account_ref: str,
                       *, trading_access_fingerprint: str | None = None) -> ReadBindingDecision:
    """Fail closed until external verified *same venue account* binding exists.

    Merely possessing a GET-capable key does not prove its permissions or account.
    """
    if type(key) is not DedicatedReadKey or key.venue not in VENUES:
        raise ReadOnlyBlocked("READ_KEY_INVALID")
    if (type(original_account_ref) is not str or len(original_account_ref) != 64
            or any(c not in "0123456789abcdef" for c in original_account_ref)):
        raise ReadOnlyBlocked("READ_ORIGINAL_SCOPE_INVALID")
    read_ref = account_ref(key.venue, key.access)
    read_digest = hashlib.sha256(key.access.encode()).hexdigest()
    if trading_access_fingerprint is not None:
        if (type(trading_access_fingerprint) is not str
                or len(trading_access_fingerprint) != 64
                or any(c not in "0123456789abcdef" for c in trading_access_fingerprint)):
            raise ReadOnlyBlocked("READ_TRADE_FINGERPRINT_INVALID")
        if hmac.compare_digest(read_digest, trading_access_fingerprint):
            raise ReadOnlyBlocked("READ_TRADING_KEY_REUSE")
    if hmac.compare_digest(read_ref, original_account_ref):
        raise ReadOnlyBlocked("READ_TRADING_KEY_REUSE")
    return ReadBindingDecision(
        venue=key.venue, original_account_ref=original_account_ref,
        read_account_ref=read_ref, separated=True, authorized=False,
        status="READ_EXTERNAL_ACCOUNT_BINDING_AND_PERMISSIONS_UNVERIFIED",
    )


def require_operational_authorization(decision: ReadBindingDecision):
    """No activation possible until a separately audited product integration."""
    if type(decision) is not ReadBindingDecision:
        raise ReadOnlyBlocked("READ_BINDING_INVALID")
    raise ReadOnlyBlocked("READ_OPERATIONAL_ACTIVATION_BLOCKED")


class OfflineReadHostSettings:
    """Mock-only adapter for the unmodified actual ReadHost. No key fallback.

    In-process Python isolation is not a security boundary; do not deploy.
    """
    __slots__ = ("_key",)

    def __init__(self, key):
        if type(key) is not DedicatedReadKey or key.venue not in VENUES:
            raise ReadOnlyBlocked("READ_KEY_INVALID")
        object.__setattr__(self, "_key", key)

    def __repr__(self):
        return "OfflineReadHostSettings(<redacted>)"

    def __getattr__(self, name):
        key = object.__getattribute__(self, "_key")
        return {
            key.venue + "_KEY": key.access,
            key.venue + "_SECRET": key.secret,
            key.venue + "_PASSPHRASE": key.passphrase,
        }.get(name)
