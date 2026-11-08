"""No order calls: test complete Vultr cloud-init handoff and original installer."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import sys

import pytest
from deployment import ubuntu22_oneclick as O

ROOT = Path(__file__).resolve().parents[1]


def valid_input():
    return {
        "domain": "AUTO",
        "db_admin_email": "poa-admin@example.org",
        "credentials": {
            "PASSWORD": "AUTO",
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/12345/token_123",
            "UPBIT_KEY": "synthetic_upbit_key",
            "UPBIT_SECRET": "synthetic_upbit_secret",
        },
    }


def test_auto_hostname_password_and_preserve_exchange_configuration():
    data = valid_input()
    result = O.normalized_input(data, public_ip="203.0.113.7" if False else "8.8.8.8")
    assert result["domain"] == "8.8.8.8.sslip.io"
    assert len(result["credentials"]["PASSWORD"]) >= 32
    assert result["credentials"]["PASSWORD"] != "AUTO"
    assert result["credentials"]["UPBIT_KEY"] == "synthetic_upbit_key"
    assert result["credentials"]["DISCORD_WEBHOOK_URL"].startswith("https://discord.com/")
    assert data["domain"] == "AUTO" and data["credentials"]["PASSWORD"] == "AUTO"


@pytest.mark.parametrize("field", ["DISCORD_WEBHOOK_URL", "UPBIT_KEY", "UPBIT_SECRET"])
def test_unfilled_credential_rejected_without_mutation(field):
    value = valid_input()
    value["credentials"][field] = "SET_" + field
    with pytest.raises(O.OneclickError, match="SET_REQUIRED_CREDENTIAL_" + field):
        O.normalized_input(value, public_ip="8.8.8.8")


def test_existing_domain_avoids_external_ip_discovery(monkeypatch):
    data = valid_input()
    data["domain"] = "my-webhook.example.org"
    monkeypatch.setattr(O, "public_ipv4",
                        lambda: pytest.fail("manual domain must not call public IPv4"))
    out = O.normalized_input(data)
    assert out["domain"] == "my-webhook.example.org"


def test_cloud_init_full_install_embeds_exact_code_and_stage_preserves_manifest():
    user_data = (ROOT / "deployment" / "POA_VULTR_ONECLICK_USER_DATA.yaml").read_text()
    assert user_data.startswith("#cloud-config\n")
    cfg = json.loads(user_data.split("\n", 1)[1])
    embedded = {entry["path"]: entry for entry in cfg["write_files"]}
    original = json.loads(
        (ROOT / "deployment" / "ubuntu22_turnkey_USER_DATA.yaml").read_text().split("\n", 1)[1]
    )
    existing = {entry["path"]: entry for entry in original["write_files"]}
    for path in existing:
        assert embedded[path] == existing[path]
    assert embedded["/usr/local/lib/poa/ubuntu22_oneclick.py"]["content"] == (
        ROOT / "deployment" / "ubuntu22_oneclick.py"
    ).read_text()
    ast.parse(embedded["/usr/local/lib/poa/ubuntu22_oneclick.py"]["content"])
    first = cfg["runcmd"]
    assert len(first) == 2
    assert "bootstrap-ubuntu22.py" in first[0][-1]
    assert "ubuntu22_oneclick.py" in first[1][-1]
    assert not any("order" in str(cmd).lower() for cmd in first)
    assert len(user_data.encode("utf-8")) < 16000
    assert cfg["final_message"].startswith("POA bootstrap completed")
    assert not (ROOT / ".env").exists()
    assert not (ROOT / "poa-credentials.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="service activation is Linux-only")
def test_boot_activates_full_local_and_https_without_orders(tmp_path, monkeypatch, capsys):
    input_path = tmp_path / "input.json"
    stage_path = tmp_path / "stage.json"
    runtime = tmp_path / "runtime.json"
    result = tmp_path / "result.json"
    input_path.write_text(json.dumps(valid_input()), encoding="utf-8")
    stage_path.write_text(json.dumps({
        "status": "STAGED_NOT_LIVE", "commit": "1" * 40
    }), encoding="utf-8")
    input_path.chmod(0o600)
    stage_path.chmod(0o600)
    monkeypatch.setattr(O, "CONFIG_PATH", input_path)
    monkeypatch.setattr(O, "STAGE_PATH", stage_path)
    monkeypatch.setattr(O, "RUNTIME_PATH", runtime)
    monkeypatch.setattr(O, "RESULT_PATH", result)
    monkeypatch.setattr(O, "public_ipv4", lambda: "8.8.8.8")
    actions = []
    monkeypatch.setattr(O, "_setup_command", lambda *args: actions.append(args))
    monkeypatch.setattr(O, "_https_ready", lambda hostname: actions.append(("tls", hostname)))
    report = O.complete()
    assert [a[0] for a in actions] == ["fresh", "publish", "tls"]
    assert actions[0][1] == "--config"
    assert actions[1] == ("publish", "--domain", "8.8.8.8.sslip.io")
    assert report["state"] == "RUNNING_HTTPS"
    assert report["live_order_test_performed"] is False
    assert report["webhook_endpoint_published"] is True
    assert report["source_commit"] == "1" * 40
    assert result.stat().st_mode & 0o077 == 0
    assert json.loads(result.read_text()) == report
    assert not input_path.exists()
    assert not runtime.exists()
    output = capsys.readouterr().out
    assert "synthetic_upbit_key" not in output
    assert "token_123" not in output
    assert "POA_ONECLICK_READY=https://8.8.8.8.sslip.io/order" in output


@pytest.mark.skipif(sys.platform == "win32", reason="private POSIX files")
def test_failed_stage_never_starts_service(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    stage = tmp_path / "stage.json"
    cfg.write_text(json.dumps(valid_input()))
    stage.write_text('{"status":"FAILED"}')
    cfg.chmod(0o600)
    stage.chmod(0o600)
    monkeypatch.setattr(O, "CONFIG_PATH", cfg)
    monkeypatch.setattr(O, "STAGE_PATH", stage)
    monkeypatch.setattr(O, "public_ipv4", lambda: "8.8.8.8")
    monkeypatch.setattr(O, "_setup_command",
                        lambda *args: pytest.fail("never start on failed Stage"))
    with pytest.raises(O.OneclickError, match="STAGE_NOT_READY"):
        O.complete()
