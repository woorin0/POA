"""Offline inspect / backup / rollback gate. Never imports the trading app."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "backup", "rollback-check"])
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = args.database.resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
        rows = [json.loads(r[0]) for r in con.execute(
            "SELECT o.data FROM safe_orders o JOIN safe_locks l ON o.signal=l.signal")]
        if args.action == "backup":
            if args.output is None or args.output.exists():
                parser.error("Specify a NEW --output file; existing files are never overwritten")
            with closing(sqlite3.connect(args.output)) as dst:
                con.backup(dst)
            print("Backup completed")
        else:
            for row in rows:
                print(json.dumps({key: row[key] for key in (
                    "exchange", "symbol", "client_id", "state", "target", "filled", "remaining")}, ensure_ascii=True))
            print(f"Unresolved orders: {len(rows)}")
            if args.action == "rollback-check" and rows:
                print("BLOCKED: keep ingress stopped; reconcile with the exchange before rollback")
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
