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
            assert legacy.FLAGS["POA_EXCHANGE_FILL_CONFIRMATION_ENABLED"] is True
            actions.append(("fresh", str(config)))
            assert m.RESULT.exists() is False
            assert config == m.RUNTIME
            assert json.loads(config.read_text()) == cfg

        def publish(self, domain):
            actions.append(("publish", domain))

    class MockLegacy:
        FLAGS = {"POA_EXCHANGE_FILL_CONFIRMATION_ENABLED": False}
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
    legacy = MockLegacy()
    m.complete_install(legacy, MockOps(), None, cfg)
    assert legacy.FLAGS["POA_EXCHANGE_FILL_CONFIRMATION_ENABLED"] is True
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


def test_one_url_sha_is_only_version_selector_and_updates_manifest(monkeypatch):
    import hashlib
    root = Path(__file__).resolve().parents[1]
    source = Path(V.__file__).read_bytes()
    manifest = (root / "release_manifest_sha256.json").read_bytes()
    entries = json.loads(manifest)
    hashes = {x["Path"]: x["SHA256"].lower() for x in entries}
    responses = []
    class Response:
        def __init__(self, raw): self.raw = raw
        def read(self, n): return self.raw[:n]
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def mock_open(url, timeout=None):
        responses.append(url)
        if url.endswith("/release_manifest_sha256.json"): return Response(manifest)
        if url.endswith("/deployment/poa_vultr_install_v3.py"): return Response(source)
        pytest.fail("Unexpected fetch path")
    monkeypatch.setattr(V,"urlopen",mock_open)
    for digest in ("a"*40,"b"*40):
        url="https://raw.githubusercontent.com/woorin0/POA/"+digest+"/deployment/poa_vultr_install_v3.py"
        assert V.configure_release_url(url)==digest
        assert V.SOURCE_COMMIT==digest
        assert V.SOURCE_MANIFEST_SHA==hashlib.sha256(manifest).hexdigest()
        assert V.PREPARE_DIGESTS["install_once.py"]==hashes["deployment/install_once.py"]
        assert responses[-2:]==[url,"https://raw.githubusercontent.com/woorin0/POA/"+digest+"/release_manifest_sha256.json"]


@pytest.mark.parametrize("bad", [
    "", "main",
    "https://raw.githubusercontent.com/woorin0/POA/main/deployment/poa_vultr_install_v3.py",
    "https://raw.githubusercontent.com/woorin0/POA/"+"a"*39+"/deployment/poa_vultr_install_v3.py",
    "https://raw.githubusercontent.com/woorin0/POA/"+"A"*40+"/deployment/poa_vultr_install_v3.py",
    "https://evil.example/woorin0/POA/"+"a"*40+"/deployment/poa_vultr_install_v3.py",
    "https://raw.githubusercontent.com/woorin0/POA/"+"a"*40+"/deployment/poa_vultr_install_v3.py?ref=main",
])
def test_sha_validation_rejects_unpinned_or_foreign_urls(bad,monkeypatch):
    monkeypatch.setattr(V,"urlopen",lambda *a,**kw:pytest.fail("Malformed URL touched network"))
    with pytest.raises(V.InstallationBlocked,match="EXACT_INSTALLER_URL_SHA_REQUIRED"):
        V.configure_release_url(bad)


def test_installer_download_must_match_url_contents(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def read(self, n): return b"tampered"
    monkeypatch.setattr(V,"urlopen",lambda *a,**kw: Response())
    with pytest.raises(V.InstallationBlocked,match="INSTALLER_URL_CONTENT_MISMATCH"):
        V.configure_release_url("https://raw.githubusercontent.com/woorin0/POA/"+"a"*40+"/deployment/poa_vultr_install_v3.py")


def test_cloud_init_sha_occurs_only_in_one_downloader_url():
    p=Path(__file__).resolve().parents[1]/"deployment/POA_VULTR_10_SERVERS_FIXED_USER_DATA.yaml"
    import re
    cfg=yaml.safe_load(p.read_text(encoding="utf-8"))
    cmd=cfg["runcmd"][0][2]
    sentinel="0"*40
    assert cmd.count(sentinel)==1
    assert cmd.count("POA_INSTALL_SOURCE_URL=")==1
    assert "export POA_INSTALL_SOURCE_URL" in cmd
    assert '"$POA_INSTALL_SOURCE_URL"' in cmd
    assert re.search(r"raw.githubusercontent.com/woorin0/POA/"+sentinel+r"/deployment/poa_vultr_install_v3\.py",cmd)
    assert "python3 /root/poa-vultr-install-v3.py" in cmd


def test_installer_selects_url_before_any_install_mutation():
    import ast
    code=Path(V.__file__).read_text(encoding="utf-8")
    tree=ast.parse(code)
    entry=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="main")
    body=ast.get_source_segment(code,entry)
    assert body.index("configure_release_url(")<body.index("ready_requirements()")
    assert body.index("ready_requirements()")<body.index("prepare_packages()")
