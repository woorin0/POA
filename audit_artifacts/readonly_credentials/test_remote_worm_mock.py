"""Offline adversarial mock-network E2E for PR17 external WORM protocol.

No sockets; every 'remote' request passes canonical JSON bytes through
MockNetworkLink to a separately modeled server with no client private keys.
Zero real exchange, operating database, Discord or order execution.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit_artifacts.readonly_credentials import immutable_binding as B
from audit_artifacts.readonly_credentials import remote_worm_mock as R
from audit_artifacts.readonly_credentials.test_immutable_binding import (
    NOW, h, signed_bundle,
)
from audit_artifacts.readonly_credentials.test_independent_witness import (
    scene, signed_witness, good,
)
from exchange.verified_pnl import ledger


@pytest.fixture
def net(scene):
    keys = {role: Ed25519PrivateKey.generate() for role in R.ROLES}
    names = {role: "independent-" + role for role in R.ROLES}
    pubs = {role: R.pubkey(key) for role, key in keys.items()}
    pins = R.PinnedRemoteRoles(
        {role: {"kid": names[role], "public_key": pubs[role]} for role in R.ROLES},
        {role: hashlib.sha256(pubs[role]).hexdigest() for role in R.ROLES})
    server = R.MockRemoteWORMServer(
        pins, scene["rig"]["authorities"], scene["witness"],
        keys["checkpoint"], lambda: NOW)
    link = R.MockNetworkLink(server)
    wa = R.IndependentMockAnchor(pins, scene["rig"]["authorities"], scene["witness"])
    aa = R.IndependentMockAnchor(pins, scene["rig"]["authorities"], scene["witness"])
    writer = R.MockRemoteClient(role="writer", private_key=keys["writer"],
                                pins=pins, link=link, anchor=wa)
    auditor = R.MockRemoteClient(role="auditor", private_key=keys["auditor"],
                                 pins=pins, link=link, anchor=aa)
    return {"scene": scene, "keys": keys, "names": names, "pins": pins,
            "server": server, "link": link,
            "writer": writer, "auditor": auditor,
            "wanchor": wa, "aanchor": aa}


def error(code, call, *args, **kwargs):
    with pytest.raises(B.BindingBlocked) as err:
        call(*args, **kwargs)
    assert err.value.code == code
    assert str(err.value) == code


def evidence_for(net, *, head=None, challenge=None, new_account=False):
    head = head or B.TrustedHead(0, B.ZERO)
    if new_account:
        venue = "BYBIT"
        tkey = ledger.identity(venue, "second-unrelated-trading-key")
        rkey = ledger.identity(venue, "second-unrelated-read-key")
        pair = signed_bundle(
            net["scene"]["rig"], venue=venue, scope="spot", trusted_head=head,
            challenge=challenge or h("distinct-approved-second-nonce"),
            account=h("independently-observed-second-account"),
            subaccount=h("independently-observed-second-subaccount"),
            trade_patch={"key_ref": tkey}, read_patch={"key_ref": rkey},
            approval_patch={"legacy_ref": tkey, "read_ref": rkey})
        pair[3]["expected_legacy_ref"] = tkey
        pair[3]["expected_read_ref"] = rkey
    else:
        pair, _ = good(net["scene"])
    trade, read, approval, context = pair
    witnessed = signed_witness(net["scene"], pair)
    ev = {"trade": trade, "read": read, "approval": approval,
          "witness": witnessed,
          "context": {k: v for k, v in context.items()
                      if k not in ("trusted_head", "now")}}
    return ev


def request(net, *, nonce="first-approved-request", head=None, new_account=False,
            approved=True):
    expected = head if head is not None else net["server"].head
    ev = evidence_for(net, head=expected, new_account=new_account)
    frame = net["writer"].make_append_frame(h(nonce), ev, expected)
    if approved:
        frame["grant"] = R.operator_grant(
            net["keys"]["operator"], net["pins"],
            net["scene"]["rig"]["authorities"],
            net["scene"]["witness"], frame, issued_at=NOW-1, expires_at=NOW+60)
    return frame


def first_commit(net):
    net["writer"].query(h("probe-initial-head"))
    frame = request(net)
    out = net["writer"].append(frame)
    assert out.sequence == 1
    return frame


def second_commit(net):
    first_commit(net)
    oldhead = net["server"].head
    frame = request(net, nonce="second-independent-grant", head=oldhead,
                    new_account=True)
    out = net["writer"].append(frame)
    assert out.sequence == 2
    return frame


def rawframe(net, frame):
    return net["server"].dispatch(R.encode(frame))


@pytest.mark.parametrize("venue,scope", [
    ("UPBIT", "spot"), ("BINANCE", "spot"), ("BYBIT", "linear"), ("OKX", "spot")])
def test_signed_cas_and_client_auditor_checkpoint_mock_network(net, venue, scope):
    initial = net["auditor"].query(h("audit-before-append"))
    assert initial == B.TrustedHead(0, B.ZERO)
    net["writer"].query(h("writer-before-append"))
    if venue == "BYBIT" and scope == "spot":
        ev = evidence_for(net)
    else:
        pair = signed_bundle(net["scene"]["rig"], venue=venue, scope=scope)
        ev = {"trade": pair[0], "read": pair[1], "approval": pair[2],
              "witness": signed_witness(net["scene"], pair),
              "context": {k: v for k, v in pair[3].items()
                          if k not in ("trusted_head", "now")}}
    wframe = net["writer"].make_append_frame(h("unique" + venue + scope),
                                                ev, net["writer"].anchor.floor)
    wframe["grant"] = R.operator_grant(
        net["keys"]["operator"], net["pins"], net["scene"]["rig"]["authorities"],
        net["scene"]["witness"], wframe, issued_at=NOW - 1, expires_at=NOW + 50)
    newhead = net["writer"].append(wframe)
    assert newhead == net["server"].head and newhead.sequence == 1
    assert net["auditor"].query(h("audit-after-" + venue + scope)) == newhead
    assert len(net["server"].records) == 1
    assert net["server"].records[0]["request_digest"] == B.digest(wframe)


def test_sequential_independent_accounts_extend_signed_chain_and_auditor_sees_it(net):
    second_commit(net)
    head = net["server"].head
    assert head.sequence == 2
    assert net["auditor"].query(h("auditor-third-party-verifies-two")) == head
    assert net["aanchor"].floor == head
    entries = net["server"].records
    assert entries[0]["chain_hash"] == entries[1]["previous"]


def test_writer_is_unable_to_grant_its_own_append(net):
    frame = request(net, approved=False)
    error("REMOTE_ROLE_FORBIDDEN", rawframe, net, frame)
    assert net["server"].head.sequence == 0


def test_auditor_cannot_construct_or_submit_append(net):
    error("REMOTE_CLIENT_ROLE_FORBIDDEN", net["auditor"].make_append_frame,
          h("not-writer"), evidence_for(net), net["server"].head)
    error("REMOTE_CLIENT_ROLE_FORBIDDEN", net["auditor"].append, request(net))


def test_operator_cannot_impersonate_writer_with_its_private_key(net):
    error("REMOTE_CLIENT_ROLE_FORBIDDEN", R.MockRemoteClient,
          role="writer", private_key=net["keys"]["operator"], pins=net["pins"],
          link=net["link"], anchor=net["wanchor"])
    forged = request(net)
    forged["writer"] = R.make_envelope(net["keys"]["operator"],
                                     kid=net["pins"].kid("writer"), kind="APPEND_PROOF",
                                     body=forged["writer"]["body"])
    error("REMOTE_SIGNATURE_INVALID", rawframe, net, forged)


def test_writer_private_key_cannot_forge_operator_grant(net):
    frame = request(net)
    frame["grant"] = R.make_envelope(net["keys"]["writer"],
                                    kid=net["pins"].kid("operator"), kind="APPEND_GRANT",
                                    body=frame["grant"]["body"])
    error("REMOTE_SIGNATURE_INVALID", rawframe, net, frame)
    assert net["server"].head.sequence == 0


def test_writer_role_cannot_sign_as_auditor(net):
    bad = {"op": "HEAD", "actor": "auditor",
           "query": R.make_envelope(net["keys"]["writer"],
               kid=net["pins"].kid("auditor"), kind="CHECKPOINT_QUERY",
               body={"nonce": h("pretend-auditor")})}
    error("REMOTE_SIGNATURE_INVALID", rawframe, net, bad)


def test_operator_grant_scope_digest_replacement_is_rejected(net):
    frame = request(net)
    frame["writer"]["body"]["expected"]["sequence"] = 1
    error("REMOTE_SIGNATURE_INVALID", rawframe, net, frame)


@pytest.mark.parametrize("changed", [
    {"request_id": h("other-grant")},
    {"writer_digest": h("forged-writer")},
    {"operator_roots": h("forged-operator-roots")},
    {"source_roots": h("forged-observer-roots")},
    {"witness_root": h("forged-witness-root")},
    {"expected": {"sequence": 1, "chain_hash": h("wrong-head")}},
])
def test_even_independently_signed_operator_grant_mismatch_denied(net, changed):
    frame = request(net)
    grant = {**frame["grant"]["body"], **changed}
    frame["grant"] = R.make_envelope(net["keys"]["operator"],
            kid=net["pins"].kid("operator"), kind="APPEND_GRANT", body=grant)
    error("REMOTE_OPERATOR_GRANT_MISMATCH", rawframe, net, frame)


@pytest.mark.parametrize("expiry", [NOW-1, NOW+301])
def test_expired_or_unbounded_operator_grant_denied(net, expiry):
    frame = request(net)
    grant = {**frame["grant"]["body"], "expires_at": expiry}
    frame["grant"] = R.make_envelope(net["keys"]["operator"],
                  kid=net["pins"].kid("operator"), kind="APPEND_GRANT", body=grant)
    error("BINDING_STALE_OR_FUTURE", rawframe, net, frame)


def test_valid_operator_grant_cannot_be_replayed_for_other_writer_request(net):
    frame = request(net)
    original_grant = frame["grant"]
    other = request(net, nonce="different-writer-request-id")
    other["grant"] = original_grant
    error("REMOTE_OPERATOR_GRANT_MISMATCH", rawframe, net, other)


def test_missing_or_bad_witness_prevents_remote_commit(net):
    frame = request(net)
    frame["writer"]["body"]["evidence"]["witness"]["signature"] = "a" * 88
    frame["writer"] = R.make_envelope(net["keys"]["writer"], kid=net["pins"].kid("writer"),
                              kind="APPEND_PROOF", body=frame["writer"]["body"])
    frame["grant"] = R.operator_grant(
            net["keys"]["operator"], net["pins"], net["scene"]["rig"]["authorities"],
            net["scene"]["witness"], frame, issued_at=NOW - 1, expires_at=NOW + 60)
    error("WITNESS_SIGNATURE_INVALID", rawframe, net, frame)
    assert net["server"].head.sequence == 0


def test_cas_requires_same_external_highwater_before_append(net):
    first_commit(net)
    second = request(net, nonce="stale-cas",
                     head=B.TrustedHead(0, B.ZERO), new_account=True)
    error("REMOTE_CAS_CONFLICT", rawframe, net, second)
    error("REMOTE_CLIENT_STALE_ANCHOR", net["writer"].append, second)
    assert net["server"].head.sequence == 1


def test_before_commit_transport_failure_leaves_remote_record_unwritten(net):
    net["writer"].query(h("before-commit-head"))
    frame = request(net)
    net["server"].fail_before_commit_once = True
    with pytest.raises(R.MockLinkLost, match="before atomic commit"):
        net["writer"].append(frame)
    assert net["server"].head == B.TrustedHead(0, B.ZERO)
    assert net["server"].records == ()
    head = net["writer"].append(frame)
    assert head.sequence == 1 and len(net["server"].records) == 1


def test_mock_network_drop_before_send_retry_never_commits_twice(net):
    net["writer"].query(h("before-drop"))
    frame = request(net)
    net["link"].drop_before_once = True
    with pytest.raises(R.MockLinkLost):
        net["writer"].append(frame)
    assert net["server"].head.sequence == 0
    assert net["writer"].append(frame).sequence == 1
    assert len(net["server"].records) == 1


def test_mock_network_dropped_response_after_atomic_commit_idempotent_retry(net):
    net["writer"].query(h("before-dropped-ack"))
    frame = request(net)
    net["link"].drop_after_once = True
    with pytest.raises(R.MockLinkLost, match="after server processing"):
        net["writer"].append(frame)
    assert net["server"].head.sequence == 1 and len(net["server"].records) == 1
    assert net["wanchor"].floor.sequence == 0
    assert net["writer"].append(frame).sequence == 1
    assert len(net["server"].records) == 1


def test_duplicate_same_signed_frame_is_idempotent_no_second_append(net):
    net["writer"].query(h("before-duplicate"))
    frame = request(net)
    assert net["writer"].append(frame).sequence == 1
    assert net["writer"].append(frame).sequence == 1
    assert len(net["server"].records) == 1


def test_same_request_id_different_signed_payload_rejected(net):
    first_commit(net)
    second = request(net, nonce="first-approved-request", head=net["server"].head,
                     new_account=True)
    error("REMOTE_REQUEST_ID_CONFLICT", rawframe, net, second)
    assert len(net["server"].records) == 1


def test_same_seq_distinct_valid_requests_racing_commit_only_one(net):
    old = net["server"].head
    frames = [request(net, nonce="race-" + str(i), head=old) for i in range(20)]
    def run(frame):
        try:
            rawframe(net, frame)
            return "COMMITTED"
        except B.BindingBlocked as e:
            return e.code
    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(run, frames))
    assert outcomes.count("COMMITTED") == 1
    assert outcomes.count("REMOTE_CAS_CONFLICT") == 19
    assert len(net["server"].records) == 1 and net["server"].head.sequence == 1


def test_attacker_cannot_post_unsigned_append_or_anonymous_read(net):
    error("REMOTE_ROLE_FORBIDDEN", rawframe, net, {"op": "APPEND"})
    error("REMOTE_ROLE_FORBIDDEN", rawframe, net,
          {"op": "HEAD", "actor": "operator", "query": {}})
    error("REMOTE_OPERATION_INVALID", rawframe, net, {"op": "HEAD"})


@pytest.mark.parametrize("payload,code", [
    (b'{"op":"HEAD","op":"APPEND"}', "REMOTE_FRAME_INVALID"),
    (b'{"op":"HEAD", "actor":null}', "REMOTE_FRAME_NONCANONICAL"),
    (b'{"op":NaN}', "REMOTE_FRAME_INVALID"),
    (b"", "REMOTE_FRAME_INVALID"),
    (b"{" * 22000, "REMOTE_FRAME_INVALID"),
    (b'{"op":"\xff"}', "REMOTE_FRAME_INVALID"),
])
def test_noncanonical_duplicate_invalid_or_oversize_wire_is_rejected(net, payload, code):
    error(code, net["server"].dispatch, payload)


def test_tampered_checkpoints_are_denied_and_do_not_advance_independent_anchor(net):
    first_commit(net)
    assert net["wanchor"].floor.sequence == 1
    def tamper(raw):
        changed = R.strict_decode(raw)
        changed["checkpoint"]["body"]["sequence"] = 2
        return R.encode(changed)
    net["link"].response_transform = tamper
    error("REMOTE_SIGNATURE_INVALID", net["writer"].query, h("tampered-checkpoint"))
    assert net["wanchor"].floor.sequence == 1


def test_server_side_record_tamper_is_detected_by_signed_log_digest(net):
    first_commit(net)
    def tamper(raw):
        changed = R.strict_decode(raw)
        changed["entries"][0]["binding_digest"] = h("attack")
        return R.encode(changed)
    net["link"].response_transform = tamper
    error("REMOTE_ENTRIES_DIGEST_INVALID", net["writer"].query, h("alter-log-record"))


def test_duplicate_replayed_signed_checkpoint_response_fails_new_nonce(net):
    net["writer"].query(h("before-replay"))
    cache = {}
    def store(raw):
        cache["old"] = raw
        return raw
    net["link"].response_transform = store
    net["writer"].query(h("first-request-nonce"))
    net["link"].response_transform = lambda raw: cache["old"]
    error("REMOTE_RESPONSE_REPLAY", net["writer"].query, h("second-request-nonce"))


def test_remote_server_full_reset_rejected_by_independent_highwater(net):
    first_commit(net)
    assert net["wanchor"].floor.sequence == 1
    # Adversary controls the server's local snapshot, not the remote auditor floor.
    net["link"].server = R.MockRemoteWORMServer(
        net["pins"], net["scene"]["rig"]["authorities"],
        net["scene"]["witness"], net["keys"]["checkpoint"], lambda: NOW)
    error("REMOTE_ROLLBACK_DETECTED", net["writer"].query, h("reset-server"))
    assert net["wanchor"].floor.sequence == 1


def test_exact_valid_older_server_snapshot_rejected_after_second_commit(net):
    first_commit(net)
    saved = R.MockRemoteWORMServer(
        net["pins"], net["scene"]["rig"]["authorities"],
        net["scene"]["witness"], net["keys"]["checkpoint"], lambda: NOW)
    saved._head = net["server"].head
    saved._records = net["server"].records
    old_server = net["link"].server
    # A separate full second append advances the independently held head.
    now_head = net["server"].head
    frame = request(net, nonce="second-independent-record", head=now_head,
                    new_account=True)
    net["writer"].append(frame)
    assert net["wanchor"].floor.sequence == 2
    net["link"].server = saved
    error("REMOTE_ROLLBACK_DETECTED", net["writer"].query, h("old-snapshot"))
    net["link"].server = old_server


def test_signer_stolen_can_forge_split_view_but_independent_anchor_rejects(net):
    first_commit(net)
    def signed_fork(raw):
        changed = R.strict_decode(raw)
        changed["entries"][0]["chain_hash"] = h("forked-new-head")
        body = changed["checkpoint"]["body"]
        body["chain_hash"] = changed["entries"][0]["chain_hash"]
        body["entries_digest"] = B.digest(changed["entries"])
        changed["checkpoint"] = R.make_envelope(
            net["keys"]["checkpoint"], kid=net["pins"].kid("checkpoint"),
            kind="SIGNED_CHECKPOINT", body=body)
        return R.encode(changed)
    net["link"].response_transform = signed_fork
    error("REMOTE_SPLIT_VIEW", net["writer"].query, h("split-view-attack"))


def test_equal_tip_with_rewritten_history_rejected_via_prefix_anchor(net):
    second_commit(net)
    assert net["wanchor"].floor.sequence == 2
    # Re-signed checkpoint from a compromised server signing key with same tip,
    # changed historical entry. This time all entry hashes remain syntactically valid.
    def forged_history(raw):
        changed = R.strict_decode(raw)
        changed["entries"][0]["binding_digest"] = h("forked-historical-proof")
        body = changed["checkpoint"]["body"]
        body["entries_digest"] = B.digest(changed["entries"])
        changed["checkpoint"] = R.make_envelope(
            net["keys"]["checkpoint"], kid=net["pins"].kid("checkpoint"),
            kind="SIGNED_CHECKPOINT", body=body)
        return R.encode(changed)
    net["link"].response_transform = forged_history
    error("REMOTE_PREFIX_REWRITTEN", net["writer"].query, h("forged-prefix"))


def test_root_key_ownership_and_checkpoint_operator_keys_are_separate(net):
    with pytest.raises(TypeError):
        net["pins"]._roles["writer"] = net["pins"]._roles["operator"]
    error("REMOTE_ROOT_IMMUTABLE", setattr, net["pins"], "_roles", {})
    error("REMOTE_ROOT_IMMUTABLE", setattr, net["pins"], "_commitment", h("evil"))
    assert len({net["pins"].public_bytes(role) for role in R.ROLES}) == 4
    assert net["pins"].commitment == net["server"]._pins.commitment


def test_replace_checkpoint_root_without_independent_pin_fails_closed(net):
    attacker = Ed25519PrivateKey.generate()
    error("REMOTE_SIGNER_PIN_MISMATCH", R.MockRemoteWORMServer,
          net["pins"], net["scene"]["rig"]["authorities"],
          net["scene"]["witness"], attacker, lambda: NOW)


def test_replace_all_remote_roles_and_self_signed_pins_does_not_pass_original_client(net):
    evil = {role: Ed25519PrivateKey.generate() for role in R.ROLES}
    pubs = {role: R.pubkey(evil[role]) for role in R.ROLES}
    forged = R.PinnedRemoteRoles(
        {role: {"kid": net["names"][role], "public_key": pubs[role]}
         for role in R.ROLES},
        {role: hashlib.sha256(pubs[role]).hexdigest() for role in R.ROLES})
    attacker_server = R.MockRemoteWORMServer(
        forged, net["scene"]["rig"]["authorities"], net["scene"]["witness"],
        evil["checkpoint"], lambda: NOW)
    net["link"].server = attacker_server
    error("REMOTE_SIGNATURE_INVALID", net["writer"].query, h("replaced-root"))


def test_operator_private_key_must_not_equal_any_source_witness_private_key(net):
    stolen = net["scene"]["private"]
    error("REMOTE_ROOT_ROLE_COLLISION", R.PinnedRemoteRoles(
        {role: {"kid": net["names"][role],
                "public_key": (R.pubkey(stolen) if role == "operator"
                               else net["pins"].public_bytes(role))}
         for role in R.ROLES},
        {role: hashlib.sha256(
            R.pubkey(stolen) if role == "operator" else net["pins"].public_bytes(role)
         ).hexdigest() for role in R.ROLES}).validate_disjoint,
         net["scene"]["rig"]["authorities"], net["scene"]["witness"])


def test_full_server_and_external_anchor_reset_is_undetectable_in_memory_mock(net):
    """Explicit UNSOLVED residual: only an independently durable store helps."""
    first_commit(net)
    fresh = R.MockRemoteWORMServer(
        net["pins"], net["scene"]["rig"]["authorities"],
        net["scene"]["witness"], net["keys"]["checkpoint"], lambda: NOW)
    net["link"].server = fresh
    # Attacker cannot reset a genuine independently-operated WORM high-water;
    # both resets are possible here because BOTH stores are mock memory.
    new_anchor = R.IndependentMockAnchor(
        net["pins"], net["scene"]["rig"]["authorities"], net["scene"]["witness"])
    another = R.MockRemoteClient(
        role="writer", private_key=net["keys"]["writer"],
        pins=net["pins"], link=net["link"], anchor=new_anchor)
    assert another.query(h("fresh-unanchored-client")).sequence == 0
    assert net["wanchor"].floor.sequence == 1
    error("REMOTE_ROLLBACK_DETECTED", net["writer"].query, h("independent-anchor-persists"))


def test_failed_append_never_activates_live_product(net):
    pair, witness_record = good(net["scene"])
    p = B.verify_pair(net["scene"]["rig"]["authorities"],
                      net["scene"]["rig"]["authorities"],
                      *pair[:3], **pair[3])
    assert p.operationally_authorized is False
    error("BINDING_LIVE_ACTIVATION_BLOCKED",
          B.require_operational_activation, p)
