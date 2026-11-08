"""PR23 isolated VULTR P0/P1 execution kill-switch — irreversible in this branch.

There is intentionally NO allow/live/CI flag, environment override, trust key,
feature enablement, monkey-patch hook, or exchange dependency. Every call
stops before any order, leverage, margin, stock POST, Safe Retry or ledger
write. Re-enabling trading requires a separately reviewed subsequent PR,
updated release manifest and independent live-operational authorization.

This Python guard cannot stop arbitrary code that imports ccxt directly
outside POA or a privileged operator that replaces executable files.
"""
from __future__ import annotations


class OrderExecutionBlocked(RuntimeError):
    """Fixed non-secret denial code; not a subclass of network errors."""
    code = "POA_ORDER_EXECUTION_BLOCKED"

    def __init__(self):
        super().__init__(self.code)


def require_order_disabled():
    """Never return success on any code path in this audit-only candidate."""
    raise OrderExecutionBlocked()


__all__ = ("OrderExecutionBlocked", "require_order_disabled")
