"""Offline regression for recovery of a brand-new server's failed PocketBase fetch."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from deployment import recover_failed_pocketbase_stage as R


def synthetic_archive():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("pocketbase", b"synthetic mock executable")
    return data.getvalue()


def test_retry_keeps_original_official_version_and_verified_hash(tmp_path, monkeypatch):
    payload = synthetic_archive()
    monkeypatch.setattr(R, "DB", tmp_path)
    monkeypatch.setattr(R, "PB_SHA", hashlib.sha256(payload).hexdigest())
    partial = tmp_path / "pocketbase.zip"
    partial.write_bytes(b"partial pre-existing download - not database")

    def download_mock(argv, **kwargs):
        assert argv[0] == "curl"
        assert "--retry-all-errors" in argv and argv[argv.index("--retry") + 1] == "6"
        assert "--connect-timeout" in argv
        assert "github.com/pocketbase/pocketbase/releases/download/v0.22.48/" in argv[-1]
        assert kwargs["timeout"] >= 1000
        (tmp_path / "pocketbase-recovery.tmp").write_bytes(payload)
        return SimpleNamespace(returncode=0, stdout=b"200", stderr=b"")

    monkeypatch.setattr(R.subprocess, "run", download_mock)
    assert R.download() == partial
    assert partial.read_bytes() == payload
    assert not (tmp_path / "pocketbase-recovery.tmp").exists()


def test_download_http_failure_does_not_overwrite_partial_state(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "DB", tmp_path)
    partial = tmp_path / "pocketbase.zip"
    partial.write_bytes(b"original partial bytes")
    monkeypatch.setattr(R, "PB_SHA", hashlib.sha256(b"expected different").hexdigest())

    def failed_mock(argv, **kwargs):
        (tmp_path / "pocketbase-recovery.tmp").write_bytes(b"failed response")
        return SimpleNamespace(returncode=22, stdout=b"403", stderr=b"secret diagnostics")

    monkeypatch.setattr(R.subprocess, "run", failed_mock)
    with pytest.raises(RuntimeError, match="POCKETBASE_DOWNLOAD_FAILED_HTTP_403"):
        R.download()
    assert partial.read_bytes() == b"original partial bytes"
    assert not (tmp_path / "pocketbase-recovery.tmp").exists()


def test_invalid_hash_never_replaces_existing_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "DB", tmp_path)
    monkeypatch.setattr(R, "PB_SHA", "0" * 64)
    partial = tmp_path / "pocketbase.zip"
    partial.write_bytes(b"original state")

    def corrupt_mock(argv, **kwargs):
        (tmp_path / "pocketbase-recovery.tmp").write_bytes(b"bad payload")
        return SimpleNamespace(returncode=0, stdout=b"200", stderr=b"")

    monkeypatch.setattr(R.subprocess, "run", corrupt_mock)
    with pytest.raises(RuntimeError, match="POCKETBASE_SHA256_MISMATCH"):
        R.download()
    assert partial.read_bytes() == b"original state"
    assert not (tmp_path / "pocketbase-recovery.tmp").exists()


def test_existing_live_state_stops_before_any_command(tmp_path, monkeypatch):
    app = tmp_path / "POA"
    db = tmp_path / "db"
    app.mkdir()
    db.mkdir()
    config = tmp_path / "input.json"
    config.write_text("{}")
    config.chmod(0o600)
    (app / ".env").write_text("PRESERVE_ME")
    monkeypatch.setattr(R, "APP", app)
    monkeypatch.setattr(R, "DB", db)
    monkeypatch.setattr(R, "STAGE", tmp_path / "stage.json")
    monkeypatch.setattr(R, "CONFIG", config)
    monkeypatch.setattr(R.os, "geteuid", lambda: 0, raising=False)

    original = Path.read_text

    def fake_os_release(self, *args, **kwargs):
        if str(self) == "/etc/os-release":
            return 'ID=ubuntu\nVERSION_ID="22.04"\n'
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_os_release)
    monkeypatch.setattr(R, "run", lambda *args, **kwargs: pytest.fail("No process on live server"))
    with pytest.raises(RuntimeError, match="EXISTING_DATA_STOP_NO_OVERWRITE"):
        R.check()
    assert (app / ".env").read_text() == "PRESERVE_ME"


def test_recovery_downloads_exact_pinned_user_data_not_api_keys():
    from pathlib import Path
    from hashlib import sha256
    root = Path(__file__).resolve().parents[1]
    deployed = (root / "deployment" / "POA_VULTR_ONECLICK_USER_DATA.yaml").read_bytes()
    assert R.SOURCE == "531daf84282de9957a59b1ea781770658afee2ef"
    assert sha256(deployed).hexdigest() == R.YAML_SHA
    assert R.PIN == "5b86b4368fb28adfe38bc98397bad32a01df9426"
    assert "POA_ONECLICK" not in R.CONFIG.name
