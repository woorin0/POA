"""Offline BOT03 existing-server reinstall staging and preservation tests.

All writes stay inside pytest isolated temp directories. Never touch BOT03,
exchange accounts, real .env, real PocketBase, active services or orders.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from deployment import bot03_reinstall as B


def git(root, *args):
    proc = subprocess.run(["git", "-C", str(root), *args],
                          text=True, capture_output=True, timeout=20)
    assert proc.returncode == 0, (args, proc.stderr[-500:])
    return proc.stdout.strip()


def make_repo(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Mock Evidence Only")
    git(root, "config", "user.email", "offline@example.invalid")
    files = {"main.py": b"# inert only\n", "run.py": b"# no application start\n",
             "requirements.txt": b""}
    for i in range(69):
        files[f"release_checks/mock_asset_{i:02d}.txt"] = (
            f"offline-{i:02d}\n".encode("ascii")
        )
    for path, body in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    manifest = [
        {"Path": path, "Bytes": len(body),
         "SHA256": hashlib.sha256(body).hexdigest().upper()}
        for path, body in sorted(files.items())
    ]
    (root / "release_manifest_sha256.json").write_text(json.dumps(manifest))
    git(root, "add", ".")
    git(root, "commit", "-m", "offline fixture 72-source manifest")
    return root, git(root, "rev-parse", "HEAD")


def private_parent(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def test_inspect_existing_paths_metadata_only_no_secret_read(tmp_path):
    app = tmp_path / "existing_POA"
    db = tmp_path / "existing_db"
    app.mkdir(); db.mkdir()
    (db / "pb_data").mkdir()
    (app / "run.py").write_text("FAKE_ONLY")
    (app / ".env").write_text("BINANCE_SECRET=SYNTHETIC_CANARY_DO_NOT_PRINT")
    (db / "pocketbase").write_bytes(b"MOCK_EXECUTABLE")
    result = B.inspect(app, db)
    assert result["metadata_gate"] == "PASS"
    assert all(x == "PASS" for x in result["checks"].values())
    assert "SYNTHETIC_CANARY_DO_NOT_PRINT" not in json.dumps(result)
    assert result["exchange_key_integrity_and_permissions"] == "UNVERIFIED"
    assert result["sqlite_wal_shm_consistency"] == "UNVERIFIED"
    assert result["database_backup_consistency"] == "UNVERIFIED"
    assert result["approved_for_reinstall"] is False


def test_missing_or_symlinked_db_fails_closed(tmp_path):
    app = tmp_path/"app"; db=tmp_path/"db"
    app.mkdir(); db.mkdir()
    # Missing directory must block on every OS; symlink requires Windows privilege.
    if sys.platform != "win32":
        (db/"pb_data").symlink_to(app, target_is_directory=True)
    report=B.inspect(app,db)
    assert report["metadata_gate"]=="BLOCKED"
    assert report["checks"]["pocketbase_data_directory"]=="BLOCKED"
    assert report["originals_modified"] is False


def test_pinned_clean_fixture_stages_only_into_new_private_directory(tmp_path):
    source, sha = make_repo(tmp_path)
    parent = private_parent(tmp_path)
    dst = parent/"staged"
    (source/".env").write_text("PASSWORD=LOCAL_PRIVATE_CANARY")
    (source/"notifications.sqlite3").write_bytes(b"LOCAL_DB_CANARY")
    protected_app = tmp_path / "BOT03"
    protected_db = tmp_path / "BOT03_db"
    result = B.stage(source, dst, sha, protected_app=protected_app,protected_db=protected_db)
    assert result["pinned_source_commit"] == sha
    assert result["manifest_entries_verified"] == 72
    assert result["tracked_files_staged"] == 73
    assert result["approved_for_deployment"] is False
    assert result["database_or_key_files_copied"] is False
    assert result["live_orders"] == 0
    assert (dst/"main.py").read_bytes() == b"# inert only\n"
    assert not (dst/".env").exists()
    assert not (dst/"notifications.sqlite3").exists()
    assert "LOCAL_PRIVATE_CANARY" not in json.dumps(result)
    assert "LOCAL_DB_CANARY" not in json.dumps(result)
    marker=json.loads((dst/"STAGED_ONLY_NOT_LIVE.json").read_text())
    assert marker == result
    with pytest.raises(B.ReinstallBlocked,match="STAGE_DESTINATION_ALREADY_EXISTS"):
        B.stage(source,dst,sha,protected_app=protected_app,protected_db=protected_db)
    assert not (protected_app).exists() and not (protected_db).exists()


def test_pinned_head_mismatch_is_blocked_before_destination_creation(tmp_path):
    source, sha = make_repo(tmp_path)
    dst = private_parent(tmp_path)/"stage"
    with pytest.raises(B.ReinstallBlocked, match="SOURCE_HEAD_NOT_PINNED"):
        B.stage(source,dst,"0"*40)
    assert not dst.exists()


def test_dirty_tracked_source_does_not_stage(tmp_path):
    source, sha = make_repo(tmp_path)
    (source/"main.py").write_text("MODIFIED")
    dst=private_parent(tmp_path)/"stage"
    with pytest.raises(B.ReinstallBlocked,match="SOURCE_TRACKED_FILES_DIRTY"):
        B.stage(source,dst,sha)
    assert not dst.exists()


def test_manifest_mismatch_blocks_even_if_git_clean(tmp_path):
    source, _ = make_repo(tmp_path)
    (source/"main.py").write_bytes(b"CORRUPT")
    git(source,"add","main.py")
    git(source,"commit","-m","fixture inconsistent manifest")
    sha=git(source,"rev-parse","HEAD")
    dst=private_parent(tmp_path)/"stage"
    with pytest.raises(B.ReinstallBlocked,match="RELEASE_MANIFEST_MISMATCH"):
        B.stage(source,dst,sha)
    assert not dst.exists()


def test_tracked_secret_or_db_denied(tmp_path):
    source,_=make_repo(tmp_path)
    (source/".env").write_text("BINANCE_SECRET=NEVER_COPY")
    git(source,"add","-f",".env")
    git(source,"commit","-m","fixture forbidden tracked secret")
    sha=git(source,"rev-parse","HEAD")
    dst=private_parent(tmp_path)/"stage"
    with pytest.raises(B.ReinstallBlocked,match="TRACKED_SECRET_OR_DB_PATH"):
        B.stage(source,dst,sha)
    assert not dst.exists()


@pytest.mark.parametrize("unsafe", [
    ".env", ".env.production", "pb_data/index", "data/orders.sqlite3",
    "data/orders.sqlite3-wal", "cert/private.key", "../main.py",
    "/etc/poa/.env", "nested/../../db", "x\\y",
])
def test_untrusted_manifest_and_source_path_is_rejected(unsafe):
    with pytest.raises(B.ReinstallBlocked):
        B._relative_file(unsafe)


def test_destination_cannot_overlap_any_preserved_app_db(tmp_path):
    source,sha=make_repo(tmp_path)
    protected_app=tmp_path/"BOT03"
    protected_db=tmp_path/"data"
    protected_app.mkdir(); protected_db.mkdir()
    for dst in (protected_app/"staged",protected_db/"new",source/"temp"):
        with pytest.raises(B.ReinstallBlocked,match="STAGE_OVERLAPS_PROTECTED_DATA"):
            B.stage(source,dst,sha,protected_app=protected_app,protected_db=protected_db)
        assert not dst.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission model")
def test_private_destination_parent_required(tmp_path):
    source,sha=make_repo(tmp_path)
    parent=tmp_path/"public"
    parent.mkdir()
    parent.chmod(0o755)
    dst=parent/"staged"
    with pytest.raises(B.ReinstallBlocked,match="STAGE_PARENT_MUST_BE_OWNER_PRIVATE"):
        B.stage(source,dst,sha)
    assert not dst.exists()


def test_invalid_new_stage_result_never_claims_installed(tmp_path,capsys):
    source,sha=make_repo(tmp_path)
    result=B.main(["stage","--source",str(source),"--destination",
                   str(tmp_path/"non_private"/"destination"),"--expected-head",sha])
    report=json.loads(capsys.readouterr().out)
    assert result==75
    assert report["approved_for_reinstall"] is False
    assert report["install_activated"] is False
    assert report["originals_modified"] is False


def test_actual_poa_checkout_can_stage_whole_verified_source_without_db_or_keys(tmp_path):
    """Exercise the real CI checkout, not only synthetic 72-file fixtures."""
    source = Path(__file__).resolve().parents[1]
    sha = git(source, "rev-parse", "HEAD")
    target = private_parent(tmp_path)/"verified-release"
    evidence = B.stage(source,target,sha)
    assert evidence["pinned_source_commit"] == sha
    assert evidence["manifest_entries_verified"] == 72
    assert evidence["tracked_files_staged"] >= 72
    assert evidence["approved_for_deployment"] is False
    assert (target/"release_manifest_sha256.json").read_bytes() == (
        source/"release_manifest_sha256.json").read_bytes()
    for untracked in (".env","notifications.sqlite3","notifications.sqlite3-wal",
                      "notifications.sqlite3-shm","pb_data"):
        assert not (target/untracked).exists()


def test_only_additive_pr_files_and_original_financial_tree_is_unchanged():
    root=Path(__file__).resolve().parents[1]
    baseline=B.ORIGINAL_MAIN
    changed=set(git(root,"diff","--name-only",baseline,"HEAD").splitlines())
    allowed={
        "deployment/bot03_reinstall.py",
        "deployment/BOT03_PRESERVE_REINSTALL.md",
        "tests/test_bot03_reinstall.py",
        ".github/workflows/bot03_reinstall_ci.yml",
    }
    assert changed==allowed,("UNAUTHORIZED_DELTA",sorted(changed ^ allowed))
    git(root,"diff","--exit-code",baseline,"HEAD","--",
        "main.py","run.py","exchange","requirements.txt",
        "release_manifest_sha256.json","deployment/install_once.py",
        "deployment/prepare_ubuntu.py","deployment/build_cloud_config.py")
    # Every item in the 72-entry original verified release manifest stays pinned.
    assert json.loads((root/"release_manifest_sha256.json").read_text()) == json.loads(
        subprocess.check_output(["git","show",baseline+":release_manifest_sha256.json"],cwd=root)
    )


def test_existing_installer_is_fresh_install_only_and_not_used_by_candidate():
    root=Path(__file__).resolve().parents[1]
    old=(root/"deployment/install_once.py").read_text(encoding="utf-8")
    new=(root/"deployment/bot03_reinstall.py").read_text(encoding="utf-8")
    assert "EXISTING_INSTALLATION_PRESERVED_STOP" in old
    assert "NEW_SERVER_STAGE_ONLY" in old
    assert "STAGE_DESTINATION_ALREADY_EXISTS" in new
    assert "approved_for_deployment" in new
    assert not any(x in new for x in (
        "systemctl start", "systemctl stop", "apt-get install",
        "docker compose up", "git clone", "create_order(", "urlopen(",
        "shutil.rmtree", "os.replace(", "os.unlink(",
    ))
    assert "def install(" not in new


def test_known_unverified_operational_requirements_are_not_hidden():
    doc=(Path(__file__).resolve().parents[1]/
         "deployment/BOT03_PRESERVE_REINSTALL.md").read_text(encoding="utf-8")
    for expected in (
        "PRODUCTION_READY=BLOCKED",
        "DB_BACKUP_AND_RESTORE=UNVERIFIED",
        "API_KEYS_PRESERVED_ON_HOST=UNVERIFIED",
        "SERVICE_TOPOLOGY=UNVERIFIED",
        "NO_MERGE_NO_DEPLOY_NO_LIVE_ORDERS",
    ):
        assert expected in doc
