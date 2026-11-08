"""Completed-install Discord retry: no live webhook, exchange, or orders."""
from __future__ import annotations
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from deployment import retry_install_discord as R


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "ENV", tmp_path / ".env")
    monkeypatch.setattr(R, "RESULT", tmp_path / "result.json")
    monkeypatch.setattr(R, "STATUS", tmp_path / "status.json")
    R.ENV.write_text(
        'PASSWORD="do-not-print"\nDISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/123456/token-secret"\n'
        'UPBIT_SECRET="not-a-real-credential"\n', encoding="utf-8")
    R.RESULT.write_text(json.dumps({
        "state": "RUNNING_HTTPS",
        "webhook_url": "https://bot01.example.org/order"
    }), encoding="utf-8")
    monkeypatch.setattr(R.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 0})())
    return tmp_path


def test_reads_only_json_encoded_webhook(cfg):
    assert R.read_webhook() == "https://discord.com/api/webhooks/123456/token-secret"
    assert R.valid_discord_webhook(R.read_webhook())
    assert not R.valid_discord_webhook("https://evil.example.com/api/webhooks/123456/token")
    assert not R.valid_discord_webhook("http://discord.com/api/webhooks/123456/token")


def test_sends_one_notice_and_no_secrets_in_output(cfg, monkeypatch, capsys):
    outgoing = []

    class Response:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def pretend(req, timeout):
        outgoing.append(req)
        assert req.get_header("User-agent") == "POA-Install-Notifier/1.1"
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["embeds"][0]["title"] == "✅ POA 설치 완료"
        assert payload["embeds"][0]["fields"][1]["value"] == "https://bot01.example.org/order"
        assert "not-a-real-credential" not in json.dumps(payload)
        return Response()

    monkeypatch.setattr(R, "urlopen", pretend)
    assert R.main() == 0
    assert R.main() == 0
    assert len(outgoing) == 1
    assert json.loads(R.STATUS.read_text())["status"] == "SENT"
    output = capsys.readouterr().out
    assert "HTTP_204" in output
    assert "token-secret" not in output
    assert "not-a-real-credential" not in output


def test_http_404_is_diagnosed_not_retried_or_leaked(cfg, monkeypatch, capsys):
    called = []
    def fake(req, timeout):
        called.append(req)
        raise HTTPError(req.full_url, 404, "missing", {}, None)

    monkeypatch.setattr(R, "urlopen", fake)
    assert R.main() == 3
    assert len(called) == 1
    assert json.loads(R.STATUS.read_text()) == {
        "event": "POA_INSTALL_COMPLETE", "status": "NOT_DELIVERED", "reason": "HTTP_404"
    }
    out = capsys.readouterr().out
    assert "HTTP_404" in out and "token-secret" not in out


def test_network_failure_retries_but_preserves_services(cfg, monkeypatch):
    count = []
    monkeypatch.setattr(R.time, "sleep", lambda _: None)
    def network_error(req, timeout):
        count.append(1)
        raise OSError("synthetic outage")
    monkeypatch.setattr(R, "urlopen", network_error)
    assert R.main() == 3
    assert len(count) == 3
    assert json.loads(R.STATUS.read_text())["reason"] == "NETWORK_OR_TLS_ERROR"


def test_rejects_failed_install_without_outbound_message(cfg, monkeypatch):
    R.RESULT.write_text(json.dumps({"state": "FAILED", "webhook_url": "https://bot01.example.org/order"}))
    monkeypatch.setattr(R, "urlopen", lambda *a, **k: pytest.fail("No Discord on failed install"))
    assert R.main() == 4
    assert not R.STATUS.exists()
