"""PR20 -> S3 Object Lock COMPLIANCE stub-network adversarial E2E.

No boto3 client is created, no credentials, sockets, cloud endpoints, live orders
or operating DB. All S3 public API operations are strictly in-memory stubs.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import threading
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit_artifacts.readonly_credentials import immutable_binding as B
from audit_artifacts.readonly_credentials import remote_worm_mock as R
from audit_artifacts.readonly_credentials import s3_object_lock_commit as S
from audit_artifacts.readonly_credentials.test_immutable_binding import rig, NOW, h
from audit_artifacts.readonly_credentials.test_independent_witness import scene
from audit_artifacts.readonly_credentials.test_remote_worm_mock import net, request

FREEZE = datetime.fromtimestamp(NOW, tz=timezone.utc)
OWNER = "111122223333"
BUCKET = "poa-independent-immutable-worm-test"
PREFIX = "source-evidence"


class StubS3Backend:
    def __init__(self):
        self.lock = threading.RLock()
        self.versions = {}
        self.versioning = "Enabled"
        self.lock_enabled = "Enabled"
        self.next_id = 0
        self.drop_after_put = False
        self.fake_missing_version = False
        self.force_mode = None
        self.force_retention = None
        self.force_last_modified = None
        self.corrupt_body = False
        self.corrupt_checksum = False
        self.corrupt_metadata = False
        self.wrong_head_version = False
        self.wrong_get_version = False
        self.split_head_retention = False
        self.fail_reads = False
        self.network_calls = 0


class StubS3Client:
    """Strict role-limited boto3 signature with no external I/O."""
    def __init__(self, data, *, role):
        self.data, self.role, self.calls = data, role, []

    def _owner(self, kwargs):
        assert kwargs["Bucket"] == BUCKET
        assert kwargs["ExpectedBucketOwner"] == OWNER
        self.calls.append(copy.deepcopy({k: v for k,v in kwargs.items() if k != "Body"}))

    def get_bucket_versioning(self, **kwargs):
        self._owner(kwargs)
        return {"Status": self.data.versioning}

    def get_object_lock_configuration(self, **kwargs):
        self._owner(kwargs)
        return {"ObjectLockConfiguration":{"ObjectLockEnabled":self.data.lock_enabled}}

    def put_object(self, **kwargs):
        if self.role != "writer":
            raise PermissionError("reader may not PUT")
        self._owner(kwargs)
        if (kwargs["ObjectLockMode"] != "COMPLIANCE"
                or kwargs["IfNoneMatch"] != "*"
                or kwargs["ChecksumAlgorithm"] != "SHA256"
                or kwargs["ContentType"] != "application/json"):
            raise AssertionError("S3 write request is not compliance-locked")
        data = kwargs["Body"]
        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
        if kwargs["ChecksumSHA256"] != checksum:
            raise ValueError("BadDigest")
        with self.data.lock:
            key = kwargs["Key"]
            if key in self.data.versions and self.data.versions[key][-1] is not None:
                raise RuntimeError("PreconditionFailed")
            self.data.next_id += 1
            vid = f"v-{self.data.next_id:06d}"
            rec = {
                "Body": data, "VersionId": vid,
                "ObjectLockMode": kwargs["ObjectLockMode"],
                "ObjectLockRetainUntilDate": kwargs["ObjectLockRetainUntilDate"],
                "LastModified":FREEZE,
                "Metadata": dict(kwargs["Metadata"]), "ChecksumSHA256":checksum}
            self.data.versions.setdefault(key, []).append(rec)
            if self.data.drop_after_put:
                self.data.drop_after_put = False
                raise ConnectionError("post-PUT ACK lost")
            return {"VersionId":None if self.data.fake_missing_version else vid,
                    "ETag":'"not-a-sha256-proof"'}

    def _record(self, kwargs):
        if self.role not in ("reader", "writer"):
            raise PermissionError("unknown role")
        if self.role == "writer":
            raise PermissionError("writer cannot independently audit stored bytes")
        self._owner(kwargs)
        if self.data.fail_reads:
            raise OSError("read unavailable")
        assert "VersionId" in kwargs, "never consult latest key"
        key, vid = kwargs["Key"],kwargs["VersionId"]
        with self.data.lock:
            matching = [r for r in self.data.versions.get(key, [])
                        if r is not None and r["VersionId"] == vid]
            if not matching:
                raise FileNotFoundError(vid)
            return matching[0]

    def head_object(self, **kwargs):
        r = self._record(kwargs)
        return {"VersionId":"v-fake" if self.data.wrong_head_version else r["VersionId"],
                "ObjectLockMode":self.data.force_mode or r["ObjectLockMode"],
                "ObjectLockRetainUntilDate":self.data.force_retention or r["ObjectLockRetainUntilDate"],
                "LastModified":self.data.force_last_modified or r["LastModified"],
                "ContentLength":len(r["Body"]),
                "Metadata":({} if self.data.corrupt_metadata else dict(r["Metadata"]))}

    def get_object_retention(self, **kwargs):
        r = self._record(kwargs)
        date = r["ObjectLockRetainUntilDate"] - timedelta(days=1) if self.data.split_head_retention else r["ObjectLockRetainUntilDate"]
        return {"Retention":{"Mode":self.data.force_mode or r["ObjectLockMode"],
                             "RetainUntilDate":date}}

    def get_object(self, **kwargs):
        if kwargs.get("ChecksumMode") != "ENABLED":
            raise AssertionError("full checksum mode mandatory")
        r = self._record(kwargs)
        raw = r["Body"] + b"x" if self.data.corrupt_body else r["Body"]
        return {"VersionId":"v-other" if self.data.wrong_get_version else r["VersionId"],
                "Body":io.BytesIO(raw),
                "ChecksumSHA256":"broken" if self.data.corrupt_checksum else r["ChecksumSHA256"]}

    def put_object_retention(self, **_):
        raise PermissionError("cannot weaken retention")

    def delete_object(self, **kwargs):
        """S3 version-delete forbidden under retention; non-version marker possible."""
        self._owner(kwargs)
        if "VersionId" in kwargs:
            raise PermissionError("AccessDenied: COMPLIANCE retention")
        self.data.versions.setdefault(kwargs["Key"], []).append(None)
        return {"DeleteMarker":True}


@pytest.fixture
def wire(net):
    data=StubS3Backend()
    writer=StubS3Client(data,role="writer")
    reader=StubS3Client(data,role="reader")
    policy=S.ObjectLockPolicy(owner=OWNER,bucket=BUCKET,prefix=PREFIX,
                              min_days=30,clock=lambda:FREEZE)
    keys=net["keys"]
    priv=Ed25519PrivateKey.generate()
    attestor_pin=S.sha(R.pubkey(priv))
    args={
        "policy":policy,"pins":net["pins"],
        "sources":net["scene"]["rig"]["authorities"],
        "witness":net["scene"]["witness"],
        "now_unix":lambda:NOW}
    producer=S.S3ComplianceWriter(client=writer,**args)
    verifier=S.IndependentObjectProofVerifier(
        reader=reader,attestor_public_key=R.pubkey(priv),
        attestor_kid="separately-pinned-auditor",
        pin_sha256=attestor_pin,**args)
    attestor=S.IndependentObjectCommitAttestor(
        reader=reader,private_key=priv,kid="separately-pinned-auditor",
        public_key_pin=attestor_pin,**args)
    frame=request(net)
    assert net["writer"].append(frame).sequence==1
    response=R.strict_decode(net["server"].dispatch(R.encode(frame)))
    return {"backend":data,"writer_client":writer,"reader_client":reader,
            "policy":policy,"producer":producer,"attestor":attestor,
            "verifier":verifier,"signer":priv,"frame":frame,
            "response":response,"net":net}


def fails(code, func, *args, **kwargs):
    with pytest.raises(B.BindingBlocked) as err:
        func(*args,**kwargs)
    assert err.value.code==code


def committed(wire):
    locator=wire["producer"].put(wire["frame"],wire["response"])
    proof=wire["attestor"].attest(locator)
    return locator,proof


def mutate_version(wire, locator, **changes):
    rows=wire["backend"].versions[locator["key"]]
    target=next(x for x in rows if x and x["VersionId"]==locator["version_id"])
    target.update(changes)


def re_sign(wire, proof, changes):
    p=copy.deepcopy(proof)
    p["attestation"].update(changes)
    unsigned={k:p[k] for k in ("domain","attestor_kid","attestation")}
    p["signature"]=base64.b64encode(wire["signer"].sign(R.encode(unsigned))).decode()
    return p


def test_full_signed_object_lock_commit_and_separate_auditor_stub_e2e(wire):
    locator, proof=committed(wire)
    assert wire["verifier"].verify(proof)==locator
    assert locator["version_id"].startswith("v-")
    assert locator["sequence"]==1 and locator["chain_hash"]==wire["net"]["server"].head.chain_hash
    assert wire["writer_client"] is not wire["reader_client"]
    assert wire["backend"].network_calls==0
    assert wire["reader_client"].calls
    assert not any("Body" in c for c in wire["writer_client"].calls)
    assert not any("IfNoneMatch" in c for c in wire["reader_client"].calls)


def test_only_writer_can_create_retained_object_and_reader_cannot_put(wire):
    locator,_=committed(wire)
    assert len(wire["backend"].versions[locator["key"]])==1
    with pytest.raises(PermissionError):
        wire["reader_client"].put_object(Bucket=BUCKET)
    with pytest.raises(PermissionError):
        wire["writer_client"].get_object(Bucket=BUCKET,Key=locator["key"],
                                         VersionId=locator["version_id"],
                                         ExpectedBucketOwner=OWNER,ChecksumMode="ENABLED")


@pytest.mark.parametrize("param,value,expected",[
    ("versioning","Suspended","S3_VERSIONING_OR_OBJECT_LOCK_REQUIRED"),
    ("lock_enabled","Disabled","S3_VERSIONING_OR_OBJECT_LOCK_REQUIRED"),
])
def test_bucket_without_versioning_or_lock_blocks_all_writes(wire,param,value,expected):
    setattr(wire["backend"],param,value)
    fails(expected,wire["producer"].put,wire["frame"],wire["response"])
    assert not wire["backend"].versions


def test_compliance_mode_retention_checksum_and_precondition_required_on_put(wire):
    locator,_=committed(wire)
    c=next(x for x in wire["writer_client"].calls if "IfNoneMatch" in x)
    assert c["ObjectLockMode"]=="COMPLIANCE"
    assert c["ChecksumAlgorithm"]=="SHA256"
    assert c["IfNoneMatch"]=="*"
    assert c["ObjectLockRetainUntilDate"]>=FREEZE+timedelta(days=30)
    assert c["Key"]==locator["key"]


def test_second_same_key_fails_closed_not_false_receipt(wire):
    committed(wire)
    fails("S3_PUT_UNCONFIRMED",wire["producer"].put,wire["frame"],wire["response"])
    assert sum(len(v) for v in wire["backend"].versions.values())==1


def test_post_put_ack_loss_is_ambiguous_never_claimed_committed(wire):
    wire["backend"].drop_after_put=True
    fails("S3_PUT_UNCONFIRMED",wire["producer"].put,wire["frame"],wire["response"])
    assert sum(len(v) for v in wire["backend"].versions.values())==1
    fails("S3_PUT_UNCONFIRMED",wire["producer"].put,wire["frame"],wire["response"])


def test_missing_or_null_s3_version_id_cannot_be_used_as_proof(wire):
    wire["backend"].fake_missing_version=True
    fails("S3_VERSION_ID_MISSING",wire["producer"].put,wire["frame"],wire["response"])


@pytest.mark.parametrize("field,new", [
    ("owner","999988887777"),("bucket","some-other-account"),
    ("key","unsafe/non-pinned-key.json"),("object_sha256","0"*64),
    ("version_id","v-forged"),("request_digest","0"*64),
    ("chain_hash","1"*64),("sequence",3),
    ("object_length",9)])
def test_exact_owner_bucket_key_version_digest_and_source_pins_required(wire,field,new):
    locator,_=committed(wire)
    wrong={**locator,field:new}
    with pytest.raises(B.BindingBlocked):
        wire["attestor"].attest(wrong)


@pytest.mark.parametrize("attack,code", [
    ("force_mode","S3_COMPLIANCE_VERSION_PIN_FAILED"),
    ("force_retention","S3_COMPLIANCE_VERSION_PIN_FAILED"),
    ("split_head_retention","S3_COMPLIANCE_VERSION_PIN_FAILED"),
    ("wrong_head_version","S3_COMPLIANCE_VERSION_PIN_FAILED"),
    ("wrong_get_version","S3_COMPLIANCE_VERSION_PIN_FAILED"),
    ("corrupt_metadata","S3_COMPLIANCE_VERSION_PIN_FAILED"),
    ("corrupt_body","S3_OBJECT_HASH_MISMATCH"),
    ("corrupt_checksum","S3_SDK_CHECKSUM_UNVERIFIED"),
])
def test_s3_metadata_version_retention_payload_and_checksum_tamper(wire,attack,code):
    locator,_=committed(wire)
    if attack=="force_mode":
        wire["backend"].force_mode="GOVERNANCE"
    elif attack=="force_retention":
        wire["backend"].force_retention=FREEZE+timedelta(days=1)
    else:
        setattr(wire["backend"],attack,True)
    fails(code,wire["attestor"].attest,locator)


def test_exact_locked_old_version_remains_verifiable_after_delete_marker(wire):
    locator,proof=committed(wire)
    wire["writer_client"].delete_object(Bucket=BUCKET,Key=locator["key"],
                                        ExpectedBucketOwner=OWNER)
    # Latest key now resolves to delete marker: version-pinned GET still works.
    assert wire["backend"].versions[locator["key"]][-1] is None
    assert wire["verifier"].verify(proof)==locator


def test_compliance_protected_version_cannot_be_deleted_by_stub(wire):
    locator,proof=committed(wire)
    with pytest.raises(PermissionError):
        wire["writer_client"].delete_object(Bucket=BUCKET,Key=locator["key"],
              ExpectedBucketOwner=OWNER,VersionId=locator["version_id"])
    assert wire["verifier"].verify(proof)==locator


def test_noncanonical_or_forged_attestor_signature_rejected(wire):
    _,proof=committed(wire)
    p=copy.deepcopy(proof)
    p["signature"]="A"*88
    fails("S3_PROOF_SIGNATURE_INVALID",wire["verifier"].verify,p)
    p=copy.deepcopy(proof)
    p["attestation"]["locator"]["version_id"]="v-other"
    fails("S3_PROOF_SIGNATURE_INVALID",wire["verifier"].verify,p)


def test_re_signed_proof_cannot_override_injected_expected_bucket(wire):
    _,proof=committed(wire)
    forged=re_sign(wire,proof,{"locator":{**proof["attestation"]["locator"],
                                         "bucket":"attacker-controlled-bucket"}})
    fails("S3_LOCATOR_PIN_MISMATCH",wire["verifier"].verify,forged)


def test_re_signed_proof_cannot_override_original_checkpoint_pins(wire):
    _,proof=committed(wire)
    forged=re_sign(wire,proof,{"operator_roots":"0"*64})
    fails("S3_PROOF_ROOT_OR_DOMAIN_INVALID",wire["verifier"].verify,forged)


def test_attestor_key_substitution_fails_out_of_band_pin(wire):
    other=Ed25519PrivateKey.generate()
    w=wire
    fails("S3_ATTESTOR_ROOT_UNPINNED",
        S.IndependentObjectCommitAttestor,reader=w["reader_client"],
        policy=w["policy"],pins=w["net"]["pins"],
        sources=w["net"]["scene"]["rig"]["authorities"],
        witness=w["net"]["scene"]["witness"],private_key=other,
        kid="separately-pinned-auditor",public_key_pin=S.sha(R.pubkey(w["signer"])),
        now_unix=lambda:NOW)


def test_original_checkpoint_signer_must_not_also_be_independent_object_attestor(wire):
    n=wire["net"]
    shared=n["keys"]["checkpoint"]
    fails("S3_ATTESTOR_NOT_INDEPENDENT",
        S.IndependentObjectCommitAttestor,
        reader=wire["reader_client"],policy=wire["policy"],
        pins=n["pins"],sources=n["scene"]["rig"]["authorities"],
        witness=n["scene"]["witness"],private_key=shared,kid="shared-checkpoint",
        public_key_pin=S.sha(R.pubkey(shared)),now_unix=lambda:NOW)


def test_source_checkpoint_signature_tamper_blocked_before_put(wire):
    r=copy.deepcopy(wire["response"])
    r["checkpoint"]["signature"]="A"*88
    fails("REMOTE_SIGNATURE_INVALID",wire["producer"].put,wire["frame"],r)
    assert not wire["backend"].versions


def test_source_operator_grant_tamper_blocked_before_put(wire):
    frame=copy.deepcopy(wire["frame"])
    frame["grant"]["body"]["request_id"]="f"*64
    fails("REMOTE_SIGNATURE_INVALID",wire["producer"].put,frame,wire["response"])
    assert not wire["backend"].versions


def test_source_checkpoint_claims_valid_but_append_entry_not_included(wire):
    r=copy.deepcopy(wire["response"])
    r["entries"][0]["request_digest"]="f"*64
    fails("S3_SOURCE_ENTRIES_INVALID",wire["producer"].put,wire["frame"],r)


def test_reader_unavailable_cannot_attest_even_after_successful_put(wire):
    locator=wire["producer"].put(wire["frame"],wire["response"])
    wire["backend"].fail_reads=True
    fails("S3_VERSION_READ_UNVERIFIED",wire["attestor"].attest,locator)


def test_forged_server_response_unrelated_to_actual_storage_cannot_create_proof(wire):
    locator,proof=committed(wire)
    wire["backend"].versions[locator["key"]].clear()
    fails("S3_VERSION_READ_UNVERIFIED",wire["verifier"].verify,proof)


def test_version_replacement_attack_rejects_injected_wrong_version_object(wire):
    locator,proof=committed(wire)
    mutate_version(wire,locator,Body=b"attacker-altered-object")
    fails("S3_COMPLIANCE_VERSION_PIN_FAILED",wire["verifier"].verify,proof)


def test_attestation_not_equivalent_to_external_s3_compliance_enforcement(wire):
    """Residual: a dishonest stub can answer correctly without storage.

    The proof only establishes validity conditional on the S3 provider and
    independent verifier being genuine. NO cloud/object retention test ran.
    """
    locator,proof=committed(wire)
    assert wire["verifier"].verify(proof)==locator
    assert wire["backend"].network_calls==0
    # Full storage-owner compromise (outside Compliance implementation) can
    # rewrite the stub's in-memory source of truth despite signed proof.
    wire["backend"].versions.clear()
    fails("S3_VERSION_READ_UNVERIFIED",wire["verifier"].verify,proof)


def test_no_live_network_and_no_product_operational_activation(wire):
    assert wire["backend"].network_calls==0
    assert wire["producer"].__class__ is S.S3ComplianceWriter
    assert wire["net"]["server"].records
    assert wire["net"]["wanchor"].floor.sequence==1


def test_externally_pinned_storage_policy_cannot_be_swapped_at_runtime(wire):
    for field, value in (("owner","999988887777"),("bucket","rogue-bucket"),
                         ("prefix","malicious-prefix"),("min_days",1),
                         ("clock",lambda:FREEZE)):
        fails("S3_POLICY_PIN_IMMUTABLE",setattr,wire["policy"],field,value)


def test_archived_signed_approval_stays_verifiable_during_original_retention(wire):
    loc,proof=committed(wire)
    # 10 days after the 5-minute PR19 operator approval expires, historical
    # verification uses pinned S3 LastModified, not a forged current approval.
    late_policy = S.ObjectLockPolicy(owner=OWNER,bucket=BUCKET,prefix=PREFIX,
                                     min_days=30,clock=lambda:FREEZE+timedelta(days=10))
    wire["verifier"].policy = late_policy
    assert wire["verifier"].verify(proof)==loc


def test_storage_after_operator_grant_expires_cannot_be_attested(wire):
    loc=wire["producer"].put(wire["frame"],wire["response"])
    wire["backend"].force_last_modified=FREEZE+timedelta(hours=1)
    # This also fails the bounded future-date guard; no time-travel accepted.
    fails("S3_STORAGE_TIMESTAMP_UNTRUSTED",wire["attestor"].attest,loc)


def test_storage_timestamp_before_original_signer_grant_is_rejected(wire):
    loc=wire["producer"].put(wire["frame"],wire["response"])
    wire["backend"].force_last_modified=FREEZE-timedelta(days=1)
    fails("BINDING_STALE_OR_FUTURE",wire["attestor"].attest,loc)


def test_expired_compliance_retention_is_not_claimed_as_active_lock(wire):
    loc,proof=committed(wire)
    # The bucket record still exists, but proof no longer claims active lock.
    expired_policy = S.ObjectLockPolicy(owner=OWNER,bucket=BUCKET,prefix=PREFIX,
                                        min_days=30,clock=lambda:FREEZE+timedelta(days=31))
    wire["verifier"].policy = expired_policy
    fails("S3_COMPLIANCE_VERSION_PIN_FAILED",wire["verifier"].verify,proof)
