"""POA one-command installer/resumer for NEW Ubuntu 22.04 Vultr servers.

No obsolete PocketBase v0.22.x download. PocketBase v0.16.6 is built from
its pinned upstream source with Ubuntu Go 1.18; all POA product code, release
manifest, order routing and financial calculations remain byte-exact.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

APP = Path("/root/POA")
DB = Path("/root/db")
GO_CACHE_ROOT = Path("/root/.cache/poa-go")
INPUT = Path("/root/poa-oneclick-input.json")
STAGE = Path("/root/POA_INSTALL_RESULT.json")
RESULT = Path("/root/POA_ONECLICK_RESULT.json")
STAMP = Path("/root/POA_INSTALL_DISCORD_ATTEMPT.json")
NOTICE_STATUS = Path("/root/POA_INSTALL_DISCORD_STATUS.json")
RUNTIME = Path("/root/poa-runtime-v3.json")
SOURCE_COMMIT = "5b86b4368fb28adfe38bc98397bad32a01df9426"
SOURCE_MANIFEST_SHA = "0c464b1387d7ced078d4d957a1a9bdc947b8bb1d425518aba631fcc623d279fa"
POCKETBASE_COMMIT = "91bbbc4bdb943eefcc29ea78d90c77bac185949c"
POCKETBASE_VERSION = "0.16.6"
STEPS = [
    "PREPARE", "SOURCE", "PYTHON", "POCKETBASE_SOURCE_BUILD",
    "RELEASE_TEST", "POA_LOCAL_START", "HTTPS", "COMPLETE"
]


class InstallationBlocked(Exception):
    pass


def private_write(path: Path, payload: dict) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)


def execute(command, code, *, cwd=None, timeout=1800, env=None):
    proc = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, check=False,
                          env=env)
    if proc.returncode != 0:
        # Local root-only diagnostic for otherwise opaque apt/Go/Python failures.
        # No API credentials are passed on any subprocess command line.
        diagnostic = Path("/root/POA_INSTALL_LAST_ERROR.log")
        raw = proc.stderr[-4096:].decode("utf-8", errors="replace")
        raw = raw.replace("https://discord.com/api/webhooks/", "[DISCORD_URL_REDACTED]/")
        fd = os.open(diagnostic, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(code + " exit=" + str(proc.returncode) + "\n" + raw)
        raise InstallationBlocked(code)
    return proc.stdout


def ready_requirements():
    if os.geteuid() != 0 or 'VERSION_ID="22.04"' not in Path("/etc/os-release").read_text():
        raise InstallationBlocked("UBUNTU2204_ROOT_REQUIRED")
    if execute(["dpkg", "--print-architecture"], "AMD64_CHECK_FAILED").strip() != b"amd64":
        raise InstallationBlocked("AMD64_REQUIRED")
    if RESULT.exists():
        report = json.loads(RESULT.read_text(encoding="utf-8"))
        if report.get("state") == "RUNNING_HTTPS":
            print("POA_ALREADY_RUNNING=" + report["webhook_url"], flush=True)
            return False
        raise InstallationBlocked("EXISTING_RESULT_STOP")
    if not INPUT.is_file() or INPUT.stat().st_mode & 0o077:
        raise InstallationBlocked("PRIVATE_USER_CONFIG_REQUIRED")
    if (APP / ".env").exists() or (APP / "LOCAL_READY").exists() or (DB / "pb_data").exists():
        raise InstallationBlocked("EXISTING_DB_OR_ORDER_CONFIGURATION_STOP")
    return True


def prepare_packages():
    if not APP.is_dir():
        # Reuse the previously verified Ubuntu 22.04 package/Caddy setup,
        # including its official Caddy APT repository (Jammy lacks Caddy).
        target = Path("/usr/local/lib/poa")
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        checked = {
            "install_once.py": "5572d3cef61050bfeba684dcb573b4057f0714da85fbdb6b9bfe62761a471374",
            "prepare_ubuntu.py": "7d8f570f756cd41694001de12b271f4dc443fd21a45a06acc1b68c972bad605b",
            "validate_config.py": "9ecd177b5196c9c455d54f4a03e43f9ded7c663b6efff7710cca702d45736931",
        }
        for name, digest in checked.items():
            dst = target / name
            if not dst.exists():
                url = ("https://raw.githubusercontent.com/woorin0/POA/"
                       + SOURCE_COMMIT + "/deployment/" + name)
                with urlopen(url, timeout=45) as response:
                    payload = response.read(90000)
                if hashlib.sha256(payload).hexdigest() != digest:
                    raise InstallationBlocked("PREPARE_SCRIPT_HASH_MISMATCH")
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
        stage_input = Path("/root/poa-stage-input.json")
        if not stage_input.exists():
            private_write(stage_input, {
                "install_mode": "NEW_SERVER_STAGE_ONLY",
                "repo_ref": SOURCE_COMMIT,
                "release_manifest_sha256": SOURCE_MANIFEST_SHA,
            })
        sys.path.insert(0, str(target))
        from prepare_ubuntu import prepare
        prepare()
    # Existing failed-stage servers already have these helpers, but
    # brand-new servers need the systemd ExecStartPre validator too.
    if not (Path("/usr/local/lib/poa") / "validate_config.py").is_file():
        raise InstallationBlocked("RUNTIME_CONFIG_VALIDATOR_MISSING")
    execute(["apt-get", "install", "-y", "golang-go"], "GO_INSTALL_FAILED",
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})


def source_checkout():
    if APP.is_dir():
        observed = execute(["git", "-C", str(APP), "rev-parse", "HEAD"],
                           "EXISTING_POA_NOT_GIT").decode().strip()
        if observed != SOURCE_COMMIT:
            raise InstallationBlocked("EXISTING_POA_DIFFERENT_VERSION")
    else:
        execute(["git", "init", str(APP)], "GIT_INIT_FAILED")
        execute(["git", "-C", str(APP), "config", "core.autocrlf", "false"],
                "GIT_CONFIG_FAILED")
        execute(["git", "-C", str(APP), "remote", "add", "origin",
                 "https://github.com/woorin0/POA.git"], "GIT_REMOTE_FAILED")
        execute(["git", "-C", str(APP), "fetch", "--depth", "1", "origin",
                 SOURCE_COMMIT], "GIT_FETCH_FAILED", timeout=900)
        execute(["git", "-C", str(APP), "checkout", "--detach", "FETCH_HEAD"],
                "GIT_CHECKOUT_FAILED")
    # The existing independent finance release gate uses pre-existing commits.
    if execute(["git", "-C", str(APP), "rev-parse", "--is-shallow-repository"],
               "GIT_SHALLOW_CHECK_FAILED").strip() == b"true":
        execute(["git", "-C", str(APP), "fetch", "--unshallow", "origin"],
                "GIT_FULL_HISTORY_FAILED", timeout=900)
    if execute(["git", "-C", str(APP), "rev-parse", "HEAD"],
               "GIT_COMMIT_CHECK_FAILED").decode().strip() != SOURCE_COMMIT:
        raise InstallationBlocked("SOURCE_COMMIT_MISMATCH")


def product_modules():
    sys.path.insert(0, str(APP))
    from deployment import install_once as legacy
    from deployment import ubuntu22_operational as operations
    from deployment import ubuntu22_oneclick as oneclick
    return legacy, operations, oneclick


def python_install(legacy):
    legacy.verify_source(SOURCE_MANIFEST_SHA)
    python = APP / ".venv/bin/python"
    if not python.is_file():
        execute(["python3.10", "-m", "venv", str(APP / ".venv")],
                "PYTHON_VENV_FAILED")
    # Do not reinstall the dependencies on BOT01 if they already passed.
    try:
        execute([str(python), "-m", "pip", "check"], "PYTHON_CHECK_FAILED",
                timeout=60)
        execute([str(python), "-c",
                 "import fastapi, ccxt, pocketbase, jwt, cryptography"],
                "PYTHON_IMPORT_FAILED", timeout=60)
    except InstallationBlocked:
        execute([str(python), "-m", "pip", "install", "--no-cache-dir",
                 "pip==26.3", "setuptools==84.2.1"], "PYTHON_BOOTSTRAP_FAILED")
        execute([str(python), "-m", "pip", "install", "--no-cache-dir", "-r",
                 str(APP / "requirements.txt")], "PYTHON_REQUIREMENTS_FAILED",
                timeout=1800)
        execute([str(python), "-m", "pip", "check"], "PYTHON_CHECK_FAILED")
        execute([str(python), "-c",
                 "import fastapi, ccxt, pocketbase, jwt, cryptography"],
                "PYTHON_IMPORT_FAILED")


def pocketbase_build():
    DB.mkdir(mode=0o700, exist_ok=True)
    binary = DB / "pocketbase"
    if binary.exists():
        version = execute([str(binary), "--version"], "PB_VERSION_FAILED").decode()
        if POCKETBASE_VERSION not in version:
            raise InstallationBlocked("PB_VERSION_MISMATCH")
        return
    upstream = DB / "pocketbase-src-v0.16.6"
    if not upstream.exists():
        execute(["git", "clone", "--depth", "1", "--branch",
                 "v0.16.6", "https://github.com/pocketbase/pocketbase.git",
                 str(upstream)], "PB_SOURCE_FETCH_FAILED", timeout=900)
    observed = execute(["git", "-C", str(upstream), "rev-parse", "HEAD"],
                       "PB_SOURCE_REV_FAILED").decode().strip()
    if observed != POCKETBASE_COMMIT:
        raise InstallationBlocked("PB_SOURCE_COMMIT_MISMATCH")
    candidate = DB / "pocketbase.v0166.build"
    if candidate.exists():
        # A failed Go build can leave only this build-target temporary file;
        # never touch the real PB binary or any existing PocketBase DB.
        if binary.exists():
            raise InstallationBlocked("PB_BUILD_ALREADY_PRESENT")
        candidate.unlink()
    # cloud-init does not always have HOME/GOPATH; Go then cannot locate
    # its module cache (confirmed on BOT01: "neither GOMODCACHE nor GOPATH").
    go_home = GO_CACHE_ROOT
    go_mod_cache = go_home / "pkg/mod"
    go_mod_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {**os.environ, "HOME": "/root", "GOPATH": str(go_home),
           "GOMODCACHE": str(go_mod_cache), "CGO_ENABLED": "0",
           "GOTOOLCHAIN": "local",
           "GOPROXY": "https://proxy.golang.org,direct",
           "GOSUMDB": "sum.golang.org", "GOFLAGS": "-p=1",
           "GOMAXPROCS": "2", "GOGC": "50"}
    execute(["go", "build", "-trimpath", "-ldflags",
             "-X github.com/pocketbase/pocketbase.Version=0.16.6",
             "-o", str(candidate), "./examples/base"],
            "PB_SOURCE_COMPILE_FAILED", cwd=str(upstream), timeout=2400,
            env=env)
    candidate.chmod(0o700)
    version = execute([str(candidate), "--version"],
                      "PB_COMPILED_VERSION_FAILED").decode()
    if POCKETBASE_VERSION not in version:
        raise InstallationBlocked("PB_COMPILED_VERSION_MISMATCH")
    candidate.rename(binary)


def release_gate(legacy):
    python = str(APP / ".venv/bin/python")
    execute([python, "-m", "pip", "install", "--no-cache-dir",
             "pytest==9.1.1", "PyYAML==6.0.3"],
            "RELEASE_TEST_DEPS_FAILED", timeout=1800)
    execute([python, str(APP / "release_checks/run.py"), "-q", "--tb=short"],
            "RELEASE_GATES_FAILED", cwd=str(APP), timeout=600)
    execute(["git", "-C", str(APP), "diff", "--exit-code", "HEAD"],
            "SOURCE_MUTATED")
    legacy.verify_source(SOURCE_MANIFEST_SHA)
    if not STAGE.exists():
        private_write(STAGE, {"status": "STAGED_NOT_LIVE",
                              "commit": SOURCE_COMMIT,
                              "manifest_sha256": SOURCE_MANIFEST_SHA,
                              "RESTORE_VERIFIED": "NO", "OPERATING_READY": "NO"})
    else:
        if json.loads(STAGE.read_text(encoding="utf-8")).get("commit") != SOURCE_COMMIT:
            raise InstallationBlocked("STAGE_MARKER_MISMATCH")


def complete_install(legacy, operations, oneclick, data):
    def already_compiled_pb():
        version = execute([str(DB / "pocketbase"), "--version"],
                          "PB_RUNTIME_VERSION_FAILED").decode()
        if POCKETBASE_VERSION not in version:
            raise InstallationBlocked("PB_RUNTIME_VERSION_MISMATCH")
        return True
    # The pinned app's legacy fresh() normally downloads a second PocketBase
    # binary, then swaps out a temporary 0.22.x binary. Here a verified
    # v0.16.6 build already exists, so it needs neither download nor swap.
    operations._legacy_binary_for_fresh = already_compiled_pb
    private_write(RUNTIME, data)
    operations.fresh(RUNTIME)
    operations.publish(data["domain"])
    legacy.wait_ready(lambda: legacy.local_https(data["domain"]),
                      "HTTPS_NOT_READY_CHECK_DNS", seconds=180)
    for name in ("poa", "pocketbase", "caddy"):
        execute(["systemctl", "is-active", "--quiet", name],
                "SERVICE_NOT_ACTIVE_"+name.upper())
    report = {
        "state": "RUNNING_HTTPS", "poa": "LOCAL_HEALTH_PASS",
        "pocketbase": "LOCAL_HEALTH_PASS",
        "https": "TLS_VALID_403_ON_GET",
        "webhook_url": "https://" + data["domain"] + "/order",
        "webhook_endpoint_published": True,
        "live_order_test_performed": False,
        "source_commit": SOURCE_COMMIT,
        "pocketbase_version": POCKETBASE_VERSION,
    }
    private_write(RESULT, report)
    # Mark the attempt before calling Discord; never send install-start/failure.
    if not STAMP.exists():
        # One send attempt after successful HTTPS, never on failed installation.
        private_write(STAMP, {"event": "POA_INSTALL_COMPLETE", "attempted": True})
        body = json.dumps({"embeds": [{
            "title": "✅ POA 설치 완료",
            "description": "POA · PocketBase · HTTPS 정상 실행",
            "color": 2263842,
            "fields": [
                {"name": "서버 도메인", "value": data["domain"], "inline": False},
                {"name": "TradingView 웹훅", "value": report["webhook_url"],
                 "inline": False},
            ],
        }]}, ensure_ascii=False).encode("utf-8")
        reason = "UNKNOWN"
        try:
            request = Request(data["credentials"]["DISCORD_WEBHOOK_URL"],
                              data=body,
                              headers={"Content-Type": "application/json",
                                       "Accept": "application/json",
                                       "User-Agent": "POA-Install-Notifier/1.1"},
                              method="POST")
            with urlopen(request, timeout=15) as response:
                status = int(response.status)
            sent = status in (200, 204)
            reason = "HTTP_" + str(status)
        except HTTPError as error:
            sent = False
            reason = "HTTP_" + str(int(error.code))
        except (URLError, OSError):
            sent = False
            reason = "NETWORK_OR_TLS_ERROR"
        except (ValueError, KeyError, TypeError):
            sent = False
            reason = "WEBHOOK_CONFIG_INVALID"
        # Persist a token-free explanation for diagnosis or a deliberate retry.
        if not NOTICE_STATUS.exists():
            private_write(NOTICE_STATUS, {
                "event": "POA_INSTALL_COMPLETE",
                "status": "SENT" if sent else "NOT_DELIVERED",
                "reason": reason,
            })
        print("POA_INSTALL_DISCORD=" + ("SENT" if sent else "NOT_DELIVERED")
              + " REASON=" + reason, flush=True)
    INPUT.unlink(missing_ok=True)
    RUNTIME.unlink(missing_ok=True)
    print("POA_ONECLICK_READY=" + report["webhook_url"], flush=True)


def main():
    os.umask(0o077)
    step = "PREPARE"
    try:
        if not ready_requirements():
            return 0
        prepare_packages()
        step = "SOURCE"
        source_checkout()
        legacy, operations, oneclick = product_modules()
        data = oneclick.normalized_input(
            json.loads(INPUT.read_text(encoding="utf-8")))
        step = "PYTHON"
        python_install(legacy)
        step = "POCKETBASE_SOURCE_BUILD"
        pocketbase_build()
        step = "RELEASE_TEST"
        release_gate(legacy)
        step = "POA_LOCAL_START"
        complete_install(legacy, operations, oneclick, data)
        return 0
    except (InstallationBlocked, OSError, ValueError, subprocess.TimeoutExpired) as error:
        code = str(error) if isinstance(error, InstallationBlocked) else "INSTALL_UNEXPECTED_ERROR"
        # Do not log secret values and preserve partial files for a single retry.
        print("POA_SETUP_FAILED=" + code + " STEP=" + step, file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
