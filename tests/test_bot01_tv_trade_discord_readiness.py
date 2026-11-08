"""BOT01 read-only source-contract checks. Never contact exchanges or Discord."""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace

from deployment.install_once import FLAGS
from exchange.model.schemas import Settings
from exchange.verified_pnl import order_fill_confirmation as confirmations


ROOT = Path(__file__).resolve().parents[1]
BOT01_SOURCE = "5b86b4368fb28adfe38bc98397bad32a01df9426"


def test_bot01_order_notification_source_is_unchanged():
    for path in (
        "main.py",
        "exchange/verified_pnl/dispatcher.py",
        "exchange/verified_pnl/outbox.py",
        "exchange/verified_pnl/order_fill_confirmation.py",
        "exchange/verified_pnl/alert_presentation.py",
        "exchange/utility/discord_embed.py",
    ):
        baseline = subprocess.check_output(["git", "show", BOT01_SOURCE + ":" + path], cwd=ROOT)
        assert (ROOT / path).read_bytes() == baseline, path


def test_bot01_install_enables_discord_but_not_native_order_fill_confirmation(tmp_path):
    assert FLAGS["VERIFIED_DISCORD_ALERTS"] is True
    assert FLAGS["VERIFIED_FILL_PRICE"] is True
    assert FLAGS["DISCORD_OUTBOX_ENABLED"] is True
    assert FLAGS["UPBIT_VERIFIED_ALERTS"] is True
    assert "POA_EXCHANGE_FILL_CONFIRMATION_ENABLED" not in FLAGS
    assert Settings.__fields__["POA_EXCHANGE_FILL_CONFIRMATION_ENABLED"].default is False
    st = SimpleNamespace(
        POA_EXCHANGE_FILL_CONFIRMATION_ENABLED=False,
        VERIFIED_NOTIFICATIONS_DB_PATH=str(tmp_path / "test.sqlite3"),
    )
    assert confirmations.prepare(st) is None


def test_bot01_verified_alert_routing_excludes_bitget():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef)
                and x.name == "is_verified_exchange")
    node.decorator_list = []
    scope = {"settings": SimpleNamespace(UPBIT_VERIFIED_ALERTS=True)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "bot01-main-order-route", "exec"), scope)
    supported = {name for name in ("BINANCE", "BYBIT", "OKX", "UPBIT", "BITGET")
                 if scope["is_verified_exchange"](name)}
    assert supported == {"BINANCE", "BYBIT", "OKX", "UPBIT"}


def test_native_fill_confirmation_supports_only_documented_markets():
    from exchange.verified_pnl.order_fill_read import VENUES
    assert VENUES == {"UPBIT", "BINANCE", "BYBIT", "OKX"}


def test_tv_signal_is_never_execution_proof():
    from exchange.verified_pnl.tradingview_signal import capture
    from exchange.verified_pnl.ledger import identity

    signal = {"kind": "strategy_order_fill", "price": "99999",
              "quantity": "42", "action": "buy", "order_id": "tv-1",
              "signal_id": "test-tv-1", "strategy_id": "test-strategy"}
    data = capture("BINANCE", {"id": "exchange-order-1"},
                   {"tv_signal": signal, "unified_symbol": "BTC/USDT",
                    "strategy_id": "test-strategy", "is_buy": True,
                    "is_futures": False, "is_coinm": False},
                   identity("BINANCE", "synthetic-only"), True)
    assert data["average"] == "99999"
    assert data["quantity"] == "42"
    assert data["confirmed"] is False
    assert data["price_source"] == "UNAVAILABLE"
    assert data["references"] == []
