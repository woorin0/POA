"""New-server operational release: mocked systemd, DB restore and ingress E2E.

No real exchange request, DB migration, systemd or network actions.
Existing financial code and original Stage User Data remain unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from unittest.mock import Mock
import pytest

from deployment import ubuntu22_operational as O

def priv(path, *, directory=False):
    if directory:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    else:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(b"MOCK_ONLY_NOT_SECRET")
        path.chmod(0o600)
    return path

def test_operator_local_units_never_expose_external_trading(tmp_path, monkeypatch):
    app=priv(tmp_path/"app",directory=True)
    db=priv(tmp_path/"db",directory=True)
    units=priv(tmp_path/"units",directory=True)
    monkeypatch.setattr(O, "APP", app)
    monkeypatch.setattr(O, "DB", db)
    monkeypatch.setattr(O, "SERVICE_DIR", units)
    calls=[]
    monkeypatch.setattr(O, "_run",lambda cmd,*a,**kw:calls.append(cmd))
    O._install_units()
    poa=(units/"poa.service").read_text()
    pb=(units/"pocketbase.service").read_text()
    assert "--host=127.0.0.1 --port=8000" in poa
    assert "127.0.0.1:8090" in pb
    assert "LOCAL_READY" in poa
    assert "validate_config.py" in poa
    assert "0.0.0.0" not in poa+pb
    assert not (tmp_path/"etc"/"Caddyfile").exists()
    assert [a[:2] for a in calls].count(["systemctl","unmask"]) == 2
    assert not any("enable" in a or "start" in a for a in calls)
    with pytest.raises(O.SetupBlocked,match="EXISTING_SERVICE_PRESERVED_STOP"):
        O._install_units()

def test_fresh_requires_local_config_then_starts_only_loopback(monkeypatch,tmp_path):
    app=priv(tmp_path/"app",directory=True)
    db=priv(tmp_path/"db",directory=True)
    config=priv(tmp_path/"secret-local.json")
    config.write_text(json.dumps({"domain":"poa.example.org","credentials":{
        "PASSWORD":"synthetic", "DISCORD_WEBHOOK_URL":
        "https://discord.com/api/webhooks/123/mockurl",
        "UPBIT_KEY":"mock","UPBIT_SECRET":"mock"}}))
    config.chmod(0o600)
    monkeypatch.setattr(O,"APP",app)
    monkeypatch.setattr(O,"DB",db)
    monkeypatch.setattr(O,"_stage_ready",lambda: {"status":"STAGED_NOT_LIVE"})
    monkeypatch.setattr(O.legacy,"validate",lambda cfg:cfg["credentials"])
    monkeypatch.setattr(O,"_legacy_binary_for_fresh",lambda:True)
    monkeypatch.setattr(O,"_make_env",lambda data,email,password:True)
    called=[]
    monkeypatch.setattr(O,"_start_services",lambda **args:called.append(args) or
                        {"state":"LOCAL_RUNNING_NO_PUBLIC_WEBHOOK"})
    result=O.fresh(config)
    assert result["state"]=="LOCAL_RUNNING_NO_PUBLIC_WEBHOOK"
    assert len(called)==1 and called[0]["fresh"] is True
    assert len(called[0]["admin_password"])>=32
    assert "poa" not in str(tmp_path/"public")
    assert not (app/".env").exists()  # Mock-only no sensitive writes.
    assert not (app/"LOCAL_READY").exists()  # Not a real activation.

def test_no_credential_in_userdata_and_original_finance_unchanged():
    root=Path(__file__).resolve().parents[1]
    original=(root/"deployment"/"cloud-config-ubuntu2204-pr35-stage-only.yaml").read_text()
    assert "ExecStartPre=/usr/bin/false" in original
    source=(root/"deployment"/"ubuntu22_operational.py").read_text()
    assert "LOCAL_RUNNING_NO_PUBLIC_WEBHOOK" in source
    assert "publish(domain" in source
    assert "create_order(" not in source
    assert "systemctl" in source
    assert "curl" in source
    assert not any(p.exists() for p in (root/".env",root/"poa-key.json"))
    assert (root/"release_manifest_sha256.json").is_file()

def test_restore_preserves_original_snapshot_bytes_and_pocketbase_binary(tmp_path,monkeypatch):
    if sys.platform=="win32":
        pytest.skip("POSIX private-file mode and real backup validation")
    app=priv(tmp_path/"app",directory=True)
    db=priv(tmp_path/"db",directory=True)
    priv(db/"pocketbase")
    backup=priv(tmp_path/"offline-backup",directory=True)
    ba=priv(backup/"app",directory=True)
    bd=priv(backup/"db",directory=True)
    priv(ba/".env")
    priv(ba/"notifications.sqlite3")
    priv(ba/"safe_orders.sqlite3")
    priv(bd/"pocketbase")
    priv(bd/"pb_data",directory=True)
    priv(bd/"pb_data"/"data.db")
    priv(bd/"pb_data"/"audit.sqlite3")
    monkeypatch.setattr(O,"APP",app)
    monkeypatch.setattr(O,"DB",db)
    monkeypatch.setattr(O,"_stage_ready",lambda: {"status":"STAGED_NOT_LIVE"})
    from deployment import ubuntu22_release_handoff as handoff
    checked=[]
    monkeypatch.setattr(handoff,"_sqlite_read_only",lambda p:checked.append(p))
    report=O.restore(backup)
    assert report["state"]=="RESTORED_NOT_STARTED"
    assert report["original_pocketbase_binary"]=="PRESERVED"
    assert len(checked)==2
    assert (db/"pocketbase.stage-0.22.48").read_bytes()==b"MOCK_ONLY_NOT_SECRET"
    for f in (app/".env",app/"notifications.sqlite3",
              app/"safe_orders.sqlite3",db/"pb_data"/"data.db",
              db/"pb_data"/"audit.sqlite3"):
        assert f.read_bytes()==b"MOCK_ONLY_NOT_SECRET"
    assert not (app/"LOCAL_READY").exists()
    assert not (app/"LOCAL_READY_RESULT.json").exists()
    assert not (tmp_path/"etc"/"Caddyfile").exists()

def test_restore_with_active_wal_blocks_without_writing(tmp_path,monkeypatch):
    if sys.platform=="win32":
        pytest.skip("POSIX backup permission check")
    app=priv(tmp_path/"app",directory=True)
    db=priv(tmp_path/"db",directory=True)
    priv(db/"pocketbase")
    backup=priv(tmp_path/"backup",directory=True)
    ba=priv(backup/"app",directory=True)
    bd=priv(backup/"db",directory=True)
    priv(ba/".env");priv(ba/"notifications.sqlite3")
    priv(bd/"pocketbase")
    priv(bd/"pb_data",directory=True)
    priv(bd/"pb_data"/"data.db")
    priv(ba/"notifications.sqlite3-wal")
    monkeypatch.setattr(O,"APP",app)
    monkeypatch.setattr(O,"DB",db)
    monkeypatch.setattr(O,"_stage_ready",lambda:True)
    with pytest.raises(O.SetupBlocked,match="RESTORE_SNAPSHOT_WAL_UNCHECKPOINTED"):
        O.restore(backup)
    assert not (app/".env").exists()
    assert not (db/"pb_data").exists()
    assert not (db/"pocketbase.stage-0.22.48").exists()

def test_activate_restored_calls_db_and_local_health_only(monkeypatch,tmp_path):
    app=priv(tmp_path/"app",directory=True)
    db=priv(tmp_path/"db",directory=True)
    priv(app/".env");priv(app/"notifications.sqlite3")
    priv(db/"pb_data"/"data.db")
    monkeypatch.setattr(O,"APP",app)
    monkeypatch.setattr(O,"DB",db)
    monkeypatch.setattr(O,"_stage_ready",lambda:True)
    from deployment import ubuntu22_release_handoff as handoff
    checked=[]
    monkeypatch.setattr(handoff,"_sqlite_read_only",lambda path:checked.append(path))
    called=[]
    monkeypatch.setattr(O,"_start_services",lambda **kw: called.append(kw) or
                        {"state":"LOCAL_RUNNING_NO_PUBLIC_WEBHOOK"})
    result=O.activate_restored()
    assert result["state"]=="LOCAL_RUNNING_NO_PUBLIC_WEBHOOK"
    assert called==[{"fresh":False}]
    assert len(checked)==2

def test_manual_publish_only_after_local_result_and_original_caddy_backup(monkeypatch,tmp_path):
    app=priv(tmp_path/"app",directory=True)
    marker=priv(app/"LOCAL_READY")
    priv(app/"LOCAL_READY_RESULT.json")
    caddy=priv(tmp_path/"etc"/"Caddyfile")
    caddy.write_text("STOCK_CADDY_ONLY\n")
    monkeypatch.setattr(O,"APP",app)
    monkeypatch.setattr(O,"CADDYFILE",caddy)
    actions=[]
    monkeypatch.setattr(O,"_run",lambda args,*a,**kw:actions.append(args))
    with pytest.raises(O.SetupBlocked,match="VALID_DOMAIN_REQUIRED"):
        O.publish("notvalid")
    assert caddy.read_text()=="STOCK_CADDY_ONLY\n"
    report=O.publish("trading.example.org")
    assert report["trading_webhook_exposed"] is True
    assert (tmp_path/"etc"/"Caddyfile.poa.previous").read_text()=="STOCK_CADDY_ONLY\n"
    assert "reverse_proxy 127.0.0.1:8000" in caddy.read_text()
    assert any(a[:3]==["systemctl","enable","--now"] for a in actions)
    with pytest.raises(O.SetupBlocked,match="CADDY_ALREADY_CONFIGURED"):
        O.publish("trading.example.org")

def test_stage_check_blocks_unverified_host(monkeypatch,tmp_path):
    monkeypatch.setattr(O,"APP",tmp_path/"missing-app")
    monkeypatch.setattr(O,"DB",tmp_path/"missing-db")
    monkeypatch.setattr(O,"STAGE_RESULT",tmp_path/"missing-stage.json")
    monkeypatch.setattr(O.os,"geteuid",lambda:0, raising=False)
    with pytest.raises((O.SetupBlocked, FileNotFoundError)):
        O._stage_ready()

def test_user_data_is_one_compact_pinned_script_without_secrets_or_auto_orders():
    from deployment import ubuntu22_turnkey_build as B
    sha="a"*40
    raw=B.create(sha)
    assert raw.startswith("#cloud-config\n")
    assert len(raw.encode("utf-8")) < 16000
    cfg=json.loads(raw.split("\n",1)[1])
    files={entry["path"]:entry for entry in cfg["write_files"]}
    pin=json.loads(files["/root/poa-stage-input.json"]["content"])
    assert pin["repo_ref"]==sha
    assert pin["install_mode"]=="NEW_SERVER_STAGE_ONLY"
    assert pin["release_manifest_sha256"]==B.MANIFEST_DIGEST
    checks=json.loads(files["/usr/local/lib/poa/verified_script_hashes.json"]["content"])
    for item in B.SCRIPTS:
        assert checks[item]==hashlib.sha256(
            (B.HERE/item).read_bytes()).hexdigest()
    code=files["/usr/local/lib/poa/bootstrap-ubuntu22.py"]["content"]
    compile(code,"<no-secrets-single-user-data-bootstrap>","exec")
    assert "STAGE_PREPARATION_OR_INSTALL_FAILED" in code
    assert cfg["runcmd"]==[["bash","-euc",
        "umask 077; python3 /usr/local/lib/poa/bootstrap-ubuntu22.py"]]
    assert "LOCAL_RUNNING_NO_PUBLIC_WEBHOOK" not in raw
    assert "CHANGE_ME" in raw
    assert "UPBIT_SECRET=real" not in raw
    assert "Caddyfile" not in files
    assert "Ubuntu" not in str(pin["repo_ref"])
    assert "exec /usr/bin/python3" in files["/usr/local/bin/poa-setup"]["content"]


def test_committed_userdata_matches_generator_and_fixed_ci_green_source():
    from deployment import ubuntu22_turnkey_build as B
    pinned="cea1b7fc9d349249b92040f9a768cf66bdf428af"
    expected=B.create(pinned)
    actual=(B.HERE/"ubuntu22_turnkey_USER_DATA.yaml").read_text(encoding="utf-8")
    assert actual==expected
    assert actual.startswith("#cloud-config\n")
    assert len(actual.encode("utf-8"))<16000
    assert "STAGED_READY_NO_TRADES" in actual
    assert not any(x in actual for x in (
        "systemctl start poa", "poa-setup publish",
        '"/root/POA/.env"', '"/root/db/pb_data"'
    ))


def test_user_data_requires_fixed_commit_and_has_one_use_secret_template():
    from deployment import ubuntu22_turnkey_build as B
    for invalid in ("main","pr35","", "../"*20,"A"*40):
        with pytest.raises(ValueError):
            B.create(invalid)
    config=B.create("0"*40)
    cfg=json.loads(config.split("\n",1)[1])
    paths=[x["path"] for x in cfg["write_files"]]
    assert len(paths)==len(set(paths))
    assert paths.count("/root/poa-config.example.json")==1


def test_full_git_delta_does_not_modify_financial_order_source():
    root=Path(__file__).resolve().parents[1]
    ancestor="e1f63bcce3e73953eb75dcaf9604d23f0c2f341f"
    changed=subprocess.check_output(["git","diff","--name-only",ancestor,"HEAD"],
                                    cwd=root,text=True).splitlines()
    expected={
        "deployment/ubuntu22_operational.py",
        "deployment/ubuntu22_turnkey_build.py",
        "deployment/ubuntu22_turnkey_USER_DATA.yaml",
        "tests/test_ubuntu22_operational.py",
        ".github/workflows/ubuntu22_turnkey_ci.yml",
        "deployment/UBUNTU22_TURNKEY.md",
        "tests/test_ubuntu22_integrated_release.py",
        ".github/workflows/ubuntu22_integrated_release.yml",
    }
    assert set(changed)<=expected,("UNREQUESTED_CHANGE",changed)
    protected=["main.py","run.py","exchange","requirements.txt",
              "release_manifest_sha256.json","release_checks",
              "deployment/install_once.py","deployment/prepare_ubuntu.py"]
    subprocess.run(["git","diff","--exit-code",ancestor,"HEAD","--",*protected],
                   cwd=root,check=True)
