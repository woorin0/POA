"""Local AF_UNIX-only Stage/Read-only gateway, never TCP or order forwarding.

A non-sealed transport parent owns ONE Unix socket. Each request executes a
separate -I -B -S PR30 sealed worker with no inherited sockets, no environment
secrets and no connection to POA main, trading SDKs or operating DB.
Candidate systemd units remain deliberately disabled; NO automatic deployment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socketserver
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "poa_runtime" / "local_worker.py"
MAX_REQUEST_LINE = 512
MAX_OUTPUT = 4096
RESPONSE_503 = b'{"detail":"POA_LOCAL_WORKER_UNAVAILABLE"}'
RESPONSE_400 = b'{"detail":"POA_INVALID_LOCAL_REQUEST"}'


def _worker(role: str, method: str, path: str):
    """Never pass inherited sockets, production environment or a shell."""
    data = json.dumps({"method": method, "path": path}, separators=(",", ":"))
    child = subprocess.run(
        [sys.executable, "-I", "-B", "-S", str(WORKER), role],
        input=data, capture_output=True, text=True, timeout=12,
        cwd=str(ROOT), close_fds=True,
        env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if child.returncode != 0 or len(child.stdout) > MAX_OUTPUT:
        raise RuntimeError("SEALED_WORKER_DENIED")
    try:
        result = json.loads(child.stdout)
        if (type(result) is not dict or set(result) != {"status", "body"}
                or result["status"] not in (200, 403)
                or type(result["body"]) is not str
                or len(result["body"]) > 512
                or not result["body"].isascii()):
            raise ValueError("INVALID_RESPONSE")
    except (ValueError, TypeError):
        raise RuntimeError("INVALID_SEALED_WORKER_RESULT") from None
    return result["status"], result["body"].encode("ascii")


def response(status: int, body: bytes, method: str = "GET") -> bytes:
    if status not in (200, 400, 403, 503) or len(body) > 1024:
        raise ValueError("INVALID_RESPONSE")
    outgoing = b"" if method == "HEAD" else body
    reason = {200: b"OK", 400: b"Bad Request", 403: b"Forbidden",
              503: b"Service Unavailable"}[status]
    return (b"HTTP/1.1 " + str(status).encode("ascii") + b" " + reason
            + b"\r\nContent-Type: application/json\r\nCache-Control: no-store"
            + b"\r\nX-Content-Type-Options: nosniff"
            + b"\r\nConnection: close\r\nContent-Length: "
            + str(len(outgoing)).encode("ascii") + b"\r\n\r\n" + outgoing)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        method = "GET"
        try:
            # Consume *headers only* before replying: closing an AF_UNIX
            # stream with unread header bytes can reset the client connection.
            # Never touch any request body, bearer token or webhook payload.
            with self.request.makefile("rb", buffering=0) as stream:
                line = stream.readline(MAX_REQUEST_LINE + 1)
                if len(line) > MAX_REQUEST_LINE or not line.endswith(b"\r\n"):
                    raise ValueError("INVALID_LINE")
                header_bytes = 0
                for _ in range(64):
                    header = stream.readline(MAX_REQUEST_LINE + 1)
                    header_bytes += len(header)
                    if (len(header) > MAX_REQUEST_LINE or header_bytes > 4096
                            or not header.endswith(b"\r\n")):
                        raise ValueError("INVALID_HEADERS")
                    if header == b"\r\n":
                        break
                else:
                    raise ValueError("TOO_MANY_HEADERS")
            parts = line[:-2].split(b" ")
            if (len(parts) != 3 or parts[2] not in (b"HTTP/1.0", b"HTTP/1.1")
                    or not parts[1].startswith(b"/") or len(parts[1]) > 256
                    or not parts[0].isalpha()
                    or any(c < 33 or c > 126 for c in parts[1])):
                raise ValueError("INVALID_HTTP_REQUEST")
            method = parts[0].decode("ascii")
            path = parts[1].decode("ascii")
            try:
                status, body = _worker(self.server.poa_role, method, path)
            except (subprocess.TimeoutExpired, OSError, RuntimeError):
                status, body = 503, RESPONSE_503
        except (OSError, ValueError, UnicodeError):
            status, body = 400, RESPONSE_400
        try:
            self.request.sendall(response(status, body, method))
        except OSError:
            pass  # Closed local client; never trigger retry or an order.


# Windows Python lacks UnixStreamServer; import stays safe for Mock tests.
# Actual socket service remains strictly Linux-only, never falls back to TCP.
_UnixBase = getattr(socketserver, "UnixStreamServer", socketserver.BaseServer)


class _LocalServer(_UnixBase):
    allow_reuse_address = False
    request_queue_size = 8


def serve(role: str, path: str) -> None:
    if role not in ("stage", "read-only"):
        raise ValueError("TRADING_ROLE_DISABLED")
    location = Path(path)
    if (not location.is_absolute() or len(str(location)) > 100
            or ".." in location.parts or location.name in ("", ".", "..")):
        raise ValueError("INVALID_UNIX_SOCKET_PATH")
    parent = location.parent
    directory = parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode):
        raise ValueError("INVALID_SOCKET_DIRECTORY")
    if os.name != "nt":
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise ValueError("SOCKET_DIRECTORY_MUST_BE_PRIVATE")
    if os.path.lexists(location):
        raise ValueError("SOCKET_PATH_ALREADY_EXISTS")  # never unlink old paths
    if not hasattr(socketserver, "UnixStreamServer"):
        raise ValueError("LINUX_ONLY_UNIX_SOCKET")  # never substitute TCP
    mask = os.umask(0o077)
    try:
        with _LocalServer(str(location), _Handler) as server:
            server.poa_role = role
            os.chmod(location, 0o600)
            inode = os.lstat(location).st_ino
            try:
                server.serve_forever(poll_interval=0.5)
            finally:
                try:
                    current = os.lstat(location)
                    if stat.S_ISSOCK(current.st_mode) and current.st_ino == inode:
                        location.unlink()
                except FileNotFoundError:
                    pass
    finally:
        os.umask(mask)


def main():
    parser = argparse.ArgumentParser(description="Local-only non-trading POA health gateway")
    parser.add_argument("--role", required=True, choices=["stage", "read-only"])
    parser.add_argument("--unix-socket", required=True)
    args = parser.parse_args()
    serve(args.role, args.unix_socket)


if __name__ == "__main__":
    main()
