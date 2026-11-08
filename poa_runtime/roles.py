"""Minimal executable-role contract for *new* safe entrypoints.

Never exposes original main.py to staged services. There is no activation
switch for live trading in PR A. Product's old run.py remains unchanged;
it is NOT made safe by merely importing this module.
"""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    STAGE = "stage"
    READ_ONLY = "read-only"
    TRADING = "trading"


class RoleBlocked(RuntimeError):
    def __init__(self, code="POA_RUNTIME_TRADING_DISABLED"):
        self.code = code
        super().__init__(code)


def require_role(role, expected):
    if type(role) is not Role or role is not expected:
        raise RoleBlocked("POA_ROLE_MISMATCH")
    return True


def deny_order_execution(*_ignored, **_kwargs):
    """Unconditional guard for PR A runtime; never a live authorization gate."""
    raise RoleBlocked()


def asgi_for(role):
    """Only inert stage or read-only health may be imported."""
    if type(role) is not Role:
        raise RoleBlocked("POA_ROLE_UNRECOGNIZED")
    if role is Role.STAGE:
        from poa_stage.gateway import app
        return app
    if role is Role.READ_ONLY:
        from poa_runtime.readonly_gateway import app
        return app
    deny_order_execution()


def readonly_preflight(vault, venue, original_ref, *, trading_key_fingerprint=None):
    """Explicit read-key planner; always blocked for live exchange requests."""
    from poa_stage.read_credentials import load_explicit_read_key, stage_read_plan
    key = load_explicit_read_key(vault, venue)
    plan = stage_read_plan(key, original_ref,
        trading_key_fingerprint=trading_key_fingerprint)
    if plan.authorized:
        raise RoleBlocked("POA_READ_UNEXPECTED_AUTHORIZATION")
    return plan


def authorize_live_read(*_args, **_kwargs):
    """Venue permissions not attested: cannot permit network access."""
    raise RoleBlocked("POA_READ_ONLY_VENUE_PROOF_MISSING")
