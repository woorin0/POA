"""Offline legacy administration; stop notification workers before use.

Rejects v4 databases. Use notification_db_v4.py for explicit delivery/actor/CAS
administration. Legacy hold permits exactly one eligible row, never a bulk hold.
Does not import POA, credentials, exchange clients or Discord.
"""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


class Rejected(Exception):
    def __init__(self, code):
        self.code = code


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected('INVALID_ARGUMENTS')


def legacy_only(con):
    if con.execute("SELECT 1 FROM sqlite_master WHERE lower(name) GLOB 'vpnl_*' LIMIT 1").fetchone():
        raise Rejected('V4_DATABASE_REJECTED')


def legacy_schema(con):
    for table, key, state in (('verified_jobs', 'id', 'state'), ('verified_outbox', 'event_id', 'status')):
        row = con.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchone()
        columns = con.execute('PRAGMA table_info(' + table + ')').fetchall()
        if not row or row[0] != 'table' or {key, state} - {c[1] for c in columns}:
            raise Rejected('LEGACY_SCHEMA_REQUIRED')
        if [c[1] for c in columns if c[5]] != [key]:
            raise Rejected('LEGACY_PRIMARY_KEY_REQUIRED')


def hold_one(con):
    legacy_schema(con)
    targets = []
    for table, key, state, allowed in (
        ('verified_jobs', 'id', 'state', ('PENDING', 'VERIFYING')),
        ('verified_outbox', 'event_id', 'status', ('PENDING', 'SENDING')),
    ):
        for value, previous in con.execute(
            'SELECT ' + key + ',' + state + ' FROM ' + table + ' WHERE ' + state + ' IN (?,?) LIMIT 2', allowed
        ):
            targets.append((table, key, state, value, previous))
    if len(targets) != 1:
        raise Rejected('NO_ELIGIBLE_TARGET' if not targets else 'AMBIGUOUS_TARGET')
    table, key, state, value, previous = targets[0]
    if value is None:
        raise Rejected('LEGACY_PRIMARY_KEY_REQUIRED')
    replacement = 'UNKNOWN' if previous == 'SENDING' else 'HELD'
    legacy_only(con)  # Same IMMEDIATE transaction as selection and the mutation.
    before = con.total_changes
    changed = con.execute(
        'UPDATE ' + table + ' SET ' + state + '=? WHERE ' + key + '=? AND ' + state + '=?',
        (replacement, value, previous),
    ).rowcount
    row = con.execute('SELECT ' + state + ' FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone()
    if changed != 1 or con.total_changes - before != 1 or row != (replacement,):
        raise Rejected('TARGET_STATE_CONFLICT')
    legacy_only(con)


def status(con):
    legacy_schema(con)
    output = {}
    allowed = {'PENDING', 'VERIFYING', 'HELD', 'DONE', 'FAILED', 'SENDING', 'SENT', 'UNKNOWN'}
    for table, column in (('verified_jobs', 'state'), ('verified_outbox', 'status')):
        counts = {}
        for state, count in con.execute('SELECT ' + column + ',COUNT(*) FROM ' + table + ' GROUP BY ' + column):
            label = state if state in allowed else 'OTHER'
            counts[label] = counts.get(label, 0) + count
        output[table] = counts
    return json.dumps(output)


def main():
    stage = 'arguments'
    created_backup = None
    try:
        parser = SafeParser(prog='notification_db.py', description=__doc__)
        parser.add_argument('action', choices=['status', 'backup', 'hold'])
        parser.add_argument('database', type=Path)
        parser.add_argument('--destination', type=Path)
        args = parser.parse_args()
        if args.action == 'backup' and args.destination is None:
            raise Rejected('BACKUP_DESTINATION_REQUIRED')
        stage = 'open'
        source = args.database.resolve(strict=True)
        mode = 'rw' if args.action == 'hold' else 'ro'
        with closing(sqlite3.connect(source.as_uri() + '?mode=' + mode, uri=True, timeout=5)) as con:
            try:
                stage = 'guard'
                con.execute('BEGIN IMMEDIATE' if args.action == 'hold' else 'BEGIN')
                legacy_only(con)
                if args.action == 'hold':
                    stage = 'hold'
                    hold_one(con)
                    output = 'Pending work held; delivery-unknown work retained'
                elif args.action == 'backup':
                    stage = 'backup'
                    legacy_schema(con)
                    destination = args.destination.resolve()
                    with destination.open('xb') as file:
                        stat = destination.stat()
                        created_backup = (destination, stat.st_dev, stat.st_ino)
                    legacy_only(con)  # Snapshot is pinned by the same read transaction.
                    with closing(sqlite3.connect(destination.as_uri() + '?mode=rw', uri=True)) as target:
                        con.backup(target)
                        if target.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                            raise Rejected('BACKUP_INTEGRITY_FAILED')
                    output = 'Backup complete'
                else:
                    stage = 'status'
                    output = status(con)
                stage = 'commit'
                con.commit()
            except BaseException:
                try:
                    con.rollback()
                except Exception:
                    pass
                raise
        print(output)
        return 0
    except Exception as exc:
        if created_backup is not None:
            destination, device, inode = created_backup
            try:
                stat = destination.stat()
                if (stat.st_dev, stat.st_ino) == (device, inode):
                    destination.unlink()
            except OSError:
                pass
        code = exc.code if isinstance(exc, Rejected) else 'OPERATION_FAILED'
        result = {'ok': False, 'code': code, 'stage': stage}
        if code == 'V4_DATABASE_REJECTED':
            result['use'] = 'notification_db_v4.py'
        print(json.dumps(result), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())