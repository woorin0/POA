"""Ubuntu 22.04 POA: OFFLINE preservation evidence, never operational activation.

Use only with an already quiesced and transactionally consistent backup and an
isolated restored copy. Byte identity does NOT establish database transactional
consistency, PocketBase schema compatibility or live-trading safety. This
module never restores, migrates, starts a service, or places any orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import quote

PASS = "PASS"
BLOCKED = "BLOCKED"
UNVERIFIED = "UNVERIFIED"


class PreserveBlocked(ValueError):
    pass


def _private(path: Path, directory: bool = False) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return bool((stat.S_ISDIR(st.st_mode) if directory else
                 stat.S_ISREG(st.st_mode)) and not st.st_mode & 0o077)


def _digest(path: Path) -> bytes:
    # No secrets, paths or digests printed. Original files never opened writable.
    if not _private(path):
        raise PreserveBlocked("PRIVATE_FILE_MISSING_OR_INSECURE")
    h = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                h.update(chunk)
    except OSError:
        raise PreserveBlocked("BACKUP_FILE_UNREADABLE") from None
    return h.digest()


def _inventory(root: Path) -> dict[str, Path]:
    if not _private(root, directory=True):
        raise PreserveBlocked("PRIVATE_SNAPSHOT_ROOT_REQUIRED")
    records: dict[str, Path] = {}
    for parent_str, dirs, names in os.walk(root, followlinks=False):
        parent = Path(parent_str)
        if not _private(parent, directory=True):
            raise PreserveBlocked("SNAPSHOT_DIRECTORY_INSECURE")
        for name in dirs:
            if not _private(parent / name, directory=True):
                raise PreserveBlocked("SNAPSHOT_DIRECTORY_INSECURE")
        for name in names:
            f = parent / name
            key = f.relative_to(root).as_posix()
            if name.endswith(("-wal", "-shm")):
                raise PreserveBlocked("WAL_SHM_REQUIRES_OFFLINE_CHECKPOINT")
            if not _private(f):
                raise PreserveBlocked("SNAPSHOT_FILE_INSECURE")
            if key in records or len(records) >= 4096:
                raise PreserveBlocked("SNAPSHOT_INVENTORY_INVALID")
            records[key] = f
    if "pb_data/data.db" not in records or "pocketbase" not in records:
        raise PreserveBlocked("POCKETBASE_SNAPSHOT_INCOMPLETE")
    return records


def _sqlite_read_only(file: Path) -> None:
    # immutable=1 cannot modify SQLite journals. Only safe for checked,
    # WAL-free offline snapshots; never use on a running/active DB.
    try:
        uri = "file:" + quote(str(file.resolve()), safe="/:") + "?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            result = con.execute("PRAGMA integrity_check").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        raise PreserveBlocked("OFFLINE_SQLITE_INTEGRITY_FAILED") from None
    if result != [("ok",)]:
        raise PreserveBlocked("OFFLINE_SQLITE_INTEGRITY_FAILED")


def compare_restored_backup(source_app: Path, restored_app: Path,
                            source_db: Path, restored_db: Path) -> dict:
    """Only compare existing OFFLINE snapshots. No file creation or changes."""
    all_roots = [Path(p).resolve(strict=False) for p in
                 (source_app, restored_app, source_db, restored_db)]
    if len(set(all_roots)) != 4:
        raise PreserveBlocked("BACKUP_AND_RESTORE_NOT_DISTINCT")
    for i, a in enumerate(all_roots):
        if any(a.is_relative_to(b) or b.is_relative_to(a)
               for j, b in enumerate(all_roots) if i != j):
            raise PreserveBlocked("BACKUP_AND_RESTORE_OVERLAP")
    left_app, right_app, left_db, right_db = all_roots
    if not _private(left_app, directory=True) or not _private(right_app, directory=True):
        raise PreserveBlocked("APPLICATION_BACKUP_INSECURE")
    for name in (".env", "notifications.sqlite3"):
        if _digest(left_app / name) != _digest(right_app / name):
            raise PreserveBlocked("APPLICATION_SECRETS_OR_JOURNAL_MISMATCH")
    for app in (left_app, right_app):
        for tail in ("notifications.sqlite3-wal", "notifications.sqlite3-shm"):
            if (app / tail).exists() or (app / tail).is_symlink():
                raise PreserveBlocked("NOTIFICATIONS_WAL_REQUIRES_CHECKPOINT")
    _sqlite_read_only(right_app / "notifications.sqlite3")
    originals = _inventory(left_db)
    restored = _inventory(right_db)
    if set(originals) != set(restored):
        raise PreserveBlocked("RESTORED_SNAPSHOT_INVENTORY_MISMATCH")
    for key in sorted(originals):
        if _digest(originals[key]) != _digest(restored[key]):
            raise PreserveBlocked("RESTORED_SNAPSHOT_BYTES_DIFFER")
    _sqlite_read_only(restored["pb_data/data.db"])
    return {
        "schema": "poa-ubuntu22-offline-restore-comparison-v1",
        "api_key_file_preserved_byte_for_byte": PASS,
        "notifications_db_preserved_byte_for_byte": PASS,
        "pocketbase_binary_and_data_bytes_equal": PASS,
        "restored_sqlite_integrity_check": PASS,
        "transaction_consistent_backup": UNVERIFIED,
        "pocketbase_version_and_migration": UNVERIFIED,
        "safe_retry_other_db_paths": UNVERIFIED,
        "api_key_permissions_on_exchange": UNVERIFIED,
        "orders_and_exchange_reconciliation": UNVERIFIED,
        "operational_ready": BLOCKED,
        "trading_enabled": False,
        "original_data_modified": False,
        "deployment_performed": False,
    }


def inspect_source(root: Path) -> dict:
    from deployment import bot03_reinstall as pin
    from deployment import py312_requirements as pins312
    root = Path(root)
    try:
        if pin._metadata(root) != "DIRECTORY":
            raise PreserveBlocked("PINNED_CHECKOUT_MISSING")
        sha = pin._git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise PreserveBlocked("CHECKOUT_HEAD_INVALID")
        pin._manifest(root)
        digest = hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest()
        if digest != pins312.SOURCE_SHA256:
            raise PreserveBlocked("ORIGINAL_PYTHON310_PINS_CHANGED")
    except (OSError, UnicodeError, pin.ReinstallBlocked):
        raise PreserveBlocked("PINNED_RELEASE_SOURCE_INVALID") from None
    return {
        "schema": "poa-ubuntu22-pinned-source-v1",
        "release_sha": sha,
        "manifest_72": PASS,
        "python3_10_original_pins": PASS,
        "operational_ready": BLOCKED,
        "trading_enabled": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only POA Ubuntu22 release handoff")
    sub = parser.add_subparsers(dest="action", required=True)
    source = sub.add_parser("inspect-source")
    source.add_argument("--checkout", type=Path, required=True)
    cmp = sub.add_parser("compare-offline-restore")
    for field in ("source-app", "restored-app", "source-db", "restored-db"):
        cmp.add_argument("--" + field, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "inspect-source":
            result = inspect_source(args.checkout)
        else:
            result = compare_restored_backup(
                args.source_app, args.restored_app, args.source_db, args.restored_db
            )
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (PreserveBlocked, OSError) as exc:
        code = exc.args[0] if isinstance(exc, PreserveBlocked) else "OFFLINE_EVIDENCE_UNAVAILABLE"
        print(json.dumps({"status": BLOCKED, "reason_code": code,
                          "original_data_modified": False,
                          "trading_enabled": False},
                         sort_keys=True, separators=(",", ":")))
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
