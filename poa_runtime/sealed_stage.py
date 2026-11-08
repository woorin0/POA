"""Stage-only ASGI entrypoint: seal process *before* importing its app."""
from poa_runtime.roles import Role
from poa_runtime.sealed_boundary import seal_role

seal_role(Role.STAGE)
from poa_stage.gateway import app  # noqa: E402
