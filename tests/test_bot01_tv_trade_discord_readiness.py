"""BOT01 read-only source-contract checks. Never contact exchanges or Discord."""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace



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
    installer = ast.parse((ROOT / "deployment/install_once.py").read_text(encoding="utf-8"))
    flags_node = next(node for node in installer.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "FLAGS"
                              for target in node.targets))
    flags = ast.literal_eval(flags_node.value)
    assert flags["VERIFIED_DISCORD_ALERTS"] is True
    assert flags["VERIFIED_FILL_PRICE"] is True
    assert flags["DISCORD_OUTBOX_ENABLED"] is True
    assert flags["UPBIT_VERIFIED_ALERTS"] is True
    assert "POA_EXCHANGE_FILL_CONFIRMATION_ENABLED" not in flags

    schemas = ast.parse((ROOT / "exchange/model/schemas.py").read_text(encoding="utf-8"))
    settings_cls = next(x for x in schemas.body
                        if isinstance(x, ast.ClassDef) and x.name == "Settings")
    native_switch = next(x for x in settings_cls.body
                         if isinstance(x, ast.AnnAssign)
                         and isinstance(x.target, ast.Name)
                         and x.target.id == "POA_EXCHANGE_FILL_CONFIRMATION_ENABLED")
    assert ast.literal_eval(native_switch.value) is False

    confirmation = ast.parse(
        (ROOT / "exchange/verified_pnl/order_fill_confirmation.py").read_text(encoding="utf-8")
    )
    prepare = next(x for x in confirmation.body
                   if isinstance(x, ast.FunctionDef) and x.name == "prepare")
    prepare.decorator_list = []
    scope = {"_HOSTS": {}, "Path": Path}
    exec(compile(ast.Module(body=[prepare], type_ignores=[]),
                 "bot01-native-read-prepare", "exec"), scope)
    st = SimpleNamespace(POA_EXCHANGE_FILL_CONFIRMATION_ENABLED=False,
                         VERIFIED_NOTIFICATIONS_DB_PATH=str(tmp_path / "test.sqlite3"))
    assert scope["prepare"](st) is None


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
    source = ast.parse((ROOT / "exchange/verified_pnl/order_fill_read.py")
                       .read_text(encoding="utf-8"))
    node = next(x for x in source.body if isinstance(x, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "VENUES" for t in x.targets))
    assert isinstance(node.value, ast.Call)
    assert set(ast.literal_eval(node.value.args[0])) == {"UPBIT", "BINANCE", "BYBIT", "OKX"}


def test_tv_signal_is_never_execution_proof():
    source = (ROOT / "exchange/verified_pnl/tradingview_signal.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "capture")
    body = ast.get_source_segment(source, node)
    assert "confirmed=False" in body
    assert "price_source='UNAVAILABLE'" in body
    assert "notional=value" in body  # Signal-based estimate only, not an execution receipt
