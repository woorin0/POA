"""GitHub Actions fresh Ubuntu 22.04 host rehearsal using the actual cloud-init runcmd.

Only two external effects are replaced: a synthetic .localhost HTTPS certificate is
allowed locally (public ACME cannot be issued in CI), and Discord is captured
locally rather than sent. POA/PocketBase/Caddy actually run in systemd.
NO REAL EXCHANGE KEYS, NO TRADE REQUESTS, NO PRODUCTION DEPLOYMENT.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
from urllib.request import Request

import yaml

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "deployment/POA_VULTR_10_SERVERS_FIXED_USER_DATA.yaml"
DOMAIN = "bot01.localhost"
WEBHOOK = "https://discord.com/api/webhooks/123456789/ci-only-no-real-token"


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def local_tls_check():
    """TLS + SNI + HTTP 403 from real Caddy; only CI local CA trust is bypassed."""
    with socket.create_connection(("127.0.0.1", 443), timeout=5) as conn:
        context = ssl._create_unverified_context()
        with context.wrap_socket(conn, server_hostname=DOMAIN) as sock:
            sock.sendall(
                ("GET / HTTP/1.1\r\nHost: " + DOMAIN +
                 "\r\nConnection: close\r\n\r\n").encode("ascii")
            )
            return sock.recv(128).startswith((b"HTTP/1.1 403", b"HTTP/2 403"))


def main():
    require(os.geteuid() == 0, "RUN_AS_ROOT_ON_EPHEMERAL_UBUNTU_RUNNER")
    source = USER_DATA.read_text(encoding="utf-8")
    require(source.startswith("#cloud-config\n"), "INVALID_USER_DATA")
    spec = yaml.safe_load(source)
    cfg = json.loads(spec["write_files"][0]["content"])
    require(cfg["domain"] == "SET_EXISTING_DOMAIN", "TEMPLATE_DOMAIN_CHANGED")
    require(len(spec["runcmd"]) == 1, "BOOT_COMMAND_CHANGED")
    cfg["domain"] = DOMAIN
    cfg["credentials"].update({
        "PASSWORD": "CI_ONLY_DO_NOT_USE_FOR_LIVE_TRADING",
        "DISCORD_WEBHOOK_URL": WEBHOOK,
        "UPBIT_KEY": "SYNTHETIC_CI_KEY_NO_REAL_ACCOUNT",
        "UPBIT_SECRET": "SYNTHETIC_CI_SECRET_NO_REAL_ACCOUNT",
    })
    target = Path(spec["write_files"][0]["path"])
    require(not target.exists(), "RUNNER_NOT_CLEAN")
    with target.open("x", encoding="utf-8") as file:
        json.dump(cfg, file)
    target.chmod(0o600)

    runcmd = spec["runcmd"][0]
    require(runcmd[:2] == ["bash", "-euc"], "UNEXPECTED_BOOT_COMMAND")
    command = runcmd[2]
    require(command.count("; python3 ") == 1, "UNEXPECTED_BOOT_COMMAND")
    # Execute the actual User Data's exact pinned download command, then
    # invoke the downloaded main() with CI-only TLS/Discord replacements.
    download = command.split("; python3 ")[0]
    subprocess.run(["bash", "-euc", download], check=True, timeout=180)
    script = Path("/root/poa-vultr-install-v3.py")
    require(script.is_file(), "PINNED_SCRIPT_DOWNLOAD_FAILED")
    installed = importlib.util.spec_from_file_location("poa_firstboot_real", script)
    module = importlib.util.module_from_spec(installed)
    installed.loader.exec_module(module)

    delivered = []
    original_modules = module.product_modules

    def local_verified_modules():
        legacy, operations, oneclick = original_modules()
        legacy.local_https = lambda domain: (
            require(domain == DOMAIN, "DOMAIN_CHANGED") or local_tls_check()
        )

        def intercept_discord(request, timeout=12):
            require(isinstance(request, Request), "UNEXPECTED_OUTBOUND_REQUEST")
            require(request.full_url == WEBHOOK, "UNEXPECTED_OUTBOUND_URL")
            require(Path("/root/POA_ONECLICK_RESULT.json").is_file(),
                    "DISCORD_BEFORE_INSTALL_SUCCESS")
            payload = json.loads(request.data)
            require(payload["embeds"][0]["title"] == "✅ POA 설치 완료",
                    "WRONG_DISCORD_NOTICE")
            delivered.append(payload)

            class Response:
                status = 204
                def __enter__(self): return self
                def __exit__(self, *args): return False

            return Response()

        module.urlopen = intercept_discord
        return legacy, operations, oneclick

    module.product_modules = local_verified_modules
    result = module.main()
    if result != 0:
        diag = Path("/root/POA_INSTALL_LAST_ERROR.log")
        if diag.exists():
            print("INSTALL_DIAGNOSTIC_BEGIN", flush=True)
            print(diag.read_text(encoding="utf-8", errors="replace")[-2000:], flush=True)
            print("INSTALL_DIAGNOSTIC_END", flush=True)
        subprocess.run(["journalctl", "-u", "poa", "-u", "pocketbase",
                        "-u", "caddy", "-n", "30", "--no-pager"], check=False)
        raise RuntimeError("REAL_FIRSTBOOT_INSTALL_FAILED")

    report = json.loads(Path("/root/POA_ONECLICK_RESULT.json").read_text())
    require(report["state"] == "RUNNING_HTTPS", "FULL_INSTALL_NOT_READY")
    require(report["webhook_url"] == "https://" + DOMAIN + "/order",
            "WEBHOOK_URL_MISMATCH")
    require(len(delivered) == 1, "DISCORD_SUCCESS_NOTIFICATION_NOT_ONCE")
    for service in ("poa", "pocketbase", "caddy"):
        subprocess.run(["systemctl", "is-active", "--quiet", service], check=True)
        subprocess.run(["systemctl", "is-enabled", "--quiet", service], check=True)
    from urllib.request import urlopen
    require(json.load(urlopen("http://127.0.0.1:8000/hi", timeout=3)) == "hi!!",
            "POA_HEALTH_FAILED")
    require(json.load(urlopen("http://127.0.0.1:8090/api/health", timeout=3))["code"] == 200,
            "POCKETBASE_HEALTH_FAILED")
    require(local_tls_check(), "LOCAL_TLS_403_FAILED")
    # Do not send an order POST; synthetic exchange keys have no live account.
    print("REAL_FIRSTBOOT_REHEARSAL=PASS")
    print("RUNNING_SERVICES=poa,pocketbase,caddy; DISCORD_SUCCESS_ONCE=PASS")
    print("NO_REAL_EXCHANGE_KEYS=PASS; ORDER_POSTS=0; PUBLIC_ACME_NOT_TESTED")


if __name__ == "__main__":
    main()
