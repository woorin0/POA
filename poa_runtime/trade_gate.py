"""Intentionally unstartable Trading role for candidate PR A.

No side effects are performed before blocking. This is NOT a gate
installed into legacy main.py, raw CCXT, KIS or other Python processes.
"""
from poa_runtime.roles import deny_order_execution


def start_trading():
    deny_order_execution()


async def app(scope, receive, send):
    deny_order_execution()
