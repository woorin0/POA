"""PR20 S3 Object Lock COMPLIANCE API contract; injection-only, no live S3.

A boto3-compatible client may be passed explicitly by an authorized caller,
but this audit-only module never instantiates a cloud client, credentials,
socket, or real service. CI supplies fake objects implementing the public S3
API operations. A signed checkpoint is NOT proof of a persisted object.
The independent auditor must fetch the EXACT immutable VersionId, rehash the
bytes and compare COMPLIANCE retention before signing the commit attestation.

This is a *version-pinned object receipt protocol*, NOT a global CAS/WORM
implementation. Uncoordinated prefixes / independent auditors may still fork.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import immutable_binding as B
from . import remote_worm_mock as R
from . import independent_witness as W

DOMAIN = "POA_S3_OBJECT_LOCK_COMMIT_V1"
ATTESTATION_DOMAIN = "POA_INDEPENDENT_S3_COMMIT_ATTESTATION_V1"
MAX_OBJECT_BYTES = 16384  # upstream PR19 canonical JSON frames are 16 KiB max
LOCATOR_FIELDS = {"domain", "owner", "bucket", "key", "version_id",
                  "object_sha256", "object_length", "retained_until",
                  "sequence", "chain_hash", "request_digest"}
PROOF_FIELDS = {"domain", "attestor_kid", "attestation", "signature"}


def deny(code):
    B.deny(code)


def ishex(s):
    return type(s) is str and re.fullmatch(r"[0-9a-f]{64}", s) is not None


def utc(dt):
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        deny("S3_LOCK_DATE_INVALID")
    return dt.astimezone(timezone.utc)


def iso(dt):
    return utc(dt).isoformat().replace("+00:00", "Z")


def parse_iso(raw):
    if type(raw) is not str or not raw.endswith("Z"):
        deny("S3_LOCK_DATE_INVALID")
    try:
        dt = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError:
        deny("S3_LOCK_DATE_INVALID")
    if iso(dt) != raw:
        deny("S3_LOCK_DATE_NONCANONICAL")
    return dt


def equal(a, b):
    return type(a) is str and type(b) is str and hmac.compare_digest(a, b)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _source_contract(pins, sources, witness, frame, response, now_unix):
    """Independently rederive signed PR19 source and exact included append."""
    if not R.fields(frame, {"op", "writer", "grant"}) or frame["op"] != "APPEND":
        deny("S3_SOURCE_NOT_APPEND")
    writer = pins.verify("writer", frame["writer"], "APPEND_PROOF")
    grant = pins.verify("operator", frame["grant"], "APPEND_GRANT")
    if not R.fields(writer, R.APPEND_FIELDS) or not R.fields(grant, R.GRANT_FIELDS):
        deny("S3_SOURCE_SCHEMA")
    if (grant["request_id"] != writer["request_id"]
            or grant["writer_digest"] != B.digest(frame["writer"])
            or grant["expected"] != writer["expected"]
            or grant["operator_roots"] != pins.commitment
            or grant["source_roots"] != sources.root_commitment()
            or grant["witness_root"] != witness._root_commitment):
        deny("S3_SOURCE_GRANT_MISMATCH")
    B.check_time(grant, now_unix)
    if not R.fields(response, {"checkpoint", "entries"}):
        deny("S3_SOURCE_CHECKPOINT_INVALID")
    cp = pins.verify("checkpoint", response["checkpoint"], "SIGNED_CHECKPOINT")
    if not R.fields(cp, R.CP_FIELDS) or cp["purpose"] != "APPEND":
        deny("S3_SOURCE_CHECKPOINT_INVALID")
    if (cp["operator_roots"] != pins.commitment
            or cp["source_roots"] != sources.root_commitment()
            or cp["witness_root"] != witness._root_commitment
            or cp["request_digest"] != B.digest(frame)):
        deny("S3_SOURCE_ROOT_OR_REQUEST_MISMATCH")
    head = R.parse_head({"sequence": cp["sequence"], "chain_hash": cp["chain_hash"]})
    expected = R.parse_head(writer["expected"])
    entries = response["entries"]
    if type(entries) is not list or not entries or len(entries) != head.sequence:
        deny("S3_SOURCE_ENTRIES_INVALID")
    if B.digest(entries) != cp["entries_digest"]:
        deny("S3_SOURCE_ENTRIES_INVALID")
    prev = B.ZERO
    for i, entry in enumerate(entries, 1):
        if (not R.fields(entry, R.ENTRY_FIELDS)
                or entry["sequence"] != i or type(entry["sequence"]) is not int
                or entry["previous"] != prev):
            deny("S3_SOURCE_ENTRIES_INVALID")
        expected_hash = B.digest({
            "domain": B.DOMAIN, "sequence": i, "previous": prev,
            "approval_digest": entry["binding_digest"],
            "trade_observation_digest": entry["trade_observation_digest"],
            "read_observation_digest": entry["read_observation_digest"]})
        if entry["chain_hash"] != expected_hash:
            deny("S3_SOURCE_CHAIN_FORGED")
        prev = entry["chain_hash"]
    if prev != head.chain_hash:
        deny("S3_SOURCE_CHAIN_FORGED")
    matches = [e for e in entries if e["request_digest"] == B.digest(frame)]
    if len(matches) != 1:
        deny("S3_SOURCE_RECEIPT_NOT_INCLUDED")
    entry = matches[0]
    ev = writer["evidence"]
    if not R.fields(ev, R.EVIDENCE_FIELDS) or not R.fields(ev["context"], R.CONTEXT_FIELDS):
        deny("S3_SOURCE_EVIDENCE_INVALID")
    ctx = ev["context"]
    proposal = W.verify_witnessed_pair(
        sources, witness, ev["trade"], ev["read"], ev["approval"], ev["witness"],
        trusted_head=expected, now=now_unix, **ctx)
    if (entry["sequence"] != expected.sequence + 1
            or entry["previous"] != expected.chain_hash
            or entry["chain_hash"] != proposal.successor.chain_hash
            or entry["binding_digest"] != proposal.binding_digest
            or entry["trade_observation_digest"] != B.digest(ev["trade"])
            or entry["read_observation_digest"] != B.digest(ev["read"])
            or entry["venue"] != proposal.venue
            or entry["scope"] != proposal.scope
            or entry["legacy_ref"] != proposal.legacy_ref
            or entry["read_ref"] != proposal.read_ref
            or entry["challenge"] != proposal.challenge):
        deny("S3_SOURCE_RECEIPT_MISMATCH")
    return {"sequence": head.sequence, "chain_hash": head.chain_hash,
            "request_digest": B.digest(frame)}


class ObjectLockPolicy:
    """Out-of-band operator-controlled bucket/owner/root and UTC retention.

    Fail closed on ordinary in-process pin replacement after validation.
    Interpreter compromise still defeats this Python-level boundary.
    """
    __slots__ = ("owner", "bucket", "prefix", "min_days", "clock")

    def __setattr__(self, name, value):
        if name in self.__slots__ and hasattr(self, name):
            deny("S3_POLICY_PIN_IMMUTABLE")
        object.__setattr__(self, name, value)

    def __init__(self, *, owner, bucket, prefix, min_days, clock):
        if (type(owner) is not str or re.fullmatch(r"[0-9]{12}", owner) is None
                or type(bucket) is not str
                or re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,61}[a-z0-9]", bucket) is None
                or type(prefix) is not str
                or re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", prefix) is None
                or type(min_days) is not int or not (1 <= min_days <= 36500)
                or not callable(clock)):
            deny("S3_POLICY_INVALID")
        self.owner = owner
        self.bucket = bucket
        self.prefix = prefix
        self.min_days = min_days
        self.clock = clock

    def now(self):
        return utc(self.clock())

    def params(self):
        return {"Bucket": self.bucket, "ExpectedBucketOwner": self.owner}

    def validate_bucket(self, s3):
        try:
            version = s3.get_bucket_versioning(**self.params())
            config = s3.get_object_lock_configuration(**self.params())
        except Exception:
            deny("S3_BUCKET_CONFIG_UNVERIFIED")
        if (type(version) is not dict or version.get("Status") != "Enabled"
                or type(config) is not dict
                or type(config.get("ObjectLockConfiguration")) is not dict
                or config["ObjectLockConfiguration"].get("ObjectLockEnabled") != "Enabled"):
            deny("S3_VERSIONING_OR_OBJECT_LOCK_REQUIRED")


def _canonical_document(pins, sources, witness, frame, response, now_unix):
    source = _source_contract(pins, sources, witness, frame, response, now_unix)
    doc = {"domain": DOMAIN, "frame": frame, "response": response}
    raw = R.encode(doc)
    if len(raw) > MAX_OBJECT_BYTES:
        deny("S3_DOCUMENT_TOO_LARGE")
    return raw, source


def _version(value):
    if (type(value) is not str or not (1 <= len(value) <= 256)
            or value.lower() == "null" or re.fullmatch(r"[A-Za-z0-9._-]+", value) is None):
        deny("S3_VERSION_ID_MISSING")
    return value


def _locator(policy, raw, version_id, until, source):
    d = sha(raw)
    return {"domain": DOMAIN, "owner": policy.owner, "bucket": policy.bucket,
            "key": f"{policy.prefix}/{d}.json", "version_id": _version(version_id),
            "object_sha256": d, "object_length": len(raw),
            "retained_until": iso(until),
            **source}


class S3ComplianceWriter:
    """WRITE only. Injected boto3-shaped API, no SDK/client auto-initialization.

    This returns an UNATTESTED locator, never a claimed durable commit.
    Only an independent read-only S3 client+attestor may upgrade it to proof.
    """
    def __init__(self, client, policy, pins, sources, witness, now_unix):
        self._s3 = client
        self.policy = policy
        self.pins = pins
        self.sources = sources
        self.witness = witness
        self.now_unix = now_unix

    def put(self, frame, response):
        raw, source = _canonical_document(
            self.pins, self.sources, self.witness, frame, response, self.now_unix())
        policy = self.policy
        policy.validate_bucket(self._s3)
        until = policy.now() + timedelta(days=policy.min_days, seconds=60)
        digest = hashlib.sha256(raw).digest()
        key = f"{policy.prefix}/{digest.hex()}.json"
        try:
            result = self._s3.put_object(
                **policy.params(), Key=key, Body=raw,
                ContentType="application/json",
                Metadata={"poa-sha256": digest.hex(), "poa-schema": DOMAIN},
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=base64.b64encode(digest).decode("ascii"),
                IfNoneMatch="*",
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=until)
        except Exception:
            # Lost post-commit ACK = AMBIGUOUS. NEVER claim success or write
            # another version blindly; recover by independently pinned VersionId.
            deny("S3_PUT_UNCONFIRMED")
        if type(result) is not dict:
            deny("S3_PUT_RESPONSE_INVALID")
        return _locator(policy, raw, result.get("VersionId"), until, source)


def _read_version(s3, policy, locator):
    if not R.fields(locator, LOCATOR_FIELDS) or locator["domain"] != DOMAIN:
        deny("S3_LOCATOR_SCHEMA_INVALID")
    if (locator["owner"] != policy.owner or locator["bucket"] != policy.bucket
            or not ishex(locator["object_sha256"])
            or locator["key"] != f"{policy.prefix}/{locator['object_sha256']}.json"
            or not isinstance(locator["object_length"], int)
            or isinstance(locator["object_length"], bool)
            or locator["object_length"] <= 0
            or locator["object_length"] > MAX_OBJECT_BYTES
            or not ishex(locator["request_digest"])
            or not ishex(locator["chain_hash"])
            or type(locator["sequence"]) is not int
            or locator["sequence"] < 1):
        deny("S3_LOCATOR_PIN_MISMATCH")
    version_id = _version(locator["version_id"])
    expect_until = parse_iso(locator["retained_until"])
    policy.validate_bucket(s3)
    args = {**policy.params(), "Key": locator["key"], "VersionId": version_id}
    try:
        head = s3.head_object(**args)
        retention = s3.get_object_retention(**args)
        got = s3.get_object(**args, ChecksumMode="ENABLED")
    except Exception:
        deny("S3_VERSION_READ_UNVERIFIED")
    if (type(head) is not dict or type(retention) is not dict
            or type(got) is not dict or type(retention.get("Retention")) is not dict):
        deny("S3_VERSION_METADATA_INVALID")
    lock = retention["Retention"]
    try:
        until_head = utc(head["ObjectLockRetainUntilDate"])
        until_retention = utc(lock["RetainUntilDate"])
    except (KeyError, TypeError):
        deny("S3_COMPLIANCE_RETENTION_NOT_VERIFIED")
    if (head.get("ObjectLockMode") != "COMPLIANCE"
            or lock.get("Mode") != "COMPLIANCE"
            or until_head != until_retention
            or until_head < expect_until
            or until_head < policy.now() + timedelta(days=policy.min_days)
            or head.get("VersionId") != version_id
            or got.get("VersionId") != version_id
            or head.get("ContentLength") != locator["object_length"]
            or head.get("Metadata", {}).get("poa-sha256") != locator["object_sha256"]
            or head.get("Metadata", {}).get("poa-schema") != DOMAIN):
        deny("S3_COMPLIANCE_VERSION_PIN_FAILED")
    stream = got.get("Body")
    try:
        if stream is None or not callable(stream.read):
            deny("S3_OBJECT_BODY_UNVERIFIED")
        # Bounded read; don't accept huge malicious stub responses.
        raw = stream.read(MAX_OBJECT_BYTES + 1)
    except Exception:
        deny("S3_OBJECT_BODY_UNVERIFIED")
    if (type(raw) is not bytes or len(raw) != locator["object_length"]
            or not equal(sha(raw), locator["object_sha256"])):
        deny("S3_OBJECT_HASH_MISMATCH")
    checksum = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")
    if got.get("ChecksumSHA256") != checksum:
        deny("S3_SDK_CHECKSUM_UNVERIFIED")
    document = R.strict_decode(raw)
    if not R.fields(document, {"domain", "frame", "response"}) or document["domain"] != DOMAIN:
        deny("S3_IMMUTABLE_DOCUMENT_INVALID")
    return document


class IndependentObjectCommitAttestor:
    """Separate READ client + independently pinned signing principal.

    Calls ONLY version-pinned GET, HEAD, retention and bucket configuration.
    Never accepts writer's S3 version metadata as evidence by itself.
    """
    def __init__(self, *, reader, policy, pins, sources, witness,
                 private_key, kid, public_key_pin, now_unix):
        key = R.pubkey(private_key)
        if (type(kid) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,40}", kid) is None
                or type(public_key_pin) is not str
                or not equal(sha(key), public_key_pin)):
            deny("S3_ATTESTOR_ROOT_UNPINNED")
        forbidden = [pins.public_bytes(role) for role in R.ROLES]
        forbidden.extend(sources.role_public_key(role) for role in ("observer", "approver"))
        forbidden.append(witness._key.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        if key in forbidden:
            deny("S3_ATTESTOR_NOT_INDEPENDENT")
        self._reader = reader
        self._private = private_key
        self._policy = policy
        self._pins = pins
        self._sources = sources
        self._witness = witness
        self._now_unix = now_unix
        self.kid = kid

    def attest(self, locator):
        document = _read_version(self._reader, self._policy, locator)
        source = _source_contract(
            self._pins, self._sources, self._witness,
            document["frame"], document["response"], self._now_unix())
        if any(source[k] != locator[k] for k in source):
            deny("S3_COMMIT_SOURCE_MISMATCH")
        body = {"domain": ATTESTATION_DOMAIN, "locator": locator,
                "operator_roots": self._pins.commitment,
                "source_roots": self._sources.root_commitment(),
                "witness_root": self._witness._root_commitment}
        signed = {"domain": ATTESTATION_DOMAIN,
                  "attestor_kid": self.kid, "attestation": body}
        signed["signature"] = base64.b64encode(
            self._private.sign(R.encode(signed))).decode("ascii")
        return signed


class IndependentObjectProofVerifier:
    """READ only. Rechecks the exact object VersionId on every verification."""
    def __init__(self, *, reader, policy, pins, sources, witness,
                 attestor_public_key, attestor_kid, pin_sha256, now_unix):
        if (type(attestor_public_key) is not bytes
                or len(attestor_public_key) != 32
                or not equal(sha(attestor_public_key), pin_sha256)
                or type(attestor_kid) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{1,40}", attestor_kid) is None):
            deny("S3_ATTESTOR_ROOT_UNPINNED")
        forbidden = [pins.public_bytes(r) for r in R.ROLES]
        forbidden.extend(sources.role_public_key(r) for r in ("observer", "approver"))
        forbidden.append(witness._key.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        if attestor_public_key in forbidden:
            deny("S3_ATTESTOR_NOT_INDEPENDENT")
        self._key = Ed25519PublicKey.from_public_bytes(attestor_public_key)
        self._kid = attestor_kid
        self.reader, self.policy = reader, policy
        self.pins, self.sources, self.witness = pins, sources, witness
        self.now_unix = now_unix

    def verify(self, proof):
        if not R.fields(proof, PROOF_FIELDS) or (
                proof["domain"] != ATTESTATION_DOMAIN
                or proof["attestor_kid"] != self._kid):
            deny("S3_PROOF_SCHEMA_INVALID")
        raw_sig = proof["signature"]
        if type(raw_sig) is not str or len(raw_sig) != 88:
            deny("S3_PROOF_SIGNATURE_INVALID")
        unsigned = {k: proof[k] for k in ("domain", "attestor_kid", "attestation")}
        try:
            decoded = base64.b64decode(raw_sig, validate=True)
            if (len(decoded) != 64
                    or base64.b64encode(decoded).decode("ascii") != raw_sig):
                deny("S3_PROOF_SIGNATURE_INVALID")
            self._key.verify(decoded, R.encode(unsigned))
        except (InvalidSignature, ValueError, TypeError):
            deny("S3_PROOF_SIGNATURE_INVALID")
        body = proof["attestation"]
        if (not R.fields(body, {"domain", "locator", "operator_roots",
                                "source_roots", "witness_root"})
                or body["domain"] != ATTESTATION_DOMAIN
                or body["operator_roots"] != self.pins.commitment
                or body["source_roots"] != self.sources.root_commitment()
                or body["witness_root"] != self.witness._root_commitment):
            deny("S3_PROOF_ROOT_OR_DOMAIN_INVALID")
        loc = body["locator"]
        document = _read_version(self.reader, self.policy, loc)
        source = _source_contract(
            self.pins, self.sources, self.witness,
            document["frame"], document["response"], self.now_unix())
        if any(source[k] != loc[k] for k in source):
            deny("S3_PROOF_SOURCE_MISMATCH")
        return loc.copy()
