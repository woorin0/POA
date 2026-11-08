"""Read-only, local Ubuntu 24.04 pre-install assessment. NO mutation or network.

Never reads production DB, secrets, key contents or writes to filesystem.
"PASS" means only static/pre-install facts; operational authorization stays
BLOCKED until independent Vultr systemd/FD/network and DB restoration gates.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess


REQUIRED_VERSION = "24.04"
MIN_CPU = 2
MIN_RAM_BYTES = 2 * 1024**3
MIN_FREE_BYTES = 10 * 1024**3


def _os_release():
    values = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key in ("ID", "VERSION_ID"):
                    values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def _memory_bytes():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _socket_fds():
    """Best-effort private FD census; unreadable == fail closed."""
    try:
        found = 0
        for number in os.listdir("/proc/self/fd"):
            try:
                dest = os.readlink("/proc/self/fd/" + number)
            except FileNotFoundError:
                continue
            if dest.startswith("socket:["):
                found += 1
        return found
    except OSError:
        return -1


def _legacy_process_count():
    """Inspect only command names; never report argv, secrets, PID or env."""
    count = 0
    try:
        paths = Path("/proc").iterdir()
        for path in paths:
            if not path.name.isdigit() or int(path.name) == os.getpid():
                continue
            try:
                fields = (path / "cmdline").read_bytes().split(b"\x00")
            except (OSError, PermissionError):
                continue
            # Paths to separate legacy POA order workers; conservative match.
            if any(re.search(
                rb"(?:^|[/\\])(?:main|run)\.py$|(?:^|[/\\])safe_retry(?:\.py)?$",
                part, re.I,
            ) for part in fields if part):
                count += 1
    except OSError:
        return -1
    return count


def _systemd_version():
    executable = shutil.which("systemd-analyze")
    if not executable:
        return 0
    try:
        p = subprocess.run([executable, "--version"], capture_output=True,
                           text=True, timeout=5, check=False)
        match = re.search(r"\bsystemd\s+(\d+)", p.stdout[:120])
        return int(match.group(1)) if p.returncode == 0 and match else 0
    except (OSError, subprocess.TimeoutExpired):
        return 0


def _seccomp_available():
    try:
        actions = Path("/proc/sys/kernel/seccomp/actions_avail").read_text()
        return "errno" in actions.split() and "kill_process" in actions.split()
    except OSError:
        return False


def collect(target="/"):
    """Only reads filesystem metadata and proc/sys diagnostics."""
    release = _os_release()
    try:
        filesystem = os.statvfs(target)
        free_bytes = filesystem.f_bavail * filesystem.f_frsize
    except OSError:
        free_bytes = 0
    keys = tuple(os.environ)
    # Only output boolean, never names or values of any credentials.
    sensitive_env = any(
        any(x in name.upper() for x in (
            "API_KEY", "API_SECRET", "ACCESS_TOKEN", "PRIVATE_KEY",
            "BINANCE", "BYBIT", "BITGET", "UPBIT", "OKX", "KIS",
        )) for name in keys
    )
    return {
        "ubuntu_2404": (platform.system() == "Linux"
                        and release.get("ID") == "ubuntu"
                        and release.get("VERSION_ID") == REQUIRED_VERSION),
        "amd64": platform.machine() in ("x86_64", "AMD64"),
        "cpu_count": os.cpu_count() or 0,
        "ram_bytes": _memory_bytes(),
        "free_bytes": free_bytes,
        "systemd_version": _systemd_version(),
        "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
        "seccomp_actions": _seccomp_available(),
        "unprivileged": getattr(os, "geteuid", lambda: 0)() != 0,
        "inherited_socket_count": _socket_fds(),
        "legacy_process_count": _legacy_process_count(),
        "sensitive_env_present": sensitive_env,
        "docker_socket_present": Path("/var/run/docker.sock").exists(),
    }


def evaluate(facts):
    if type(facts) is not dict:
        raise ValueError("INVALID_PREFLIGHT_FACTS")
    gates = {
        "ubuntu_2404_amd64": facts.get("ubuntu_2404") is True and facts.get("amd64") is True,
        "cpu_at_least_two": type(facts.get("cpu_count")) is int and facts["cpu_count"] >= MIN_CPU,
        "ram_at_least_2gib": type(facts.get("ram_bytes")) is int and facts["ram_bytes"] >= MIN_RAM_BYTES,
        "free_at_least_10gib": type(facts.get("free_bytes")) is int and facts["free_bytes"] >= MIN_FREE_BYTES,
        "systemd_parser": type(facts.get("systemd_version")) is int and facts["systemd_version"] >= 255,
        "cgroup_v2_present": facts.get("cgroup_v2") is True,
        "kernel_seccomp_actions": facts.get("seccomp_actions") is True,
        "non_root_invocation": facts.get("unprivileged") is True,
        "no_inherited_sockets": facts.get("inherited_socket_count") == 0,
        "no_legacy_poa_process": facts.get("legacy_process_count") == 0,
        "no_trading_secrets_in_environment": facts.get("sensitive_env_present") is False,
        "no_docker_control_socket": facts.get("docker_socket_present") is False,
    }
    return {
        "schema": "poa-vultr-ubuntu24-preflight-v1",
        "preinstall_static_gate": "PASS" if all(gates.values()) else "BLOCKED",
        "checks": {key: ("PASS" if ok else "BLOCKED") for key, ok in gates.items()},
        "host_systemd_network_fd_enforcement": "UNVERIFIED",
        "db_copy_restore_and_financial_integrity": "UNVERIFIED",
        "venue_account_and_read_permission": "UNVERIFIED",
        "live_exchange_reads": "BLOCKED",
        "trading": "BLOCKED",
        "operational_ready": "BLOCKED",
        "install_or_mutation_performed": False,
    }


def main():
    parser = argparse.ArgumentParser(description="POA Ubuntu 24.04 read-only preinstall checks")
    parser.add_argument("--target", default="/", help="Existing disk path to inspect; no writes")
    args = parser.parse_args()
    data = evaluate(collect(args.target))
    print(json.dumps(data, sort_keys=True, separators=(",", ":")))
    raise SystemExit(0 if data["preinstall_static_gate"] == "PASS" else 75)


if __name__ == "__main__":
    main()
