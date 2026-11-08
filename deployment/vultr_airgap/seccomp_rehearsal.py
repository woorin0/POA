"""Standalone Linux seccomp offline rehearsal for POA's prospective Vultr airgap.

THIS FILE IS NOT A PRODUCTION ORDER WORKER OR INSTALLER. It deliberately
does not import POA, CCXT, KIS SDK, any trading credential or operating DB.
A single-purpose isolated child installs a kernel seccomp filter that denies
socket/socketpair/connect/bind/listen, network send/recv, io_uring, namespace
change and pidfd_getfd. It then checks only SOCKET CREATION on inert fake SDK
clients, not an external host or a real order API.

If libseccomp/prctl/filter installation fails, exit nonzero WITHOUT attempting
a network syscall. This is a Linux CI proof only, not a substitute for actual
Vultr systemd/firewall tests or host process/FD isolation.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import json
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path

SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO_EPERM = 0x00050000 | errno.EPERM
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39
CRITICAL_SYSCALLS = ("socket", "socketpair", "connect", "bind", "listen",
                     "sendto", "sendmsg", "recvfrom", "recvmsg",
                     "io_uring_setup", "io_uring_enter", "io_uring_register",
                     "setns", "unshare", "pidfd_getfd")
OPTIONAL_SYSCALLS = ("accept", "accept4", "sendmmsg", "recvmmsg")


class SandboxUnavailable(RuntimeError):
    pass


def _linux_only():
    if platform.system() != "Linux":
        raise SandboxUnavailable("LINUX_ONLY")
    if platform.machine() not in ("x86_64", "aarch64"):
        raise SandboxUnavailable("ARCH_UNVERIFIED")


def _libseccomp():
    _linux_only()
    name = ctypes.util.find_library("seccomp")
    if not name:
        raise SandboxUnavailable("LIBSECCOMP_NOT_PRESENT")
    lib = ctypes.CDLL(name, use_errno=True)
    lib.seccomp_init.argtypes = (ctypes.c_uint32,)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.c_int, ctypes.c_uint)
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_load.argtypes = (ctypes.c_void_p,)
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = (ctypes.c_void_p,)
    lib.seccomp_release.restype = None
    return lib


def install_kernel_airgap():
    """Runs only inside disposable child process, before any socket probes."""
    lib = _libseccomp()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong)
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SandboxUnavailable("NO_NEW_PRIVS_REJECTED")
    if libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise SandboxUnavailable("NO_NEW_PRIVS_NOT_SET")
    ctx = lib.seccomp_init(SCMP_ACT_ALLOW)
    if not ctx:
        raise SandboxUnavailable("SECCOMP_INIT_FAILED")
    try:
        for syscall in CRITICAL_SYSCALLS + OPTIONAL_SYSCALLS:
            number = lib.seccomp_syscall_resolve_name(syscall.encode("ascii"))
            if number < 0:
                if syscall in CRITICAL_SYSCALLS:
                    raise SandboxUnavailable("SECCOMP_REQUIRED_SYSCALL_UNKNOWN:" + syscall)
                continue
            rc = lib.seccomp_rule_add(ctx, SCMP_ACT_ERRNO_EPERM, number, 0)
            if rc != 0:
                raise SandboxUnavailable("SECCOMP_RULE_FAILED:" + syscall)
        if lib.seccomp_load(ctx) != 0:
            raise SandboxUnavailable("SECCOMP_LOAD_FAILED")
    finally:
        lib.seccomp_release(ctx)
    return True


def deny_socket_probe(family=socket.AF_INET):
    """NEVER calls connect/send; check kernel rejects creating a socket."""
    try:
        handle = socket.socket(family, socket.SOCK_STREAM, 0)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EACCES):
            raise AssertionError("UNEXPECTED_SOCKET_ERROR") from None
        return "KERNEL_SOCKET_DENIED"
    else:
        handle.close()
        raise AssertionError("SOCKET_CREATED_WITHOUT_KERNEL_DENIAL")


def deny_raw_libc_socket_probe():
    """FFI bypasses Python monkeypatch; only an OS syscall filter can stop it."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.socket.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
    libc.socket.restype = ctypes.c_int
    fd = libc.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    if fd >= 0:
        os.close(fd)
        raise AssertionError("FFI_SOCKET_CREATED_WITHOUT_KERNEL_DENIAL")
    if ctypes.get_errno() not in (errno.EPERM, errno.EACCES):
        raise AssertionError("UNEXPECTED_FFI_SOCKET_ERROR")
    return "FFI_KERNEL_DENIED"


def fake_ccxt_direct_create_order():
    """Synthetic CCXT escape path, contains no SDK/account/order submission."""
    return deny_socket_probe(socket.AF_INET)


def fake_kis_direct_post_order():
    """Synthetic KIS direct HTTPS transport, no HTTP POST or real credentials."""
    return deny_socket_probe(socket.AF_INET6)


def child_inherited_filter():
    """A fresh Python process WITHOUT reinstalling filter inherits seccomp."""
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--nested"],
        check=False, capture_output=True, text=True, timeout=15, close_fds=True,
        env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode != 0 or result.stdout.strip() != "CHILD_INHERITED_SECCOMP=PASS":
        raise AssertionError("SUBPROCESS_FILTER_NOT_INHERITED")
    return "CHILD_INHERITED_SECCOMP=PASS"


def run(kind):
    _linux_only()
    if kind == "nested":
        assert deny_socket_probe() == "KERNEL_SOCKET_DENIED"
        print("CHILD_INHERITED_SECCOMP=PASS")
        return
    install_kernel_airgap()
    if kind == "ccxt":
        assert fake_ccxt_direct_create_order() == "KERNEL_SOCKET_DENIED"
    elif kind == "kis":
        assert fake_kis_direct_post_order() == "KERNEL_SOCKET_DENIED"
    elif kind == "ffi":
        assert deny_raw_libc_socket_probe() == "FFI_KERNEL_DENIED"
    elif kind == "subprocess":
        assert child_inherited_filter() == "CHILD_INHERITED_SECCOMP=PASS"
    elif kind == "all":
        assert fake_ccxt_direct_create_order() == "KERNEL_SOCKET_DENIED"
        assert fake_kis_direct_post_order() == "KERNEL_SOCKET_DENIED"
        assert deny_raw_libc_socket_probe() == "FFI_KERNEL_DENIED"
        assert child_inherited_filter() == "CHILD_INHERITED_SECCOMP=PASS"
    else:
        raise ValueError("INVALID_OFFLINE_PROBE")
    print(json.dumps({"result":"LINUX_SECCOMP_KERNEL_DENY_PASS",
                      "probe":kind,"live_orders":0,"api_calls":0,
                      "subprocess_inherits":kind in ("all","subprocess")},
                     sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in (
        "--ccxt","--kis","--ffi","--subprocess","--all","--nested"
    ):
        raise SystemExit("UNAUTHORIZED_REHEARSAL_MODE")
    try:
        run(sys.argv[1][2:])
    except (SandboxUnavailable, AssertionError, OSError) as exc:
        print(type(exc).__name__ + ":" + str(exc), file=sys.stderr)
        raise SystemExit(75)
