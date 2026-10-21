import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from harness import ROOT, setup, load
schema, legacy, util = setup()
Settings = schema.Settings
OrderRequest = schema.OrderRequest
from exchange.safe_retry import (
    Store, Manager, Config, Adapter, SafetyError, classify,
    CLIENT_KEYS, UPBIT_CLIENT_KEYS, ALL_CLIENT_KEYS, safe_submit
)
from exchange.upbit import Upbit
import ccxt


class FakeUpbitClient:
    def __init__(self):
        self.options = {"defaultType": "spot"}
        self.calls = []
        self.balances = {"KRW": 100_000_000.0, "BTC": 2.5}
        self.ticker_price = 50_000_000.0
        self.orders = {}
        self.order_counter = 0

    def market(self, symbol):
        return {
            "contract": False,
            "contractSize": 1.0,
            "precision": {"amount": 0.0001, "price": 1000.0},
            "limits": {
                "amount": {"min": 0.0001, "max": 100.0},
                "cost": {"min": 5000.0, "max": 1_000_000_000.0},
            },
        }

    def amount_to_precision(self, symbol, value):
        return str(round(float(value), 4))

    def price_to_precision(self, symbol, value):
        return str(round(float(value), 0))

    def fetch_ticker(self, symbol):
        return {"last": self.ticker_price, "bid": self.ticker_price, "ask": self.ticker_price}

    def fetch_free_balance(self):
        return dict(self.balances)

    fetch_total_balance = fetch_free_balance

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = params or {}
        self.calls.append(("create_order", symbol, type, side, amount, price, dict(params)))
        self.order_counter += 1
        order_uuid = f"upbit-uuid-{self.order_counter}"
        cid = params.get("identifier") or params.get("clientOrderId")
        order_record = {
            "id": order_uuid,
            "uuid": order_uuid,
            "clientOrderId": cid,
            "identifier": cid,
            "symbol": symbol,
            "type": type,
            "side": side,
            "amount": float(amount),
            "price": float(price) if price else self.ticker_price,
            "status": "closed",
            "state": "done",
            "filled": float(amount),
            "executed_volume": float(amount),
            "remaining": 0.0,
        }
        self.orders[order_uuid] = order_record
        if cid:
            self.orders[cid] = order_record
        return order_record

    def fetch_order(self, id, symbol=None, params=None):
        if id in self.orders:
            return self.orders[id]
        raise ccxt.OrderNotFound(f"Order {id} not found")

    def privateGetOrder(self, params=None):
        params = params or {}
        if "uuid" in params and params["uuid"] in self.orders:
            return self.orders[params["uuid"]]
        if "identifier" in params and params["identifier"] in self.orders:
            return self.orders[params["identifier"]]
        raise ccxt.OrderNotFound("Order not found")

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return [o for o in self.orders.values() if o.get("status") == "open" or o.get("state") in ("wait", "watch")]

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        return [o for o in self.orders.values() if o.get("status") == "closed" or o.get("state") == "done"]


class UpbitSafeRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "upbit_orders.db"
        self.store = Store(self.db_path)
        self.events = []
        self.now = 1000.0
        self.manager = Manager(
            self.store,
            Config(base_delay=0, jitter=False),
            self.events.append,
            lambda: self.now,
            lambda _: None,
        )
        self.settings = Settings(
            _env_file=None,
            PASSWORD="test-only",
            SAFE_RETRY_ENABLED=True,
            UPBIT_SAFE_RETRY_ENABLED=True,
            ORDER_RECONCILIATION_ENABLED=True,
            DUPLICATE_SIGNAL_PROTECTION=True,
            PERSIST_ORDER_STATE=True,
            SAFE_RETRY_DB_PATH=str(self.db_path),
        )

        self.client = FakeUpbitClient()
        self.bot = Upbit.__new__(Upbit)
        self.bot.client = self.client
        self.bot.position_mode = "one-way"
        self.bot.order_info = None

    def make_order_request(self, side="buy", amount=0.01, percent=None, signal_id="sig-test-1"):
        payload = {
            "exchange": "UPBIT",
            "base": "BTC",
            "quote": "KRW",
            "side": side,
            "signal_id": signal_id,
            "price": 50_000_000.0,
        }
        if amount is not None:
            payload["amount"] = amount
        if percent is not None:
            payload["percent"] = percent
        order_info = OrderRequest(**payload)
        self.bot.init_info(order_info)
        return order_info

    def rows(self):
        with closing(sqlite3.connect(self.store.path)) as con:
            return [json.loads(r[0]) for r in con.execute("SELECT data FROM safe_orders")]

    def test_upbit_client_keys_configuration(self):
        self.assertIn("UPBIT", UPBIT_CLIENT_KEYS)
        self.assertEqual(UPBIT_CLIENT_KEYS["UPBIT"], "identifier")
        self.assertIn("UPBIT", ALL_CLIENT_KEYS)
        self.assertEqual(ALL_CLIENT_KEYS["UPBIT"], "identifier")
        # Ensure legacy CLIENT_KEYS retains exactly 3 exchanges for test_success_and_client_ids_all_exchanges
        self.assertEqual(set(CLIENT_KEYS.keys()), {"BINANCE", "BYBIT", "OKX"})

    def test_upbit_safe_retry_market_buy_with_signal_id(self):
        order_info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-buy-001")
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})
        adapter = Adapter(self.bot, order_info)
        result = self.manager.execute(self.client.create_order, args, order_info, adapter)

        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["filled"], 0.01)
        self.assertEqual(len(self.client.calls), 1)

        call = self.client.calls[0]
        params = call[6]
        self.assertIn("identifier", params)
        cid = params["identifier"]
        self.assertTrue(cid.startswith("poa"))
        self.assertEqual(len(cid), 32)

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "FILLED")
        self.assertEqual(rows[0]["client_id"], cid)
        self.assertEqual(rows[0]["order_id"], result["id"])

    def test_upbit_safe_retry_market_sell_with_signal_id(self):
        order_info = self.make_order_request(side="sell", amount=0.02, signal_id="sig-sell-001")
        args = ("BTC/KRW", "market", "sell", 0.02, None, {})
        adapter = Adapter(self.bot, order_info)
        result = self.manager.execute(self.client.create_order, args, order_info, adapter)

        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["filled"], 0.02)
        self.assertEqual(len(self.client.calls), 1)
        cid = self.client.calls[0][6]["identifier"]
        self.assertTrue(cid.startswith("poa"))
        self.assertEqual(len(cid), 32)
        self.assertEqual(self.rows()[0]["state"], "FILLED")

    def test_upbit_percent_based_buy_and_sell(self):
        # 50% KRW buy: 100M * 50% = 50M KRW / 50M price = 1.0 BTC
        buy_info = self.make_order_request(side="buy", amount=None, percent=50.0, signal_id="sig-pct-buy")
        self.assertEqual(self.bot.get_amount(buy_info), 1.0)

        # 100% BTC sell: 2.5 BTC * 100% = 2.5 BTC
        sell_info = self.make_order_request(side="sell", amount=None, percent=100.0, signal_id="sig-pct-sell")
        self.assertEqual(self.bot.get_amount(sell_info), 2.5)

    def test_upbit_amount_based_buy_and_sell(self):
        order_info = self.make_order_request(side="buy", amount=0.05, signal_id="sig-amt-1")
        self.assertEqual(self.bot.get_amount(order_info), 0.05)

    def test_upbit_tradingview_entry_close_aliases(self):
        # Test entry/buy
        entry_info = self.make_order_request(side="entry/buy", amount=0.01, signal_id="sig-entry-1")
        self.assertTrue(entry_info.is_entry)
        self.assertTrue(entry_info.is_buy)
        self.assertEqual(entry_info.side, "buy")

        # Test close/sell
        close_info = self.make_order_request(side="close/sell", amount=0.01, signal_id="sig-close-1")
        self.assertTrue(close_info.is_close)
        self.assertTrue(close_info.is_sell)
        self.assertEqual(close_info.side, "sell")

        # Spot close on Upbit should NOT require reduceOnly or positions check
        args = ("BTC/KRW", "market", "sell", 0.01, None, {})
        adapter = Adapter(self.bot, close_info)
        result = self.manager.execute(self.client.create_order, args, close_info, adapter)
        self.assertEqual(result["status"], "closed")

    def test_upbit_identifier_uniqueness_across_orders(self):
        cids = set()
        for i in range(3):
            info = self.make_order_request(side="buy", amount=0.01, signal_id=f"sig-unique-{i}")
            args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})
            self.manager.execute(self.client.create_order, args, info, Adapter(self.bot, info))
            cid = self.client.calls[-1][6]["identifier"]
            cids.add(cid)
            self.assertTrue(cid.startswith("poa"))
            self.assertEqual(len(cid), 32)
        self.assertEqual(len(cids), 3)

    def test_upbit_duplicate_signal_id_blocked(self):
        info1 = self.make_order_request(side="buy", amount=0.01, signal_id="sig-dup-1")
        args1 = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})
        self.manager.execute(self.client.create_order, args1, info1, Adapter(self.bot, info1))
        self.assertEqual(len(self.client.calls), 1)

        info2 = self.make_order_request(side="buy", amount=0.01, signal_id="sig-dup-1")
        args2 = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})
        with self.assertRaises(SafetyError) as ctx:
            self.manager.execute(self.client.create_order, args2, info2, Adapter(self.bot, info2))
        self.assertIn("Signal already recorded", str(ctx.exception))
        self.assertEqual(len(self.client.calls), 1)

    def test_upbit_concurrent_duplicate_signals(self):
        results = []
        errors = []

        def submit():
            info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-concurrent-dup")
            args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})
            try:
                res = self.manager.execute(self.client.create_order, args, info, Adapter(self.bot, info))
                results.append(res)
            except SafetyError as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(submit) for _ in range(5)]
            for f in futs:
                f.result()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 4)
        self.assertEqual(len(self.client.calls), 1)

    def test_upbit_missing_signal_id_rejected(self):
        # Construct directly without signal_id
        order_info = OrderRequest(
            exchange="UPBIT",
            base="BTC",
            quote="KRW",
            side="buy",
            amount=0.01,
            signal_id=None,
            price=50_000_000.0,
        )
        self.bot.init_info(order_info)
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})
        adapter = Adapter(self.bot, order_info)
        with self.assertRaises(SafetyError) as ctx:
            self.manager.execute(self.client.create_order, args, order_info, adapter)
        self.assertIn("requires 'signal_id'", str(ctx.exception))
        self.assertEqual(len(self.client.calls), 0)

    def test_upbit_safe_retry_disabled_flag_blocks(self):
        disabled_settings = Settings(
            _env_file=None,
            PASSWORD="test-only",
            SAFE_RETRY_ENABLED=True,
            UPBIT_SAFE_RETRY_ENABLED=False,
            ORDER_RECONCILIATION_ENABLED=True,
            DUPLICATE_SIGNAL_PROTECTION=True,
            PERSIST_ORDER_STATE=True,
            SAFE_RETRY_DB_PATH=str(self.db_path),
        )
        order_info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-flag-off")
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})
        with self.assertRaises(SafetyError) as ctx:
            safe_submit(self.client.create_order, args, order_info, self.bot, disabled_settings)
        self.assertIn("UPBIT safe mode is disabled", str(ctx.exception))
        self.assertEqual(len(self.client.calls), 0)

    def test_upbit_insufficient_krw_balance_rejected_before_exchange(self):
        self.client.balances["KRW"] = 100.0  # Need 0.01 * 50M = 500,000 KRW
        order_info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-no-krw")
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})
        adapter = Adapter(self.bot, order_info)
        row = {"created": self.now, "reference": 50_000_000.0}
        with self.assertRaises(SafetyError) as ctx:
            adapter.validate(args, row, self.manager.cfg, self.now)
        self.assertIn("Insufficient available spot balance", str(ctx.exception))

        with self.assertRaises(SafetyError) as ctx:
            self.manager.execute(self.client.create_order, args, order_info, adapter)
        self.assertIn("Order risk validation failed", str(ctx.exception))
        self.assertEqual(len(self.client.calls), 0)

    def test_upbit_insufficient_btc_balance_for_sell_rejected_before_exchange(self):
        self.client.balances["BTC"] = 0.005  # Need 0.02 BTC
        order_info = self.make_order_request(side="sell", amount=0.02, signal_id="sig-no-btc")
        args = ("BTC/KRW", "market", "sell", 0.02, None, {})
        adapter = Adapter(self.bot, order_info)
        row = {"created": self.now, "reference": 50_000_000.0}
        with self.assertRaises(SafetyError) as ctx:
            adapter.validate(args, row, self.manager.cfg, self.now)
        self.assertIn("Insufficient available spot balance", str(ctx.exception))

        with self.assertRaises(SafetyError) as ctx:
            self.manager.execute(self.client.create_order, args, order_info, adapter)
        self.assertIn("Order risk validation failed", str(ctx.exception))
        self.assertEqual(len(self.client.calls), 0)

    def test_upbit_auth_error_fatal_no_retry(self):
        order_info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-auth-err")
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})
        submit = Mock(side_effect=ccxt.AuthenticationError('{"error": {"name": "jwt_verification"}}'))
        with self.assertRaises(SafetyError) as ctx:
            self.manager.execute(submit, args, order_info, Adapter(self.bot, order_info))
        self.assertIn("Order rejected; retry stopped", str(ctx.exception))
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.rows()[0]["state"], "FAILED")

    def test_upbit_ratelimit_retry_generates_new_identifier(self):
        order_info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-rate-retry")
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})

        captured_cids = []

        def submit_with_rate_limit(*call_args):
            cid = call_args[5].get("identifier")
            captured_cids.append(cid)
            if len(captured_cids) == 1:
                raise ccxt.RateLimitExceeded('{"error": {"name": "too_many_requests"}}')
            return {"id": "upbit-rate-uuid", "status": "closed", "filled": 0.01}

        result = self.manager.execute(submit_with_rate_limit, args, order_info, Adapter(self.bot, order_info))
        self.assertEqual(result["status"], "closed")
        self.assertEqual(len(captured_cids), 2)
        # CRITICAL: Verify Upbit identifier rotated to avoid duplicate identifier rejection
        self.assertNotEqual(captured_cids[0], captured_cids[1])
        self.assertTrue(captured_cids[0].startswith("poa"))
        self.assertTrue(captured_cids[1].startswith("poa"))

    def test_upbit_timeout_filled_reconciles_by_identifier_without_resubmit(self):
        order_info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-timeout-rec")
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})

        def timeout_submit(*call_args):
            cid = call_args[5].get("identifier")
            # Order actually placed on exchange
            self.client.orders[cid] = {
                "id": "upbit-found-uuid",
                "uuid": "upbit-found-uuid",
                "clientOrderId": cid,
                "identifier": cid,
                "status": "closed",
                "state": "done",
                "filled": 0.01,
                "executed_volume": 0.01,
            }
            # Response lost
            raise ccxt.RequestTimeout("Network timeout waiting for order response")

        submit = Mock(side_effect=timeout_submit)
        result = self.manager.execute(submit, args, order_info, Adapter(self.bot, order_info))
        self.assertEqual(result["id"], "upbit-found-uuid")
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.rows()[0]["state"], "FILLED")

    def test_upbit_order_state_unknown_blocks_resubmission(self):
        order_info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-unknown-block")
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})

        submit = Mock(side_effect=ccxt.NetworkError("Exchange completely unreachable"))
        with self.assertRaises(SafetyError) as ctx:
            self.manager.execute(submit, args, order_info, Adapter(self.bot, order_info))
        self.assertIn("Order status unknown; automatic resend blocked", str(ctx.exception))
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.rows()[0]["state"], "UNKNOWN")

    def test_upbit_partial_fill_prevents_full_resubmission(self):
        order_info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-partial")
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})

        partial_result = {
            "id": "upbit-part-uuid",
            "uuid": "upbit-part-uuid",
            "status": "open",
            "state": "wait",
            "filled": 0.004,
            "executed_volume": 0.004,
            "remaining": 0.006,
        }
        submit = Mock(return_value=partial_result)
        result = self.manager.execute(submit, args, order_info, Adapter(self.bot, order_info))
        self.assertEqual(result["id"], "upbit-part-uuid")
        row = self.rows()[0]
        self.assertEqual(row["state"], "PARTIALLY_FILLED")
        self.assertEqual(row["filled"], "0.004")
        self.assertEqual(row["remaining"], "0.006")

    def test_upbit_server_restart_sqlite_recovery(self):
        order_info = self.make_order_request(side="buy", amount=0.01, signal_id="sig-recovery")
        args = ("BTC/KRW", "market", "buy", 0.01, 50_000_000.0, {})

        def crash_submit(*call_args):
            raise SystemExit("Simulated power loss")

        with self.assertRaises(SystemExit):
            self.manager.execute(crash_submit, args, order_info, Adapter(self.bot, order_info))

        self.assertEqual(self.rows()[0]["state"], "SUBMITTING")
        recovered = self.store.recover()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(self.rows()[0]["state"], "UNKNOWN")

    def test_upbit_fastapi_route_integration_full_lifecycle(self):
        import exchange.safe_retry as safe
        from fastapi.testclient import TestClient

        previous = safe._manager
        safe._manager = self.manager
        util.settings.SAFE_RETRY_ENABLED = True
        util.settings.UPBIT_SAFE_RETRY_ENABLED = True
        self.addCleanup(setattr, safe, "_manager", previous)
        self.addCleanup(setattr, util.settings, "SAFE_RETRY_ENABLED", False)
        self.addCleanup(setattr, util.settings, "UPBIT_SAFE_RETRY_ENABLED", False)

        main = load("upbit_integration_main", ROOT / "main.py")
        main.whitelist.append("testclient")
        main.get_bot = lambda *args: self.bot
        client = TestClient(main.app)

        payload = {
            "password": "test-only",
            "exchange": "UPBIT",
            "base": "BTC",
            "quote": "KRW",
            "side": "buy",
            "amount": 0.01,
            "signal_id": "route-upbit-1",
        }
        res1 = client.post("/order", json=payload)
        self.assertEqual(res1.json(), {"result": "success"})
        self.assertEqual(len(self.client.calls), 1)

        # Duplicate signal
        res2 = client.post("/order", json=payload)
        self.assertIsNone(res2.json())
        self.assertEqual(len(self.client.calls), 1)

    def test_upbit_fastapi_route_disabled_flag_blocks(self):
        import exchange.safe_retry as safe
        from fastapi.testclient import TestClient

        previous = safe._manager
        safe._manager = self.manager
        util.settings.SAFE_RETRY_ENABLED = True
        util.settings.UPBIT_SAFE_RETRY_ENABLED = False
        self.addCleanup(setattr, safe, "_manager", previous)
        self.addCleanup(setattr, util.settings, "SAFE_RETRY_ENABLED", False)
        self.addCleanup(setattr, util.settings, "UPBIT_SAFE_RETRY_ENABLED", False)

        main = load("upbit_disabled_main", ROOT / "main.py")
        main.whitelist.append("testclient")
        main.get_bot = lambda *args: self.bot
        client = TestClient(main.app)

        payload = {
            "password": "test-only",
            "exchange": "UPBIT",
            "base": "BTC",
            "quote": "KRW",
            "side": "buy",
            "amount": 0.01,
            "signal_id": "route-upbit-disabled",
        }
        res = client.post("/order", json=payload)
        self.assertIsNone(res.json())
        self.assertEqual(len(self.client.calls), 0)


if __name__ == "__main__":
    unittest.main()
