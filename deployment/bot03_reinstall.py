"""BOT03 preserve-first, existing-server update preparation.

SAFE MODES ONLY:
  inspect: read metadata of existing local install; no DB/.env content access.
  stage: copy tracked, verified source to a NEW separate directory. Never
         install packages, change running services, connect to a DB/provider,
         edit original data, or activate trading.

This module cannot deploy/start/reload/rollback a running bot. Those actions
require separate verified backups, host evidence and explicit approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys

ORIGINAL_MAIN = "c5566c4cc2a4ce716f1f0d66260bd60e4f95546f"
DEFAULT_APP = Path("/root/POA")
DEFAULT_DB = Path("/root/db")
MAX_TRACKED = 8192
MAX_SOURCE_BYTES = 64 * 1024 * 1024
DENIED_BASENAMES = frozenset({
    ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
    "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml",
    "poa-install-input.json", "poa-stage-input.json", "pb_data",
})
DENIED_SUFFIXES = (".sqlite", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm",
                   ".db", ".db-wal", ".db-shm", ".pem", ".key", ".p12",
                   ".pfx", ".jks", ".age")


class ReinstallBlocked(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _metadata(path: Path):
    """Never follow a symlink and never open data or secret file contents."""
    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return "MISSING"
    except OSError:
        return "UNVERIFIED"
    if stat.S_ISLNK(mode):
        return "SYMLINK_BLOCKED"
    if stat.S_ISREG(mode):
        return "FILE"
    if stat.S_ISDIR(mode):
        return "DIRECTORY"
    return "SPECIAL_BLOCKED"


def _git(root: Path, *argv):
    try:
        p = subprocess.run(
            ["git", "-C", str(root), *argv], capture_output=True,
            timeout=15, check=False,
            env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                 "GIT_OPTIONAL_LOCKS": "0", "PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReinstallBlocked("LOCAL_GIT_READ_UNAVAILABLE") from None
    if p.returncode:
        raise ReinstallBlocked("LOCAL_GIT_READ_FAILED")
    return p.stdout


def inspect(app: Path = DEFAULT_APP, db: Path = DEFAULT_DB):
    """Observe expected BOT03 paths only, never read production content."""
    app, db = Path(app), Path(db)
    checks = {
        "application_directory": _metadata(app) == "DIRECTORY",
        "database_directory": _metadata(db) == "DIRECTORY",
        "application_config_present": _metadata(app / ".env") == "FILE",
        "pocketbase_data_directory": _metadata(db / "pb_data") == "DIRECTORY",
        "application_run_entrypoint": _metadata(app / "run.py") == "FILE",
        "database_binary_present": _metadata(db / "pocketbase") == "FILE",
    }
    status = "PASS" if all(checks.values()) else "BLOCKED"
    installed_commit = "UNVERIFIED"
    if checks["application_directory"]:
        try:
            got = _git(app, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
            if re.fullmatch(r"[0-9a-f]{40}", got):
                installed_commit = got
        except (UnicodeError, ReinstallBlocked):
            pass
    return {
        "schema": "poa-bot03-existing-install-inspection-v1",
        "expected_application": "BOT03",
        "checks": {k: ("PASS" if v else "BLOCKED") for k, v in checks.items()},
        "installed_commit": installed_commit,
        "metadata_gate": status,
        # Existence alone does NOT prove consistent/restorable DB backups.
        "database_backup_consistency": "UNVERIFIED",
        "sqlite_wal_shm_consistency": "UNVERIFIED",
        "notification_outbox_and_safe_retry": "UNVERIFIED",
        "exchange_key_integrity_and_permissions": "UNVERIFIED",
        "service_and_network_configuration": "UNVERIFIED",
        "production_orders_and_open_positions": "UNVERIFIED",
        "data_or_credentials_read": False,
        "originals_modified": False,
        "install_activated": False,
        "approved_for_reinstall": False,
    }


def _relative_file(raw: str):
    if type(raw) is not str or not raw or len(raw) > 240 or "\\" in raw:
        raise ReinstallBlocked("UNSAFE_TRACKED_FILENAME")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in ("..", ".", "") for part in raw.split("/")):
        raise ReinstallBlocked("UNSAFE_TRACKED_FILENAME")
    parts = path.parts
    if any(p in (".git", ".venv", "__pycache__", "pb_data") for p in parts):
        raise ReinstallBlocked("UNSAFE_TRACKED_FILENAME")
    if any(p.lower() in DENIED_BASENAMES for p in parts):
        raise ReinstallBlocked("TRACKED_SECRET_OR_DB_PATH")
    if path.name.lower().endswith(DENIED_SUFFIXES):
        raise ReinstallBlocked("TRACKED_SECRET_OR_DB_PATH")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ReinstallBlocked("UNSAFE_TRACKED_FILENAME")
    return path


def _tracked(root: Path):
    output = _git(root, "ls-files", "--stage", "-z")
    if len(output) > 2 * 1024 * 1024:
        raise ReinstallBlocked("TRACKED_INVENTORY_TOO_LARGE")
    files = []
    for line in output.split(b"\0"):
        if not line:
            continue
        try:
            prefix, raw = line.split(b"\t", 1)
            mode, _sha, stage = prefix.decode("ascii").split(" ")
            path = _relative_file(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise ReinstallBlocked("UNSAFE_GIT_INDEX_ENTRY") from None
        if mode not in ("100644", "100755") or stage != "0":
            raise ReinstallBlocked("SYMLINK_OR_SUBMODULE_TRACKED")
        files.append((path, mode))
    if not files or len(files) > MAX_TRACKED or len({p for p, _ in files}) != len(files):
        raise ReinstallBlocked("TRACKED_INVENTORY_INVALID")
    return files


def _manifest(root: Path):
    file = root / "release_manifest_sha256.json"
    if _metadata(file) != "FILE":
        raise ReinstallBlocked("RELEASE_MANIFEST_UNAVAILABLE")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ReinstallBlocked("RELEASE_MANIFEST_INVALID") from None
    if type(data) is not list or len(data) != 72:
        raise ReinstallBlocked("RELEASE_MANIFEST_INVALID")
    approved = set()
    for item in data:
        if type(item) is not dict or not {"Path", "Bytes", "SHA256"} <= set(item):
            raise ReinstallBlocked("RELEASE_MANIFEST_INVALID")
        p = _relative_file(item["Path"])
        expected = item["SHA256"]
        count = item["Bytes"]
        if (p in approved or type(count) is not int or count < 0
                or count > MAX_SOURCE_BYTES or type(expected) is not str
                or re.fullmatch(r"[0-9A-F]{64}", expected) is None):
            raise ReinstallBlocked("RELEASE_MANIFEST_INVALID")
        source = root.joinpath(*p.parts)
        if _metadata(source) != "FILE":
            raise ReinstallBlocked("RELEASE_MANIFEST_FILE_UNAVAILABLE")
        try:
            blob = source.read_bytes()
        except OSError:
            raise ReinstallBlocked("RELEASE_MANIFEST_FILE_UNAVAILABLE") from None
        if len(blob) != count or hashlib.sha256(blob).hexdigest().upper() != expected:
            raise ReinstallBlocked("RELEASE_MANIFEST_MISMATCH")
        approved.add(p)
    if not {PurePosixPath(n) for n in (
            "main.py", "run.py", "requirements.txt")} <= approved:
        raise ReinstallBlocked("RELEASE_MANIFEST_INCOMPLETE")
    return approved


def _reject_protected_destination(destination: Path, source: Path,
                                  protected_app: Path, protected_db: Path):
    if not destination.is_absolute():
        raise ReinstallBlocked("STAGE_PATH_MUST_BE_ABSOLUTE")
    target = destination.resolve(strict=False)
    for protected in (source, protected_app, protected_db, Path("/etc/poa")):
        forbidden = protected.resolve(strict=False)
        if target == forbidden or target.is_relative_to(forbidden):
            raise ReinstallBlocked("STAGE_OVERLAPS_PROTECTED_DATA")
        # Parent or ancestor path could swallow an existing installation.
        if forbidden.is_relative_to(target):
            raise ReinstallBlocked("STAGE_CONTAINS_PROTECTED_DATA")
    if destination.exists() or destination.is_symlink():
        raise ReinstallBlocked("STAGE_DESTINATION_ALREADY_EXISTS")
    # New staging path must be under a previously existing, private parent.
    parent = destination.parent
    if _metadata(parent) != "DIRECTORY":
        raise ReinstallBlocked("STAGE_PARENT_UNAVAILABLE")
    try:
        st = parent.stat()
        if os.name != "nt" and (st.st_uid != os.geteuid() or st.st_mode & 0o077):
            raise ReinstallBlocked("STAGE_PARENT_MUST_BE_OWNER_PRIVATE")
        if not parent.resolve(strict=True).is_dir():
            raise ReinstallBlocked("STAGE_PARENT_UNAVAILABLE")
    except OSError:
        raise ReinstallBlocked("STAGE_PARENT_UNAVAILABLE") from None


def stage(source: Path, destination: Path, expected_head: str,
          *, protected_app: Path = DEFAULT_APP, protected_db: Path = DEFAULT_DB):
    """Copy verified tracked files to a NEW folder, never activate/install."""
    source, destination = Path(source), Path(destination)
    if (_metadata(source) != "DIRECTORY" or
            re.fullmatch(r"[0-9a-f]{40}", expected_head) is None):
        raise ReinstallBlocked("SOURCE_OR_PIN_INVALID")
    _reject_protected_destination(destination, source, protected_app, protected_db)
    observed = _git(source, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    if observed != expected_head:
        raise ReinstallBlocked("SOURCE_HEAD_NOT_PINNED")
    if _git(source, "status", "--porcelain", "--untracked-files=no").strip():
        raise ReinstallBlocked("SOURCE_TRACKED_FILES_DIRTY")
    files = _tracked(source)
    manifest = _manifest(source)
    if not manifest <= {p for p, _ in files}:
        raise ReinstallBlocked("MANIFEST_FILES_NOT_TRACKED")

    # No writes happen before ALL preflight checks above succeed.
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    count = 0
    for name, mode in files:
        original = source.joinpath(*name.parts)
        if _metadata(original) != "FILE":
            raise ReinstallBlocked("SOURCE_FILE_CHANGED_DURING_COPY")
        entry = destination.joinpath(*name.parts)
        entry.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            data = original.read_bytes()
            if len(data) > MAX_SOURCE_BYTES:
                raise ReinstallBlocked("SOURCE_FILE_TOO_LARGE")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(entry, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            entry.chmod(0o700 if mode == "100755" else 0o600)
        except OSError:
            raise ReinstallBlocked("STAGE_COPY_FAILED_PARTIAL_LEFT_FOR_INSPECTION") from None
        count += 1
    # Release manifest byte identity is rechecked on the stage copy.
    _manifest(destination)
    report = {
        "schema": "poa-bot03-staged-candidate-v1",
        "pinned_source_commit": observed,
        "tracked_files_staged": count,
        "manifest_entries_verified": 72,
        "stage_only": True,
        "originals_modified": False,
        "database_or_key_files_copied": False,
        "packages_installed": False,
        "services_started": False,
        "live_orders": 0,
        "approved_for_deployment": False,
        "backup_restore_verified": False,
    }
    marker = destination / "STAGED_ONLY_NOT_LIVE.json"
    with marker.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True, separators=(",", ":"))
    marker.chmod(0o600)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="BOT03 read-only audit / offline candidate staging")
    sub = parser.add_subparsers(dest="action", required=True)
    audit = sub.add_parser("inspect", help="metadata-only on BOT03, no writes")
    audit.add_argument("--app", type=Path, default=DEFAULT_APP)
    audit.add_argument("--db", type=Path, default=DEFAULT_DB)
    stager = sub.add_parser("stage", help="copy pinned source into NEW private folder ONLY")
    stager.add_argument("--source", type=Path, required=True)
    stager.add_argument("--destination", type=Path, required=True)
    stager.add_argument("--expected-head", required=True)
    stager.add_argument("--protected-app", type=Path, default=DEFAULT_APP)
    stager.add_argument("--protected-db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    try:
        if args.action == "inspect":
            report = inspect(args.app, args.db)
            exit_code = 0 if report["metadata_gate"] == "PASS" else 75
        else:
            report = stage(args.source, args.destination, args.expected_head,
                           protected_app=args.protected_app, protected_db=args.protected_db)
            exit_code = 0
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return exit_code
    except (ReinstallBlocked, UnicodeError, OSError) as e:
        code = e.code if isinstance(e, ReinstallBlocked) else "UNVERIFIED_LOCAL_ERROR"
        print(json.dumps({
            "result": "BLOCKED", "reason_code": code,
            "originals_modified": False, "install_activated": False,
            "approved_for_reinstall": False,
        }, sort_keys=True, separators=(",", ":")))
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
