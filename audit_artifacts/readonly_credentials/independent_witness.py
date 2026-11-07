"""Offline-only third-party evidence witness for PR16 signed account binding.

The independent witness signs digests of authenticated, independently observed
venue account and permission evidence, BOTH original role attestations and the
signed approval, with frozen root commitment, challenge and ledger predecessor.

This is ONLY a Mock protocol: no authenticated exchange observation, external
pin store, independent signer custody or durable monotonic ledger is provided.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import immutable_binding as B

KIND = "INDEPENDENT_EVIDENCE_WITNESS_V1"
SOURCE_KIND = "INDEPENDENT_AUTHENTICATED_VENUE_CAPTURE"
FIELDS = frozenset({
    "venue", "scope", "account_tag", "subaccount_tag", "legacy_ref",
    "read_ref", "challenge", "sequence", "previous", "root_commitment",
    "trade_observation_digest", "read_observation_digest", "approval_digest",
    "trade_source_digest", "read_source_digest",
    "trade_permissions", "read_permissions", "source_kind",
    "issued_at", "expires_at",
})


class IndependentWitness:
    __slots__ = ("_kid", "_key", "_pin", "_root_commitment")

    def __setattr__(self, name, value):
        if name in self.__slots__ and hasattr(self, name):
            B.deny("WITNESS_ROOT_IMMUTABLE")
        object.__setattr__(self, name, value)

    def __init__(self, authorities, *, kid, public_key, expected_sha256,
                 expected_roots_commitment):
        if type(authorities) is not B.PinnedAuthorities:
            B.deny("WITNESS_AUTHORITY_INVALID")
        if (type(kid) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", kid)
                or type(public_key) is not bytes or len(public_key) != 32
                or not B.is_hash(expected_sha256)
                or not B.is_hash(expected_roots_commitment)):
            B.deny("WITNESS_ROOT_INVALID")
        if not hmac.compare_digest(hashlib.sha256(public_key).hexdigest(),
                                   expected_sha256):
            B.deny("WITNESS_ROOT_PIN_MISMATCH")
        if not hmac.compare_digest(authorities.root_commitment(),
                                   expected_roots_commitment):
            B.deny("WITNESS_APPROVAL_ROOT_REPLACED")
        if any(hmac.compare_digest(public_key, authorities.role_public_key(role))
               for role in ("observer", "approver")):
            B.deny("WITNESS_ROLE_KEY_REUSE")
        try:
            key = Ed25519PublicKey.from_public_bytes(public_key)
        except ValueError:
            B.deny("WITNESS_ROOT_INVALID")
        self._kid = kid
        self._key = key
        self._pin = expected_sha256
        self._root_commitment = expected_roots_commitment

    def check_roots(self, authorities):
        if (type(authorities) is not B.PinnedAuthorities
                or not hmac.compare_digest(authorities.root_commitment(),
                                           self._root_commitment)):
            B.deny("WITNESS_APPROVAL_ROOT_REPLACED")

    def verify(self, envelope):
        # Take an immutable snapshot before checking the detached signature.
        envelope = json.loads(B.canonical(envelope))
        if not B.valid_fields(envelope, {"domain", "kind", "kid", "body", "signature"}):
            B.deny("WITNESS_ENVELOPE_INVALID")
        if (envelope["domain"] != B.DOMAIN or envelope["kind"] != KIND
                or envelope["kid"] != self._kid
                or not B.valid_fields(envelope["body"], FIELDS)):
            B.deny("WITNESS_SIGNER_OR_SCHEMA_INVALID")
        signature = envelope["signature"]
        if type(signature) is not str or len(signature) != 88:
            B.deny("WITNESS_SIGNATURE_INVALID")
        try:
            raw = base64.b64decode(signature, validate=True)
            if len(raw) != 64 or base64.b64encode(raw).decode("ascii") != signature:
                B.deny("WITNESS_SIGNATURE_INVALID")
            signed = {k: envelope[k] for k in ("domain", "kind", "kid", "body")}
            self._key.verify(raw, B.canonical(signed))
        except (InvalidSignature, ValueError, TypeError):
            B.deny("WITNESS_SIGNATURE_INVALID")
        return envelope["body"]


def verify_witnessed_pair(authorities, witness, trade, read, approval,
                          witness_record, *, expected_venue, expected_scope,
                          expected_legacy_ref, expected_read_ref,
                          expected_challenge, trusted_head, now):
    if type(witness) is not IndependentWitness:
        B.deny("WITNESS_INVALID")
    witness.check_roots(authorities)
    # Copy everything once to avoid signature verification on one byte string
    # and binding the digest of a subsequently modified Python object.
    trade, read, approval, witness_record = (
        json.loads(B.canonical(x)) for x in (trade, read, approval, witness_record)
    )
    proposal = B.verify_pair(
        authorities, authorities, trade, read, approval,
        expected_venue=expected_venue, expected_scope=expected_scope,
        expected_legacy_ref=expected_legacy_ref, expected_read_ref=expected_read_ref,
        expected_challenge=expected_challenge, trusted_head=trusted_head, now=now)
    observed_trade, observed_read, approved = (
        trade["body"], read["body"], approval["body"]
    )
    body = witness.verify(witness_record)
    B.check_time(body, now)
    if (body["source_kind"] != SOURCE_KIND
            or body["venue"] != expected_venue
            or body["scope"] != expected_scope
            or body["account_tag"] != observed_trade["account_tag"]
            or body["subaccount_tag"] != observed_trade["subaccount_tag"]
            or body["legacy_ref"] != expected_legacy_ref
            or body["read_ref"] != expected_read_ref
            or body["challenge"] != expected_challenge
            or type(body["sequence"]) is not int
            or body["sequence"] != proposal.successor.sequence
            or body["previous"] != proposal.predecessor.chain_hash
            or body["root_commitment"] != authorities.root_commitment()):
        B.deny("WITNESS_CONTEXT_MISMATCH")
    if any(not B.is_hash(body[k]) for k in
           ("trade_observation_digest", "read_observation_digest",
            "approval_digest", "trade_source_digest", "read_source_digest")):
        B.deny("WITNESS_EVIDENCE_SCHEMA_INVALID")
    if (not hmac.compare_digest(body["trade_observation_digest"], B.digest(trade))
            or not hmac.compare_digest(body["read_observation_digest"], B.digest(read))
            or not hmac.compare_digest(body["approval_digest"], B.digest(approval))
            or not hmac.compare_digest(body["trade_source_digest"],
                                       observed_trade["source_digest"])
            or not hmac.compare_digest(body["read_source_digest"],
                                       observed_read["source_digest"])):
        B.deny("WITNESS_EVIDENCE_MISMATCH")
    if (body["read_permissions"] != ["READ"]
            or body["trade_permissions"] != ["READ", "TRADE"]
            or type(body["read_permissions"]) is not list
            or type(body["trade_permissions"]) is not list):
        B.deny("WITNESS_PERMISSIONS_MISMATCH")
    if body["issued_at"] < approved["issued_at"]:
        B.deny("WITNESS_ATTESTATION_ORDER")
    # A witnessed signature still asserts, but does not prove, real venue
    # provenance or externally safe account binding.
    return proposal


class MockWitnessedRegistry(B.MockAppendOnlyRegistry):
    """Anchored, one-witness CAS Mock only; memory can be reset or replaced."""

    def __init__(self, trusted_head, authorities, witness):
        if type(authorities) is not B.PinnedAuthorities or type(witness) is not IndependentWitness:
            B.deny("WITNESS_AUTHORITY_INVALID")
        witness.check_roots(authorities)
        super().__init__(trusted_head)
        self._authorities = authorities
        self._witness = witness

    def compare_and_append(self, trade, read, approval, witness_record, **kwargs):
        with self._lock:
            proposal = verify_witnessed_pair(
                self._authorities, self._witness, trade, read, approval,
                witness_record, trusted_head=self._head, **kwargs)
            return self._append_verified(proposal)
