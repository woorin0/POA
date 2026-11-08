"""POA Ubuntu 22.04 NEW-server activation after the verified Stage installation.

No unattended trading. Boot installs only Stage. After locally supplying secrets
or an OFFLINE consistent backup, the operator runs one explicit command.
The POA service binds localhost only; publishing /order is a separate command.
This module never modifies financial calculations, orders, or a running BOT03.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import sys
import urllib.request
import zipfile

try:
    from deployment import install_once as legacy
except ModuleNotFoundError:  # Embedded /usr/local/lib/poa cloud-init helper
    import install_once as legacy

APP = Path("/root/POA")
DB = Path("/root/db")
STAGE_RESULT = Path("/root/POA_INSTALL_RESULT.json")
SERVICE_DIR = Path("/etc/systemd/system")
CADDYFILE = Path("/etc/caddy/Caddyfile")
PRIVATE_FILE_MODE = 0o600
PB_LEGACY_VERSION = "0.16.6"
PB_LEGACY_URL = (
    "https://github.com/pocketbase/pocketbase/releases/download/v0.16.6/"
    "pocketbase_0.16.6_linux_amd64.zip"
)

class SetupBlocked(ValueError):
    pass

def _assert_private_regular(file: Path):
    st = file.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise SetupBlocked("PRIVATE_FILE_REQUIRED")
    return file

def _assert_private_directory(folder: Path):
    st = folder.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o077:
        raise SetupBlocked("PRIVATE_DIRECTORY_REQUIRED")
    return folder

def _stage_ready():
    if os.name != "posix" or os.geteuid() != 0:
        raise SetupBlocked("UBUNTU_ROOT_REQUIRED")
    if legacy.runtime_for_os(Path("/etc/os-release").read_text()) != "python3.10":
        raise SetupBlocked("UBUNTU2204_REQUIRED")
    _assert_private_regular(STAGE_RESULT)
    result = json.loads(STAGE_RESULT.read_text(encoding="utf-8"))
    if result.get("status") != "STAGED_NOT_LIVE":
        raise SetupBlocked("STAGE_NOT_VERIFIED")
    if not APP.is_dir() or not DB.is_dir() or not (APP / ".venv/bin/python").is_file():
        raise SetupBlocked("STAGED_RUNTIME_MISSING")
    legacy.verify_source(result["manifest_sha256"])
    # Confirm install_once produced exactly the expected fixed source.
    observed = legacy.run(
        ["git", "-C", str(APP), "rev-parse", "HEAD"], "STAGE_COMMIT_UNKNOWN"
    ).decode("ascii").strip()
    if observed != result["commit"]:
        raise SetupBlocked("STAGED_SOURCE_CHANGED")
    return result

def _run(argv, name, timeout=120):
    return legacy.run(argv, name, timeout=timeout)

def _install_units():
    SERVICE_DIR.mkdir(mode=0o755, exist_ok=True)
    units = {
        "pocketbase.service": (
            "[Unit]\nDescription=POA PocketBase\nAfter=network.target\n"
            "[Service]\nType=simple\nUser=root\nUMask=0077\n"
            "WorkingDirectory=/root/db\nExecStart=/root/db/pocketbase serve "
            "--http=127.0.0.1:8090\nRestart=on-failure\nRestartSec=5\n"
            "[Install]\nWantedBy=multi-user.target\n"
        ),
        "poa.service": (
            "[Unit]\nDescription=POA webhook local service\n"
            "After=network-online.target pocketbase.service\n"
            "Requires=pocketbase.service\n"
            "ConditionPathExists=/root/POA/LOCAL_READY\n"
            "[Service]\nType=simple\nUser=root\nUMask=0077\n"
            "WorkingDirectory=/root/POA\nEnvironment=TZ=UTC\n"
            "Environment=PYTHONUNBUFFERED=1\n"
            "ExecStartPre=/root/POA/.venv/bin/python "
            "/usr/local/lib/poa/validate_config.py\n"
            "ExecStart=/root/POA/.venv/bin/python run.py "
            "--host=127.0.0.1 --port=8000\n"
            "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=120\n"
            "[Install]\nWantedBy=multi-user.target\n"
        ),
    }
    for name, data in units.items():
        target = SERVICE_DIR / name
        if target.exists() or target.is_symlink():
            # Stage intentionally never installs real units.
            raise SetupBlocked("EXISTING_SERVICE_PRESERVED_STOP")
        target.write_text(data, encoding="utf-8")
        target.chmod(0o644)
    _run(["systemctl", "daemon-reload"], "DAEMON_RELOAD_FAILED")
    for name in units:
        _run(["systemctl", "unmask", name], "SERVICE_UNMASK_FAILED")

def _local_ready():
    import time
    def check():
        with urllib.request.urlopen("http://127.0.0.1:8000/hi", timeout=2) as response:
            return json.load(response) == "hi!!"
    legacy.wait_ready(check, "POA_LOCAL_HEALTH_FAILED", seconds=60)

def _pocketbase_ready():
    legacy.wait_ready(lambda: legacy.api("/api/health").get("code") == 200,
                      "POCKETBASE_LOCAL_HEALTH_FAILED", seconds=60)

def _start_services(*, fresh: bool, admin_email="", admin_password=""):
    _install_units()
    _run(["systemctl", "start", "pocketbase"], "POCKETBASE_START_FAILED")
    _pocketbase_ready()
    if fresh:
        legacy.bootstrap_db(admin_email, admin_password)
    # No Caddy/port-80/443, no TradingView webhook exposure.
    # Local REST startup does not execute an order without a user request.
    (APP / "LOCAL_READY").touch(mode=0o600, exist_ok=False)
    _run(["systemctl", "start", "poa"], "POA_START_FAILED")
    _local_ready()
    _run(["systemctl", "enable", "pocketbase", "poa"], "AUTOSTART_FAILED")
    report = {
        "state": "LOCAL_RUNNING_NO_PUBLIC_WEBHOOK",
        "pocketbase": "LOCAL_HEALTH_PASS",
        "poa": "LOCAL_HEALTH_PASS",
        "public_orders_enabled": False,
        "live_order_test_performed": False,
    }
    result = APP / "LOCAL_READY_RESULT.json"
    result.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    result.chmod(0o600)
    return report

def _private_json(path: Path):
    _assert_private_regular(path)
    return json.loads(path.read_text(encoding="utf-8"))

def _legacy_binary_for_fresh():
    # Existing Debian12 operational path uses PocketBase 0.16.6;
    # old 0.22.48 Stage binary is retained, not deleted.
    if (DB / "pb_data").exists() or (DB / "pocketbase.stage-0.22.48").exists():
        raise SetupBlocked("POCKETBASE_PREEXISTING_DB_OR_MIGRATION_BLOCKED")
    archive = DB / "pocketbase-v0.16.6.zip"
    if archive.exists():
        raise SetupBlocked("POCKETBASE_DOWNLOAD_ALREADY_PRESENT")
    _run(["curl", "--fail", "--proto", "=https", "--proto-redir", "=https",
          "--location", "--output", str(archive), PB_LEGACY_URL],
         "POCKETBASE_LEGACY_DOWNLOAD_FAILED", timeout=300)
    with zipfile.ZipFile(archive) as package:
        entries = package.namelist()
        if "pocketbase" not in entries:
            raise SetupBlocked("POCKETBASE_ARCHIVE_INVALID")
        candidate = DB / "pocketbase.legacy.new"
        with candidate.open("xb") as stream:
            stream.write(package.read("pocketbase"))
    candidate.chmod(0o700)
    observed = _run([str(candidate), "--version"], "POCKETBASE_LEGACY_BINARY_INVALID")
    if PB_LEGACY_VERSION.encode() not in observed:
        raise SetupBlocked("POCKETBASE_LEGACY_VERSION_MISMATCH")
    (DB / "pocketbase").rename(DB / "pocketbase.stage-0.22.48")
    candidate.rename(DB / "pocketbase")
    return True

def _make_env(values: dict, email: str, password: str):
    read_config, binding = legacy.upbit_read_configuration(values, values["credentials"])
    if binding is not None:
        raise SetupBlocked("CUSTOM_UPBIT_BINDING_REQUIRES_SEPARATE_CONFIGURATION")
    env = {
        **{k: values["credentials"].get(k, "") for k in legacy.ENV_KEYS},
        "DB_ID": email, "DB_PASSWORD": password, "PORT": 8000,
        "WHITELIST": ["127.0.0.1"],
        **legacy.FLAGS, **read_config,
        "VERIFIED_NOTIFICATIONS_DB_PATH": str(APP / "notifications.sqlite3"),
        "SAFE_RETRY_ENABLED": False,
        "ORDER_RECONCILIATION_ENABLED": False,
    }
    text = "".join(key + "=" + json.dumps(val, ensure_ascii=False) + "\n"
                   for key, val in env.items())
    legacy.write_private(APP / ".env", text)
    return True

def fresh(config_path: Path):
    _stage_ready()
    if (APP / ".env").exists() or (APP / "LOCAL_READY").exists():
        raise SetupBlocked("EXISTING_CONFIG_OR_SERVICE_PRESERVED")
    if (DB / "pb_data").exists():
        raise SetupBlocked("POCKETBASE_DATA_ALREADY_PRESENT")
    data = _private_json(config_path)
    legacy.validate(data)
    email = data.get("db_admin_email", "poa-admin@example.com")
    admin_password = secrets.token_urlsafe(32)
    # Freeze all inputs BEFORE creating files; the source is never mutated.
    _legacy_binary_for_fresh()
    _make_env(data, email, admin_password)
    return _start_services(fresh=True, admin_email=email, admin_password=admin_password)

def _copy_backup_tree(src: Path, dst: Path):
    # Backup contains PocketBase state ONLY; refuse symlinks and WAL.
    _assert_private_directory(src)
    if dst.exists() or dst.is_symlink():
        raise SetupBlocked("DATA_DESTINATION_ALREADY_EXISTS")
    dst.mkdir(mode=0o700)
    for parent, directories, files in os.walk(src, followlinks=False):
        relative = Path(parent).relative_to(src)
        dest = dst / relative
        for folder in directories:
            source_dir = Path(parent) / folder
            _assert_private_directory(source_dir)
            (dest / folder).mkdir(mode=0o700)
        for name in files:
            source_file = Path(parent) / name
            if name.endswith(("-wal", "-shm")):
                raise SetupBlocked("CONSISTENT_BACKUP_CHECKPOINT_REQUIRED")
            _assert_private_regular(source_file)
            target = dest / name
            if target.exists() or target.is_symlink():
                raise SetupBlocked("TARGET_ALREADY_PRESENT")
            shutil.copyfile(source_file, target)
            target.chmod(0o600)
            if hashlib.sha256(source_file.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
                raise SetupBlocked("BACKUP_COPY_MISMATCH")

def restore(backup: Path):
    _stage_ready()
    if (APP / ".env").exists() or (APP / "LOCAL_READY").exists():
        raise SetupBlocked("EXISTING_CONFIG_OR_SERVICE_PRESERVED")
    root = _assert_private_directory(backup.resolve(strict=True))
    source_app = _assert_private_directory(root / "app")
    source_db = _assert_private_directory(root / "db")
    # Strict offline-only snapshot: no source code transfer or automatic DB migration.
    app_files = {p.name for p in source_app.iterdir()}
    if not {".env", "notifications.sqlite3"} <= app_files:
        raise SetupBlocked("RESTORE_ENV_OR_NOTIFICATIONS_MISSING")
    if any(name.endswith(("-wal", "-shm")) for name in app_files):
        raise SetupBlocked("RESTORE_SNAPSHOT_WAL_UNCHECKPOINTED")
    if (DB / "pb_data").exists() or (DB / "pocketbase.stage-0.22.48").exists():
        raise SetupBlocked("POCKETBASE_DATA_ALREADY_PRESENT")
    _assert_private_regular(source_db / "pocketbase")
    _assert_private_directory(source_db / "pb_data")
    if not (source_db / "pb_data" / "data.db").is_file():
        raise SetupBlocked("POCKETBASE_RESTORED_DB_REQUIRED")
    # Verify SQLite integrity before moving anything into staged directories.
    try:
        from deployment import ubuntu22_release_handoff as handoff
    except ModuleNotFoundError:
        import ubuntu22_release_handoff as handoff
    handoff._sqlite_read_only(source_app / "notifications.sqlite3")
    handoff._sqlite_read_only(source_db / "pb_data" / "data.db")
    for name in app_files:
        if name not in {".env", "notifications.sqlite3",
                        "safe_orders.sqlite3"}:
            raise SetupBlocked("UNRECOGNIZED_EXTERNAL_STATE_REQUIRES_REVIEW")
        _assert_private_regular(source_app / name)
    # Preserves original PocketBase binary. NEVER migrate the live DB here.
    _copy_backup_tree(source_db / "pb_data", DB / "pb_data")
    staged = DB / "pocketbase.stage-0.22.48"
    (DB / "pocketbase").rename(staged)
    shutil.copyfile(source_db / "pocketbase", DB / "pocketbase")
    (DB / "pocketbase").chmod(0o700)
    for name in app_files:
        shutil.copyfile(source_app / name, APP / name)
        (APP / name).chmod(0o600)
    return {
        "state": "RESTORED_NOT_STARTED",
        "source_db_migration": "NOT_PERFORMED",
        "original_pocketbase_binary": "PRESERVED",
        "api_keys": "FILE_COPIED_NOT_PRINTED",
        "public_orders_enabled": False,
        "next_step": "poa-activate-local",
    }

def activate_restored():
    _stage_ready()
    _assert_private_regular(APP / ".env")
    if (APP / "LOCAL_READY").exists():
        raise SetupBlocked("ALREADY_ACTIVE")
    try:
        from deployment import ubuntu22_release_handoff as handoff
    except ModuleNotFoundError:
        import ubuntu22_release_handoff as handoff
    handoff._sqlite_read_only(DB / "pb_data" / "data.db")
    handoff._sqlite_read_only(APP / "notifications.sqlite3")
    return _start_services(fresh=False)

def publish(domain: str):
    # Publishing exposes the order endpoint. Not invoked by User Data or CI.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{3,250}", domain) or "." not in domain:
        raise SetupBlocked("VALID_DOMAIN_REQUIRED")
    _assert_private_regular(APP / "LOCAL_READY_RESULT.json")
    if not (APP / "LOCAL_READY").is_file():
        raise SetupBlocked("LOCAL_HEALTH_NOT_CONFIRMED")
    caddy = CADDYFILE
    saved = caddy.with_name("Caddyfile.poa.previous")
    if saved.exists() or saved.is_symlink():
        raise SetupBlocked("CADDY_ALREADY_CONFIGURED")
    if caddy.exists() or caddy.is_symlink():
        if caddy.is_symlink() or not caddy.is_file():
            raise SetupBlocked("CADDY_FILE_UNSAFE")
        shutil.copy2(caddy, saved)  # Preserve packaged Caddy welcome config.
    acl = "52.89.214.238 34.212.75.30 54.218.53.128 52.32.178.7"
    content = (domain + " {\n @tradingview {\n remote_ip " + acl +
               "\n method POST\n path / /order\n }\n handle @tradingview {\n"
               " reverse_proxy 127.0.0.1:8000\n }\n respond 403\n}\n")
    caddy.write_text(content, encoding="utf-8")
    _run(["caddy", "validate", "--config", str(caddy)], "CADDY_VALIDATE_FAILED")
    _run(["systemctl", "unmask", "caddy.service"], "CADDY_UNMASK_FAILED")
    for port in ("22/tcp", "80/tcp", "443/tcp"):
        _run(["ufw", "allow", port], "FIREWALL_RULE_FAILED")
    _run(["ufw", "--force", "enable"], "UFW_ENABLE_FAILED")
    _run(["systemctl", "enable", "--now", "caddy.service"], "CADDY_START_FAILED")
    return {"state": "HTTPS_PUBLICATION_REQUESTED", "trading_webhook_exposed": True,
            "live_order_test_performed": False}

def main(argv=None):
    cli = argparse.ArgumentParser(description="POA Ubuntu22 operator-first setup")
    sub = cli.add_subparsers(dest="action", required=True)
    first = sub.add_parser("fresh", help="new DB from locally held config (0600)")
    first.add_argument("--config", type=Path, required=True)
    recovered = sub.add_parser("restore", help="copy existing OFFLINE backup; do NOT start")
    recovered.add_argument("--backup", type=Path, required=True)
    sub.add_parser("activate-restored", help="start restored DB and local POA only")
    exposed = sub.add_parser("publish", help="explicitly expose TradingView order endpoint")
    exposed.add_argument("--domain", required=True)
    opts = cli.parse_args(argv)
    try:
        if opts.action == "fresh":
            result = fresh(opts.config)
        elif opts.action == "restore":
            result = restore(opts.backup)
        elif opts.action == "activate-restored":
            result = activate_restored()
        else:
            result = publish(opts.domain)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (SetupBlocked, legacy.InstallError, OSError, ValueError, zipfile.BadZipFile):
        print('POA_SETUP_BLOCKED: consult local logs; DB/API keys preserved', file=sys.stderr)
        return 75

if __name__ == "__main__":
    raise SystemExit(main())
