"""PR B: fail-closed *role-process* boundary, not a global POA order switch.

Installed only by the new sealed Stage/Read-only entrypoints. No POA main,
exchange, KIS, Safe Retry, trading settings, DB, or credentials are imported.
This defense-in-depth CPython boundary is NOT an OS sandbox: privileged code,
preopened FDs, untrusted native code, or a separately launched legacy POA
process require independently verified host-level isolation.
"""
from __future__ import annotations

import importlib.abc
import os
from pathlib import Path
import sys
import threading

from poa_runtime.roles import Role

_ROOT = Path(__file__).resolve().parents[1]
_DENIED_TOP = frozenset({
    "main", "run", "exchange", "ccxt", "ccxtpro", "pykis", "kis",
    "pocketbase", "sqlite3",
})
_DENIED_EVENTS = frozenset({
    "socket.__new__", "socket.connect", "socket.connect_ex", "socket.bind",
    "subprocess.Popen", "os.system", "os.fork", "os.forkpty",
    "os.exec", "os.posix_spawn", "ctypes.dlopen", "ctypes.dlsym",
    "ctypes.call_function", "socket.sendto", "socket.getaddrinfo",
})
_LOCK = threading.RLock()
_active_role: Role | None = None
_finder: _DenyProductImport | None = None  # runtime instance, assigned at seal


class SealedRoleDenied(PermissionError):
    """Fixed-code denial, with no secrets, user input or environment echoed."""

    def __init__(self, code: str = "POA_SEALED_ROLE_SIDE_EFFECT_DENIED"):
        self.code = code
        super().__init__(code)


def _is_denied_import(name: object) -> bool:
    return type(name) is str and name.split(".", 1)[0] in _DENIED_TOP


def _is_product_code(filename: object) -> bool:
    if type(filename) is not str or filename.startswith("<"):
        return False
    try:
        path = Path(filename).resolve()
        return (
            path in (_ROOT / "main.py", _ROOT / "run.py")
            or (_ROOT / "exchange") in path.parents
        )
    except (OSError, RuntimeError, ValueError):
        # Unresolvable filenames must not be treated as trusted product code.
        return True


class _DenyProductImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if _is_denied_import(fullname):
            raise SealedRoleDenied("POA_SEALED_ROLE_PRODUCT_IMPORT_DENIED")
        return None


def _audit(event: str, args: tuple) -> None:
    # Fired by the CPython runtime before the normal API call executes.
    if event == "import" and args and _is_denied_import(args[0]):
        raise SealedRoleDenied("POA_SEALED_ROLE_PRODUCT_IMPORT_DENIED")
    if event in _DENIED_EVENTS or event.startswith(("os.spawn", "os.exec")):
        raise SealedRoleDenied()
    if event == "exec" and args and _is_product_code(
        getattr(args[0], "co_filename", None)
    ):
        raise SealedRoleDenied("POA_SEALED_ROLE_PRODUCT_EXEC_DENIED")


def _reject_inherited_socket_fds() -> None:
    """Linux preflight against inherited network/socketpair FDs.

    This is only a startup tripwire; it cannot block a separately started
    process or a compromised interpreter writing to an FD created later.
    On Windows OS handle inheritance needs an independent host test.
    """
    if sys.platform != "linux":
        return
    try:
        descriptors = os.listdir("/proc/self/fd")
    except OSError:
        raise SealedRoleDenied("POA_SEALED_ROLE_FD_CENSUS_UNAVAILABLE")
    for number in descriptors:
        try:
            destination = os.readlink("/proc/self/fd/" + number)
        except FileNotFoundError:
            continue  # the directory scan's own short-lived descriptor
        except OSError:
            raise SealedRoleDenied("POA_SEALED_ROLE_FD_CENSUS_UNAVAILABLE")
        if destination.startswith("socket:["):
            raise SealedRoleDenied("POA_SEALED_ROLE_INHERITED_SOCKET_FD")


def assert_seal_intact(role: Role) -> bool:
    """Request-boundary tripwire; NOT an arbitrary-code sandbox."""
    if type(role) is not Role or _active_role is not role:
        raise SealedRoleDenied("POA_SEALED_ROLE_STATE_INVALID")
    if _finder is None or not sys.meta_path or sys.meta_path[0] is not _finder:
        raise SealedRoleDenied("POA_SEALED_ROLE_IMPORT_POLICY_TAMPERED")
    if any(_is_denied_import(name) for name in sys.modules):
        raise SealedRoleDenied("POA_SEALED_ROLE_MODULE_CACHE_TAMPERED")
    return True


def seal_role(role: Role) -> Role:
    """Seal current process before loading an ASGI health-only application.

    No environment/CI/secret flag can activate the Trading role. Importing
    legacy modules *before* sealing is rejected, instead of silently trusting
    preloaded modules that bypass the Python import finder.
    """
    global _active_role, _finder
    if type(role) is not Role or role not in (Role.STAGE, Role.READ_ONLY):
        raise SealedRoleDenied("POA_SEALED_ROLE_TRADING_DISABLED")
    with _LOCK:
        if _active_role is not None:
            if _active_role is not role:
                raise SealedRoleDenied("POA_SEALED_ROLE_CONFLICT")
            assert_seal_intact(role)
            return role
        if any(_is_denied_import(name) for name in sys.modules):
            raise SealedRoleDenied("POA_SEALED_ROLE_PRELOADED_PRODUCT")
        _reject_inherited_socket_fds()
        _finder = _DenyProductImport()
        sys.meta_path.insert(0, _finder)
        sys.addaudithook(_audit)
        _active_role = role
        return role


def active_role() -> Role | None:
    return _active_role
