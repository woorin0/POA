"""Persistent SQLite/WAL and separate auditor checkpoint adversarial CI.

Unlike PR18/19's in-memory Mock, this test opens real SQLite files, exercises
WAL FULL transactions and verifies committed rows from a SECOND OS process.
All network frames still go through an isolated no-socket MockNetworkLink.
The SQLite owner CAN drop triggers or roll back both DB files: not real WORM.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from audit_artifacts.readonly_credentials import durable_sqlite_worm as D
from audit_artifacts.readonly_credentials import remote_worm_mock as R
from audit_artifacts.readonly_credentials import immutable_binding as B
from audit_artifacts.readonly_credentials import independent_witness as W
from audit_artifacts.readonly_credentials.test_independent_witness import scene
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from audit_artifacts.readonly_credentials.test_remote_worm_mock import (
    net, request, first_commit, second_commit, rawframe, evidence_for,
    NOW, h,
)


@pytest.fixture
def disk(net, tmp_path):
    store_dir = tmp_path / "storage_operator"
    audit_dir = tmp_path / "independent_auditor"
    writer_dir = tmp_path / "independent_writer"
    for path in (store_dir, audit_dir, writer_dir):
        path.mkdir()
    server_db = store_dir / "immutable_store.sqlite3"
    auditor_db = audit_dir / "auditor_monotonic.sqlite3"
    writer_db = writer_dir / "writer_checkpoint.sqlite3"
    r = D.DurableSQLiteWORMServer(
        server_db, create=True, pins=net["pins"],
        sources=net["scene"]["rig"]["authorities"],
        witness=net["scene"]["witness"],
        checkpoint_private=net["keys"]["checkpoint"], clock=lambda: NOW)
    link = R.MockNetworkLink(r)
    keys = net["keys"]
    audit = D.DurableSQLiteAuditor(
        auditor_db, create=True, pins=net["pins"],
        sources=net["scene"]["rig"]["authorities"],
        witness=net["scene"]["witness"])
    writer = D.DurableSQLiteAuditor(
        writer_db, create=True, pins=net["pins"],
        sources=net["scene"]["rig"]["authorities"],
        witness=net["scene"]["witness"])
    net.update(server=r, link=link, writer=R.MockRemoteClient(
        role="writer", private_key=keys["writer"], pins=net["pins"],
        link=link, anchor=writer), auditor=R.MockRemoteClient(
        role="auditor", private_key=keys["auditor"], pins=net["pins"],
        link=link, anchor=audit), wanchor=writer, aanchor=audit,
        server_db=server_db, auditor_db=auditor_db, writer_db=writer_db)
    return net


def block(code, fn, *args, **kwargs):
    with pytest.raises(B.BindingBlocked) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code


def reopen_server(disk, *, path=None, create=False):
    return D.DurableSQLiteWORMServer(
        path or disk["server_db"], create=create,
        pins=disk["pins"], sources=disk["scene"]["rig"]["authorities"],
        witness=disk["scene"]["witness"],
        checkpoint_private=disk["keys"]["checkpoint"],
        clock=lambda: NOW)


def reopen_auditor(disk, *, path=None, create=False):
    return D.DurableSQLiteAuditor(
        path or disk["auditor_db"], create=create,
        pins=disk["pins"], sources=disk["scene"]["rig"]["authorities"],
        witness=disk["scene"]["witness"])


def reopen_writer(disk):
    return D.DurableSQLiteAuditor(
        disk["writer_db"], create=False, pins=disk["pins"],
        sources=disk["scene"]["rig"]["authorities"],
        witness=disk["scene"]["witness"])


def query_child_sqlite(path):
    """The OS child reads actually committed disk bytes, not in-memory Python."""
    proc = subprocess.run(
        [sys.executable, "-B", "-c",
         "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]);"
         "print(c.execute('SELECT COUNT(*) FROM immutable_entries').fetchone()[0]);"
         "c.close()", str(path)],
        check=True, capture_output=True, text=True, timeout=15)
    return int(proc.stdout.strip())


def test_genesis_is_real_sqlite_file_not_in_memory_mock(disk):
    assert disk["server_db"].is_file()
    assert disk["auditor_db"].is_file()
    assert disk["writer_db"].is_file()
    assert query_child_sqlite(disk["server_db"]) == 0
    with sqlite3.connect(disk["server_db"]) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0] >= 4


def test_signed_commit_survives_new_server_and_independent_auditor_instances(disk):
    first_commit(disk)
    assert query_child_sqlite(disk["server_db"]) == 1
    assert disk["auditor"].query(h("persist-independent-auditor")).sequence == 1
    persisted = reopen_server(disk)
    writer = reopen_writer(disk)
    auditor = reopen_auditor(disk)
    assert persisted.inspect_disk()[0].sequence == 1
    assert len(persisted.inspect_disk()[1]) == 1
    assert writer.floor.sequence == 1 and auditor.floor.sequence == 1
    disk["link"].server = persisted
    assert disk["writer"].query(h("new-server-same-owner")).sequence == 1
    assert disk["auditor"].query(h("new-server-external-auditor")).sequence == 1


def test_sqlite_rows_remain_visible_after_process_exits(disk):
    first_commit(disk)
    assert query_child_sqlite(disk["server_db"]) == 1
    with sqlite3.connect(disk["server_db"]) as con:
        assert con.execute("SELECT MIN(sequence), MAX(sequence) FROM immutable_entries").fetchone() == (1, 1)


def test_after_restart_same_commit_replay_stays_idempotent(disk):
    disk["writer"].query(h("before-signed-append"))
    frame = request(disk)
    assert disk["writer"].append(frame).sequence == 1
    disk["link"].server = reopen_server(disk)
    assert disk["writer"].append(frame).sequence == 1
    assert query_child_sqlite(disk["server_db"]) == 1


def test_response_lost_after_disk_commit_exact_retry_after_restart(disk):
    disk["writer"].query(h("before-network-ack-loss"))
    frame = request(disk)
    disk["link"].drop_after_once = True
    with pytest.raises(R.MockLinkLost):
        disk["writer"].append(frame)
    assert query_child_sqlite(disk["server_db"]) == 1
    disk["link"].server = reopen_server(disk)
    assert disk["writer"].append(frame).sequence == 1
    assert query_child_sqlite(disk["server_db"]) == 1


def test_failure_before_sqlite_commit_releases_all_cas_slots(disk):
    disk["writer"].query(h("durable-before-crash"))
    frame = request(disk)
    disk["server"].fail_before_disk_commit_once = True
    with pytest.raises(R.MockLinkLost, match="before sqlite COMMIT"):
        disk["writer"].append(frame)
    assert query_child_sqlite(disk["server_db"]) == 0
    assert reopen_server(disk).head.sequence == 0
    assert disk["writer"].append(frame).sequence == 1
    assert query_child_sqlite(disk["server_db"]) == 1


def test_prepared_but_unpublished_in_memory_state_is_not_committed(disk):
    disk["writer"].query(h("durable-prepare-stage"))
    frame = request(disk)
    disk["server"].fail_after_prepare_once = True
    with pytest.raises(R.MockLinkLost, match="after prepare"):
        disk["writer"].append(frame)
    assert query_child_sqlite(disk["server_db"]) == 0
    assert reopen_server(disk).head.sequence == 0
    assert disk["writer"].append(frame).sequence == 1


def test_stale_cas_fails_after_reopening_store(disk):
    first_commit(disk)
    disk["link"].server = reopen_server(disk)
    stale = request(disk, nonce="stale-after-restart",
                    head=B.TrustedHead(0, B.ZERO), new_account=True)
    block("REMOTE_CAS_CONFLICT", rawframe, disk, stale)
    assert query_child_sqlite(disk["server_db"]) == 1


def test_cross_instance_concurrent_writers_one_durable_cas_winner(disk):
    a = disk["server"]
    b = reopen_server(disk)
    genesis = B.TrustedHead(0, B.ZERO)
    frames = [request(disk, nonce="concurrent-durable-" + str(i),
                      head=genesis, new_account=(i % 2 == 0))
              for i in range(24)]
    def run(i):
        try:
            srv = a if i % 2 == 0 else b
            srv.dispatch(R.encode(frames[i]))
            return "COMMIT"
        except B.BindingBlocked as err:
            return err.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        out = list(pool.map(run, range(len(frames))))
    assert out.count("COMMIT") == 1
    assert out.count("REMOTE_CAS_CONFLICT") == 23
    assert query_child_sqlite(disk["server_db"]) == 1
    assert reopen_server(disk).head.sequence == 1


def test_sequential_two_real_durable_entries_and_signed_auditor_checkpoint(disk):
    second_commit(disk)
    assert query_child_sqlite(disk["server_db"]) == 2
    assert disk["auditor"].query(h("auditor-after-two-sqlite-commits")).sequence == 2
    persisted = reopen_auditor(disk)
    assert persisted.floor.sequence == 2
    with sqlite3.connect(disk["auditor_db"]) as con:
        assert con.execute("SELECT COUNT(*) FROM immutable_checkpoints").fetchone()[0] == 3


@pytest.mark.parametrize("table", ["immutable_entries", "immutable_metadata"])
@pytest.mark.parametrize("action", ["DELETE", "UPDATE"])
def test_database_sql_triggers_reject_delete_or_rewrite(disk, table, action):
    first_commit(disk)
    with sqlite3.connect(disk["server_db"]) as con:
        with pytest.raises(sqlite3.DatabaseError, match="IMMUTABLE_STORAGE_MUTATION_DENIED"):
            if action == "DELETE":
                con.execute(f"DELETE FROM {table}")
            else:
                column = "entry" if table == "immutable_entries" else "content"
                con.execute(f"UPDATE {table} SET {column}={column}")
    assert query_child_sqlite(disk["server_db"]) == 1


@pytest.mark.parametrize("action", ["DELETE", "UPDATE"])
def test_independent_auditor_checkpoint_sql_triggers_fail_closed(disk, action):
    first_commit(disk)
    disk["auditor"].query(h("audit-pin-before-tamper"))
    with sqlite3.connect(disk["auditor_db"]) as con:
        with pytest.raises(sqlite3.DatabaseError, match="IMMUTABLE_STORAGE_MUTATION_DENIED"):
            if action == "DELETE":
                con.execute("DELETE FROM immutable_checkpoints WHERE sequence=1")
            else:
                con.execute("UPDATE immutable_checkpoints SET chain_hash=? WHERE sequence=1", (h("forged"),))
    assert reopen_auditor(disk).floor.sequence == 1


def test_explicit_delete_of_actual_db_file_is_refused_on_safe_open(disk):
    # No secret API or filesystem daemon protects this test directory.
    disk["server_db"].unlink()
    block("DURABLE_STORAGE_MISSING", reopen_server, disk)
    # Durable auditor high-water remains. A new, manually created genesis
    # would be seen as rollback if the independent witness is queried.


def test_recreate_deleted_server_from_genesis_detected_by_untouched_auditor(disk):
    first_commit(disk)
    disk["auditor"].query(h("pinned-before-object-delete"))
    disk["server_db"].unlink()
    fresh = reopen_server(disk, create=True)
    disk["link"].server = fresh
    block("REMOTE_ROLLBACK_DETECTED", disk["auditor"].query, h("after-delete"))


def test_manual_single_file_rollback_detected_after_restart_by_auditor(disk, tmp_path):
    first_commit(disk)
    disk["auditor"].query(h("persist-auditor-seq1"))
    snap = tmp_path / "before-second.sqlite3"
    with sqlite3.connect(disk["server_db"]) as current:
        with sqlite3.connect(snap) as dest:
            current.backup(dest)
    disk["writer"].query(h("writer-before-second"))
    nextframe = request(disk,nonce="second-persisted-approval",
                        head=disk["server"].head,new_account=True)
    assert disk["writer"].append(nextframe).sequence == 2
    disk["auditor"].query(h("auditor-persisted-seq2"))
    assert reopen_auditor(disk).floor.sequence == 2
    # Adversary with full file control overwrites SQLite and WAL sidecars.
    for suffix in ("-wal", "-shm"):
        p = Path(str(disk["server_db"]) + suffix)
        if p.exists():
            p.unlink()
    shutil.copyfile(snap, disk["server_db"])
    restored = reopen_server(disk)
    assert restored.head.sequence == 1
    disk["link"].server = restored
    block("REMOTE_ROLLBACK_DETECTED", disk["auditor"].query, h("old-signed-state"))


def test_forked_durable_stores_detected_by_single_persisted_auditor(disk, tmp_path):
    alpha = disk["server"]
    beta_path = tmp_path / "other_operator.sqlite3"
    beta = reopen_server(disk, path=beta_path, create=True)
    first = request(disk, nonce="first-fork-a")
    other = request(disk, nonce="second-fork-b", new_account=True)
    alpha.dispatch(R.encode(first))
    beta.dispatch(R.encode(other))
    assert alpha.head.sequence == beta.head.sequence == 1
    assert alpha.head.chain_hash != beta.head.chain_hash
    disk["link"].server = alpha
    assert disk["auditor"].query(h("persist-fork-alpha")).sequence == 1
    disk["link"].server = beta
    block("REMOTE_SPLIT_VIEW", disk["auditor"].query, h("persist-fork-beta"))
    assert reopen_auditor(disk).floor == alpha.head


def test_two_owners_without_shared_external_anchors_still_accept_forks(disk, tmp_path):
    alpha = disk["server"]
    beta = reopen_server(disk, path=tmp_path / "forked.sqlite3", create=True)
    alpha.dispatch(R.encode(request(disk, nonce="isolated-owner-a")))
    beta.dispatch(R.encode(request(disk,nonce="isolated-owner-b",new_account=True)))
    assert alpha.head != beta.head
    auditor_b = D.DurableSQLiteAuditor(
        tmp_path / "forked-independent-auditor.sqlite3", create=True,
        pins=disk["pins"], sources=disk["scene"]["rig"]["authorities"],
        witness=disk["scene"]["witness"])
    other_client = R.MockRemoteClient(
        role="auditor", private_key=disk["keys"]["auditor"],
        pins=disk["pins"], link=disk["link"], anchor=auditor_b)
    disk["link"].server = alpha
    assert disk["auditor"].query(h("a-coordinator")).sequence == 1
    disk["link"].server = beta
    assert other_client.query(h("b-uncoordinated")).sequence == 1
    assert disk["aanchor"].floor != auditor_b.floor


def test_signed_checkpoint_forgery_without_durable_entry_is_still_not_storage_proof(disk):
    from audit_artifacts.readonly_credentials.test_pr18_remote_worm_reaudit import (
        InterceptOnly, checkpoint,
    )
    disk["writer"].query(h("before-fabricated-stored-receipt"))
    frame = request(disk)
    ev = frame["writer"]["body"]["evidence"]
    proposal = W.verify_witnessed_pair(
        disk["scene"]["rig"]["authorities"], disk["scene"]["witness"],
        ev["trade"], ev["read"], ev["approval"], ev["witness"],
        trusted_head=B.TrustedHead(0,B.ZERO), now=NOW, **ev["context"])
    entry = {
        "sequence":1, "previous":B.ZERO,
        "chain_hash":proposal.successor.chain_hash,
        "binding_digest":proposal.binding_digest,
        "trade_observation_digest":B.digest(ev["trade"]),
        "read_observation_digest":B.digest(ev["read"]),
        "venue":proposal.venue,"scope":proposal.scope,
        "legacy_ref":proposal.legacy_ref,"read_ref":proposal.read_ref,
        "challenge":proposal.challenge,
        "request_digest":B.digest(frame),
    }
    actual = disk["server"]
    disk["link"].server = InterceptOnly(
        lambda incoming: checkpoint(disk,incoming,[entry],proposal.successor))
    assert disk["writer"].append(frame) == proposal.successor
    assert query_child_sqlite(disk["server_db"]) == 0
    # The pinned auditor holds independent state only if it was updated from
    # a real storage receipt. This forged signer demonstrates missing proof.


def test_restart_with_wrong_role_pins_is_rejected_before_any_success(disk):
    bad_key = R.pubkey(Ed25519PrivateKey.generate())
    role_entries = {role: {"kid":disk["pins"].kid(role),
                           "public_key":bad_key if role=="checkpoint"
                           else disk["pins"].public_bytes(role)} for role in R.ROLES}
    alt = R.PinnedRemoteRoles(role_entries, {
        role: hashlib.sha256(role_entries[role]["public_key"]).hexdigest()
        for role in R.ROLES})
    # Wrong server private signer is denied at constructor if pin mismatch.
    block("REMOTE_SIGNER_PIN_MISMATCH",D.DurableSQLiteWORMServer,
       disk["server_db"],create=False,pins=alt,
       sources=disk["scene"]["rig"]["authorities"],
       witness=disk["scene"]["witness"],
       checkpoint_private=disk["keys"]["checkpoint"],clock=lambda:NOW)


def test_owner_can_drop_triggers_residual_not_true_regulatory_worm(disk):
    first_commit(disk)
    # This counterexample proves database-owner access is too powerful:
    # SQLite SQL triggers are not an externally controlled retention lock.
    with sqlite3.connect(disk["server_db"]) as con:
        con.execute("DROP TRIGGER immutable_entries_delete_blocked")
        con.execute("DELETE FROM immutable_entries")
    assert query_child_sqlite(disk["server_db"]) == 0
    assert reopen_server(disk).head.sequence == 0
    # No physical WORM or Object Lock guarantee is claimed.


def test_external_highwater_if_both_files_rolled_back_remains_unsolved(disk):
    first_commit(disk)
    disk["auditor"].query(h("persist-prior-highwater"))
    for name in ("server_db", "auditor_db", "writer_db"):
        disk[name].unlink()
    fresh = reopen_server(disk, create=True)
    fresh_a = reopen_auditor(disk, create=True)
    assert fresh.head.sequence == 0
    assert fresh_a.floor.sequence == 0
    # Full administrative storage compromise still violates immutability.
