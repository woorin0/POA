"""Independent Linux OS network syscall denial rehearsal (not production code).

Reuses the proven minimal libseccomp approach from PR #27. All routes are
synthetic transport stubs, never actual POA main, CCXT, KIS or Safe Retry.
No trading credentials, hostnames, socket connections or order submissions.
Must run only as an isolated CI child; filter is irreversible inside child.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import json
import os
import platform
from pathlib import Path
import socket
import subprocess
import sys

ACTION_ALLOW = 0x7FFF0000
ACTION_EPERM = 0x00050000 | errno.EPERM
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39
REQUIRED = (
    "socket", "socketpair", "connect", "bind", "listen",
    "sendto", "sendmsg", "recvfrom", "recvmsg",
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    "setns", "unshare", "pidfd_getfd",
)
OPTIONAL = ("accept", "accept4", "sendmmsg", "recvmmsg")
ROUTES = ("main", "ccxt", "kis", "safe-retry")
NESTED = tuple("nested-" + name for name in ROUTES) + ("nested-ffi",)
MODES = ROUTES + ("ffi", "subprocess", "all", "inherited-socket") + NESTED


class IsolationBlocked(RuntimeError):
    pass


def require_linux() -> None:
    if platform.system() != "Linux":
        raise IsolationBlocked("LINUX_ONLY")
    if platform.machine() not in ("x86_64", "aarch64"):
        raise IsolationBlocked("ARCH_NOT_REVIEWED")


def assert_no_preopened_sockets() -> bool:
    """Fail closed if Linux process inherited an already usable socket FD."""
    require_linux()
    try:
        entries = os.listdir("/proc/self/fd")
    except OSError:
        raise IsolationBlocked("FD_CENSUS_UNAVAILABLE") from None
    for number in entries:
        try:
            destination = os.readlink("/proc/self/fd/" + number)
        except FileNotFoundError:
            continue  # own short-lived directory descriptor
        except OSError:
            raise IsolationBlocked("FD_CENSUS_UNAVAILABLE") from None
        if destination.startswith("socket:["):
            raise IsolationBlocked("PREOPENED_SOCKET_FD")
    return True


def _libseccomp():
    require_linux()
    name = ctypes.util.find_library("seccomp")
    if not name:
        raise IsolationBlocked("LIBSECCOMP_MISSING")
    lib = ctypes.CDLL(name, use_errno=True)
    lib.seccomp_init.argtypes = (ctypes.c_uint32,)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    )
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_load.argtypes = (ctypes.c_void_p,)
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = (ctypes.c_void_p,)
    lib.seccomp_release.restype = None
    return lib


def install_filter() -> bool:
    """Install kernel-level denial before untrusted synthetic child routes."""
    require_linux()
    assert_no_preopened_sockets()
    lib = _libseccomp()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = (
        ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_ulong, ctypes.c_ulong,
    )
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise IsolationBlocked("PR_SET_NO_NEW_PRIVS_FAILED")
    if libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise IsolationBlocked("PR_GET_NO_NEW_PRIVS_FAILED")
    ctx = lib.seccomp_init(ACTION_ALLOW)
    if not ctx:
        raise IsolationBlocked("SECCOMP_INIT_FAILED")
    try:
        for name in REQUIRED + OPTIONAL:
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                if name in REQUIRED:
                    raise IsolationBlocked("REQUIRED_SYSCALL_MISSING:" + name)
                continue
            if lib.seccomp_rule_add(ctx, ACTION_EPERM, number, 0) != 0:
                raise IsolationBlocked("SECCOMP_RULE_FAILED:" + name)
        if lib.seccomp_load(ctx) != 0:
            raise IsolationBlocked("SECCOMP_LOAD_FAILED")
    finally:
        lib.seccomp_release(ctx)
    return True


def deny_socket(family=socket.AF_INET) -> None:
    # Creation only. NEVER connect(), HTTP, route to any external host.
    try:
        sock = socket.socket(family, socket.SOCK_STREAM, 0)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EACCES):
            raise AssertionError("UNEXPECTED_SOCKET_ERROR") from None
    else:
        sock.close()
        raise AssertionError("NETWORK_SOCKET_WAS_CREATED")


def deny_ffi() -> None:
    # Native libc syscall cannot be intercepted by Python import/audit hooks.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.socket.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
    libc.socket.restype = ctypes.c_int
    fd = libc.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    if fd >= 0:
        os.close(fd)
        raise AssertionError("LIBC_SOCKET_WAS_CREATED")
    if ctypes.get_errno() not in (errno.EPERM, errno.EACCES):
        raise AssertionError("UNEXPECTED_LIBC_SOCKET_ERROR")


def mock_transport(route: str) -> None:
    if route not in ROUTES:
        raise AssertionError("UNKNOWN_SYNTHETIC_ROUTE")
    # main.py, raw CCXT, KIS and restarted Safe Retry are *labels only*.
    deny_socket(socket.AF_INET6 if route == "kis" else socket.AF_INET)


def nested_exec(route: str) -> None:
    # The child DOES NOT reinstall the filter; exec inherits Linux seccomp.
    code = "nested-" + route
    r = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--" + code],
        capture_output=True, text=True, close_fds=True, timeout=20,
        env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if r.returncode != 0 or r.stdout.strip() != "NESTED_FILTER_INHERITED=PASS":
        raise AssertionError("CHILD_FILTER_NOT_INHERITED:" + route)


def run(mode: str) -> None:
    require_linux()
    if mode == "inherited-socket":
        assert_no_preopened_sockets()
        raise AssertionError("INHERITED_SOCKET_NOT_DETECTED")
    if mode in NESTED:
        if mode == "nested-ffi":
            deny_ffi()
        else:
            mock_transport(mode[len("nested-"):])
        print("NESTED_FILTER_INHERITED=PASS")
        return
    install_filter()
    if mode == "all":
        for route in ROUTES:
            mock_transport(route)
            nested_exec(route)
        deny_ffi()
        nested_exec("ffi")
    elif mode == "subprocess":
        for route in ROUTES:
            nested_exec(route)
    elif mode == "ffi":
        deny_ffi()
    elif mode in ROUTES:
        mock_transport(mode)
    else:
        raise AssertionError("MODE_NOT_AUTHORIZED")
    print(json.dumps({
        "result": "OS_KERNEL_SYSCALL_DENIAL_PASS", "route": mode,
        "actual_orders": 0, "external_connections": 0,
        "real_sdk_imports": 0, "main_executed": False,
    }, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("--"):
        raise SystemExit(75)
    name = sys.argv[1][2:]
    if name not in MODES:
        raise SystemExit(75)
    try:
        run(name)
    except (IsolationBlocked, AssertionError, OSError) as exc:
        print(type(exc).__name__ + ":" + str(exc), file=sys.stderr)
        raise SystemExit(75)
