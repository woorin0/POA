"""READ_ONLY health-only ASGI entrypoint, sealed before importing the app.

Request-boundary integrity is checked even after initial process startup.
This does not authorize legacy order routing or provide host-level isolation.
"""
from poa_runtime.roles import Role
from poa_runtime.sealed_boundary import assert_seal_intact, seal_role

seal_role(Role.READ_ONLY)
from poa_runtime.readonly_gateway import app as _inert_app  # noqa: E402


async def app(scope, receive, send):
    assert_seal_intact(Role.READ_ONLY)
    await _inert_app(scope, receive, send)
