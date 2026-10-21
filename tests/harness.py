"""Load real source with isolated settings/DB/Discord and a network tripwire."""
import ast
import importlib.util
import os
from pathlib import Path
import socket
import sys
import types
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def blocked(*a, **k):
    raise AssertionError("Network is forbidden in tests")

_connect = socket.socket.connect
def local_connect(sock, address):
    if address[0] not in ("127.0.0.1", "::1"):
        return blocked()
    return _connect(sock, address)
socket.socket.connect = local_connect
socket.create_connection = blocked

def module(name, **attrs):
    obj = types.ModuleType(name)
    obj.__dict__.update(attrs)
    sys.modules[name] = obj
    return obj

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj

def setup(root=ROOT):
    for name in list(sys.modules):
        if name == "exchange" or name.startswith("exchange."):
            del sys.modules[name]
    try:
        from pydantic import class_validators
        if hasattr(class_validators, "_FUNCS"):
            class_validators._FUNCS.clear()
    except Exception:
        pass
    pkg = module("exchange", __path__=[str(root / "exchange")])
    with patch("os.path.isfile", return_value=False):
        schema = load("exchange.model", root / "exchange/model/schemas.py")
    schema.Settings.Config.env_file = None
    os.environ["PASSWORD"] = "test-only"
    settings = schema.Settings(_env_file=None, PASSWORD="test-only", WHITELIST=[])
    util = module("exchange.utility", settings=settings, __path__=[str(root / "exchange/utility")])
    for name in ["log_message", "log_order_message", "print_alert_message", "log_alert_message",
                 "logger_test", "log_order_error_message", "log_validation_error_message",
                 "log_hedge_message", "log_error_message"]:
        setattr(util, name, Mock())
    module("exchange.database", db=Mock())
    module("exchange.stock", KoreaInvestment=type("KoreaInvestment", (), {}), __path__=[])
    module("exchange.stock.kis", KoreaInvestment=sys.modules["exchange.stock"].KoreaInvestment)
    module("exchange.pocket")
    module("pendulum")
    import ccxt
    import httpx
    pre = module("exchange.pexchange", ccxt=ccxt, ccxt_async=Mock(), httpx=httpx)
    pkg.error = load("exchange.error", root / "exchange/error.py")
    for name in ["binance", "bybit", "okx", "bitget", "upbit"]:
        load("exchange." + name, root / ("exchange/" + name + ".py"))
    # Execute into the same object used by circular imports.
    pre.__package__ = "exchange"
    exec(compile((root / "exchange/pexchange.py").read_text(encoding="utf-8"),
                 str(root / "exchange/pexchange.py"), "exec"), pre.__dict__)
    for name in ["get_exchange", "get_bot"]:
        setattr(pkg, name, getattr(pre, name))
    pkg.db = sys.modules["exchange.database"].db
    pkg.settings = settings
    pkg.log_message = util.log_message
    return schema, pre, util

class FakeClient:
    def __init__(self):
        self.options = {}
        self.calls = []
    def market(self, symbol):
        return {"contract": ":" in symbol, "contractSize": .001,
                "precision": {"amount": .001}, "limits": {"amount": {"min": .001}}}
    def amount_to_precision(self, symbol, value):
        return str(int(float(value) * 1000) / 1000)
    def price_to_precision(self, symbol, value):
        return str(value)
    def fetch_ticker(self, symbol):
        return {"last": 100.0}
    def fetch_free_balance(self):
        return {"BTC": 10, "USDT": 1000}
    fetch_total_balance = fetch_free_balance
    def fetch_positions(self, symbols=None):
        return [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 10,
                 "entryPrice": 90, "leverage": 2}]
    def fetch_balance(self):
        return {"info": {"positions": [{"symbol": "BTCUSDT", "positionAmt": "10",
                "positionSide": "BOTH", "entryPrice": "90", "leverage": "2"}]}}
    def fetch_trading_fee(self, symbol):
        return {"taker": .001}
    def set_leverage(self, *a, **k):
        self.calls.append(["leverage", list(a), k])
    def create_order(self, *a):
        self.calls.append(["order", list(a)])
        return {"id": "fake1", "status": "closed", "amount": a[3], "filled": a[3],
                "price": 100, "average": 100, "info": {"orderQty": a[3]}}

def bot_for(name, info):
    cls = getattr(sys.modules["exchange." + name.lower()], name.title())
    bot = cls.__new__(cls)
    bot.client = FakeClient()
    bot.position_mode = "one-way"
    bot.init_info(info)
    return bot
