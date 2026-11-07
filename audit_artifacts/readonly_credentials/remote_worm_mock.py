"""External WORM/CAS protocol reference: strictly offline simulated network.

NO remote service is deployed, and this in-memory server is NOT WORM.
Verifies PR17 triple-signed evidence before a distinct remote-operator grant,
serializes CAS + request nonce under one lock, and issues Ed25519 checkpoints.
An independent MOCK high-water client rejects server rollback and split-view.
Only synthetic principals/evidence may be supplied in the CI workflow.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import immutable_binding as B
from . import independent_witness as W

DOMAIN = "POA_REMOTE_WORM_BOUNDARY_V1"
ZERO = B.ZERO
ROLES = ("writer", "operator", "auditor", "checkpoint")
KINDS = {"writer": ("APPEND_PROOF", "CHECKPOINT_QUERY"),
         "operator": ("APPEND_GRANT",),
         "auditor": ("CHECKPOINT_QUERY",),
         "checkpoint": ("SIGNED_CHECKPOINT",)}
QUERY_FIELDS = {"nonce"}
APPEND_FIELDS = {"request_id", "expected", "evidence"}
EVIDENCE_FIELDS = {"trade", "read", "approval", "witness", "context"}
CONTEXT_FIELDS = {"expected_venue", "expected_scope", "expected_legacy_ref",
                  "expected_read_ref", "expected_challenge"}
GRANT_FIELDS = {"request_id", "writer_digest", "expected", "operator_roots",
                "source_roots", "witness_root", "issued_at", "expires_at"}
HEAD_FIELDS = {"sequence", "chain_hash"}
CP_FIELDS = {"sequence", "chain_hash", "entries_digest", "request_digest",
             "operator_roots", "source_roots", "witness_root", "purpose"}
ENTRY_FIELDS = {"sequence", "previous", "chain_hash", "binding_digest",
                "venue", "scope", "legacy_ref", "read_ref", "challenge",
                "request_digest"}


def fail(code):
    B.deny(code)


def clean(obj):
    return json.loads(B.canonical(obj))


def fields(value, expected):
    return B.valid_fields(value, expected)


def hash_ok(value):
    return B.is_hash(value)


def pubkey(private):
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def make_envelope(private, *, kid, kind, body):
    if type(body) is not dict:
        fail("REMOTE_ENVELOPE_INVALID")
    obj = {"domain": DOMAIN, "kind": kind, "kid": kid, "body": clean(body)}
    obj["signature"] = base64.b64encode(private.sign(B.canonical(obj))).decode("ascii")
    return obj


def strict_decode(raw):
    if type(raw) is not bytes or not (1 <= len(raw) <= 16384):
        fail("REMOTE_FRAME_INVALID")
    try:
        def unique(pairs):
            out = {}
            for k, v in pairs:
                if k in out:
                    raise ValueError("duplicate")
                out[k] = v
            return out
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if B.canonical(parsed) != raw:
            fail("REMOTE_FRAME_NONCANONICAL")
        return parsed
    except B.BindingBlocked:
        raise
    except (ValueError, UnicodeError, TypeError):
        fail("REMOTE_FRAME_INVALID")


def encode(obj):
    return B.canonical(obj)


def head_dict(head):
    return {"sequence": head.sequence, "chain_hash": head.chain_hash}


def parse_head(data):
    if not fields(data, HEAD_FIELDS):
        fail("REMOTE_HEAD_SCHEMA")
    return B.TrustedHead(data["sequence"], data["chain_hash"])


class PinnedRemoteRoles:
    """Public-key trust material MUST be supplied independently of every frame."""
    __slots__ = ("_roles", "_commitment")

    def __setattr__(self, name, value):
        if name in self.__slots__ and hasattr(self, name):
            fail("REMOTE_ROOT_IMMUTABLE")
        object.__setattr__(self, name, value)

    def __init__(self, entries, externally_pinned_sha256):
        if not fields(entries, ROLES) or not fields(externally_pinned_sha256, ROLES):
            fail("REMOTE_ROOT_SET_INVALID")
        stored = {}
        seen = set()
        for role in ROLES:
            item = entries[role]
            if not fields(item, {"kid", "public_key"}):
                fail("REMOTE_ROOT_SET_INVALID")
            kid, public = item["kid"], item["public_key"]
            if (type(kid) is not str
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,40}", kid) is None
                    or type(public) is not bytes or len(public) != 32
                    or not hash_ok(externally_pinned_sha256[role])
                    or not hmac.compare_digest(hashlib.sha256(public).hexdigest(),
                                               externally_pinned_sha256[role])
                    or public in seen):
                fail("REMOTE_ROOT_PIN_INVALID")
            try:
                key = Ed25519PublicKey.from_public_bytes(public)
            except ValueError:
                fail("REMOTE_ROOT_PIN_INVALID")
            seen.add(public)
            stored[role] = (kid, key, public)
        self._roles = MappingProxyType(stored)
        self._commitment = B.digest({
            "domain": DOMAIN, "public_roles": [
                {"role": role, "kid": entries[role]["kid"],
                 "pin": externally_pinned_sha256[role]} for role in ROLES]})

    @property
    def commitment(self):
        return self._commitment

    def public_bytes(self, role):
        return self._roles[role][2]

    def kid(self, role):
        return self._roles[role][0]

    def verify(self, role, envelope, kind):
        if role not in ROLES or kind not in KINDS[role]:
            fail("REMOTE_ROLE_FORBIDDEN")
        envelope = clean(envelope)
        if not fields(envelope, {"domain", "kind", "kid", "body", "signature"}):
            fail("REMOTE_ENVELOPE_SCHEMA")
        kid, key, _ = self._roles[role]
        if (envelope["domain"] != DOMAIN or envelope["kind"] != kind
                or envelope["kid"] != kid or type(envelope["body"]) is not dict):
            fail("REMOTE_ROLE_OR_DOMAIN")
        signature = envelope["signature"]
        if type(signature) is not str or len(signature) != 88:
            fail("REMOTE_SIGNATURE_INVALID")
        try:
            signature_bytes = base64.b64decode(signature, validate=True)
            if (len(signature_bytes) != 64
                    or base64.b64encode(signature_bytes).decode() != signature):
                fail("REMOTE_SIGNATURE_INVALID")
            key.verify(signature_bytes, B.canonical(
                {k: envelope[k] for k in ("domain", "kind", "kid", "body")}))
        except (InvalidSignature, ValueError, TypeError):
            fail("REMOTE_SIGNATURE_INVALID")
        return envelope["body"]

    def validate_disjoint(self, sources, witness):
        if type(sources) is not B.PinnedAuthorities or type(witness) is not W.IndependentWitness:
            fail("REMOTE_ROOT_ROLE_COLLISION")
        original = [sources.role_public_key(r) for r in ("observer", "approver")]
        original.append(witness._key.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        if any(pub in original for pub in (self.public_bytes(r) for r in ROLES)):
            fail("REMOTE_ROOT_ROLE_COLLISION")


class IndependentMockAnchor:
    """Separate local simulation of an externally durable high-water ledger.

    Does NOT persist across process restart; real signed external WORM required.
    """
    def __init__(self, pins, sources, witness):
        pins.validate_disjoint(sources, witness)
        self.pins = pins
        self.source_pin = sources.root_commitment()
        self.witness_pin = witness._root_commitment
        self._lock = threading.Lock()
        self._floor = B.TrustedHead(0, ZERO)
        self._prefix = B.digest([])

    @property
    def floor(self):
        return self._floor

    def verify_and_pin(self, response, sent_request):
        response = clean(response)
        if not fields(response, {"checkpoint", "entries"}):
            fail("REMOTE_RESPONSE_SCHEMA")
        body = self.pins.verify("checkpoint", response["checkpoint"], "SIGNED_CHECKPOINT")
        if not fields(body, CP_FIELDS):
            fail("REMOTE_CHECKPOINT_SCHEMA")
        if (body["operator_roots"] != self.pins.commitment
                or body["source_roots"] != self.source_pin
                or body["witness_root"] != self.witness_pin):
            fail("REMOTE_CHECKPOINT_ROOT_REPLACED")
        if not hmac.compare_digest(body["request_digest"], B.digest(sent_request)):
            fail("REMOTE_RESPONSE_REPLAY")
        head = parse_head({"sequence": body["sequence"], "chain_hash": body["chain_hash"]})
        entries = response["entries"]
        if (type(entries) is not list or len(entries) > 1000
                or len(entries) != head.sequence):
            fail("REMOTE_ENTRIES_COUNT_INVALID")
        if body["entries_digest"] != B.digest(entries):
            fail("REMOTE_ENTRIES_DIGEST_INVALID")
        prev = ZERO
        for i, entry in enumerate(entries, 1):
            if (not fields(entry, ENTRY_FIELDS)
                    or type(entry["sequence"]) is not int
                    or entry["sequence"] != i or entry["previous"] != prev
                    or any(not hash_ok(entry[k]) for k in
                           ("previous", "chain_hash", "binding_digest", "legacy_ref",
                            "read_ref", "challenge", "request_digest"))
                    or entry["venue"] not in B.VENUES or entry["scope"] not in B.SCOPES):
                fail("REMOTE_ENTRIES_CHAIN_INVALID")
            prev = entry["chain_hash"]
        if prev != head.chain_hash:
            fail("REMOTE_ENTRIES_CHAIN_INVALID")
        if body["purpose"] not in ("HEAD", "APPEND"):
            fail("REMOTE_CHECKPOINT_SCHEMA")
        with self._lock:
            if head.sequence < self._floor.sequence:
                fail("REMOTE_ROLLBACK_DETECTED")
            if head.sequence == self._floor.sequence and head.chain_hash != self._floor.chain_hash:
                fail("REMOTE_SPLIT_VIEW")
            if B.digest(entries[:self._floor.sequence]) != self._prefix:
                fail("REMOTE_PREFIX_REWRITTEN")
            self._floor = head
            self._prefix = B.digest(entries)
        return head


class MockRemoteWORMServer:
    """Simulated separate operator/server; in-memory CAS NOT actual WORM."""
    def __init__(self, pins, sources, witness, checkpoint_private, clock):
        if type(pins) is not PinnedRemoteRoles:
            fail("REMOTE_ROOT_SET_INVALID")
        pins.validate_disjoint(sources, witness)
        if pubkey(checkpoint_private) != pins.public_bytes("checkpoint"):
            fail("REMOTE_SIGNER_PIN_MISMATCH")
        self._pins = pins
        self._sources = sources
        self._witness = witness
        self._checkpoint_private = checkpoint_private
        self._clock = clock
        self._head = B.TrustedHead(0, ZERO)
        self._records = ()
        self._completed = {}
        self._used_challenges = set()
        self._by_legacy = set()
        self._by_read = set()
        self._lock = threading.RLock()
        self.fail_before_commit_once = False

    @property
    def head(self):
        return self._head

    @property
    def records(self):
        return self._records

    def _checkpoint(self, request, *, purpose, snapshot=None):
        head, entries = snapshot or (self._head, self._records)
        body = {"sequence": head.sequence, "chain_hash": head.chain_hash,
                "entries_digest": B.digest(list(entries)),
                "request_digest": B.digest(request),
                "operator_roots": self._pins.commitment,
                "source_roots": self._sources.root_commitment(),
                "witness_root": self._witness._root_commitment,
                "purpose": purpose}
        return {"checkpoint": make_envelope(
                    self._checkpoint_private, kid=self._pins.kid("checkpoint"),
                    kind="SIGNED_CHECKPOINT", body=body),
                "entries": list(entries)}

    def dispatch(self, raw):
        frame = strict_decode(raw)
        if not type(frame) is dict or "op" not in frame:
            fail("REMOTE_OPERATION_INVALID")
        with self._lock:
            if frame["op"] == "HEAD":
                if not fields(frame, {"op", "actor", "query"}):
                    fail("REMOTE_OPERATION_INVALID")
                role = frame["actor"]
                if role not in ("auditor", "writer"):
                    fail("REMOTE_ROLE_FORBIDDEN")
                body = self._pins.verify(role, frame["query"], "CHECKPOINT_QUERY")
                if not fields(body, QUERY_FIELDS) or not hash_ok(body["nonce"]):
                    fail("REMOTE_QUERY_INVALID")
                return encode(self._checkpoint(frame, purpose="HEAD"))
            if frame["op"] != "APPEND" or not fields(frame, {"op", "writer", "grant"}):
                fail("REMOTE_ROLE_FORBIDDEN")
            writer_body = self._pins.verify("writer", frame["writer"], "APPEND_PROOF")
            grant_body = self._pins.verify("operator", frame["grant"], "APPEND_GRANT")
            if (not fields(writer_body, APPEND_FIELDS)
                    or not fields(grant_body, GRANT_FIELDS)
                    or not hash_ok(writer_body["request_id"])):
                fail("REMOTE_APPEND_SCHEMA")
            if (grant_body["request_id"] != writer_body["request_id"]
                    or grant_body["writer_digest"] != B.digest(frame["writer"])
                    or grant_body["expected"] != writer_body["expected"]
                    or grant_body["operator_roots"] != self._pins.commitment
                    or grant_body["source_roots"] != self._sources.root_commitment()
                    or grant_body["witness_root"] != self._witness._root_commitment):
                fail("REMOTE_OPERATOR_GRANT_MISMATCH")
            B.check_time(grant_body, self._clock())
            packet_digest = B.digest(frame)
            if writer_body["request_id"] in self._completed:
                old_digest, snapshot = self._completed[writer_body["request_id"]]
                if packet_digest != old_digest:
                    fail("REMOTE_REQUEST_ID_CONFLICT")
                return encode(self._checkpoint(frame, purpose="APPEND", snapshot=snapshot))
            expected = parse_head(writer_body["expected"])
            if expected != self._head:
                fail("REMOTE_CAS_CONFLICT")
            ev = writer_body["evidence"]
            if not fields(ev, EVIDENCE_FIELDS) or not fields(ev["context"], CONTEXT_FIELDS):
                fail("REMOTE_EVIDENCE_SCHEMA")
            proposal = W.verify_witnessed_pair(
                self._sources, self._witness,
                ev["trade"], ev["read"], ev["approval"], ev["witness"],
                trusted_head=self._head, now=self._clock(), **ev["context"])
            if proposal.predecessor != self._head or proposal.successor.sequence != self._head.sequence + 1:
                fail("REMOTE_CAS_CONFLICT")
            legacy = (proposal.venue, proposal.scope, proposal.legacy_ref)
            reader = (proposal.venue, proposal.scope, proposal.read_ref)
            if proposal.challenge in self._used_challenges:
                fail("REMOTE_CHALLENGE_REPLAY")
            if legacy in self._by_legacy or reader in self._by_read:
                fail("REMOTE_KEY_REBIND")
            if self.fail_before_commit_once:
                self.fail_before_commit_once = False
                raise MockLinkLost("remote simulator lost before atomic commit")
            entry = {"sequence": proposal.successor.sequence,
                     "previous": proposal.predecessor.chain_hash,
                     "chain_hash": proposal.successor.chain_hash,
                     "binding_digest": proposal.binding_digest,
                     "venue": proposal.venue, "scope": proposal.scope,
                     "legacy_ref": proposal.legacy_ref,
                     "read_ref": proposal.read_ref,
                     "challenge": proposal.challenge,
                     "request_digest": packet_digest}
            new_records = self._records + (entry,)
            new_head = proposal.successor
            self._records = new_records
            self._head = new_head
            self._by_legacy.add(legacy)
            self._by_read.add(reader)
            self._used_challenges.add(proposal.challenge)
            self._completed[writer_body["request_id"]] = (
                packet_digest, (new_head, new_records))
            return encode(self._checkpoint(frame, purpose="APPEND"))


class MockLinkLost(ConnectionError):
    pass


class MockNetworkLink:
    """Untrusted byte transport; never opens a socket. Fault injection permitted."""
    def __init__(self, server):
        self.server = server
        self.drop_before_once = False
        self.drop_after_once = False
        self.request_transform = None
        self.response_transform = None

    def send(self, raw):
        if self.drop_before_once:
            self.drop_before_once = False
            raise MockLinkLost("mock request lost")
        send_bytes = self.request_transform(raw) if self.request_transform else raw
        received = self.server.dispatch(send_bytes)
        if self.drop_after_once:
            self.drop_after_once = False
            raise MockLinkLost("mock response lost after server processing")
        return self.response_transform(received) if self.response_transform else received


class MockRemoteClient:
    """Caller has ONLY writer or auditor private key and external checkpoint pin."""
    def __init__(self, *, role, private_key, pins, link, anchor):
        if role not in ("writer", "auditor") or pubkey(private_key) != pins.public_bytes(role):
            fail("REMOTE_CLIENT_ROLE_FORBIDDEN")
        if anchor.pins is not pins:
            fail("REMOTE_CLIENT_ANCHOR_MISMATCH")
        self.role = role
        self._private = private_key
        self._pins = pins
        self._link = link
        self.anchor = anchor
        self._acknowledged = set()

    def query(self, nonce):
        if not hash_ok(nonce):
            fail("REMOTE_QUERY_INVALID")
        frame = {"op": "HEAD", "actor": self.role,
                 "query": make_envelope(self._private,
                     kid=self._pins.kid(self.role), kind="CHECKPOINT_QUERY",
                     body={"nonce": nonce})}
        return self._send_and_verify(frame)

    def make_append_frame(self, request_id, evidence, expected):
        if self.role != "writer":
            fail("REMOTE_CLIENT_ROLE_FORBIDDEN")
        if not hash_ok(request_id) or not isinstance(expected, B.TrustedHead):
            fail("REMOTE_APPEND_SCHEMA")
        body = {"request_id": request_id, "expected": head_dict(expected),
                "evidence": clean(evidence)}
        return {"op": "APPEND",
                "writer": make_envelope(self._private,
                      kid=self._pins.kid("writer"), kind="APPEND_PROOF", body=body)}

    def append(self, frame):
        if self.role != "writer":
            fail("REMOTE_CLIENT_ROLE_FORBIDDEN")
        if not fields(frame, {"op", "writer", "grant"}):
            fail("REMOTE_APPEND_SCHEMA")
        expected = parse_head(frame["writer"]["body"]["expected"])
        packet_digest = B.digest(frame)
        if self.anchor.floor != expected and packet_digest not in self._acknowledged:
            fail("REMOTE_CLIENT_STALE_ANCHOR")
        result = self._send_and_verify(frame)
        self._acknowledged.add(packet_digest)
        return result

    def _send_and_verify(self, frame):
        raw_response = self._link.send(encode(frame))
        response = strict_decode(raw_response)
        head = self.anchor.verify_and_pin(response, frame)
        if (frame["op"] == "APPEND"
                and response["checkpoint"]["body"]["purpose"] != "APPEND"):
            fail("REMOTE_RESPONSE_PURPOSE")
        if (frame["op"] == "HEAD"
                and response["checkpoint"]["body"]["purpose"] != "HEAD"):
            fail("REMOTE_RESPONSE_PURPOSE")
        return head


def operator_grant(private, pins, sources, witness, frame, *,
                   issued_at, expires_at):
    """Executed by the independently controlled MOCK operator, not writer."""
    if not fields(frame, {"op", "writer"}) or frame["op"] != "APPEND":
        fail("REMOTE_APPEND_SCHEMA")
    body = frame["writer"]["body"]
    grant_body = {"request_id": body["request_id"],
                  "writer_digest": B.digest(frame["writer"]),
                  "expected": clean(body["expected"]),
                  "operator_roots": pins.commitment,
                  "source_roots": sources.root_commitment(),
                  "witness_root": witness._root_commitment,
                  "issued_at": issued_at, "expires_at": expires_at}
    return make_envelope(private, kid=pins.kid("operator"),
                         kind="APPEND_GRANT", body=grant_body)
