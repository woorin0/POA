"""Disk-backed, isolated PR19 WORM *protocol prototype*, never production WORM.

An actual SQLite file with WAL/FULL commits, append-denying SQL triggers,
monotonic/CAS uniqueness and independent auditor database persists across
process restart. Server replays and verifies the PR17/18 original signed
binding evidence on every open. Network remains a NO-SOCKET Mock link.

Security boundary: this is software-enforced logical append-only storage.
A filesystem administrator can remove SQLite triggers, rewrite/delete DB/WAL,
roll back BOTH files or steal signing keys. No physical/legal WORM retention,
cloud Object Lock or externally independent operator is asserted or provided.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from . import immutable_binding as B
from . import independent_witness as W
from . import remote_worm_mock as R

SCHEMA_VERSION = "POA_LOCAL_DURABLE_WORM_AUDIT_V1"


def blocked(code):
    B.deny(code)


def _open(path):
    con = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _new_or_existing(path, create):
    path = Path(path)
    if not path.is_absolute() or not path.parent.is_dir():
        blocked("DURABLE_STORAGE_PATH_INVALID")
    if create:
        if path.exists():
            blocked("DURABLE_STORAGE_ALREADY_EXISTS")
    elif not path.is_file():
        blocked("DURABLE_STORAGE_MISSING")
    return path


def _digest(value):
    return B.digest(value)


def _root_meta(pins, sources, witness, *, label):
    return {
        "version": SCHEMA_VERSION,
        "role": label,
        "operator_roots": pins.commitment,
        "source_roots": sources.root_commitment(),
        "witness_root": witness._root_commitment,
    }


def _initialize(con, meta, *, auditor=False):
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("CREATE TABLE immutable_metadata ("
                    "id INTEGER PRIMARY KEY CHECK(id=1), content TEXT NOT NULL)")
        con.execute("INSERT INTO immutable_metadata VALUES(1, ?)",
                    (R.encode(meta).decode("ascii"),))
        if auditor:
            con.execute(
                "CREATE TABLE immutable_checkpoints ("
                "sequence INTEGER PRIMARY KEY CHECK(sequence >= 0), "
                "chain_hash TEXT NOT NULL, prefix_digest TEXT NOT NULL)")
            con.execute("INSERT INTO immutable_checkpoints VALUES(0, ?, ?)",
                        (B.ZERO, _digest([])))
            tab = "immutable_checkpoints"
        else:
            con.execute(
                "CREATE TABLE immutable_entries ("
                "sequence INTEGER PRIMARY KEY CHECK(sequence > 0), "
                "request_id TEXT UNIQUE NOT NULL, frame BLOB NOT NULL, "
                "entry BLOB NOT NULL, committed_at INTEGER NOT NULL)")
            tab = "immutable_entries"
        for table in ("immutable_metadata", tab):
            for action in ("UPDATE", "DELETE"):
                con.execute(
                    f"CREATE TRIGGER {table}_{action.lower()}_blocked "
                    f"BEFORE {action} ON {table} BEGIN "
                    f"SELECT RAISE(ABORT,'IMMUTABLE_STORAGE_MUTATION_DENIED'); END")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _validate_meta(con, expected):
    try:
        row = con.execute("SELECT content FROM immutable_metadata WHERE id=1").fetchone()
        if not row or R.strict_decode(row[0].encode("ascii")) != expected:
            blocked("DURABLE_TRUST_ROOT_OR_SCHEMA_CHANGED")
    except (sqlite3.Error, TypeError, UnicodeError, ValueError):
        blocked("DURABLE_TRUST_ROOT_OR_SCHEMA_CHANGED")


class DurableSQLiteWORMServer(R.MockRemoteWORMServer):
    """Real transactional disk persistence, NOT immutable hardware/object storage.

    Remote CAS protocol reuses the unchanged PR19 MockRemoteWORMServer signature
    validator and signed-checkpoint format. Each dispatch loads the currently
    committed DB state *inside* SQLite BEGIN IMMEDIATE (cross-process CAS)
    before performing the original authenticated dispatch. One INSERT and the
    SQLite transaction constitute the durable commit. A crash before COMMIT
    leaves zero new entries; after COMMIT and lost ACK idempotent replay works.
    """

    def __init__(self, path, *, create, pins, sources, witness,
                 checkpoint_private, clock):
        self.path = _new_or_existing(path, create)
        self._disk_lock = threading.RLock()
        self.fail_before_disk_commit_once = False
        super().__init__(pins, sources, witness, checkpoint_private, clock)
        expected = _root_meta(pins, sources, witness, label="server")
        try:
            with _open(self.path) as con:
                if create:
                    _initialize(con, expected)
                _validate_meta(con, expected)
                self._restore(con)
        except sqlite3.Error:
            blocked("DURABLE_STORAGE_INTEGRITY_INVALID")

    def _restore(self, con):
        try:
            rows = con.execute(
                "SELECT sequence,request_id,frame,entry,committed_at "
                "FROM immutable_entries ORDER BY sequence").fetchall()
            if con.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                blocked("DURABLE_STORAGE_INTEGRITY_INVALID")
        except sqlite3.Error:
            blocked("DURABLE_STORAGE_INTEGRITY_INVALID")
        head = B.TrustedHead(0, B.ZERO)
        records = ()
        completed = {}
        challenges, legacy_keys, read_keys = set(), set(), set()
        for expected_seq, (seq, rid, raw_frame, raw_entry, observed_at) in enumerate(rows, 1):
            if type(seq) is not int or seq != expected_seq or type(observed_at) is not int:
                blocked("DURABLE_LEDGER_SEQUENCE_INVALID")
            try:
                frame = R.strict_decode(raw_frame)
                entry = R.strict_decode(raw_entry)
            except (TypeError, ValueError, UnicodeError):
                blocked("DURABLE_LEDGER_FRAME_INVALID")
            if (not R.fields(entry, R.ENTRY_FIELDS)
                    or not R.fields(frame, {"op", "writer", "grant"})
                    or frame["op"] != "APPEND"
                    or entry["sequence"] != expected_seq
                    or entry["previous"] != head.chain_hash
                    or entry["request_digest"] != _digest(frame)):
                blocked("DURABLE_LEDGER_RECORD_INVALID")
            writer = self._pins.verify("writer", frame["writer"], "APPEND_PROOF")
            grant = self._pins.verify("operator", frame["grant"], "APPEND_GRANT")
            if (not R.fields(writer, R.APPEND_FIELDS)
                    or not R.fields(grant, R.GRANT_FIELDS)
                    or writer["request_id"] != rid
                    or writer["expected"] != R.head_dict(head)
                    or grant["request_id"] != rid
                    or grant["writer_digest"] != _digest(frame["writer"])
                    or grant["expected"] != writer["expected"]
                    or grant["operator_roots"] != self._pins.commitment
                    or grant["source_roots"] != self._sources.root_commitment()
                    or grant["witness_root"] != self._witness._root_commitment):
                blocked("DURABLE_LEDGER_APPROVAL_INVALID")
            B.check_time(grant, observed_at)
            ev = writer["evidence"]
            if not R.fields(ev, R.EVIDENCE_FIELDS) or not R.fields(ev["context"], R.CONTEXT_FIELDS):
                blocked("DURABLE_LEDGER_EVIDENCE_SCHEMA")
            proposal = W.verify_witnessed_pair(
                self._sources, self._witness,
                ev["trade"], ev["read"], ev["approval"], ev["witness"],
                trusted_head=head, now=observed_at, **ev["context"])
            if (proposal.successor.chain_hash != entry["chain_hash"]
                    or proposal.successor.sequence != seq
                    or proposal.binding_digest != entry["binding_digest"]
                    or entry["trade_observation_digest"] != _digest(ev["trade"])
                    or entry["read_observation_digest"] != _digest(ev["read"])
                    or any(entry[k] != v for k, v in (
                        ("venue", proposal.venue), ("scope", proposal.scope),
                        ("legacy_ref", proposal.legacy_ref),
                        ("read_ref", proposal.read_ref),
                        ("challenge", proposal.challenge)))):
                blocked("DURABLE_LEDGER_SOURCE_MISMATCH")
            legacy = (proposal.venue, proposal.scope, proposal.legacy_ref)
            reader = (proposal.venue, proposal.scope, proposal.read_ref)
            if rid in completed or legacy in legacy_keys or reader in read_keys or proposal.challenge in challenges:
                blocked("DURABLE_LEDGER_UNIQUENESS_INVALID")
            head = proposal.successor
            records += (entry,)
            completed[rid] = (entry["request_digest"], (head, records))
            legacy_keys.add(legacy)
            read_keys.add(reader)
            challenges.add(proposal.challenge)
        self._atomic = (head, records, completed, frozenset(challenges),
                        frozenset(legacy_keys), frozenset(read_keys))

    def dispatch(self, raw):
        with self._disk_lock:
            con = _open(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                _validate_meta(con, _root_meta(self._pins, self._sources,
                                               self._witness, label="server"))
                self._restore(con)
                old = self.head
                answer = super().dispatch(raw)
                if self.head.sequence != old.sequence:
                    if self.head.sequence != old.sequence + 1:
                        blocked("DURABLE_CAS_SEQUENCE_INVALID")
                    frame = R.strict_decode(raw)
                    writer = frame["writer"]["body"]
                    current = self.records[-1]
                    timestamp = self._clock()
                    con.execute(
                        "INSERT INTO immutable_entries "
                        "(sequence,request_id,frame,entry,committed_at) "
                        "VALUES(?,?,?,?,?)",
                        (self.head.sequence, writer["request_id"],
                         R.encode(frame), R.encode(current), timestamp))
                if self.fail_before_disk_commit_once:
                    self.fail_before_disk_commit_once = False
                    raise R.MockLinkLost("disk-backed mock power loss before sqlite COMMIT")
                con.execute("COMMIT")
                return answer
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                # Discard all volatile speculative state before next call.
                self._atomic = (B.TrustedHead(0, B.ZERO), (), {},
                                frozenset(), frozenset(), frozenset())
                raise
            finally:
                con.close()

    def inspect_disk(self):
        """Only reports committed and fully verified disk state."""
        with self._disk_lock:
            with _open(self.path) as con:
                _validate_meta(con, _root_meta(self._pins, self._sources,
                                               self._witness, label="server"))
                self._restore(con)
                return self.head, tuple(self.records)


class DurableSQLiteAuditor(R.IndependentMockAnchor):
    """Separate persistence domain; pinned high-water survives restart.

    A database owner can still rewrite/delete files. This is an isolated
    persistence test, NOT a remotely owned WORM witness.
    """
    def __init__(self, path, *, create, pins, sources, witness):
        self.path = _new_or_existing(path, create)
        self._disk_lock = threading.RLock()
        super().__init__(pins, sources, witness)
        expected = _root_meta(pins, sources, witness, label="auditor")
        try:
            with _open(self.path) as con:
                if create:
                    _initialize(con, expected, auditor=True)
                _validate_meta(con, expected)
                self._load(con)
        except sqlite3.Error:
            blocked("DURABLE_AUDITOR_INTEGRITY_INVALID")

    def _load(self, con):
        try:
            rows = con.execute(
                "SELECT sequence,chain_hash,prefix_digest "
                "FROM immutable_checkpoints ORDER BY sequence").fetchall()
            if not rows or rows[0] != (0, B.ZERO, _digest([])):
                blocked("DURABLE_AUDITOR_GENESIS_INVALID")
            if con.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                blocked("DURABLE_AUDITOR_INTEGRITY_INVALID")
            for a, b in zip(rows, rows[1:]):
                if b[0] != a[0] + 1 or not B.is_hash(b[1]) or not B.is_hash(b[2]):
                    blocked("DURABLE_AUDITOR_CHECKPOINT_GAP")
            seq, head_hash, prefix = rows[-1]
            self._floor = B.TrustedHead(seq, head_hash)
            self._prefix = prefix
        except sqlite3.Error:
            blocked("DURABLE_AUDITOR_INTEGRITY_INVALID")

    @property
    def floor(self):
        with self._disk_lock:
            with _open(self.path) as con:
                _validate_meta(con, _root_meta(self.pins, self._sources,
                                               self._witness, label="auditor"))
                self._load(con)
                return self._floor

    def verify_and_pin(self, response, sent_request):
        with self._disk_lock:
            con = _open(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                _validate_meta(con, _root_meta(self.pins, self._sources,
                                               self._witness, label="auditor"))
                self._load(con)
                old_head = self._floor
                head = super().verify_and_pin(response, sent_request)
                if head.sequence > old_head.sequence:
                    # Always persist each intermediate height as a separate
                    # append-only anchor: a 0 -> 2 sync must not introduce gaps.
                    entries = response["entries"]
                    for seq in range(old_head.sequence + 1, head.sequence + 1):
                        row = entries[seq - 1]
                        con.execute(
                            "INSERT INTO immutable_checkpoints VALUES(?,?,?)",
                            (seq, row["chain_hash"], _digest(entries[:seq])))
                con.execute("COMMIT")
                return head
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                self._floor = B.TrustedHead(0, B.ZERO)
                self._prefix = _digest([])
                raise
            finally:
                con.close()


def new_isolated_auditor(*args, **kwargs):
    """Explicit factory preserves choice of separate test disk."""
    return DurableSQLiteAuditor(*args, **kwargs)
