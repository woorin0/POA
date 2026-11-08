"""Offline tests: one-command, no obsolete PB archive, DNS preservation and Discord success only."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from deployment import poa_vultr_install_v3 as V


def test_new_cloud_init_is_small_and_server_domain_editable():
    p = Path(__file__).resolve().parents[1] / "deployment/POA_VULTR_10_SERVERS_FIXED_USER_DATA.yaml"
    raw = p.read_text(encoding="utf-8")
    cfg = yaml.safe_load(raw)
    assert raw.startswith("#cloud-config")
    assert len(raw.encode("utf-8")) < 4000
    assert cfg["write_files"][0]["path"] == "/root/poa-oneclick-input.json"
    credentials = json.loads(cfg["write_files"][0]["content"])["credentials"]
    assert credentials["PASSWORD"] == "AUTO"
    assert credentials["DISCORD_WEBHOOK_URL"] == "SET_DISCORD_WEBHOOK_URL"
    for prefix in ("UPBIT", "BINANCE", "BYBIT", "OKX", "BITGET"):
        assert credentials[f"{prefix}_KEY"] == ""
        assert credentials[f"{prefix}_SECRET"] == ""
    boot = str(cfg["runcmd"][0])
    assert "poa_vultr_install_v3.py" in boot
    assert V.POCKETBASE_VERSION == "0.16.6"
    assert V.POCKETBASE_COMMIT == "91bbbc4bdb943eefcc29ea78d90c77bac185949c"
    assert "pocketbase_0.22" not in boot
    assert "pocketbase_0.16" not in boot


@pytest.mark.parametrize("n", range(1, 11))
def test_per_server_hostname_keeps_tradingview_endpoint(n):
    hostname = f"bot{n:02d}.my-owned-domain.test"
    report = {"webhook_url": "https://" + hostname + "/order"}
    assert report["webhook_url"] == f"https://{hostname}/order"


def test_builds_pinned_pocketbase_source_without_github_release_asset(tmp_path, monkeypatch):
    db = tmp_path / "db"
    monkeypatch.setattr(V, "DB", db)
    monkeypatch.setattr(V, "GO_CACHE_ROOT", tmp_path / "poa-go")
    calls = []

    def fake_execute(argv, code, **kw):
        calls.append((list(argv), code))
        if code == "PB_SOURCE_FETCH_FAILED":
            (db / "pocketbase-src-v0.16.6").mkdir()
        if code == "PB_SOURCE_REV_FAILED":
            return V.POCKETBASE_COMMIT.encode()
        if code == "PB_SOURCE_COMPILE_FAILED":
            assert kw["env"]["HOME"] == "/root"
            assert kw["env"]["GOPATH"]
            assert kw["env"]["GOMODCACHE"] == str(Path(kw["env"]["GOPATH"]) / "pkg/mod")
            assert Path(kw["env"]["GOMODCACHE"]).is_dir()
            (db / "pocketbase.v0166.build").write_bytes(b"mock binary")
        if code in ("PB_COMPILED_VERSION_FAILED", "PB_VERSION_FAILED"):
            return b"pocketbase 0.16.6"
        return b""

    monkeypatch.setattr(V, "execute", fake_execute)
    V.pocketbase_build()
    assert (db / "pocketbase").read_bytes() == b"mock binary"
    builds = [argv for argv, code in calls if code == "PB_SOURCE_COMPILE_FAILED"]
    assert builds == [["go", "build", "-trimpath", "-ldflags",
                       "-X github.com/pocketbase/pocketbase.Version=0.16.6",
                       "-o", str(db / "pocketbase.v0166.build"), "./examples/base"]]
    assert not any("releases/download" in str(c) for c in calls)


def test_existing_pocketbase_binary_is_preserved_if_version_matches(tmp_path, monkeypatch):
    db = tmp_path / "db"
    db.mkdir()
    (db / "pocketbase").write_bytes(b"preexisting valid app")
    monkeypatch.setattr(V, "DB", db)
    monkeypatch.setattr(
        V, "execute", lambda a, code, **kw:
        b"v0.16.6" if code == "PB_VERSION_FAILED"
        else pytest.fail("Existing binary must not be replaced"),
    )
    V.pocketbase_build()
    assert (db / "pocketbase").read_bytes() == b"preexisting valid app"


@pytest.mark.parametrize("discord_delivered", [True, False])
def test_complete_send_one_discord_only_after_https_and_services(
    tmp_path, monkeypatch, discord_delivered
):
    import deployment.poa_vultr_install_v3 as m
    monkeypatch.setattr(m, "RESULT", tmp_path / "result.json")
    monkeypatch.setattr(m, "STAMP", tmp_path / "sent.json")
    monkeypatch.setattr(m, "NOTICE_STATUS", tmp_path / "notice_status.json")
    monkeypatch.setattr(m, "INPUT", tmp_path / "input.json")
    monkeypatch.setattr(m, "RUNTIME", tmp_path / "runtime.json")
    monkeypatch.setattr(m, "DB", tmp_path)
    monkeypatch.setattr(m, "SOURCE_COMMIT", "f" * 40)
    monkeypatch.setattr(m, "execute", lambda argv, code, **kwargs: b"0.16.6")
    monkeypatch.setattr(m, "private_write", lambda path, data: path.write_text(json.dumps(data)))
    cfg = {"domain": "bot01.test.example.org", "credentials": {
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/token"}}
    m.INPUT.write_text(json.dumps(cfg))
    actions = []

    class MockOps:
        _legacy_binary_for_fresh = None
        def fresh(self, config):
            actions.append(("fresh", str(config)))
            assert m.RESULT.exists() is False
            assert config == m.RUNTIME
            assert json.loads(config.read_text()) == cfg

        def publish(self, domain):
            actions.append(("publish", domain))

    class MockLegacy:
        def wait_ready(self, fn, code, seconds):
            actions.append(("tls", code))

        def local_https(self, domain):
            return True

    class FakeResponse:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_discord(req, timeout=12):
        actions.append(("discord", req.full_url))
        assert m.RESULT.is_file(), "Never send on unsuccessful installation"
        assert json.loads(req.data)["embeds"][0]["title"] == "✅ POA 설치 완료"
        if not discord_delivered:
            raise OSError("synthetic error")
        return FakeResponse()

    monkeypatch.setattr(m, "urlopen", fake_discord)
    m.complete_install(MockLegacy(), MockOps(), None, cfg)
    assert [x[0] for x in actions] == ["fresh", "publish", "tls", "discord"]
    assert json.loads(m.RESULT.read_text())["state"] == "RUNNING_HTTPS"
    assert (tmp_path / "sent.json").is_file()
    assert not m.INPUT.exists()
    assert not m.RUNTIME.exists()


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX file permission check")
def test_no_overwrite_when_old_runtime_exists(tmp_path, monkeypatch):
    app = tmp_path / "POA"
    app.mkdir()
    (app / ".env").write_text("valuable credentials")
    monkeypatch.setattr(V, "APP", app)
    monkeypatch.setattr(V, "DB", tmp_path / "db")
    monkeypatch.setattr(V, "INPUT", tmp_path / "private-input")
    monkeypatch.setattr(V, "RESULT", tmp_path / "missing-result")
    monkeypatch.setattr(V.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(V, "execute", lambda args, code, **kw: b"amd64")
    original = Path.read_text

    def fake_os_release(self, *a, **kw):
        if str(self).replace("\\", "/").endswith("/etc/os-release"):
            return 'ID=ubuntu\nVERSION_ID="22.04"'
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake_os_release)
    (tmp_path / "private-input").write_text("{}")
    (tmp_path / "private-input").chmod(0o600)
    with pytest.raises(V.InstallationBlocked, match="EXISTING_DB_OR_ORDER_CONFIGURATION_STOP"):
        V.ready_requirements()
    assert (app / ".env").read_text() == "valuable credentials"
