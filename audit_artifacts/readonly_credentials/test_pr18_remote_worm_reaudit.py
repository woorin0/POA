"""Independent adversarial re-audit of PR18 remote WORM Mock boundary.

All network paths are byte-only in-memory fault injection, no real sockets.
Retest full upstream PR15/16/17/18 suites in Windows+Ubuntu separately.
Explicit counterexamples describe remaining unproven external persistence.
"""
from __future__ import annotations

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit_artifacts.readonly_credentials import remote_worm_mock as R
from audit_artifacts.readonly_credentials import immutable_binding as B
from audit_artifacts.readonly_credentials import independent_witness as W
from audit_artifacts.readonly_credentials.test_immutable_binding import rig, signed_bundle, NOW, h
from audit_artifacts.readonly_credentials.test_independent_witness import scene, signed_witness
from audit_artifacts.readonly_credentials.test_remote_worm_mock import (
    net, request, rawframe, first_commit, second_commit, evidence_for,
)


def blocked(code, func, *args, **kwargs):
    with pytest.raises(B.BindingBlocked) as err:
        func(*args, **kwargs)
    assert err.value.code == code
    assert str(err.value) == code


class InterceptOnly:
    """Malicious transport endpoint, never invokes the real server append."""
    def __init__(self, fn):
        self.fn = fn

    def dispatch(self, raw):
        return self.fn(R.strict_decode(raw))


def checkpoint(net, original_frame, entries, head, *, purpose="APPEND"):
    body = {
        "sequence": head.sequence,
        "chain_hash": head.chain_hash,
        "entries_digest": B.digest(entries),
        "request_digest": B.digest(original_frame),
        "operator_roots": net["pins"].commitment,
        "source_roots": net["scene"]["rig"]["authorities"].root_commitment(),
        "witness_root": net["scene"]["witness"]._root_commitment,
        "purpose": purpose,
    }
    signed = R.make_envelope(
        net["keys"]["checkpoint"], kid=net["pins"].kid("checkpoint"),
        kind="SIGNED_CHECKPOINT", body=body)
    return R.encode({"checkpoint": signed, "entries": entries})


def replace_checkpoint(net, response, edits, *, entries=None):
    obj = R.strict_decode(response)
    if entries is not None:
        obj["entries"] = entries
    body = {**obj["checkpoint"]["body"], **edits}
    obj["checkpoint"] = R.make_envelope(
        net["keys"]["checkpoint"], kid=net["pins"].kid("checkpoint"),
        kind="SIGNED_CHECKPOINT", body=body)
    return R.encode(obj)


def test_pr18_reference_pin_and_product_not_activated(net):
    assert net["server"].head == B.TrustedHead(0, B.ZERO)
    assert net["writer"].query(h("independent-original-genesis")) == net["server"].head
    blocked("BINDING_LIVE_ACTIVATION_BLOCKED", B.require_operational_activation, True)


def test_forged_validly_signed_append_checkpoint_without_commit_cannot_ack(net):
    # PR18 pre-fix defect: a response signed by the checkpoint key can lie
    # 'APPEND success' with an empty log; the client previously pinned it.
    net["writer"].query(h("read-current-before-false-ack"))
    frame = request(net)
    original = net["server"]
    net["link"].server = InterceptOnly(
        lambda received: checkpoint(net, received, [], B.TrustedHead(0, B.ZERO)))
    blocked("REMOTE_APPEND_RECEIPT_MISSING", net["writer"].append, frame)
    assert original.head.sequence == 0
    assert net["wanchor"].floor.sequence == 0


def test_checkpoint_key_compromise_wrong_append_purpose_does_not_pin(net):
    net["writer"].query(h("before-purpose-confusion"))
    frame = request(net)
    def purpose_corrupt(raw):
        return replace_checkpoint(net, raw, {"purpose": "HEAD"})
    net["link"].response_transform = purpose_corrupt
    blocked("REMOTE_RESPONSE_PURPOSE", net["writer"].append, frame)
    # Real server committed, but client must NOT accept wrong-purpose ACK.
    assert net["server"].head.sequence == 1
    assert net["wanchor"].floor.sequence == 0
    net["link"].response_transform = None
    assert net["writer"].query(h("reconcile-after-wrong-purpose")).sequence == 1


@pytest.mark.parametrize("field", [
    "legacy_ref", "read_ref", "binding_digest", "challenge",
    "venue", "scope", "chain_hash", "trade_observation_digest",
    "read_observation_digest"])
def test_stolen_checkpoint_signer_cannot_change_append_receipt_lineage(net, field):
    net["writer"].query(h("pre-exact-receipt-check"))
    frame = request(net)
    def tamper(raw):
        data = R.strict_decode(raw)
        target = data["entries"][0]
        target[field] = ("UPBIT" if field == "venue"
                         else "linear" if field == "scope"
                         else h("forged-" + field))
        newbody = {"entries_digest": B.digest(data["entries"])}
        return replace_checkpoint(net, R.encode(data), newbody)
    net["link"].response_transform = tamper
    blocked("REMOTE_APPEND_RECEIPT_MISMATCH", net["writer"].append, frame)
    assert net["server"].head.sequence == 1
    assert net["wanchor"].floor.sequence == 0


def test_checkpoint_signer_cannot_erase_already_committed_append_from_receipt(net):
    net["writer"].query(h("before-erased-receipt"))
    frame = request(net)
    def erase(raw):
        return replace_checkpoint(net, raw, {"sequence": 0, "chain_hash": B.ZERO,
                     "entries_digest": B.digest([])}, entries=[])
    net["link"].response_transform = erase
    blocked("REMOTE_APPEND_RECEIPT_MISSING", net["writer"].append, frame)
    assert net["wanchor"].floor.sequence == 0


def test_signer_can_fake_self_consistent_chain_but_fresh_verifier_rederives_hash(net):
    first_commit(net)
    def forge(raw):
        data = R.strict_decode(raw)
        data["entries"][0]["binding_digest"] = h("fake-confirmation")
        return replace_checkpoint(net, R.encode(data),
            {"entries_digest": B.digest(data["entries"])})
    net["link"].response_transform = forge
    fresh = R.IndependentMockAnchor(
        net["pins"], net["scene"]["rig"]["authorities"], net["scene"]["witness"])
    independent = R.MockRemoteClient(role="auditor", private_key=net["keys"]["auditor"],
         pins=net["pins"], link=net["link"], anchor=fresh)
    blocked("REMOTE_ENTRIES_HASH_FORGED", independent.query, h("independent-recompute"))
    assert fresh.floor.sequence == 0


def test_operator_role_elevation_to_checkpoint_or_writer_denied(net):
    net["writer"].query(h("role-read"))
    frame = request(net)
    frame["writer"] = R.make_envelope(
        net["keys"]["operator"], kid=net["pins"].kid("writer"),
        kind="APPEND_PROOF", body=frame["writer"]["body"])
    blocked("REMOTE_SIGNATURE_INVALID", rawframe, net, frame)
    blocked("REMOTE_CLIENT_ROLE_FORBIDDEN", R.MockRemoteClient,
        role="writer",private_key=net["keys"]["operator"],pins=net["pins"],
        link=net["link"],anchor=net["wanchor"])
    blocked("REMOTE_SIGNER_PIN_MISMATCH", R.MockRemoteWORMServer,
        net["pins"],net["scene"]["rig"]["authorities"],net["scene"]["witness"],
        net["keys"]["operator"],lambda: NOW)


def test_absent_operator_grant_cannot_bypass_cas_via_signed_writer(net):
    frame = request(net, approved=False)
    blocked("REMOTE_ROLE_FORBIDDEN", rawframe, net, frame)
    assert net["server"].records == ()


def test_stolen_writer_key_cannot_forge_operator_grant_for_other_request(net):
    a = request(net, nonce="alice")
    b = request(net, nonce="bob")
    b["grant"] = a["grant"]
    blocked("REMOTE_OPERATOR_GRANT_MISMATCH", rawframe, net, b)
    assert net["server"].head.sequence == 0


def test_server_commit_state_is_atomic_when_abort_after_prepare(net):
    net["writer"].query(h("pre-prepare-atomic"))
    frame = request(net)
    net["server"].fail_after_prepare_once = True
    with pytest.raises(R.MockLinkLost, match="after prepare"):
        net["writer"].append(frame)
    assert net["server"].head == B.TrustedHead(0, B.ZERO)
    assert net["server"].records == ()
    assert net["server"]._completed == {}
    assert not net["server"]._used_challenges
    assert not net["server"]._by_legacy and not net["server"]._by_read
    assert net["wanchor"].floor.sequence == 0
    assert net["writer"].append(frame).sequence == 1
    assert len(net["server"].records) == 1


def test_concurrent_conflicting_cas_writes_have_one_winner(net):
    genesis = B.TrustedHead(0, B.ZERO)
    frames = [request(net, nonce=f"cas-contention-{i}", head=genesis,
                      new_account=(i % 2 == 0)) for i in range(30)]
    def send(frame):
        try:
            rawframe(net, frame)
            return "APPENDED"
        except B.BindingBlocked as e:
            return e.code
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(send, frames))
    assert results.count("APPENDED") == 1
    assert results.count("REMOTE_CAS_CONFLICT") == 29
    assert net["server"].head.sequence == 1
    assert len(net["server"].records) == 1
    assert len(net["server"]._completed) == 1


def test_dropped_post_commit_ack_retry_is_exactly_once(net):
    net["writer"].query(h("before-lost-receipt"))
    frame = request(net)
    net["link"].drop_after_once = True
    with pytest.raises(R.MockLinkLost):
        net["writer"].append(frame)
    assert net["server"].head.sequence == 1
    assert net["wanchor"].floor.sequence == 0
    assert net["writer"].append(frame).sequence == 1
    assert len(net["server"].records) == 1
    assert len(net["server"]._completed) == 1


def test_signed_receipt_replayed_to_new_append_request_id_denied(net):
    net["writer"].query(h("before-original-commit"))
    oldframe = request(net)
    recorded = {}
    def capture(raw):
        recorded["receipt"] = raw
        return raw
    net["link"].response_transform = capture
    net["writer"].append(oldframe)
    next_frame = request(net, nonce="second-new-receipt-request",
                         head=net["server"].head,new_account=True)
    net["link"].response_transform = lambda _: recorded["receipt"]
    blocked("REMOTE_APPEND_RECEIPT_MISSING", net["writer"].append, next_frame)
    assert net["wanchor"].floor.sequence == 1


def test_fork_two_worm_servers_same_genesis_detected_when_auditor_has_single_anchor(net):
    # Without durable external CAS two *independent* server instances can sign
    # mutually inconsistent but individually valid chain extensions.
    alpha = net["server"]
    beta = R.MockRemoteWORMServer(
        net["pins"],net["scene"]["rig"]["authorities"],
        net["scene"]["witness"],net["keys"]["checkpoint"],lambda: NOW)
    first = request(net, nonce="fork-first",head=B.TrustedHead(0,B.ZERO))
    second = request(net,nonce="fork-second",head=B.TrustedHead(0,B.ZERO),new_account=True)
    assert R.strict_decode(alpha.dispatch(R.encode(first)))["checkpoint"]["body"]["sequence"] == 1
    assert R.strict_decode(beta.dispatch(R.encode(second)))["checkpoint"]["body"]["sequence"] == 1
    assert alpha.head.sequence == beta.head.sequence == 1
    assert alpha.head.chain_hash != beta.head.chain_hash
    net["link"].server = alpha
    assert net["auditor"].query(h("auditor-check-fork-alpha")) == alpha.head
    net["link"].server = beta
    blocked("REMOTE_SPLIT_VIEW", net["auditor"].query,h("auditor-check-fork-beta"))
    assert net["aanchor"].floor == alpha.head


def test_fork_two_servers_without_shared_independent_anchor_remains_unresolved(net):
    alpha = net["server"]
    beta = R.MockRemoteWORMServer(
        net["pins"],net["scene"]["rig"]["authorities"],
        net["scene"]["witness"],net["keys"]["checkpoint"],lambda: NOW)
    frames = (request(net,nonce="u-fork-a"),
              request(net,nonce="u-fork-b",new_account=True))
    alpha.dispatch(R.encode(frames[0]))
    beta.dispatch(R.encode(frames[1]))
    a_anchor = R.IndependentMockAnchor(net["pins"],net["scene"]["rig"]["authorities"],net["scene"]["witness"])
    b_anchor = R.IndependentMockAnchor(net["pins"],net["scene"]["rig"]["authorities"],net["scene"]["witness"])
    net["link"].server = alpha
    a = R.MockRemoteClient(role="auditor",private_key=net["keys"]["auditor"],
                            pins=net["pins"],link=net["link"],anchor=a_anchor)
    assert a.query(h("isolated-view-a")) == alpha.head
    net["link"].server = beta
    b = R.MockRemoteClient(role="auditor",private_key=net["keys"]["auditor"],
                            pins=net["pins"],link=net["link"],anchor=b_anchor)
    assert b.query(h("isolated-view-b")) == beta.head
    assert a_anchor.floor != b_anchor.floor
    # Mock-only accepted fork is an EXPLICIT BLOCKER for true WORM or quorum.


def test_writer_cannot_replace_auditor_highwater_after_existing_verified_checkpoint(net):
    first_commit(net)
    assert net["wanchor"].floor.sequence == 1
    net["link"].server = R.MockRemoteWORMServer(
         net["pins"],net["scene"]["rig"]["authorities"],
         net["scene"]["witness"],net["keys"]["checkpoint"],lambda: NOW)
    blocked("REMOTE_ROLLBACK_DETECTED", net["writer"].query,h("rollback-detection"))


def test_full_server_and_auditor_snapshot_reset_still_unresolved(net):
    # No local-only Mock can prove resistance when *all* local snapshots reset.
    first_commit(net)
    assert net["auditor"].query(h("audit-pin-before-global-reset")).sequence == 1
    net["link"].server = R.MockRemoteWORMServer(
         net["pins"],net["scene"]["rig"]["authorities"],
         net["scene"]["witness"],net["keys"]["checkpoint"],lambda: NOW)
    fresh = R.IndependentMockAnchor(
         net["pins"],net["scene"]["rig"]["authorities"],net["scene"]["witness"])
    auditor=R.MockRemoteClient(role="auditor",private_key=net["keys"]["auditor"],
                               pins=net["pins"],link=net["link"],anchor=fresh)
    assert auditor.query(h("reset-both-unresolved")).sequence == 0
    blocked("REMOTE_ROLLBACK_DETECTED",net["auditor"].query,h("old-auditor-protects"))


def test_fake_committed_entry_by_stolen_checkpoint_key_is_not_durability_proof(net):
    # Important residual threat: the stolen checkpoint signer can fabricate
    # an internally consistent signed append receipt without ANY real append.
    # Only independent durable WORM attestations can close this gap.
    net["writer"].query(h("before-counterexample"))
    frame=request(net)
    expected=B.TrustedHead(0,B.ZERO)
    ev=frame["writer"]["body"]["evidence"]
    proposal=W.verify_witnessed_pair(
       net["scene"]["rig"]["authorities"],net["scene"]["witness"],
       ev["trade"],ev["read"],ev["approval"],ev["witness"],
       trusted_head=expected,now=NOW,**ev["context"])
    entry={
       "sequence":1,"previous":B.ZERO,"chain_hash":proposal.successor.chain_hash,
       "binding_digest":proposal.binding_digest,
       "trade_observation_digest":B.digest(ev["trade"]),
       "read_observation_digest":B.digest(ev["read"]),
       "venue":proposal.venue,"scope":proposal.scope,
       "legacy_ref":proposal.legacy_ref,"read_ref":proposal.read_ref,
       "challenge":proposal.challenge,"request_digest":B.digest(frame)}
    real=net["server"]
    net["link"].server=InterceptOnly(
         lambda incoming: checkpoint(net,incoming,[entry],proposal.successor))
    assert net["writer"].append(frame)==proposal.successor
    assert real.head.sequence == 0 and real.records == ()
    # This is an adversarial counterexample, NOT a PASS of remote durability.
    blocked("BINDING_LIVE_ACTIVATION_BLOCKED",B.require_operational_activation,proposal)
