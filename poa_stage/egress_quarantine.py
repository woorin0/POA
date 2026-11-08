"""Vultr Ubuntu 22.04 systemd NO-EGRESS contract + pure deterministic Mock.

This module NEVER executes systemctl, netns, iptables, subprocess, CCXT,
KIS, HTTP, DB, or socket APIs. Parsing an unstartable candidate unit
is NOT evidence that systemd enforced the sandbox on any host.

Important: AF-family filtering only limits socket(2), not inherited FDs,
socketpair(), socket activation, external services or privileged host access.
Those are explicit admission blockers, not magically remediated here.
"""
from __future__ import annotations

import configparser
import hashlib
from dataclasses import dataclass
from io import StringIO

UNIT_NAME = "poa-vultr-no-egress.service.candidate"
PARENT_SHA = "db343f2cda151fd396f58b5d8d0544bb7e5c5e6f"

# Strict allow-list; no aliases, writable drop-ins or additional unit keys.
UNIT_REQUIRED = {
    "Description": "POA Vultr quarantined no-egress inert audit candidate (NEVER START)",
    "Documentation": "https://github.com/woorin0/POA",
    "RefuseManualStart": "yes",
}
SERVICE_REQUIRED = {
    "Type": "exec",
    "DynamicUser": "yes",
    "WorkingDirectory": "/opt/poa-stage",
    "UMask": "0077",
    "NoNewPrivileges": "yes",
    "PrivateNetwork": "yes",
    "RestrictAddressFamilies": "none",
    "IPAddressDeny": "any",
    "RestrictNamespaces": "yes",
    "CapabilityBoundingSet": "",
    "AmbientCapabilities": "",
    "PrivateDevices": "yes",
    "PrivateTmp": "yes",
    "PrivateIPC": "yes",
    "PrivateMounts": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectControlGroups": "yes",
    "ProtectProc": "invisible",
    "ProcSubset": "pid",
    "LockPersonality": "yes",
    "MemoryDenyWriteExecute": "yes",
    "RestrictSUIDSGID": "yes",
    "RestrictRealtime": "yes",
    "SystemCallArchitectures": "native",
    "StandardInput": "null",
    "StandardOutput": "journal",
    "StandardError": "journal",
    "FileDescriptorStoreMax": "0",
    "NotifyAccess": "none",
    "Delegate": "no",
    "KillMode": "control-group",
    "Restart": "no",
    "TasksMax": "8",
    "MemoryMax": "256M",
    "CPUQuota": "50%",
    "InaccessiblePaths": "-/run/docker.sock -/var/run/docker.sock -/run/podman/podman.sock -/run/systemd/private",
    "Environment": "PYTHONDONTWRITEBYTECODE=1",
    "UnsetEnvironment": "BINANCE_KEY BINANCE_SECRET BYBIT_KEY BYBIT_SECRET BITGET_KEY BITGET_SECRET UPBIT_KEY UPBIT_SECRET OKX_KEY OKX_SECRET OKX_PASSPHRASE KIS1_KEY KIS1_SECRET",
    "ExecStartPre": "/usr/bin/false",
    "ExecStart": "/usr/bin/python3 -I -S -B /opt/poa-stage/poa_stage/quarantine_idle.py",
}


class QuarantineBlocked(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def deny(code: str):
    raise QuarantineBlocked(code)


def _strict_options(parser: configparser.ConfigParser, section: str, expected: dict):
    actual = dict(parser.items(section, raw=True))
    if actual != expected:
        missing = set(expected).difference(actual)
        unexpected = set(actual).difference(expected)
        altered = {k for k in expected.keys() & actual.keys() if expected[k] != actual[k]}
        # Never echo raw unit content (may contain secret-like data).
        if missing:
            deny("QUARANTINE_POLICY_MISSING")
        if unexpected:
            deny("QUARANTINE_POLICY_UNEXPECTED")
        if altered:
            deny("QUARANTINE_POLICY_DOWNGRADE")
        deny("QUARANTINE_POLICY_INVALID")


@dataclass(frozen=True)
class Admission:
    unit_sha256: str
    systemd_version_required: int = 249
    ubuntu_release_required: str = "22.04"
    arch_required: str = "amd64"
    enabled: bool = False
    runnable: bool = False
    network_permitted: bool = False
    operational_ready: bool = False

    def new_mock_process(self, name: str = "poa-stage"):
        # Synthetic process only. No real process, network namespace or socket.
        if type(name) is not str or not name or len(name) > 80:
            deny("QUARANTINE_PROCESS_ID_INVALID")
        return MockProcess(self, name, depth=0)


def validate_candidate(text: str) -> Admission:
    if type(text) is not str or len(text) > 16_384 or "\x00" in text:
        deny("QUARANTINE_UNIT_INPUT_INVALID")
    parser = configparser.ConfigParser(
        strict=True, interpolation=None, delimiters=("=",), comment_prefixes=("#",)
    )
    parser.optionxform = str
    try:
        parser.read_file(StringIO(text))
    except configparser.Error:
        deny("QUARANTINE_UNIT_SYNTAX_INVALID")
    if set(parser.sections()) != {"Unit", "Service"} or "[DEFAULT]" in text or parser.defaults():
        deny("QUARANTINE_EXTRA_SECTION")
    _strict_options(parser, "Unit", UNIT_REQUIRED)
    _strict_options(parser, "Service", SERVICE_REQUIRED)
    return Admission(hashlib.sha256(text.encode("utf-8")).hexdigest())


# These are simulation labels, not actual network stack implementations.
TRADES = frozenset((
    "ccxt.create_order", "ccxt.raw.private_post", "ccxt.set_leverage",
    "binance.sltp", "bybit.create_order", "bitget.create_order",
    "okx.create_order", "upbit.create_order",
    "kis.session.post", "kis.hashkey", "kis.oauth",
    "subprocess.python_ccxt", "subprocess.curl", "subprocess.kis",
    "fork.worker", "grandchild.raw_ccxt", "module_swap.exec",
))
TRANSPORTS = frozenset((
    "AF_INET", "AF_INET6", "AF_PACKET", "AF_UNIX_PROXY", "LOOPBACK",
    "DNS", "UNIX_CONTROL", "SOCKET_ACTIVATION_FD",
    "SCM_RIGHTS_FD", "INHERITED_FD", "RAW_SOCKET",
))


@dataclass(frozen=True)
class MockProcess:
    admission: Admission
    name: str
    depth: int

    def spawn(self, name: str, *, privileged: bool = False, inherited_fd: bool = False):
        if privileged:
            deny("QUARANTINE_PRIVILEGE_ESCALATION_BLOCKED_IN_MODEL")
        if inherited_fd:
            deny("QUARANTINE_SOCKET_FD_INJECTION_UNVERIFIED")
        if type(name) is not str or not name or self.depth >= 8:
            deny("QUARANTINE_SPAWN_LIMIT")
        return MockProcess(self.admission, name, self.depth + 1)

    def attempt(self, operation: str, transport: str = "AF_INET", *,
                inherited_fd: bool = False):
        if self.admission.enabled or self.admission.runnable:
            deny("QUARANTINE_BAD_ADMISSION")
        if inherited_fd or transport in ("SOCKET_ACTIVATION_FD", "SCM_RIGHTS_FD", "INHERITED_FD"):
            deny("QUARANTINE_EXTERNAL_FD_TRUST_NOT_PROVEN")
        if type(operation) is not str or type(transport) is not str:
            deny("QUARANTINE_INVALID_ATTACK")
        # All outbound transport, including raw SDKs and child processes,
        # is fail-closed IN THE MOCK. This does not prove actual OS policy.
        deny("QUARANTINE_MOCK_EGRESS_DENIED")
