"""Adversarial offline Mock tests for P0 immutable exchange account/key binding.

No real keys or provider calls. Two independent ephemeral TEST Ed25519 signers
make claims about synthetic exchange-authenticated observations. The signed
proof is NOT itself real exchange evidence; product activation stays BLOCKED.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit_artifacts.readonly_credentials import immutable_binding as M
from audit_artifacts.readonly_credentials import isolated_broker as B
from exchange.verified_pnl import ledger
from exchange.verified_pnl.order_fill_read import ReadHost, ReadRejected

PRODUCT = "ad88384b8b6030c1109843b09b6761395b3f2d1c"
TREE = "663c82e1134f592beb9c8bc652daccc7b2185975"
ROOT = Path(__file__).resolve().parents[2]
NOW = 1794000000
VENUES = [("UPBIT", "spot"), ("BINANCE", "spot"), ("BINANCE", "linear"),
          ("BYBIT", "spot"), ("BYBIT", "linear"),
          ("OKX", "spot"), ("OKX", "linear")]


def h(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def key_bytes(private):
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def seal(private, kid, kind, body):
    unsigned = dict(domain=M.DOMAIN, kind=kind, kid=kid, body=copy.deepcopy(body))
    signature = private.sign(M.canonical(unsigned))
    return dict(unsigned, signature=base64.b64encode(signature).decode("ascii"))


@pytest.fixture
def rig():
    observed = Ed25519PrivateKey.generate()
    approved = Ed25519PrivateKey.generate()
    op = key_bytes(observed)
    ap = key_bytes(approved)
    roots = M.PinnedAuthorities(
        dict(observer=dict(kid="separate-exchange-observer", public_key=op),
             approver=dict(kid="independent-policy-approver", public_key=ap)),
        dict(observer=hashlib.sha256(op).hexdigest(),
             approver=hashlib.sha256(ap).hexdigest()),
    )
    return dict(observer=observed, approver=approved, authorities=roots)


def signed_bundle(rig, venue="BYBIT", scope="spot", *,
                  trade_patch=None, read_patch=None, approval_patch=None,
                  trusted_head=None, challenge=None, account=None, subaccount=None):
    challenge = challenge or h("external-challenge-9a")
    account = account or h("exchange-authenticated-account-7")
    subaccount = subaccount or h("exchange-authenticated-subaccount-3")
    oldref = ledger.identity(venue, "legacy-trading-key")
    newref = ledger.identity(venue, "dedicated-read-key")
    def observation(role, ref, permissions):
        return dict(venue=venue, scope=scope, role=role, account_tag=account,
                    subaccount_tag=subaccount, key_ref=ref, permissions=permissions,
                    source_kind="AUTHENTICATED_EXCHANGE_RESPONSE",
                    source_digest=h("synthetic-raw-source:" + role + venue + scope),
                    challenge=challenge, issued_at=NOW - 20, expires_at=NOW + 80)
    trade = observation("trade", oldref, ["READ", "TRADE"])
    read = observation("read", newref, ["READ"])
    trade.update(trade_patch or {})
    read.update(read_patch or {})
    tobs = seal(rig["observer"], "separate-exchange-observer",
                "KEY_OBSERVATION", trade)
    robs = seal(rig["observer"], "separate-exchange-observer",
                "KEY_OBSERVATION", read)
    head = trusted_head or M.TrustedHead(0, M.ZERO)
    a = dict(venue=venue, scope=scope, account_tag=account,
             subaccount_tag=subaccount, legacy_ref=oldref, read_ref=newref,
             trade_observation_digest=M.digest(tobs),
             read_observation_digest=M.digest(robs), sequence=head.sequence + 1,
             previous=head.chain_hash, challenge=challenge,
             issued_at=NOW - 10, expires_at=NOW + 80, policy=M.POLICY)
    a.update(approval_patch or {})
    cert = seal(rig["approver"], "independent-policy-approver",
                "BINDING_APPROVAL", a)
    args = dict(expected_venue=venue, expected_scope=scope,
                expected_legacy_ref=oldref, expected_read_ref=newref,
                expected_challenge=challenge, trusted_head=head, now=NOW)
    return tobs, robs, cert, args


def verify(rig, inputs):
    t, r, c, args = inputs
    return M.verify_pair(rig["authorities"], rig["authorities"], t, r, c, **args)


def blocked(expected, call, *args, **kwargs):
    with pytest.raises(M.BindingBlocked) as exc:
        call(*args, **kwargs)
    assert exc.value.code == expected
    assert str(exc.value) == expected


def test_exact_unchanged_product_commit_tree_and_previous_pr15_broker():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == PRODUCT
    assert subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip() == TREE
    assert subprocess.check_output(["git", "diff", "--name-only"], cwd=ROOT, text=True).strip() == ""
    key = B.load_dedicated_key({
        "POA_FILL_READ_ONLY_BYBIT_KEY": "dedicated-read-key",
        "POA_FILL_READ_ONLY_BYBIT_SECRET": "synthetic-secret",
    }, "BYBIT")
    with pytest.raises(ReadRejected, match="FILL_ACCOUNT_MISMATCH"):
        ReadHost(B.OfflineReadHostSettings(key))._client(dict(
            exchange="BYBIT", market_type="spot",
            account=ledger.identity("BYBIT", "legacy-trading-key")))


@pytest.mark.parametrize("venue,scope", VENUES)
def test_two_signatures_same_account_produce_nonactivatable_append_proposal(rig, venue, scope):
    p = verify(rig, signed_bundle(rig, venue, scope))
    assert p.venue == venue and p.scope == scope
    assert p.predecessor == M.TrustedHead(0, M.ZERO)
    assert p.successor.sequence == 1 and p.successor.chain_hash != M.ZERO
    assert p.legacy_ref != p.read_ref
    assert p.externally_proven is False and p.operationally_authorized is False
    blocked("BINDING_LIVE_ACTIVATION_BLOCKED", M.require_operational_activation, p)


@pytest.mark.parametrize("venue,scope", VENUES)
def test_read_only_policy_and_distinct_trading_credential_claim(rig, venue, scope):
    t, r, a, kw = signed_bundle(rig, venue, scope)
    assert t["body"]["permissions"] == ["READ", "TRADE"]
    assert r["body"]["permissions"] == ["READ"]
    assert verify(rig, (t, r, a, kw)).successor.sequence == 1
    assert kw["expected_legacy_ref"] == ledger.identity(venue, "legacy-trading-key")


@pytest.mark.parametrize("field,value", [
    ("account_tag", h("other-account")),
    ("subaccount_tag", h("other-subaccount")),
    ("venue", "OKX"), ("scope", "linear"),
])
def test_swapped_read_venue_account_or_subaccount_is_denied(rig, field, value):
    blocked("BINDING_CROSS_ACCOUNT", verify, rig,
            signed_bundle(rig, read_patch={field: value}))


@pytest.mark.parametrize("field,value", [
    ("account_tag", h("other-account")),
    ("subaccount_tag", h("other-subaccount")),
    ("venue", "OKX"), ("scope", "linear"),
])
def test_swapped_trading_venue_account_or_subaccount_is_denied(rig, field, value):
    blocked("BINDING_CROSS_ACCOUNT", verify, rig,
            signed_bundle(rig, trade_patch={field: value}))


@pytest.mark.parametrize("patch", [
    dict(permissions=["READ", "TRADE"]),
    dict(permissions=["READ", "WITHDRAW"]),
    dict(permissions=["READ", "TRANSFER"]),
    dict(permissions=[]), dict(permissions=["READ", "READ"]),
    dict(permissions="READ"),
])
def test_write_or_ambiguous_read_key_permission_claim_is_denied(rig, patch):
    blocked("BINDING_PERMISSION_SCOPE", verify, rig,
            signed_bundle(rig, read_patch=patch))


@pytest.mark.parametrize("patch", [
    dict(permissions=["TRADE"]),
    dict(permissions=["READ", "TRADE", "WITHDRAW"]),
    dict(permissions=["READ", "TRADE", "TRANSFER"]),
])
def test_trading_key_policy_disallows_unknown_or_privileged_extra_scope(rig, patch):
    blocked("BINDING_PERMISSION_SCOPE", verify, rig,
            signed_bundle(rig, trade_patch=patch))


def test_legacy_account_ref_mismatch_fails_even_with_valid_dual_signatures(rig):
    b = signed_bundle(rig)
    b[3]["expected_legacy_ref"] = h("wrong-legacy-account-key")
    blocked("BINDING_KEY_REF_MISMATCH", verify, rig, b)


def test_read_key_ref_mismatch_fails_even_with_valid_dual_signatures(rig):
    b = signed_bundle(rig)
    b[3]["expected_read_ref"] = h("wrong-read-key")
    blocked("BINDING_KEY_REF_MISMATCH", verify, rig, b)


def test_same_api_key_for_both_roles_is_rejected(rig):
    b = signed_bundle(rig)
    b[3]["expected_read_ref"] = b[3]["expected_legacy_ref"]
    blocked("BINDING_SAME_KEY_REUSE", verify, rig, b)


def test_forged_approval_link_to_other_observation_is_denied(rig):
    blocked("BINDING_OBSERVATION_SUBSTITUTION", verify, rig,
            signed_bundle(rig, approval_patch=dict(read_observation_digest=h("fake-read-observation"))))


@pytest.mark.parametrize("patch", [
    dict(legacy_ref=h("forged-trade-ref")),
    dict(read_ref=h("forged-read-ref")),
])
def test_certificate_key_ref_substitution_is_denied(rig, patch):
    blocked("BINDING_KEY_REF_MISMATCH", verify, rig,
            signed_bundle(rig, approval_patch=patch))


def test_mutating_observation_after_signature_fails(rig):
    t, r, c, kw = signed_bundle(rig)
    r["body"]["account_tag"] = h("forged-after-signature")
    blocked("BINDING_SIGNATURE_INVALID", M.verify_pair,
            rig["authorities"], rig["authorities"], t, r, c, **kw)


def test_mutating_approval_after_signature_fails(rig):
    t, r, c, kw = signed_bundle(rig)
    c["body"]["legacy_ref"] = h("tampered-legacy-ref")
    blocked("BINDING_SIGNATURE_INVALID", M.verify_pair,
            rig["authorities"], rig["authorities"], t, r, c, **kw)


def test_forged_signer_signature_fails_even_if_kid_is_trusted(rig):
    t, r, c, kw = signed_bundle(rig)
    attacker = Ed25519PrivateKey.generate()
    c["signature"] = base64.b64encode(
        attacker.sign(M.canonical({k: c[k] for k in ("domain", "kind", "kid", "body")}))).decode("ascii")
    blocked("BINDING_SIGNATURE_INVALID", M.verify_pair,
            rig["authorities"], rig["authorities"], t, r, c, **kw)


def test_observer_cannot_sign_as_approver(rig):
    t, r, c, kw = signed_bundle(rig)
    c = seal(rig["observer"], "independent-policy-approver", "BINDING_APPROVAL", c["body"])
    blocked("BINDING_SIGNATURE_INVALID", M.verify_pair,
            rig["authorities"], rig["authorities"], t, r, c, **kw)


def test_approver_cannot_sign_an_observer_claim(rig):
    t, r, c, kw = signed_bundle(rig)
    r = seal(rig["approver"], "separate-exchange-observer", "KEY_OBSERVATION", r["body"])
    blocked("BINDING_SIGNATURE_INVALID", M.verify_pair,
            rig["authorities"], rig["authorities"], t, r, c, **kw)


def test_wrong_domain_even_with_re_signed_attestation_denied(rig):
    t, r, c, kw = signed_bundle(rig)
    r = seal(rig["observer"], "separate-exchange-observer", "KEY_OBSERVATION", r["body"])
    r["domain"] = "OTHER_SYSTEM"
    blocked("BINDING_SIGNER_OR_DOMAIN_INVALID", M.verify_pair,
            rig["authorities"], rig["authorities"], t, r, c, **kw)


def test_untrusted_public_key_replacement_denied_by_external_pin(rig):
    attacker = Ed25519PrivateKey.generate()
    fake = key_bytes(attacker)
    observer_original = key_bytes(rig["observer"])
    approver_original = key_bytes(rig["approver"])
    blocked("BINDING_ROOT_PIN_INVALID", M.PinnedAuthorities,
            dict(observer=dict(kid="separate-exchange-observer", public_key=fake),
                 approver=dict(kid="independent-policy-approver", public_key=approver_original)),
            dict(observer=hashlib.sha256(observer_original).hexdigest(),
                 approver=hashlib.sha256(approver_original).hexdigest()))


def test_same_signing_key_for_both_trust_roles_is_denied(rig):
    pub = key_bytes(rig["observer"])
    blocked("BINDING_ROOT_PIN_INVALID", M.PinnedAuthorities,
            dict(observer=dict(kid="O", public_key=pub),
                 approver=dict(kid="A", public_key=pub)),
            dict(observer=hashlib.sha256(pub).hexdigest(),
                 approver=hashlib.sha256(pub).hexdigest()))


@pytest.mark.parametrize("change", [
    dict(sequence=0), dict(sequence=2), dict(previous=h("other-ledger-head")),
])
def test_sequence_skip_and_forged_predecessor_are_denied(rig, change):
    blocked("BINDING_ROLLBACK_OR_CONFLICT", verify, rig,
            signed_bundle(rig, approval_patch=change))


def test_replay_to_advanced_monotonic_checkpoint_is_denied(rig):
    registry = M.MockAppendOnlyRegistry(M.TrustedHead(0, M.ZERO))
    t, r, c, args = signed_bundle(rig)
    call = lambda: registry.compare_and_append(rig["authorities"], rig["authorities"],
                                                t, r, c, **{k: v for k, v in args.items()
                                                            if k != "trusted_head"})
    proposal = call()
    assert registry.head == proposal.successor
    blocked("BINDING_ROLLBACK_OR_CONFLICT", call)
    assert registry.history == (proposal.successor.chain_hash,)


def test_signed_successor_chain_can_append_only_with_fresh_sequence(rig):
    registry = M.MockAppendOnlyRegistry(M.TrustedHead(0, M.ZERO))
    t, r, c, args = signed_bundle(rig)
    head1 = registry.compare_and_append(
        rig["authorities"], rig["authorities"], t, r, c,
        **{k: v for k, v in args.items() if k != "trusted_head"}).successor
    t2, r2, c2, args2 = signed_bundle(
        rig, trusted_head=head1, challenge=h("external-second-challenge"))
    head2 = registry.compare_and_append(
        rig["authorities"], rig["authorities"], t2, r2, c2,
        **{k: v for k, v in args2.items() if k != "trusted_head"}).successor
    assert head2.sequence == 2
    assert head1.chain_hash != head2.chain_hash
    assert registry.history == (head1.chain_hash, head2.chain_hash)


def test_concurrent_append_only_one_winner_for_same_sequence(rig):
    registry = M.MockAppendOnlyRegistry(M.TrustedHead(0, M.ZERO))
    t, r, c, args = signed_bundle(rig)
    def attempt(_):
        try:
            return registry.compare_and_append(
                rig["authorities"], rig["authorities"], t, r, c,
                **{k: v for k, v in args.items() if k != "trusted_head"}).successor.sequence
        except M.BindingBlocked as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        out = list(pool.map(attempt, range(16)))
    assert out.count(1) == 1
    assert out.count("BINDING_ROLLBACK_OR_CONFLICT") == 15
    assert len(registry.history) == 1 and registry.head.sequence == 1


@pytest.mark.parametrize("change", [
    dict(challenge=h("replay-old-nonce")),
    dict(issued_at=NOW + 1),
    dict(expires_at=NOW - 1),
    dict(expires_at=NOW + 301),
    dict(issued_at=True),
])
def test_replayed_future_or_expired_observation_denied(rig, change):
    code = "BINDING_CHALLENGE_MISMATCH" if "challenge" in change else "BINDING_STALE_OR_FUTURE"
    blocked(code, verify, rig, signed_bundle(rig, read_patch=change))


@pytest.mark.parametrize("change", [
    dict(challenge=h("another-client-challenge")),
    dict(issued_at=NOW + 1),
    dict(expires_at=NOW - 1),
    dict(expires_at=NOW + 301),
    dict(issued_at=True),
])
def test_replayed_future_or_expired_approval_denied(rig, change):
    code = "BINDING_APPROVAL_CONTEXT" if "challenge" in change else "BINDING_STALE_OR_FUTURE"
    blocked(code, verify, rig, signed_bundle(rig, approval_patch=change))


def test_signing_order_must_observe_first_then_approve(rig):
    blocked("BINDING_ATTESTATION_ORDER", verify, rig,
            signed_bundle(rig, read_patch=dict(issued_at=NOW - 2)))


@pytest.mark.parametrize("change", [
    dict(policy="ALLOW_ORDERS"),
    dict(venue="BINANCE"), dict(scope="linear"),
])
def test_cross_domain_policy_venue_or_scope_approval_denied(rig, change):
    blocked("BINDING_APPROVAL_CONTEXT", verify, rig,
            signed_bundle(rig, approval_patch=change))


def test_extra_unsigned_fields_in_approval_are_not_accepted(rig):
    blocked("BINDING_APPROVAL_SCHEMA", verify, rig,
            signed_bundle(rig, approval_patch=dict(extra="ignore-me")))


def test_extra_signed_claim_fields_in_observer_evidence_are_not_accepted(rig):
    blocked("BINDING_OBSERVATION_SCHEMA", verify, rig,
            signed_bundle(rig, read_patch=dict(role_override="read")))


def test_unauthorized_source_is_denied_even_if_signed_by_mock_observer(rig):
    blocked("BINDING_OBSERVATION_SCOPE", verify, rig,
            signed_bundle(rig, read_patch=dict(source_kind="LOCAL_OPERATOR_ASSERTION")))


def test_wrong_challenge_cannot_be_submitted_to_a_new_external_request(rig):
    b = signed_bundle(rig)
    b[3]["expected_challenge"] = h("different-external-challenge")
    blocked("BINDING_CHALLENGE_MISMATCH", verify, rig, b)


def test_corrupted_external_checkpoint_cannot_be_replaced_with_genesis(rig):
    # The immutable head MUST be stored out-of-process. Only the provided
    # independently trusted checkpoint is tested here.
    b = signed_bundle(rig)
    b[3]["trusted_head"] = M.TrustedHead(9, h("externally-pinned-high-water"))
    blocked("BINDING_ROLLBACK_OR_CONFLICT", verify, rig, b)


@pytest.mark.parametrize("arg", [True, None, {}, {"allow": True}, "MOCK_PASS"])
def test_even_synthetic_verified_binding_never_activates_operations(rig, arg):
    p = verify(rig, signed_bundle(rig))
    blocked("BINDING_LIVE_ACTIVATION_BLOCKED",
            M.require_operational_activation, arg if arg is not True else p)


def test_error_diagnostics_do_not_include_synthetic_secrets(rig):
    b = signed_bundle(rig)
    b[3]["expected_read_ref"] = h("private-synthetic-key")
    with pytest.raises(M.BindingBlocked) as err:
        verify(rig, b)
    assert "private-synthetic-key" not in str(err.value)
    assert "legacy-trading-key" not in str(err.value)
    assert err.value.code == "BINDING_KEY_REF_MISMATCH"


def test_valid_newer_approval_cannot_rebind_existing_legacy_account_to_another_read_key(rig):
    registry = M.MockAppendOnlyRegistry(M.TrustedHead(0, M.ZERO))
    t, r, c, args = signed_bundle(rig)
    common = {k: v for k, v in args.items() if k != "trusted_head"}
    first = registry.compare_and_append(rig["authorities"], rig["authorities"], t, r, c, **common)
    alternate = ledger.identity("BYBIT", "another-dedicated-read-key")
    t2, r2, c2, args2 = signed_bundle(
        rig, trusted_head=first.successor, challenge=h("second-signed-rebind-nonce"),
        read_patch=dict(key_ref=alternate), approval_patch=dict(read_ref=alternate))
    args2["expected_read_ref"] = alternate
    blocked("BINDING_ALREADY_BOUND", registry.compare_and_append,
            rig["authorities"], rig["authorities"], t2, r2, c2,
            **{k: v for k, v in args2.items() if k != "trusted_head"})
    assert registry.head == first.successor and len(registry.history) == 1


def test_read_key_cannot_be_bound_to_another_legacy_trading_identity(rig):
    registry = M.MockAppendOnlyRegistry(M.TrustedHead(0, M.ZERO))
    t, r, c, args = signed_bundle(rig)
    first = registry.compare_and_append(
        rig["authorities"], rig["authorities"], t, r, c,
        **{k: v for k, v in args.items() if k != "trusted_head"})
    alternate = ledger.identity("BYBIT", "other-trading-account")
    t2, r2, c2, args2 = signed_bundle(
        rig, trusted_head=first.successor,
        challenge=h("cross-legacy-account-rebind-challenge"),
        trade_patch=dict(key_ref=alternate),
        approval_patch=dict(legacy_ref=alternate))
    args2["expected_legacy_ref"] = alternate
    blocked("BINDING_ALREADY_BOUND", registry.compare_and_append,
            rig["authorities"], rig["authorities"], t2, r2, c2,
            **{k: v for k, v in args2.items() if k != "trusted_head"})
    assert registry.head == first.successor and len(registry.history) == 1


def test_duplicate_same_binding_new_sequence_is_not_implicit_rotation_or_reapproval(rig):
    registry = M.MockAppendOnlyRegistry(M.TrustedHead(0, M.ZERO))
    t, r, c, args = signed_bundle(rig)
    first = registry.compare_and_append(
        rig["authorities"], rig["authorities"], t, r, c,
        **{k: v for k, v in args.items() if k != "trusted_head"})
    t2, r2, c2, args2 = signed_bundle(
        rig, trusted_head=first.successor, challenge=h("later-reapproval-challenge"))
    blocked("BINDING_ALREADY_BOUND", registry.compare_and_append,
            rig["authorities"], rig["authorities"], t2, r2, c2,
            **{k: v for k, v in args2.items() if k != "trusted_head"})
    assert registry.head == first.successor and registry.history == (first.successor.chain_hash,)
