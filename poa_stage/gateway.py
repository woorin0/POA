"""Standalone fail-closed ASGI stage ingress.

THIS IS NOT A WRAPPER AROUND main.py.  No production order/hedge/price,
background retry, DB, trading-key settings, PocketBase or Discord modules
are imported.  Do not route any live trading traffic here.

All HTTP routes are denied except the inert stage-health endpoint; every
WebSocket upgrade is denied. This is the sole ASGI export for *staging*.
Do not describe it as blocking arbitrary direct calls into live main.py.
"""
from __future__ import annotations

import json

_STAGE_HEALTH = b'{"state":"STAGE_ONLY","orders_enabled":false,"read_enabled":false,"db_attached":false}'
_DENIAL = b'{"detail":"POA_STAGE_ORDER_DENIED"}'
_HEADERS = [
    (b"content-type", b"application/json; charset=utf-8"),
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
]


async def _reply(send, status, payload, *, head=False):
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [*_HEADERS, (b"content-length", str(0 if head else len(payload)).encode("ascii"))],
    })
    await send({"type": "http.response.body", "body": b"" if head else payload})


async def app(scope, receive, send):
    """No configurable route to POA's real order or hedge handlers."""
    protocol = scope.get("type")
    if protocol == "lifespan":
        while True:
            event = await receive()
            if event.get("type") == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif event.get("type") == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
            else:
                raise RuntimeError("STAGE_LIFESPAN_EVENT_INVALID")
    elif protocol == "websocket":
        await send({"type": "websocket.close", "code": 4403, "reason": "STAGE_ONLY"})
    elif protocol == "http":
        path = scope.get("path")
        method = scope.get("method")
        if path == "/__stage/health" and method in ("GET", "HEAD"):
            await _reply(send, 200, _STAGE_HEALTH, head=method == "HEAD")
        else:
            # Includes /order, /hedge, /, /price, /ip, aliases and unknown
            # routes; never parse a webhook, instantiate bot or enqueue work.
            await _reply(send, 403, _DENIAL, head=method == "HEAD")
    else:
        # No alternative dispatch protocol that could carry a trade.
        raise RuntimeError("STAGE_UNSUPPORTED_ASGI_PROTOCOL")
