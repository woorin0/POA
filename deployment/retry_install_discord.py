"""Retry ONE completed POA install notice; never install, restart, or send orders.

Run as root on an already healthy POA host. Reads the existing root-owned .env;
does not print the webhook URL, exchange API keys, or HTTP error response body.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path("/root")
ENV = ROOT / "POA/.env"
RESULT = ROOT / "POA_ONECLICK_RESULT.json"
STATUS = ROOT / "POA_INSTALL_DISCORD_STATUS.json"


def read_webhook() -> str:
    if not ENV.is_file():
        raise ValueError("POA_ENV_MISSING")
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("DISCORD_WEBHOOK_URL="):
            try:
                value = json.loads(line.partition("=")[2])
            except (ValueError, TypeError):
                raise ValueError("WEBHOOK_CONFIG_MALFORMED") from None
            if not isinstance(value, str) or not value.strip():
                raise ValueError("WEBHOOK_NOT_CONFIGURED")
            return value.strip()
    raise ValueError("WEBHOOK_NOT_CONFIGURED")


def valid_discord_webhook(url: str) -> bool:
    try:
        parts = urlsplit(url)
        path = parts.path.split("/")
        return (parts.scheme == "https"
                and parts.hostname in {"discord.com", "discordapp.com",
                                       "canary.discord.com", "ptb.discord.com"}
                and len(path) == 5
                and path[1:3] == ["api", "webhooks"]
                and path[3].isdigit()
                and bool(path[4])
                and not parts.username and not parts.password)
    except ValueError:
        return False


def payload_for(report: dict) -> bytes:
    domain = urlsplit(report["webhook_url"]).hostname or "UNKNOWN"
    payload = {"embeds": [{
        "title": "✅ POA 설치 완료",
        "description": "POA · PocketBase · HTTPS 정상 실행",
        "color": 2263842,
        "fields": [
            {"name": "서버 도메인", "value": domain, "inline": False},
            {"name": "TradingView 웹훅", "value": report["webhook_url"],
             "inline": False},
        ],
    }]}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def deliver(url: str, report: dict) -> tuple[bool, str]:
    if not valid_discord_webhook(url):
        return False, "WEBHOOK_URL_INVALID"
    req = Request(url, data=payload_for(report), method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "POA-Install-Notifier/1.1",
    })
    reason = "UNKNOWN"
    for attempt in range(3):
        delay = attempt + 1.0
        try:
            with urlopen(req, timeout=15) as response:
                code = int(response.status)
            if code in (200, 204):
                return True, "HTTP_" + str(code)
            reason = "HTTP_" + str(code)
            if code < 500:
                return False, reason
        except HTTPError as exc:
            code = int(exc.code)
            reason = "HTTP_" + str(code)
            if code not in (429, 500, 502, 503, 504):
                return False, reason
            if code == 429:
                try:
                    delay = min(max(float(exc.headers.get("Retry-After", "1")), 1), 5)
                except (ValueError, TypeError):
                    delay = 1
        except (URLError, OSError, TimeoutError):
            reason = "NETWORK_OR_TLS_ERROR"
        if attempt < 2:
            time.sleep(delay)
    return False, reason


def write_status(result: str, reason: str) -> None:
    temp = STATUS.with_name(STATUS.name + ".tmp")
    if temp.exists() or temp.is_symlink():
        raise ValueError("STATUS_TMP_EXISTS")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump({"event": "POA_INSTALL_COMPLETE", "status": result,
                   "reason": reason}, out)
    os.replace(temp, STATUS)


def verify_live() -> dict:
    if os.geteuid() != 0:
        raise ValueError("ROOT_REQUIRED")
    if not RESULT.is_file():
        raise ValueError("INSTALL_RESULT_MISSING")
    report = json.loads(RESULT.read_text(encoding="utf-8"))
    if report.get("state") != "RUNNING_HTTPS":
        raise ValueError("INSTALL_NOT_COMPLETE")
    if not str(report.get("webhook_url", "")).startswith("https://"):
        raise ValueError("WEBHOOK_ENDPOINT_MISSING")
    for unit in ("poa", "pocketbase", "caddy"):
        proc = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            capture_output=True, timeout=10, check=False
        )
        if proc.returncode != 0:
            raise ValueError("SERVICE_INACTIVE_" + unit.upper())
    return report


def main() -> int:
    os.umask(0o077)
    try:
        report = verify_live()
        if STATUS.is_file():
            status = json.loads(STATUS.read_text(encoding="utf-8"))
            if status.get("status") == "SENT":
                print("POA_INSTALL_DISCORD=ALREADY_SENT")
                return 0
        webhook = read_webhook()
        sent, reason = deliver(webhook, report)
        write_status("SENT" if sent else "NOT_DELIVERED", reason)
        print("POA_INSTALL_DISCORD=" + ("SENT" if sent else "NOT_DELIVERED")
              + " REASON=" + reason)
        return 0 if sent else 3
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired):
        print("POA_INSTALL_DISCORD=NOT_DELIVERED REASON=CONFIG_OR_LOCAL_CHECK_FAILED")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
