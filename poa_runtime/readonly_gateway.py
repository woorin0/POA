"""Read-only role HTTP surface: inert preflight, not a trading or fill API.

The untrusted request body is never parsed. No real API key is acquired,
no DB accessed, no external network, no exchange or legacy main import.
"""
from __future__ import annotations

from poa_stage.gateway import _reply

_HEALTH = (b'{"role":"READ_ONLY","orders_enabled":false,'
           b'"exchange_reads_enabled":false,"db_attached":false}')
_DENIED = b'{"detail":"POA_READ_ONLY_NOT_AUTHORIZED"}'


async def app(scope, receive, send):
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
                raise RuntimeError("READ_ONLY_INVALID_LIFESPAN")
    elif protocol == "websocket":
        await send({"type": "websocket.close", "code": 4403,
                    "reason": "READ_ONLY_NO_WS"})
    elif protocol == "http":
        method, path = scope.get("method"), scope.get("path")
        if path == "/__read/health" and method in ("GET", "HEAD"):
            await _reply(send, 200, _HEALTH, head=method == "HEAD")
        else:
            await _reply(send, 403, _DENIED, head=method == "HEAD")
    else:
        raise RuntimeError("READ_ONLY_UNSUPPORTED_PROTOCOL")
