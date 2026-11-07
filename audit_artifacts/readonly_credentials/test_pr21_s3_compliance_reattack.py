"""Independent PR21 S3 Compliance re-audit: offline-only hostile Stub E2E.

Checks original PR21 HEAD's version/retention proof with actor-independent
time, mutable inputs, signer/policy key replacement, delete markers, conditional
PUT races and unverifiable provider collusion. No AWS client, secrets, sockets,
live exchange, operating DB, main writes, or real orders.
"""
from __future__ import annotations

import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit_artifacts.readonly_credentials import immutable_binding as B
from audit_artifacts.readonly_credentials import remote_worm_mock as R
from audit_artifacts.readonly_credentials import s3_object_lock_commit as S
from audit_artifacts.readonly_credentials.test_immutable_binding import rig, NOW, h
from audit_artifacts.readonly_credentials.test_independent_witness import scene
from audit_artifacts.readonly_credentials.test_remote_worm_mock import net
from audit_artifacts.readonly_credentials.test_s3_object_lock_commit import (
    wire, committed, fails, re_sign, mutate_version, FREEZE, OWNER, BUCKET, PREFIX,
    StubS3Backend, StubS3Client,
)


def assert_denied(code, call, *args, **kwargs):
    with pytest.raises(B.BindingBlocked) as err:
        call(*args, **kwargs)
    assert err.value.code == code


def _policy(*, clock=None, owner=OWNER, bucket=BUCKET, prefix=PREFIX, days=30):
    return S.ObjectLockPolicy(owner=owner, bucket=bucket, prefix=prefix,
                              min_days=days, clock=clock or (lambda: FREEZE))


def test_original_pr21_base_positive_and_attestation_timestamps_are_pinned(wire):
    locator, proof = committed(wire)
    assert wire["verifier"].verify_for_request(
        proof, request_digest=locator["request_digest"],
        sequence=locator["sequence"], chain_hash=locator["chain_hash"]) == locator
    assert proof["attestation"]["storage_epoch"] == int(FREEZE.timestamp())
    assert wire["backend"].network_calls == 0


@pytest.mark.parametrize("name,new_value", [
    ("_s3", object()), ("policy", object()), ("pins", object()),
    ("sources", object()), ("witness", object()), ("now_unix", lambda: 1),
])
def test_writer_injected_s3_and_trust_pins_are_immutable_after_init(wire, name, new_value):
    assert_denied("S3_WRITER_TRUST_BOUNDARY_IMMUTABLE",
                  setattr, wire["producer"], name, new_value)
    assert not wire["backend"].network_calls


@pytest.mark.parametrize("name,new_value", [
    ("_reader", object()), ("_private", Ed25519PrivateKey.generate()),
    ("_policy", object()), ("_pins", object()), ("_sources", object()),
    ("_witness", object()), ("_now_unix", lambda:1), ("kid", "untrusted-issuer"),
])
def test_independent_attestor_signer_and_reader_roots_not_reassignable(wire,name,new_value):
    assert_denied("S3_ATTESTOR_TRUST_BOUNDARY_IMMUTABLE",
                  setattr,wire["attestor"],name,new_value)


@pytest.mark.parametrize("name,new_value", [
    ("reader", object()), ("pins", object()), ("sources", object()),
    ("witness", object()), ("_key", Ed25519PrivateKey.generate().public_key()),
    ("_kid", "other-issuer"), ("now_unix", lambda:1),
    ("_last_clock", FREEZE-timedelta(days=1)),
])
def test_proof_verifier_original_trust_roots_cannot_be_reassigned(wire,name,new_value):
    assert_denied("S3_PROOF_TRUST_BOUNDARY_IMMUTABLE",
                  setattr,wire["verifier"],name,new_value)


@pytest.mark.parametrize("changed", [
    {"owner":"999988887777"},
    {"bucket":"other-tenant-object-lock"},
    {"prefix":"forged-write"},
    {"days":1},
])
def test_proof_verifier_rejects_policy_repin_even_when_new_policy_object_valid(wire,changed):
    new_policy = _policy(**changed)
    assert_denied("S3_PROOF_TRUST_BOUNDARY_IMMUTABLE",
                  setattr,wire["verifier"],"policy",new_policy)
    locator, proof = committed(wire)
    assert wire["verifier"].verify(proof)==locator


def test_historical_audit_clock_advances_but_replay_cannot_rewind_it(wire):
    locator, proof = committed(wire)
    wire["verifier"].policy = _policy(clock=lambda: FREEZE+timedelta(days=10))
    assert wire["verifier"].verify(proof) == locator
    wire["verifier"].policy = _policy(clock=lambda: FREEZE+timedelta(days=2))
    assert_denied("S3_AUDITOR_CLOCK_ROLLBACK", wire["verifier"].verify, proof)


def test_attestor_snapshots_locator_before_untrusted_head_roundtrip(wire):
    loc = wire["producer"].put(wire["frame"], wire["response"])
    original=copy.deepcopy(loc)
    underlying = wire["reader_client"].head_object
    def mutate_during_head(**kwargs):
        loc["version_id"] = "v-fabricated-after-call"
        loc["request_digest"] = "a"*64
        return underlying(**kwargs)
    wire["reader_client"].head_object=mutate_during_head
    proof=wire["attestor"].attest(loc)
    assert proof["attestation"]["locator"] == original
    assert wire["verifier"].verify(proof) == original
    assert loc != original


def test_verifier_snapshots_signed_proof_before_untrusted_s3_response(wire):
    loc, proof = committed(wire)
    original = copy.deepcopy(proof)
    real_get=wire["reader_client"].get_object
    def mutate_during_get(**kwargs):
        proof["attestation"]["locator"]["request_digest"] = "a"*64
        proof["signature"]="B"*88
        return real_get(**kwargs)
    wire["reader_client"].get_object=mutate_during_get
    assert wire["verifier"].verify(proof) == loc
    assert proof != original


def test_signed_storage_timestamp_is_part_of_immutable_commit_proof(wire):
    loc,proof=committed(wire)
    assert proof["attestation"]["storage_epoch"] == int(FREEZE.timestamp())
    wire["backend"].force_last_modified = FREEZE+timedelta(seconds=1)
    assert_denied("S3_ATTESTED_TIMESTAMP_MISMATCH",wire["verifier"].verify,proof)


def test_attestor_key_theft_cannot_rewrite_stored_provider_timestamp_claim(wire):
    _,proof=committed(wire)
    counterfeit=re_sign(wire,proof,{
        "storage_epoch":proof["attestation"]["storage_epoch"]-30})
    assert_denied("S3_ATTESTED_TIMESTAMP_MISMATCH",
                  wire["verifier"].verify,counterfeit)


def test_attestor_signature_mutation_after_signing_does_not_amend_root_binding(wire):
    _,proof=committed(wire)
    changed=copy.deepcopy(proof)
    changed["attestation"]["operator_roots"]="a"*64
    assert_denied("S3_PROOF_SIGNATURE_INVALID",
                  wire["verifier"].verify,changed)


def test_signed_receipt_replay_to_different_append_fails_context_match(wire):
    loc,proof=committed(wire)
    assert_denied("S3_COMMIT_CONTEXT_REPLAY",wire["verifier"].verify_for_request,
                  proof,request_digest="f"*64,
                  sequence=loc["sequence"],chain_hash=loc["chain_hash"])
    assert_denied("S3_COMMIT_CONTEXT_REPLAY",wire["verifier"].verify_for_request,
                  proof,request_digest=loc["request_digest"],
                  sequence=loc["sequence"]+1,chain_hash=loc["chain_hash"])
    assert_denied("S3_COMMIT_CONTEXT_REPLAY",wire["verifier"].verify_for_request,
                  proof,request_digest=loc["request_digest"],
                  sequence=loc["sequence"],chain_hash="f"*64)


def test_invalid_expected_commit_data_fail_closed(wire):
    loc,proof=committed(wire)
    assert_denied("S3_EXPECTED_COMMIT_INVALID",wire["verifier"].verify_for_request,
                  proof,request_digest="invalid",sequence=1,chain_hash=loc["chain_hash"])
    assert_denied("S3_EXPECTED_COMMIT_INVALID",wire["verifier"].verify_for_request,
                  proof,request_digest=loc["request_digest"],sequence=True,
                  chain_hash=loc["chain_hash"])


def test_malformed_s3_head_metadata_wrong_type_fails_closed_not_attribute_error(wire):
    loc=wire["producer"].put(wire["frame"],wire["response"])
    original=wire["reader_client"].head_object
    def corrupt_metadata(**kwargs):
        result=original(**kwargs)
        result["Metadata"]=["not-a-dict"]
        return result
    wire["reader_client"].head_object=corrupt_metadata
    assert_denied("S3_VERSION_METADATA_INVALID",wire["attestor"].attest,loc)


@pytest.mark.parametrize("bad_return", [None, "bad", [], {"Retention":[]}])
def test_malformed_object_retention_response_denied(wire,bad_return):
    loc=wire["producer"].put(wire["frame"],wire["response"])
    wire["reader_client"].get_object_retention=lambda **kwargs: bad_return
    assert_denied("S3_VERSION_METADATA_INVALID",wire["attestor"].attest,loc)


def test_compliance_to_governance_downgrade_after_attestation_is_denied(wire):
    loc,proof=committed(wire)
    wire["backend"].force_mode="GOVERNANCE"
    assert_denied("S3_COMPLIANCE_VERSION_PIN_FAILED",wire["verifier"].verify,proof)


def test_retention_shortening_after_attestation_is_denied(wire):
    loc,proof=committed(wire)
    wire["backend"].force_retention=FREEZE+timedelta(days=1)
    assert_denied("S3_COMPLIANCE_VERSION_PIN_FAILED",wire["verifier"].verify,proof)


def test_version_specific_deleted_marker_does_not_erase_original_protected_version(wire):
    loc,proof=committed(wire)
    writer=wire["writer_client"]
    writer.delete_object(Bucket=BUCKET,Key=loc["key"],ExpectedBucketOwner=OWNER)
    assert wire["backend"].versions[loc["key"]][-1] is None
    assert wire["verifier"].verify(proof)==loc


def test_delete_marker_allows_second_locked_version_but_only_original_version_proves_old_request(wire):
    old, proof=committed(wire)
    wire["writer_client"].delete_object(Bucket=BUCKET,Key=old["key"],
                                         ExpectedBucketOwner=OWNER)
    newer=wire["producer"].put(wire["frame"],wire["response"])
    assert newer["key"]==old["key"]
    assert newer["version_id"] != old["version_id"]
    assert wire["verifier"].verify(proof)==old
    # A VersionId change is not a valid replacement for a signed old proof.
    modified=copy.deepcopy(proof)
    modified["attestation"]["locator"]["version_id"]=newer["version_id"]
    assert_denied("S3_PROOF_SIGNATURE_INVALID",wire["verifier"].verify,modified)


def test_concurrent_if_none_match_puts_are_one_current_key_winner_not_global_cas(wire):
    def run(_):
        try:
            return wire["producer"].put(wire["frame"],wire["response"])
        except B.BindingBlocked as e:
            return e.code
    with ThreadPoolExecutor(max_workers=8) as ex:
        attempts=list(ex.map(run,range(16)))
    successes=[x for x in attempts if type(x) is dict]
    blocked=[x for x in attempts if type(x) is str]
    assert len(successes)==1
    assert blocked == ["S3_PUT_UNCONFIRMED"] * 15
    assert len(wire["backend"].versions[successes[0]["key"]])==1
    # Other keys/servers could still fork; no S3 global monotonic lock.


def test_post_put_ack_loss_never_returns_unverified_commit_locator(wire):
    wire["backend"].drop_after_put=True
    assert_denied("S3_PUT_UNCONFIRMED",wire["producer"].put,
                  wire["frame"],wire["response"])
    assert sum(len(v) for v in wire["backend"].versions.values())==1
    assert_denied("S3_PUT_UNCONFIRMED",wire["producer"].put,
                  wire["frame"],wire["response"])


def test_lost_ack_then_delete_marker_can_create_new_version_residual_ambiguity(wire):
    wire["backend"].drop_after_put=True
    assert_denied("S3_PUT_UNCONFIRMED",wire["producer"].put,
                  wire["frame"],wire["response"])
    (key, versions),=wire["backend"].versions.items()
    assert len(versions)==1
    wire["writer_client"].delete_object(Bucket=BUCKET,Key=key,ExpectedBucketOwner=OWNER)
    subsequent=wire["producer"].put(wire["frame"],wire["response"])
    assert subsequent["version_id"] != versions[0]["VersionId"]
    assert len(wire["backend"].versions[key])==3
    # This deliberately demonstrates S3 conditional PUT is NOT globally unique.


def test_wrong_bucket_owner_fails_even_with_signer_reissued_locator(wire):
    _,proof=committed(wire)
    altered=re_sign(wire,proof,{
        "locator":{**proof["attestation"]["locator"],"owner":"999988887777"}})
    assert_denied("S3_LOCATOR_PIN_MISMATCH",wire["verifier"].verify,altered)


def test_unauthenticated_s3_head_api_cannot_replace_pinned_provider(wire):
    loc,proof=committed(wire)
    assert_denied("S3_PROOF_TRUST_BOUNDARY_IMMUTABLE",
                  setattr,wire["verifier"],"reader",object())
    assert wire["verifier"].verify(proof)==loc


def test_corrupt_version_checksum_not_accepted_even_with_correct_retention(wire):
    loc,proof=committed(wire)
    wire["backend"].corrupt_checksum=True
    assert_denied("S3_SDK_CHECKSUM_UNVERIFIED",wire["verifier"].verify,proof)


def test_all_signers_and_provider_collusion_remains_outside_local_proof(wire):
    """Deliberate counterexample: Stub cannot prove it is genuine AWS S3."""
    loc,proof=committed(wire)
    assert wire["verifier"].verify(proof)==loc
    # Mutability of malicious 'provider' state demonstrates that even a fully
    # signed local receipt cannot independently prove provider retention.
    wire["backend"].versions.clear()
    assert_denied("S3_VERSION_READ_UNVERIFIED",wire["verifier"].verify,proof)
    assert wire["backend"].network_calls==0


def test_no_production_activation_and_no_cloud_socket_in_any_stub(wire):
    assert wire["backend"].network_calls==0
    assert wire["producer"].__class__ is S.S3ComplianceWriter
    assert wire["net"]["server"].head.sequence == 1
