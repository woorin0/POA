"""New Ubuntu 22.04: complete the existing pinned Stage into a running POA.

Runs once from cloud-init after bootstrap-ubuntu22.py. Only operator-owned
credentials in the private cloud-init config are used. No orders are submitted.
Financial, exchange, PocketBase and existing BOT03 code/data remain unchanged.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from urllib.request import Request, urlopen

try:
    from deployment import install_once as legacy
except ModuleNotFoundError:
    import install_once as legacy

CONFIG_PATH = Path("/root/poa-oneclick-input.json")
STAGE_PATH = Path("/root/POA_INSTALL_RESULT.json")
RUNTIME_PATH = Path("/root/poa-oneclick-runtime.json")
RESULT_PATH = Path("/root/POA_ONECLICK_RESULT.json")
SETUP = "/usr/local/bin/poa-setup"


class OneclickError(Exception):
    pass


def public_ipv4():
    with urlopen("https://api.ipify.org", timeout=20) as response:
        address = response.read(64).decode("ascii").strip()
    ip = ipaddress.ip_address(address)
    if ip.version != 4 or not ip.is_global:
        raise OneclickError("PUBLIC_IPV4_UNAVAILABLE")
    return str(ip)


def normalized_input(raw: dict, *, public_ip: str | None = None) -> dict:
    """Resolve zero-config domain and webhook password before existing validation."""
    if not isinstance(raw, dict):
        raise OneclickError("CONFIG_JSON_REQUIRED")
    data = json.loads(json.dumps(raw))
    if data.get("domain") in ("AUTO", "", None):
        ip = ipaddress.ip_address(public_ip or public_ipv4())
        if ip.version != 4 or not ip.is_global:
            raise OneclickError("PUBLIC_IPV4_UNAVAILABLE")
        data["domain"] = str(ip) + ".sslip.io"
    credentials = data.get("credentials")
    if not isinstance(credentials, dict):
        raise OneclickError("CREDENTIALS_REQUIRED")
    if credentials.get("PASSWORD") == "AUTO":
        credentials["PASSWORD"] = secrets.token_urlsafe(32)
    for name, value in credentials.items():
        if isinstance(value, str) and value.startswith(("SET_", "REPLACE_", "CHANGE_ME", "YOUR_")):
            raise OneclickError("SET_REQUIRED_CREDENTIAL_" + name)
    # Preserve the original finance/order/runtime configuration validation.
    legacy.validate(data)
    return data


def _read_private_json(path: Path) -> dict:
    status = path.lstat()
    if not path.is_file() or status.st_mode & 0o077:
        raise OneclickError("ROOT_PRIVATE_CONFIG_REQUIRED")
    return json.loads(path.read_text(encoding="utf-8"))


def _private_write(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False)
    path.chmod(0o600)


def _setup_command(*arguments: str) -> None:
    # Never print child output: future diagnostics may contain credentials.
    proc = subprocess.run([sys.executable, "/usr/local/lib/poa/stage-full-history.py",
                           "setup", *arguments], capture_output=True, timeout=900,
                          check=False)
    if proc.returncode:
        raise OneclickError("POA_SETUP_" + arguments[0].upper().replace("-", "_") + "_FAILED")


def _https_ready(domain: str) -> None:
    # The public Caddy router intentionally replies 403 on GET /; the
    # verified TLS certificate and that 403 prove HTTPS is actually serving.
    legacy.wait_ready(lambda: legacy.local_https(domain),
                      "PUBLIC_HTTPS_NOT_READY", seconds=180)



def _notify_success(data: dict, report: dict) -> bool:
    """One Discord success message, only after local+HTTPS checks pass."""
    marker = RESULT_PATH.with_name("POA_INSTALL_DISCORD_ATTEMPT.json")
    _private_write(marker, {"event": "INSTALL_COMPLETED", "attempted": True})
    payload = {"embeds": [{
        "title": "✅ POA 설치 완료",
        "description": "Vultr POA · PocketBase · HTTPS 정상 기동",
        "color": 2263842,
        "fields": [
            {"name": "서버 도메인", "value": data["domain"], "inline": False},
            {"name": "TradingView 웹훅", "value": report["webhook_url"], "inline": False}
        ]
    }]}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(data["credentials"]["DISCORD_WEBHOOK_URL"], data=body,
                  headers={"Content-Type": "application/json",
                           "User-Agent": "POA-Firstboot/1.0"}, method="POST")
    try:
        with urlopen(req, timeout=12) as response:
            return response.status in (200, 204)
    except (OSError, ValueError):
        return False


def complete() -> dict:
    if os.name != "posix" or os.geteuid() != 0:
        raise OneclickError("UBUNTU_ROOT_REQUIRED")
    stage = _read_private_json(STAGE_PATH)
    if stage.get("status") != "STAGED_NOT_LIVE":
        raise OneclickError("STAGE_NOT_READY")
    data = normalized_input(_read_private_json(CONFIG_PATH))
    _private_write(RUNTIME_PATH, data)
    _setup_command("fresh", "--config", str(RUNTIME_PATH))
    _setup_command("publish", "--domain", data["domain"])
    _https_ready(data["domain"])
    report = {
        "state": "RUNNING_HTTPS",
        "poa": "LOCAL_HEALTH_PASS",
        "pocketbase": "LOCAL_HEALTH_PASS",
        "https": "TLS_VALID_403_ON_GET",
        "webhook_url": "https://" + data["domain"] + "/order",
        "webhook_endpoint_published": True,
        "live_order_test_performed": False,
        "source_commit": stage.get("commit"),
    }
    _private_write(RESULT_PATH, report)
    sent = _notify_success(data, report)
    print("POA_INSTALL_DISCORD=" + ("SENT" if sent else "NOT_DELIVERED"),
          flush=True)
    RUNTIME_PATH.unlink(missing_ok=True)
    CONFIG_PATH.unlink(missing_ok=True)
    print("POA_ONECLICK_READY=" + report["webhook_url"], flush=True)
    return report


def main() -> int:
    os.umask(0o077)
    try:
        complete()
        return 0
    except (OneclickError, legacy.InstallError, OSError, ValueError,
            json.JSONDecodeError, subprocess.TimeoutExpired):
        print("POA_ONECLICK_FAILED: inspect cloud-init and systemd logs; no orders submitted",
              file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
