"""Independent PR16 offensive audit: signer theft/collusion, root swap, evidence tamper and replay.

The third signature is a mock INDEPENDENT WITNESS assertion. It cannot make
counterfeit underlying venue data genuine, and local ledgers are resettable.
No real API keys, network, orders or production DB are used.
"""
from __future__ import annotations

import base64
import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit_artifacts.readonly_credentials import immutable_binding as B
from audit_artifacts.readonly_credentials import independent_witness as W
from audit_artifacts.readonly_credentials.test_immutable_binding import (
    NOW, rig, signed_bundle, seal, key_bytes, h,
)
from exchange.verified_pnl import ledger


@pytest.fixture
def scene(rig):
    independent = Ed25519PrivateKey.generate()
    public = key_bytes(independent)
    pin = hashlib.sha256(public).hexdigest()
    witness = W.IndependentWitness(
        rig["authorities"], kid="external-evidence-verifier",
        public_key=public, expected_sha256=pin,
        expected_roots_commitment=rig["authorities"].root_commitment())
    return dict(rig=rig, private=independent, public=public,
                pin=pin, witness=witness)


def assert_block(code, func, *args, **kwargs):
    with pytest.raises(B.BindingBlocked) as err:
        func(*args, **kwargs)
    assert err.value.code == code
    assert str(err.value) == code


def signed_witness(scene, bundle, *, patch=None, private=None):
    t, r, a, kw = bundle
    witness_body = dict(
        venue=kw["expected_venue"], scope=kw["expected_scope"],
        account_tag=t["body"]["account_tag"],
        subaccount_tag=t["body"]["subaccount_tag"],
        legacy_ref=kw["expected_legacy_ref"], read_ref=kw["expected_read_ref"],
        challenge=kw["expected_challenge"],
        sequence=a["body"]["sequence"], previous=a["body"]["previous"],
        root_commitment=scene["rig"]["authorities"].root_commitment(),
        trade_observation_digest=B.digest(t),
        read_observation_digest=B.digest(r),
        approval_digest=B.digest(a),
        trade_source_digest=t["body"]["source_digest"],
        read_source_digest=r["body"]["source_digest"],
        trade_permissions=["READ", "TRADE"],
        read_permissions=["READ"], source_kind=W.SOURCE_KIND,
        issued_at=NOW - 1, expires_at=NOW + 60,
    )
    witness_body.update(patch or {})
    return seal(private or scene["private"], "external-evidence-verifier",
                W.KIND, witness_body)


def good(scene, venue="BYBIT", scope="spot"):
    bundle = signed_bundle(scene["rig"], venue, scope)
    return bundle, signed_witness(scene, bundle)


def verify(scene, bundle, witness_record):
    t, r, approval, args = bundle
    return W.verify_witnessed_pair(
        scene["rig"]["authorities"], scene["witness"],
        t, r, approval, witness_record, **args)


def append(scene, registry, bundle, witness_record):
    t, r, approval, args = bundle
    return registry.compare_and_append(
        t, r, approval, witness_record,
        **{k: v for k, v in args.items() if k != "trusted_head"})


@pytest.mark.parametrize("venue,scope", [
    ("UPBIT", "spot"), ("BYBIT", "spot"), ("BYBIT", "linear"),
    ("BINANCE", "spot"), ("BINANCE", "linear"),
    ("OKX", "spot"), ("OKX", "linear")])
def test_independent_witness_validates_mock_but_never_grants_operational_authority(scene, venue, scope):
    pair, receipt = good(scene, venue, scope)
    proposal = verify(scene, pair, receipt)
    assert proposal.externally_proven is False
    assert proposal.operationally_authorized is False
    assert proposal.venue == venue and proposal.scope == scope
    assert proposal.successor.sequence == 1
    assert_block("BINDING_LIVE_ACTIVATION_BLOCKED",
                 B.require_operational_activation, proposal)


def test_original_verified_root_map_cannot_be_modified_after_pin(scene):
    roots = scene["rig"]["authorities"]
    assert roots.root_commitment() == scene["witness"]._root_commitment
    with pytest.raises(TypeError):
        roots._keys["observer"] = roots._keys["approver"]
    assert_block("BINDING_ROOT_IMMUTABLE", setattr, roots, "_keys", {})
    assert_block("BINDING_ROOT_IMMUTABLE", setattr, roots, "_root_commitment", h("fake"))
    assert roots.root_commitment() == scene["witness"]._root_commitment


def test_witness_key_and_pinned_root_cannot_be_mutated_after_setup(scene):
    witness = scene["witness"]
    assert_block("WITNESS_ROOT_IMMUTABLE", setattr, witness, "_key", Ed25519PrivateKey.generate().public_key())
    assert_block("WITNESS_ROOT_IMMUTABLE", setattr, witness, "_root_commitment", h("replaced"))


def test_single_stolen_observer_key_cannot_substitute_read_evidence(scene):
    t, r, approval, args = signed_bundle(scene["rig"])
    changed = copy.deepcopy(r["body"])
    changed["source_digest"] = h("forged-with-stolen-observer")
    r = seal(scene["rig"]["observer"], "separate-exchange-observer", "KEY_OBSERVATION", changed)
    oldpair = signed_bundle(scene["rig"])
    original = signed_witness(scene, oldpair)
    assert_block("BINDING_OBSERVATION_SUBSTITUTION", verify, scene,
                 (t, r, approval, args), original)


def test_stolen_approver_key_cannot_change_account_without_observer_signature(scene):
    pair, witness_record = good(scene)
    t, r, a, args = pair
    malicious = copy.deepcopy(a["body"])
    malicious["account_tag"] = h("attacker-account")
    forged_approval = seal(scene["rig"]["approver"], "independent-policy-approver",
                           "BINDING_APPROVAL", malicious)
    assert_block("BINDING_CROSS_ACCOUNT", verify, scene,
                 (t, r, forged_approval, args), witness_record)


@pytest.mark.parametrize("forgery", [
    {"source_digest": h("fabricated-exchange-payload")},
    {"account_tag": h("fabricated-same-account-tag")},
    {"read_permission_escalation": "local_false_claim"},
])
def test_colluding_observer_and_approver_cannot_override_independent_witness(scene, forgery):
    original_pair, original_witness = good(scene)
    if "read_permission_escalation" in forgery:
        # Claimed READ-only on a key that externally actually permits trading.
        changed = dict(source_digest=h("false-readonly-attestation"))
    else:
        changed = forgery
    account = changed.get("account_tag")
    fake = signed_bundle(
        scene["rig"], read_patch=changed,
        trade_patch={"account_tag": account} if account else None,
        approval_patch={"account_tag": account} if account else None,
    )
    # Even if BOTH stolen role keys create mutually consistent documents,
    # a separately pinned third witness signed the earlier raw evidence.
    assert B.verify_pair(scene["rig"]["authorities"], scene["rig"]["authorities"],
                         *fake[:3], **fake[3]).successor.sequence == 1
    code = ("WITNESS_CONTEXT_MISMATCH" if account
            else "WITNESS_EVIDENCE_MISMATCH")
    assert_block(code, verify, scene, fake, original_witness)


def test_approval_and_observer_collusion_cannot_replace_venue_and_market_scope(scene):
    oldpair, oldw = good(scene)
    fake = signed_bundle(scene["rig"], "OKX", "linear")
    # Caller requests new scope, so ordinary dual signature verification passes.
    assert B.verify_pair(scene["rig"]["authorities"], scene["rig"]["authorities"],
                         *fake[:3], **fake[3]).successor.sequence == 1
    assert_block("WITNESS_CONTEXT_MISMATCH", verify, scene, fake, oldw)


def test_attacker_supplied_observer_and_approver_roots_rejected_by_separately_pinned_witness(scene):
    from cryptography.hazmat.primitives import serialization
    o = Ed25519PrivateKey.generate()
    a = Ed25519PrivateKey.generate()
    po, pa = key_bytes(o), key_bytes(a)
    swapped = B.PinnedAuthorities(
        dict(observer=dict(kid="separate-exchange-observer", public_key=po),
             approver=dict(kid="independent-policy-approver", public_key=pa)),
        dict(observer=hashlib.sha256(po).hexdigest(),
             approver=hashlib.sha256(pa).hexdigest()))
    assert swapped.root_commitment() != scene["rig"]["authorities"].root_commitment()
    assert_block("WITNESS_APPROVAL_ROOT_REPLACED",
                 scene["witness"].check_roots, swapped)
    assert_block("WITNESS_APPROVAL_ROOT_REPLACED",
                 W.IndependentWitness, swapped, kid="external-evidence-verifier",
                 public_key=scene["public"], expected_sha256=scene["pin"],
                 expected_roots_commitment=scene["rig"]["authorities"].root_commitment())


def test_witness_key_replacement_with_same_claim_rejected_by_independent_public_pin(scene):
    attack = Ed25519PrivateKey.generate()
    assert_block("WITNESS_ROOT_PIN_MISMATCH", W.IndependentWitness,
                 scene["rig"]["authorities"], kid="external-evidence-verifier",
                 public_key=key_bytes(attack), expected_sha256=scene["pin"],
                 expected_roots_commitment=scene["rig"]["authorities"].root_commitment())


@pytest.mark.parametrize("role", ["observer", "approver"])
def test_third_witness_cannot_reuse_either_approved_role_key(scene, role):
    key = key_bytes(scene["rig"][role])
    assert_block("WITNESS_ROLE_KEY_REUSE", W.IndependentWitness,
                 scene["rig"]["authorities"], kid="external-evidence-verifier",
                 public_key=key, expected_sha256=hashlib.sha256(key).hexdigest(),
                 expected_roots_commitment=scene["rig"]["authorities"].root_commitment())


def test_forged_witness_signature_with_correct_kid_is_denied(scene):
    pair, w = good(scene)
    attacker = Ed25519PrivateKey.generate()
    unsigned = {k: w[k] for k in ("domain", "kind", "kid", "body")}
    w["signature"] = base64.b64encode(attacker.sign(B.canonical(unsigned))).decode("ascii")
    assert_block("WITNESS_SIGNATURE_INVALID", verify, scene, pair, w)


@pytest.mark.parametrize("change,code", [
    ({"source_kind": "LOCAL_CLAIM"}, "WITNESS_CONTEXT_MISMATCH"),
    ({"read_permissions": ["READ", "TRADE"]}, "WITNESS_PERMISSIONS_MISMATCH"),
    ({"read_permissions": ["READ", "WITHDRAW"]}, "WITNESS_PERMISSIONS_MISMATCH"),
    ({"trade_source_digest": h("changed-exchange-bytes")}, "WITNESS_EVIDENCE_MISMATCH"),
    ({"read_source_digest": h("different-read-venue-source")}, "WITNESS_EVIDENCE_MISMATCH"),
    ({"trade_observation_digest": h("substituted-observer")}, "WITNESS_EVIDENCE_MISMATCH"),
    ({"read_observation_digest": h("substituted-read-proof")}, "WITNESS_EVIDENCE_MISMATCH"),
    ({"approval_digest": h("substituted-approver")}, "WITNESS_EVIDENCE_MISMATCH"),
    ({"root_commitment": h("attacker-root")}, "WITNESS_CONTEXT_MISMATCH"),
    ({"challenge": h("old-challenge")}, "WITNESS_CONTEXT_MISMATCH"),
    ({"previous": h("wrong-ledger-head")}, "WITNESS_CONTEXT_MISMATCH"),
    ({"sequence": 2}, "WITNESS_CONTEXT_MISMATCH"),
    ({"account_tag": h("another-account")}, "WITNESS_CONTEXT_MISMATCH"),
    ({"subaccount_tag": h("another-subaccount")}, "WITNESS_CONTEXT_MISMATCH"),
    ({"expires_at": NOW - 1}, "BINDING_STALE_OR_FUTURE"),
])
def test_fully_re_signed_false_independent_witness_claim_denied(scene, change, code):
    pair, _ = good(scene)
    malicious = signed_witness(scene, pair, patch=change)
    assert_block(code, verify, scene, pair, malicious)


def test_mutating_witness_body_after_signature_is_denied(scene):
    pair, w = good(scene)
    w["body"]["read_permissions"] = ["READ", "TRADE"]
    assert_block("WITNESS_SIGNATURE_INVALID", verify, scene, pair, w)


def test_mutating_witness_evidence_after_signing_is_denied(scene):
    pair, w = good(scene)
    w["body"]["read_source_digest"] = h("after-signing")
    assert_block("WITNESS_SIGNATURE_INVALID", verify, scene, pair, w)


def test_witness_replay_at_new_sequence_is_denied(scene):
    pair, w = good(scene)
    p = verify(scene, pair, w)
    next_bundle = signed_bundle(
        scene["rig"], trusted_head=p.successor,
        challenge=h("new-request-nonce"),
        account=h("new-account"), subaccount=h("new-subaccount"),
        trade_patch={"key_ref": ledger.identity("BYBIT", "other-trader")},
        read_patch={"key_ref": ledger.identity("BYBIT", "other-reader")},
        approval_patch={"legacy_ref": ledger.identity("BYBIT", "other-trader"),
                        "read_ref": ledger.identity("BYBIT", "other-reader")})
    next_bundle[3]["expected_legacy_ref"] = ledger.identity("BYBIT", "other-trader")
    next_bundle[3]["expected_read_ref"] = ledger.identity("BYBIT", "other-reader")
    assert_block("WITNESS_CONTEXT_MISMATCH", verify, scene, next_bundle, w)


def test_identical_witnessed_receipt_replay_within_one_registry_is_denied(scene):
    pair, w = good(scene)
    registry = W.MockWitnessedRegistry(B.TrustedHead(0, B.ZERO),
                                       scene["rig"]["authorities"], scene["witness"])
    first = append(scene, registry, pair, w)
    assert first.successor.sequence == 1
    assert_block("BINDING_ROLLBACK_OR_CONFLICT", append, scene, registry, pair, w)
    assert registry.history == (first.successor.chain_hash,)


def test_signed_receipts_concurrently_append_only_once(scene):
    pair, w = good(scene)
    reg = W.MockWitnessedRegistry(B.TrustedHead(0, B.ZERO),
                                  scene["rig"]["authorities"], scene["witness"])
    def one(_):
        try:
            return append(scene, reg, pair, w).successor.sequence
        except B.BindingBlocked as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(one, range(20)))
    assert outcomes.count(1) == 1
    assert outcomes.count("BINDING_ROLLBACK_OR_CONFLICT") == 19
    assert reg.head.sequence == 1 and len(reg.history) == 1


def test_fresh_signed_key_pair_must_not_reuse_old_external_challenge(scene):
    pair, w = good(scene)
    reg = W.MockWitnessedRegistry(B.TrustedHead(0, B.ZERO),
                                  scene["rig"]["authorities"], scene["witness"])
    first = append(scene, reg, pair, w)
    next_trade = ledger.identity("BYBIT", "trade-independent")
    next_read = ledger.identity("BYBIT", "read-independent")
    pair2 = signed_bundle(scene["rig"], trusted_head=first.successor,
       account=h("different-account"), subaccount=h("different-sub"),
       challenge=pair[3]["expected_challenge"],
       trade_patch={"key_ref": next_trade},
       read_patch={"key_ref": next_read},
       approval_patch={"legacy_ref": next_trade, "read_ref": next_read})
    pair2[3]["expected_legacy_ref"] = next_trade
    pair2[3]["expected_read_ref"] = next_read
    w2 = signed_witness(scene, pair2)
    assert_block("BINDING_CHALLENGE_REPLAY", append, scene, reg, pair2, w2)


def test_mock_ledger_rollback_to_new_genesis_still_replays_approved_receipt(scene):
    """Document a dangerous residual: no independent WORM checkpoint exists.

    An attacker who replaces *the whole in-memory registry* can replay a
    correctly signed prior receipt. This test MUST remain as evidence that
    operational anti-rollback is UNVERIFIED, not as an acceptance test.
    """
    pair, w = good(scene)
    reg1 = W.MockWitnessedRegistry(B.TrustedHead(0, B.ZERO),
                                   scene["rig"]["authorities"], scene["witness"])
    first = append(scene, reg1, pair, w)
    reg2 = W.MockWitnessedRegistry(B.TrustedHead(0, B.ZERO),
                                   scene["rig"]["authorities"], scene["witness"])
    replay = append(scene, reg2, pair, w)
    assert first.successor == replay.successor
    assert reg1 is not reg2
    assert first.operationally_authorized is False


def test_full_observer_approver_witness_collusion_is_cryptographically_undetectable(scene):
    """Residual threat: all 3 approved signing keys cannot prove source truth.

    This is a simulated counterexample demonstrating the external trust
    requirement, not a security PASS for the underlying venue assertion.
    """
    false_claims = signed_bundle(scene["rig"],
                read_patch={"source_digest": h("fabricated-real-venue-evidence")})
    three_signers_collude = signed_witness(scene, false_claims)
    result = verify(scene, false_claims, three_signers_collude)
    assert result.externally_proven is False
    assert result.operationally_authorized is False
    assert result.successor.sequence == 1


def test_no_live_activation_under_any_corroboration_state(scene):
    pair, w = good(scene)
    accepted = verify(scene, pair, w)
    assert_block("BINDING_LIVE_ACTIVATION_BLOCKED",
                 B.require_operational_activation, accepted)
    assert_block("BINDING_LIVE_ACTIVATION_BLOCKED",
                 B.require_operational_activation, {"all_signers": "approved"})


def test_errors_never_echo_raw_synthetic_account_credentials(scene):
    pair, w = good(scene)
    w["body"]["read_source_digest"] = h("SECRET_RAW_EXCHANGE_EVIDENCE")
    with pytest.raises(B.BindingBlocked) as exc:
        verify(scene, pair, w)
    assert "SECRET_RAW_EXCHANGE_EVIDENCE" not in str(exc.value)
    assert "legacy-trading-key" not in str(exc.value)
