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
    result = O.normalized_input(data, public_ip="8.8.8.8")
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



@pytest.mark.parametrize("server_id", range(1, 11))
def test_ten_existing_server_domains_preserve_original_tradingview_webhook_url(server_id, monkeypatch):
    domain = f"bot{server_id:02d}.example.org"
    value = valid_input()
    value["domain"] = domain
    monkeypatch.setattr(O, "public_ipv4", lambda: pytest.fail("Must not generate a different domain"))
    actual = O.normalized_input(value)
    assert actual["domain"] == domain
    assert "https://" + actual["domain"] + "/order" == f"https://{domain}/order"


def test_unset_existing_domain_is_not_silently_replaced_with_auto_hostname(monkeypatch):
    value = valid_input()
    value["domain"] = "SET_EXISTING_DOMAIN"
    monkeypatch.setattr(O, "public_ipv4", lambda: pytest.fail("Explicit domain must not be auto-generated"))
    with pytest.raises(O.legacy.InstallError, match="DOMAIN_REQUIRED"):
        O.normalized_input(value)

def test_cloud_init_full_install_embeds_exact_code_and_stage_preserves_manifest():
    user_data = (ROOT / "deployment" / "POA_VULTR_ONECLICK_USER_DATA.yaml").read_text(encoding="utf-8")
    assert user_data.startswith("#cloud-config\n")
    import yaml
    cfg = yaml.safe_load(user_data)
    embedded = {entry["path"]: entry for entry in cfg["write_files"]}
    assert cfg["write_files"][0]["path"] == "/root/poa-oneclick-input.json"
    operator = json.loads(embedded["/root/poa-oneclick-input.json"]["content"])
    assert operator["domain"] == "SET_EXISTING_DOMAIN"
    assert operator["credentials"]["PASSWORD"] == "AUTO"
    assert operator["credentials"]["DISCORD_WEBHOOK_URL"] == "SET_DISCORD_WEBHOOK_URL"
    assert all(operator["credentials"][k] == "" for k in ("UPBIT_KEY", "UPBIT_SECRET", "BINANCE_KEY", "BYBIT_KEY", "OKX_KEY", "BITGET_KEY"))
    original = json.loads(
        (ROOT / "deployment" / "ubuntu22_turnkey_USER_DATA.yaml").read_text().split("\n", 1)[1]
    )
    existing = {entry["path"]: entry for entry in original["write_files"]}
    for path in existing:
        if path in ("/root/poa-stage-input.json", "/usr/local/lib/poa/bootstrap-ubuntu22.py"):
            continue  # The exact new release ref and full-history stage runner are intentional.
        assert embedded[path] == existing[path]
    release = json.loads(embedded["/root/poa-stage-input.json"]["content"])
    assert release["repo_ref"] == "5b86b4368fb28adfe38bc98397bad32a01df9426"
    assert release["release_manifest_sha256"] == json.loads(
        existing["/root/poa-stage-input.json"]["content"]
    )["release_manifest_sha256"]
    bootstrap = embedded["/usr/local/lib/poa/bootstrap-ubuntu22.py"]["content"]
    assert "stage-full-history.py" in bootstrap
    assert "install_once.py" not in bootstrap.split("for name in (", 1)[1].split("):", 1)[0]
    ast.parse(bootstrap)
    history = embedded["/usr/local/lib/poa/stage-full-history.py"]["content"]
    ast.parse(history)
    assert "GIT_FULL_HISTORY_FAILED" in history
    assert "'GIT_CHECKOUT_FAILED'" in history
    assert "installer.install(" in history
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
    assert cfg["final_message"].startswith("POA one-click boot attempted")
    assert not (ROOT / ".env").exists()
    assert not (ROOT / "poa-credentials.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="service activation is Linux-only")
@pytest.mark.parametrize("discord_delivered", [True, False])
def test_boot_activates_full_local_and_https_without_orders(tmp_path, monkeypatch, capsys, discord_delivered):
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
    monkeypatch.setattr(O.os, "geteuid", lambda: 0)
    actions = []
    discord_payloads = []

    class FakeDiscordResponse:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_discord(req, timeout=12):
        assert result.is_file(), "No Discord notification until the verified result is recorded"
        assert req.full_url == valid_input()["credentials"]["DISCORD_WEBHOOK_URL"]
        payload = json.loads(req.data)
        assert payload["embeds"][0]["title"] == "✅ POA 설치 완료"
        assert payload["embeds"][0]["fields"][1]["value"].endswith(".sslip.io/order")
        assert "synthetic_upbit" not in json.dumps(payload)
        assert "token_123" not in json.dumps(payload)
        discord_payloads.append(payload)
        if not discord_delivered:
            raise OSError("synthetic timeout")
        return FakeDiscordResponse()

    monkeypatch.setattr(O, "urlopen", fake_discord)
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
    assert len(discord_payloads) == 1, "Exactly one install-success message attempt"
    assert (tmp_path / "POA_INSTALL_DISCORD_ATTEMPT.json").is_file()
    assert ("POA_INSTALL_DISCORD=SENT" if discord_delivered else
            "POA_INSTALL_DISCORD=NOT_DELIVERED") in output


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
    monkeypatch.setattr(O.os, "geteuid", lambda: 0)
    monkeypatch.setattr(O, "_setup_command",
                        lambda *args: pytest.fail("never start on failed Stage"))
    monkeypatch.setattr(O, "_notify_success",
                        lambda *args: pytest.fail("never notify on failed Stage"))
    with pytest.raises(O.OneclickError, match="STAGE_NOT_READY"):
        O.complete()

def test_stage_full_history_happens_before_release_checks(tmp_path, monkeypatch):
    """Mocks only stage command order, never clones or opens trading networks."""
    import types
    path = ROOT / "deployment" / "POA_VULTR_ONECLICK_USER_DATA.yaml"
    import yaml
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    stage = next(x["content"] for x in cfg["write_files"]
                 if x["path"] == "/usr/local/lib/poa/stage-full-history.py")
    entry = tmp_path / "stage.json"
    entry.write_text('{"install_mode":"NEW_SERVER_STAGE_ONLY"}', encoding="utf-8")
    calls = []
    fake = types.ModuleType("install_once")
    fake.APP = tmp_path / "POA"
    fake.STAGE_INPUT = entry
    fake.InstallError = ValueError

    def original_run(args, code, cwd=None, timeout=300):
        calls.append(code)
        if code in ("POCKETBASE_DOWNLOAD_FAILED", "POCKETBASE_LEGACY_DOWNLOAD_FAILED"):
            assert args[0:3] == ["curl", "--retry", "6"]
            assert "--retry-all-errors" in args
            assert timeout >= 1000
        if code == "GIT_HISTORY_STATUS_FAILED":
            return b"false"
        return b""

    def install(data):
        assert data["install_mode"] == "NEW_SERVER_STAGE_ONLY"
        fake.run(["git", "-C", str(fake.APP), "checkout", "--detach", "FETCH_HEAD"],
                 "GIT_CHECKOUT_FAILED")
        fake.run(["curl", "--fail", "--location", "--output", "/tmp/pb-stage.zip",
                  "https://github.com/pocketbase/pocketbase/releases/download/v0.22.48/asset.zip"],
                 "POCKETBASE_DOWNLOAD_FAILED")
        fake.run(["curl", "--fail", "--location", "--output", "/tmp/pb-runtime.zip",
                  "https://github.com/pocketbase/pocketbase/releases/download/v0.16.6/asset.zip"],
                 "POCKETBASE_LEGACY_DOWNLOAD_FAILED")
        calls.append("OFFLINE_RELEASE_GATE")
        assert calls.index("GIT_FULL_HISTORY_FAILED") < calls.index("OFFLINE_RELEASE_GATE")
        assert calls.index("GIT_BASELINE_MISSING") < calls.index("OFFLINE_RELEASE_GATE")

    fake.run = original_run
    fake.install = install
    monkeypatch.setitem(sys.modules, "install_once", fake)
    exec(compile(stage, "<embedded-full-history-stage>", "exec"), {"__name__": "stage_test"})
    assert calls == ["GIT_CHECKOUT_FAILED", "GIT_FULL_HISTORY_FAILED",
                     "GIT_HISTORY_STATUS_FAILED", "GIT_BASELINE_MISSING",
                     "POCKETBASE_DOWNLOAD_FAILED", "POCKETBASE_LEGACY_DOWNLOAD_FAILED",
                     "OFFLINE_RELEASE_GATE"]
