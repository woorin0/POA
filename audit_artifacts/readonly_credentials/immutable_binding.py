"""POA P0 offline-only exchange account key-binding attestation protocol v1.

Verifies independent Ed25519 observer + approver signatures, immutable legacy
key-reference lineage, exact account/subaccount and market scope, explicit
normalized read-only permissions, fresh challenge, chain predecessor and
monotonic sequence. A successful Mock means signed TEST CLAIMS are internally
consistent; it cannot prove the claims reflect real exchange accounts.

No exchange clients, credentials, operating DB, network or live activation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
from dataclasses import dataclass
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DOMAIN = "POA_EXTERNAL_KEY_ACCOUNT_BINDING_V1"
VENUES = frozenset(("UPBIT", "BYBIT", "BINANCE", "OKX"))
SCOPES = frozenset(("spot", "linear"))
ZERO = "0" * 64
MAX_WINDOW = 300
HEX64 = re.compile(r"^[0-9a-f]{64}$")
KINDS = {"observer": "KEY_OBSERVATION", "approver": "BINDING_APPROVAL"}


class BindingBlocked(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def deny(code):
    raise BindingBlocked(code)


def is_hash(value):
    return type(value) is str and HEX64.fullmatch(value) is not None


def canonical(obj):
    try:
        result = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        deny("BINDING_SCHEMA_INVALID")
    if len(result) > 16384:
        deny("BINDING_OVERSIZE")
    return result


def digest(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def valid_fields(obj, fields):
    return type(obj) is dict and set(obj) == set(fields)


def check_time(body, now):
    if type(now) is not int or not 1 <= now <= 4102444800:
        deny("BINDING_CLOCK_INVALID")
    issued, expires = body.get("issued_at"), body.get("expires_at")
    if (type(issued) is not int or type(expires) is not int
            or not 0 < issued <= now <= expires <= issued + MAX_WINDOW):
        deny("BINDING_STALE_OR_FUTURE")


@dataclass(frozen=True)
class TrustedHead:
    sequence: int
    chain_hash: str

    def __post_init__(self):
        if type(self.sequence) is not int or self.sequence < 0 or not is_hash(self.chain_hash):
            deny("BINDING_HEAD_INVALID")
        if (self.sequence == 0) != (self.chain_hash == ZERO):
            deny("BINDING_GENESIS_INVALID")


@dataclass(frozen=True)
class VerifiedProposal:
    predecessor: TrustedHead
    successor: TrustedHead
    binding_digest: str
    venue: str
    scope: str
    legacy_ref: str
    read_ref: str
    challenge: str = ""
    externally_proven: bool = False
    operationally_authorized: bool = False


class PinnedAuthorities:
    """Trust roots and expected fingerprints supplied out-of-band, NEVER in proof.

    A compromised local pin store can approve forged claims. External immutable
    pin distribution/revocation remains an unverified production dependency.
    """

    __slots__ = ("_keys", "_root_commitment")

    def __setattr__(self, name, value):
        if name in self.__slots__ and hasattr(self, name):
            deny("BINDING_ROOT_IMMUTABLE")
        object.__setattr__(self, name, value)

    def __init__(self, roots, expected_sha256):
        if not (valid_fields(roots, {"observer", "approver"})
                and valid_fields(expected_sha256, {"observer", "approver"})):
            deny("BINDING_ROOT_SET_INVALID")
        verified = {}
        seen = set()
        for role in ("observer", "approver"):
            entry = roots[role]
            if not valid_fields(entry, {"kid", "public_key"}):
                deny("BINDING_ROOT_SET_INVALID")
            kid, pub = entry["kid"], entry["public_key"]
            if (type(kid) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", kid)
                    or type(pub) is not bytes or len(pub) != 32
                    or not is_hash(expected_sha256[role])
                    or not hmac.compare_digest(hashlib.sha256(pub).hexdigest(), expected_sha256[role])
                    or pub in seen):
                deny("BINDING_ROOT_PIN_INVALID")
            try:
                key = Ed25519PublicKey.from_public_bytes(pub)
            except ValueError:
                deny("BINDING_ROOT_PIN_INVALID")
            verified[role] = (kid, key)
            seen.add(pub)
        # Protect the already-validated roots against ordinary mutable-dict
        # substitution. Python interpreter compromise still defeats this guard.
        self._keys = MappingProxyType(verified)
        self._root_commitment = digest({"domain": DOMAIN, "roots": [
            {"role": role, "kid": roots[role]["kid"],
             "sha256": expected_sha256[role]}
            for role in ("observer", "approver")]})

    def root_commitment(self):
        return self._root_commitment

    def role_public_key(self, role):
        if role not in self._keys:
            deny("BINDING_ROOT_SET_INVALID")
        from cryptography.hazmat.primitives import serialization
        return self._keys[role][1].public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def verify(self, role, envelope):
        if role not in self._keys:
            deny("BINDING_SIGNER_OR_DOMAIN_INVALID")
        if not valid_fields(envelope, {"domain", "kind", "kid", "body", "signature"}):
            deny("BINDING_ENVELOPE_INVALID")
        kid, key = self._keys[role]
        if (envelope["domain"] != DOMAIN or envelope["kind"] != KINDS[role]
                or envelope["kid"] != kid or type(envelope["body"]) is not dict):
            deny("BINDING_SIGNER_OR_DOMAIN_INVALID")
        signature = envelope["signature"]
        if type(signature) is not str or len(signature) != 88:
            deny("BINDING_SIGNATURE_INVALID")
        try:
            raw = base64.b64decode(signature, validate=True)
            if len(raw) != 64:
                deny("BINDING_SIGNATURE_INVALID")
            signed = {k: envelope[k] for k in ("domain", "kind", "kid", "body")}
            key.verify(raw, canonical(signed))
        except (ValueError, InvalidSignature, TypeError):
            deny("BINDING_SIGNATURE_INVALID")
        return envelope["body"]


_OBS = ("venue", "scope", "role", "account_tag", "subaccount_tag",
        "key_ref", "permissions", "source_kind", "source_digest",
        "challenge", "issued_at", "expires_at")
_APPROVAL = ("venue", "scope", "account_tag", "subaccount_tag",
             "legacy_ref", "read_ref", "trade_observation_digest",
             "read_observation_digest", "sequence", "previous",
             "challenge", "issued_at", "expires_at", "policy")
POLICY = "POA_ACCOUNT_PAIR_READ_ONLY_V1"


def checked_observation(body, role, now, challenge):
    if not valid_fields(body, _OBS):
        deny("BINDING_OBSERVATION_SCHEMA")
    if (body["venue"] not in VENUES or body["scope"] not in SCOPES
            or body["role"] != role or body["source_kind"] != "AUTHENTICATED_EXCHANGE_RESPONSE"
            or any(not is_hash(body[k]) for k in
                   ("account_tag", "subaccount_tag", "key_ref", "source_digest"))):
        deny("BINDING_OBSERVATION_SCOPE")
    if body["challenge"] != challenge:
        deny("BINDING_CHALLENGE_MISMATCH")
    if (type(body["permissions"]) is not list
            or body["permissions"] != (["READ"] if role == "read" else ["READ", "TRADE"])):
        deny("BINDING_PERMISSION_SCOPE")
    check_time(body, now)
    return body


def verify_pair(observer, approver, trade, read, approval, *,
                expected_venue, expected_scope, expected_legacy_ref,
                expected_read_ref, expected_challenge, trusted_head, now):
    """Pure verifier producing a proposal, NOT an operational account binding.

    Observer must have separately authenticated both real exchange principals;
    independent approver must have checked the raw venue observations. That
    actual behavior is deliberately outside this offline simulation.
    """
    if type(observer) is not PinnedAuthorities or approver is not observer:
        deny("BINDING_AUTHORITY_INVALID")
    # Freeze the external JSON-shaped inputs before signature verification and
    # digest calculation to avoid re-reading a concurrently mutable dict.
    trade = json.loads(canonical(trade))
    read = json.loads(canonical(read))
    approval = json.loads(canonical(approval))
    if (expected_venue not in VENUES or expected_scope not in SCOPES
            or any(not is_hash(x) for x in (expected_legacy_ref, expected_read_ref))
            or not is_hash(expected_challenge)
            or not isinstance(trusted_head, TrustedHead)):
        deny("BINDING_EXPECTATION_INVALID")
    if hmac.compare_digest(expected_legacy_ref, expected_read_ref):
        deny("BINDING_SAME_KEY_REUSE")
    t = checked_observation(observer.verify("observer", trade),
                            "trade", now, expected_challenge)
    r = checked_observation(observer.verify("observer", read),
                            "read", now, expected_challenge)
    a = approver.verify("approver", approval)
    if not valid_fields(a, _APPROVAL):
        deny("BINDING_APPROVAL_SCHEMA")
    if (a["policy"] != POLICY or a["challenge"] != expected_challenge
            or a["venue"] != expected_venue or a["scope"] != expected_scope):
        deny("BINDING_APPROVAL_CONTEXT")
    check_time(a, now)
    if (t["issued_at"] > a["issued_at"] or r["issued_at"] > a["issued_at"]):
        deny("BINDING_ATTESTATION_ORDER")
    for obs in (t, r):
        if (obs["venue"] != expected_venue or obs["scope"] != expected_scope
                or obs["account_tag"] != a["account_tag"]
                or obs["subaccount_tag"] != a["subaccount_tag"]):
            deny("BINDING_CROSS_ACCOUNT")
    if (not hmac.compare_digest(t["key_ref"], expected_legacy_ref)
            or not hmac.compare_digest(r["key_ref"], expected_read_ref)
            or a["legacy_ref"] != expected_legacy_ref
            or a["read_ref"] != expected_read_ref):
        deny("BINDING_KEY_REF_MISMATCH")
    if any(not is_hash(a[k]) for k in
           ("account_tag", "subaccount_tag", "trade_observation_digest",
            "read_observation_digest", "previous")):
        deny("BINDING_APPROVAL_SCHEMA")
    if (not hmac.compare_digest(a["trade_observation_digest"], digest(trade))
            or not hmac.compare_digest(a["read_observation_digest"], digest(read))):
        deny("BINDING_OBSERVATION_SUBSTITUTION")
    if (type(a["sequence"]) is not int
            or a["sequence"] != trusted_head.sequence + 1
            or not hmac.compare_digest(a["previous"], trusted_head.chain_hash)):
        deny("BINDING_ROLLBACK_OR_CONFLICT")
    record = {"domain": DOMAIN, "sequence": a["sequence"],
              "previous": trusted_head.chain_hash, "approval_digest": digest(approval),
              "trade_observation_digest": digest(trade),
              "read_observation_digest": digest(read)}
    successor = TrustedHead(a["sequence"], digest(record))
    return VerifiedProposal(trusted_head, successor, digest(approval),
                            expected_venue, expected_scope, expected_legacy_ref,
                            expected_read_ref, expected_challenge)


class MockAppendOnlyRegistry:
    """Thread-safe CAS mock; not WORM, no persistence or rollback protection."""

    def __init__(self, trusted_head):
        if not isinstance(trusted_head, TrustedHead):
            deny("BINDING_HEAD_INVALID")
        self._lock = threading.Lock()
        self._head = trusted_head
        self._history = ()
        self._by_legacy = {}
        self._by_read = {}
        self._used_challenges = set()

    @property
    def head(self):
        return self._head

    @property
    def history(self):
        return self._history

    def _append_verified(self, proposal):
            # A single external challenge may never be consumed twice within
            # this process, even for a different account at a later sequence.
            if proposal.predecessor != self._head:
                deny("BINDING_ROLLBACK_OR_CONFLICT")
            if proposal.challenge in self._used_challenges:
                deny("BINDING_CHALLENGE_REPLAY")
            legacy = (proposal.venue, proposal.scope, proposal.legacy_ref)
            read_key = (proposal.venue, proposal.scope, proposal.read_ref)
            # The original ledger account reference is immutable. A valid
            # approval at a later sequence is NOT a rotation authorization.
            # Neither endpoint may be rebound without a separate reviewed
            # revocation/rotation protocol.
            if legacy in self._by_legacy or read_key in self._by_read:
                deny("BINDING_ALREADY_BOUND")
            self._history += (proposal.successor.chain_hash,)
            self._by_legacy[legacy] = proposal.read_ref
            self._by_read[read_key] = proposal.legacy_ref
            self._used_challenges.add(proposal.challenge)
            self._head = proposal.successor
            return proposal

    def compare_and_append(self, observer, approver, trade, read, approval, **kwargs):
        with self._lock:
            proposal = verify_pair(observer, approver, trade, read, approval,
                                   trusted_head=self._head, **kwargs)
            return self._append_verified(proposal)


def require_operational_activation(_proposal):
    """Hard deny; there is no trading/read-key integration in this PR."""
    deny("BINDING_LIVE_ACTIVATION_BLOCKED")
