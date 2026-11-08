"""Read-only health-only ASGI entrypoint: no exchange/DB access."""
from poa_runtime.roles import Role
from poa_runtime.sealed_boundary import seal_role

seal_role(Role.READ_ONLY)
from poa_runtime.readonly_gateway import app  # noqa: E402
