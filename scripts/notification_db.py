"""Offline administration. Stop notification workers before hold/restore.

Does not import POA, credentials, exchange clients or Discord.
"""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "backup", "hold"])
    parser.add_argument("database", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    source = args.database.resolve(strict=True)
    mode = "rw" if args.action == "hold" else "ro"
    with closing(sqlite3.connect(source.as_uri() + "?mode=" + mode, uri=True)) as con:
        if args.action == "backup":
            if args.destination is None:
                parser.error("backup requires --destination (a new path)")
            # Exclusive creation protects both source and existing backups.
            with args.destination.open("xb"):
                pass
            with closing(sqlite3.connect(args.destination)) as target:
                con.backup(target)
            print("Backup complete")
        elif args.action == "hold":
            with con:
                con.execute("UPDATE verified_jobs SET state='HELD' WHERE state IN ('PENDING','VERIFYING')")
                con.execute("UPDATE verified_outbox SET status='UNKNOWN' WHERE status='SENDING'")
                con.execute("UPDATE verified_outbox SET status='HELD' WHERE status='PENDING'")
            print("Pending work held; delivery-unknown work retained")
        else:
            output = {}
            for table, column in [("verified_jobs", "state"), ("verified_outbox", "status")]:
                output[table] = dict(con.execute(f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"))
            print(json.dumps(output))


if __name__ == "__main__":
    main()
