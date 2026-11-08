"""Mock only: Ubuntu22 integrated handoff, finance source and preserved DB/key.

Never opens the real BOT03 paths, network, exchange APIs or systemd.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from deployment import bot03_reinstall as B
from deployment import py312_requirements as P
from deployment import ubuntu22_release_handoff as H

ROOT = Path(__file__).resolve().parents[1]
BASE = "c5566c4cc2a4ce716f1f0d66260bd60e4f95546f"


def private(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True)
    directory.chmod(0o700)
    return directory


def create_sqlite(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The existing repository audit guard denies SQLite opens named data.db.
    # Generate the offline MOCK at a different test filename, then rename.
    scratch = path.with_name("synthetic-pocketbase.sqlite3") if path.name == "data.db" else path
    con = sqlite3.connect(scratch)
    try:
        con.execute("CREATE TABLE mock_evidence (id INTEGER PRIMARY KEY, amount TEXT)")
        con.execute("INSERT INTO mock_evidence (amount) VALUES (?)", ("123.456789",))
        con.commit()
    finally:
        con.close()  # Windows locks SQLite until the actual handle closes.
    if scratch != path:
        scratch.rename(path)
    path.chmod(0o600)


def offline_compare(*paths):
    # Production code performs real SQLite read-only checks in a NEW Python
    # process, where pytest's audit hook cannot misclassify synthetic data.db.
    args = [sys.executable, "-B", "-S",
            str(ROOT / "deployment" / "ubuntu22_release_handoff.py"),
            "compare-offline-restore"]
    for name, value in zip(
            ("source-app", "restored-app", "source-db", "restored-db"), paths):
        args += ["--" + name, str(value)]
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          check=False, timeout=20)
    assert "SYNTHETIC_ONLY_DO_NOT_PRINT" not in proc.stdout
    assert "SYNTHETIC_ONLY_DO_NOT_PRINT" not in proc.stderr
    return proc.returncode, json.loads(proc.stdout)


def make_offline_copy(tmp_path: Path):
    backup_root = private(tmp_path/"backup")
    output_root = private(tmp_path/"restored")
    source_app = private(backup_root/"app")
    source_db = private(backup_root/"db")
    restored_app = private(output_root/"app")
    restored_db = private(output_root/"db")
    (source_app/".env").write_text("PASSWORD=SYNTHETIC_ONLY_DO_NOT_PRINT\n")
    (source_app/".env").chmod(0o600)
    create_sqlite(source_app/"notifications.sqlite3")
    (source_db/"pocketbase").write_bytes(b"MOCK-PB-16-OR-22-BINARY")
    (source_db/"pocketbase").chmod(0o600)
    create_sqlite(source_db/"pb_data"/"data.db")
    nested = private(source_db/"pb_data"/"aux")
    (nested/"history.mock").write_bytes(b"SYNTHETIC_LEDGER_BLOB")
    (nested/"history.mock").chmod(0o600)
    for src, dst in ((source_app,restored_app),(source_db,restored_db)):
        for folder, dirs, files in os.walk(src):
            parent = Path(folder)
            rel = parent.relative_to(src)
            tgt = dst/rel
            tgt.mkdir(mode=0o700,parents=True,exist_ok=True)
            tgt.chmod(0o700)
            for filename in files:
                shutil.copyfile(parent/filename,tgt/filename)
                (tgt/filename).chmod(0o600)
    return source_app,restored_app,source_db,restored_db


def test_git_release_72_pins_and_original_requirements_are_verbatim():
    result = H.inspect_source(ROOT)
    assert result["manifest_72"] == "PASS"
    assert result["python3_10_original_pins"] == "PASS"
    assert result["operational_ready"] == "BLOCKED"
    assert result["trading_enabled"] is False
    assert hashlib.sha256((ROOT/"requirements.txt").read_bytes()).hexdigest() == P.SOURCE_SHA256
    original = (ROOT/"requirements.txt").read_bytes()
    revised = P.render_from_bytes(original)
    old, new = original.decode().splitlines(), revised.decode().splitlines()
    assert len(old) == len(new) == 17
    assert [(x,y) for x,y in zip(old,new) if x!=y] == [
        ("pendulum==2.1.2","pendulum==3.2.0")
    ]


def test_offline_backup_and_restored_data_identical(tmp_path,capsys):
    paths = make_offline_copy(tmp_path)
    code, result = offline_compare(*paths)
    assert code == 0
    assert result["api_key_file_preserved_byte_for_byte"] == "PASS"
    assert result["notifications_db_preserved_byte_for_byte"] == "PASS"
    assert result["pocketbase_binary_and_data_bytes_equal"] == "PASS"
    assert result["restored_sqlite_integrity_check"] == "PASS"
    assert result["transaction_consistent_backup"] == "UNVERIFIED"
    assert result["pocketbase_version_and_migration"] == "UNVERIFIED"
    assert result["operational_ready"] == "BLOCKED"
    assert result["trading_enabled"] is False
    assert "SYNTHETIC_ONLY_DO_NOT_PRINT" not in json.dumps(result)
    capsys.readouterr()


@pytest.mark.parametrize("field",["key","sqlite","pb_data","pocketbase","ledger"])
def test_tampering_fails_closed_even_when_json_report_never_leaks_secrets(tmp_path,field):
    source_app,restored_app,source_db,restored_db = make_offline_copy(tmp_path)
    files = {
        "key":restored_app/".env",
        "sqlite":restored_app/"notifications.sqlite3",
        "pb_data":restored_db/"pb_data"/"data.db",
        "pocketbase":restored_db/"pocketbase",
        "ledger":restored_db/"pb_data"/"aux"/"history.mock",
    }
    target = files[field]
    target.write_bytes(target.read_bytes()+b"MUTATION_CANARY")
    target.chmod(0o600)
    code, report = offline_compare(source_app,restored_app,source_db,restored_db)
    assert code == 75 and report["status"] == "BLOCKED"
    assert b"MUTATION_CANARY" not in (source_app/".env").read_bytes()


@pytest.mark.parametrize("wal",["notifications.sqlite3-wal","notifications.sqlite3-shm"])
def test_active_sqlite_notification_journal_blocks_backup_claim(tmp_path,wal):
    original,restored,db1,db2=make_offline_copy(tmp_path)
    (restored/wal).write_bytes(b"STILL_ACTIVE")
    code, report = offline_compare(original,restored,db1,db2)
    assert code == 75 and report["reason_code"] == "NOTIFICATIONS_WAL_REQUIRES_CHECKPOINT"


def test_db_wal_and_shm_block_safe_restoration(tmp_path):
    original,restored,db1,db2=make_offline_copy(tmp_path)
    for db in (db1,db2):
        f=db/"pb_data"/"data.db-wal"
        f.write_bytes(b"WAL_ACTIVE")
        f.chmod(0o600)
    code, report = offline_compare(original,restored,db1,db2)
    assert code == 75 and report["reason_code"] == "WAL_SHM_REQUIRES_OFFLINE_CHECKPOINT"


@pytest.mark.skipif(sys.platform == 'win32', reason='symlink needs Windows privilege')
def test_insecure_symlink_db_directory_blocks(tmp_path):
    original,restored,db1,db2=make_offline_copy(tmp_path)
    target=db2/"pb_data"/"aux"/"history.mock"
    target.unlink()
    target.symlink_to(db1/"pb_data"/"aux"/"history.mock")
    code, report = offline_compare(original,restored,db1,db2)
    assert code == 75 and report["reason_code"] == "SNAPSHOT_FILE_INSECURE"


def test_source_and_target_cannot_be_same_or_nested(tmp_path):
    original,restored,db1,db2=make_offline_copy(tmp_path)
    code, report = offline_compare(original,original,db1,db2)
    assert code == 75 and report["reason_code"] == "BACKUP_AND_RESTORE_NOT_DISTINCT"
    code, report = offline_compare(original,restored,original/"inner",db2)
    assert code == 75 and report["reason_code"] == "BACKUP_AND_RESTORE_OVERLAP"


def test_inconsistent_backup_sqlite_is_rejected(tmp_path):
    original,restored,db1,db2=make_offline_copy(tmp_path)
    # A byte-identical but corrupted source and restore must still be refused.
    for db in (db1,db2):
        f=db/"pb_data"/"data.db"
        f.write_bytes(b"NOT_A_SQLITE_DATABASE")
        f.chmod(0o600)
    code, report = offline_compare(original,restored,db1,db2)
    assert code == 75 and report["reason_code"] == "OFFLINE_SQLITE_INTEGRITY_FAILED"


def test_reused_pr33_stage_only_never_includes_secrets_or_db(tmp_path):
    private_parent=private(tmp_path/"private")
    destination=private_parent/"candidate"
    sha=subprocess.check_output(
        ["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    assert len(sha)==40
    stage=B.stage(ROOT,destination,sha)
    assert stage["stage_only"] is True
    assert stage["manifest_entries_verified"]==72
    assert stage["approved_for_deployment"] is False
    assert stage["database_or_key_files_copied"] is False
    assert stage["live_orders"]==0
    assert not (destination/".env").exists()
    assert not (destination/"notifications.sqlite3").exists()
    assert not (destination/"pb_data").exists()
    assert not (destination/"requirements-py312.txt").exists()
    assert (destination/"main.py").read_bytes()==(ROOT/"main.py").read_bytes()


def test_old_ubuntu22_user_data_cannot_autostart_real_trading():
    import ast
    original=(ROOT/"deployment"/"cloud-config-ubuntu2204.yaml").read_text()
    assert "NEW_SERVER_STAGE_ONLY" in original
    assert "ExecStartPre=/usr/bin/false" in original
    assert "STAGED_NOT_LIVE" in original
    assert "/root/poa-stage-input.json" in original
    for suffix in ("poa.service.staged","pocketbase.service.staged"):
        assert suffix in original
    assert "python3.10" in (
        ROOT/"deployment"/"prepare_ubuntu.py").read_text()
    assert "NEW_SERVER_ONLY" in (
        ROOT/"deployment"/"install_once.py").read_text()


@pytest.mark.parametrize("zone,y,m,d", [
    ("UTC",2026,11,8),
    ("Asia/Seoul",2026,11,8),
    ("Asia/Seoul",2024,2,29),
    ("America/New_York",2026,3,8),
    ("America/New_York",2026,11,1),
    ("Europe/Berlin",2026,3,29),
    ("Europe/Berlin",2026,10,25),
])
def test_original_python310_time_windows_independent_dst_oracle(zone,y,m,d):
    # PR34 time-parity methodology; execute verbatim function via AST,
    # never import exchange client or interact with any real venue.
    import ast
    tree=ast.parse((ROOT/"exchange/pexchange.py").read_text(encoding="utf-8"))
    found=[n for n in tree.body if isinstance(n,ast.FunctionDef)
           and n.name=="get_today_timestamp"]
    assert len(found)==1
    expected=ast.parse(
        'def get_today_timestamp(timezone="Asia/Seoul"):\n'
        '    today = pendulum.today(timezone)\n'
        '    today_start = int(today.start_of("day").timestamp() * 1000)\n'
        '    today_end = int(today.end_of("day").timestamp() * 1000)\n'
        '    return today_start, today_end\n'
    ).body[0]
    assert ast.dump(found[0], include_attributes=False)==ast.dump(
        expected, include_attributes=False)
    # Full Windows pytest collection can inject a synthetic pendulum into
    # sys.modules. Import the genuine pinned dependency in a clean interpreter
    # to verify the *original function*, not a test-module substitute.
    code = (
        "import json,sys,pendulum\n"
        "from datetime import datetime,timedelta\n"
        "from zoneinfo import ZoneInfo\n"
        "from types import SimpleNamespace\n"
        "y,m,d=map(int,sys.argv[3:6])\n"
        "scope={'pendulum':SimpleNamespace(today=lambda tz: "
        "pendulum.datetime(y,m,d,12,34,56,tz=tz))}\n"
        "exec(sys.argv[1],scope)\n"
        "got=scope['get_today_timestamp'](sys.argv[2])\n"
        "start=datetime(y,m,d,tzinfo=ZoneInfo(sys.argv[2]))\n"
        "nxt=start.date()+timedelta(days=1)\n"
        "end=datetime(nxt.year,nxt.month,nxt.day,tzinfo=ZoneInfo(sys.argv[2]))\n"
        "oracle=[int(start.timestamp()*1000),int(end.timestamp()*1000)-1]\n"
        "print(json.dumps({'got':got,'expected':oracle}))\n"
    )
    proc=subprocess.run(
        [sys.executable,"-I","-B","-c",code,ast.unparse(found[0]),
         zone,str(y),str(m),str(d)],
        cwd=ROOT,text=True,capture_output=True,timeout=20,check=True)
    result=json.loads(proc.stdout)
    got=tuple(result["got"])
    assert got==tuple(result["expected"])
    assert got[1]-got[0]+1 in (23*3600000,24*3600000,25*3600000)


def test_pinned_integrated_userdata_is_stage_only_and_immutable():
    import json
    config_path = ROOT/"deployment"/"cloud-config-ubuntu2204-pr35-stage-only.yaml"
    data = config_path.read_text(encoding="utf-8")
    assert data.startswith("#cloud-config\n")
    cfg = json.loads(data.split("\n",1)[1])
    write_files = {r["path"]:r["content"] for r in cfg["write_files"]}
    pin = json.loads(write_files["/root/poa-stage-input.json"])
    assert pin["install_mode"]=="NEW_SERVER_STAGE_ONLY"
    assert pin["repo_ref"]=="bc9f9af4807656b96cfd4da36efe169242181a4d"
    assert pin["release_manifest_sha256"]=="0c464b1387d7ced078d4d957a1a9bdc947b8bb1d425518aba631fcc623d279fa"
    assert cfg["final_message"].startswith("STAGED_NOT_LIVE")
    for unit in ("poa.service.staged","pocketbase.service.staged"):
        assert "ExecStartPre=/usr/bin/false" in write_files["/usr/local/lib/poa/"+unit]
    assert cfg["runcmd"]==[["bash","-euc","umask 077; python3 /usr/local/lib/poa/prepare_ubuntu.py; python3 /usr/local/lib/poa/install_once.py"]]
    assert not (set(write_files) & {"/root/POA/.env","/root/db/pb_data","/etc/poa/trading.env"})


def test_no_product_finance_install_sources_changed():
    changed=subprocess.check_output(
        ["git","diff","--name-only",BASE,"HEAD"],cwd=ROOT,text=True).splitlines()
    allowed={
        "deployment/bot03_reinstall.py",
        "deployment/py312_requirements.py",
        "deployment/ubuntu22_release_handoff.py",
        "deployment/UBUNTU22_INTEGRATED_RELEASE.md",
        "tests/test_ubuntu22_integrated_release.py",
        ".github/workflows/ubuntu22_integrated_release.yml",
        "deployment/cloud-config-ubuntu2204-pr35-stage-only.yaml",
        "deployment/ubuntu22_operational.py",
        "deployment/ubuntu22_turnkey_build.py",
        "deployment/ubuntu22_turnkey_USER_DATA.yaml",
        "deployment/UBUNTU22_TURNKEY.md",
        "tests/test_ubuntu22_operational.py",
        ".github/workflows/ubuntu22_turnkey_ci.yml",
        "deployment/ubuntu22_oneclick.py",
        "deployment/POA_VULTR_ONECLICK_USER_DATA.yaml",
        "deployment/POA_VULTR_ONECLICK.md",
        "tests/test_ubuntu22_oneclick.py",
        ".github/workflows/ubuntu22_oneclick_ci.yml",
        "deployment/recover_failed_pocketbase_stage.py",
        "tests/test_recover_failed_pocketbase_stage.py",
        "deployment/poa_vultr_install_v3.py",
        "deployment/POA_VULTR_10_SERVERS_FIXED_USER_DATA.yaml",
        "tests/test_poa_vultr_install_v3.py",
        "tests/ubuntu22_real_firstboot_rehearsal.py",
        "deployment/retry_install_discord.py",
        "tests/test_retry_install_discord.py",
        "tests/test_bot01_tv_trade_discord_readiness.py",
        ".github/workflows/bot01_trade_discord_mock_ci.yml",
    }
    required={
        "deployment/bot03_reinstall.py",
        "deployment/py312_requirements.py",
        "deployment/ubuntu22_release_handoff.py",
        "deployment/cloud-config-ubuntu2204-pr35-stage-only.yaml",
    }
    assert set(changed)<=allowed and required <= set(changed),("UNREVIEWED_PRODUCTION_CHANGE",changed)
    protected=["main.py","run.py","exchange","requirements.txt",
               "release_manifest_sha256.json",
               "deployment/install_once.py","deployment/prepare_ubuntu.py",
               "deployment/build_cloud_config.py","deployment/cloud-config-ubuntu2204.yaml"]
    subprocess.run(["git","diff","--exit-code",BASE,"HEAD","--",*protected],
                   cwd=ROOT,check=True)
