"""Single-request sealed ASGI worker for local-only safe health gateway.

Run from a trusted checkout: python -I -B -S poa_runtime/local_worker.py stage
Accepts ONE <=2 KiB JSON request over stdin, returns status/body JSON on stdout.
No sockets, live SDKs, secrets, DB, original main.py or order capability.
Not an installer and not an independent live trading service.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys


class WorkerBlocked(RuntimeError):
    pass


def execute_once(role: str, method: str, path: str) -> dict:
    """Import PR30 sealed role in this subprocess before dispatching ASGI."""
    if role not in ("stage", "read-only"):
        raise WorkerBlocked("TRADING_ROLE_DISABLED")
    if (type(method) is not str or len(method) > 16 or not method.isascii()
            or type(path) is not str or len(path) > 256 or not path.isascii()
            or not path.startswith("/") or any(ord(c) < 33 for c in path)):
        raise WorkerBlocked("INVALID_REQUEST")
    if role == "stage":
        from poa_runtime.sealed_stage import app
    else:
        from poa_runtime.sealed_readonly import app

    events = []

    async def receive():
        raise WorkerBlocked("BODY_OR_IO_NOT_ALLOWED")

    async def send(event):
        events.append(event)

    coro = app({
        "type": "http", "method": method, "path": path,
        "headers": [], "query_string": b"", "http_version": "1.1",
    }, receive, send)
    try:
        coro.send(None)
    except StopIteration:
        pass
    else:
        coro.close()
        raise WorkerBlocked("UNEXPECTED_ASYNC_OR_IO")
    if (len(events) != 2 or events[0].get("type") != "http.response.start"
            or events[1].get("type") != "http.response.body"):
        raise WorkerBlocked("INVALID_ASGI_RESPONSE")
    status = events[0].get("status")
    body = events[1].get("body")
    if status not in (200, 403) or type(body) is not bytes or len(body) > 512:
        raise WorkerBlocked("INVALID_RESPONSE_PAYLOAD")
    return {"status": status, "body": body.decode("ascii")}


def main() -> None:
    # Isolated (-I, -S) script execution excludes CWD and site-packages.
    # Only the trusted checked-out POA tree is explicitly added to sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if len(sys.argv) != 2 or sys.argv[1] not in ("stage", "read-only"):
        raise SystemExit(77)
    data = sys.stdin.buffer.read(2049)
    if len(data) > 2048:
        raise SystemExit(77)
    try:
        request = json.loads(data)
        if type(request) is not dict or set(request) != {"method", "path"}:
            raise WorkerBlocked("INVALID_REQUEST_SHAPE")
        response = execute_once(sys.argv[1], request["method"], request["path"])
    except (WorkerBlocked, ValueError, TypeError, UnicodeError):
        raise SystemExit(77)
    sys.stdout.write(json.dumps(response, separators=(",", ":"), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
