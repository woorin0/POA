import asyncio
import concurrent.futures
from contextlib import closing
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from harness import ROOT, setup, load
schema, legacy, util = setup()
Settings = schema.Settings
OrderRequest = schema.OrderRequest
from exchange.safe_retry import (
    Store, Manager, Config, Adapter, SafetyError, classify,
    reconcile_pending, reconciliation_loop,
    CLIENT_KEYS, UPBIT_CLIENT_KEYS, ALL_CLIENT_KEYS, safe_submit
)
from exchange.upbit import Upbit
from exchange.verified_pnl.dispatcher import (
    dispatch_verified_alert, verification_tick, flush_outbox
)
from exchange.verified_pnl.models import (
    FillStatus, VerificationStatus, NotificationPayload, ExitReason, PnlResult
)
from exchange.verified_pnl.outbox import NotificationOutboxStore
from exchange.verified_pnl.verifier import FillVerifier, _VERIFY_SEMAPHORE, configure_concurrency
import ccxt


class Defect1FinalFillAlertTests(unittest.TestCase):
    """Defect 1: Prevent missing final fill alert for unconfirmed orders."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "d1_notifications.sqlite3")
        self.settings = Settings(
            _env_file=None,
            PASSWORD="test-password",
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            DISCORD_OUTBOX_ENABLED=True,
            DISCORD_RETRY_ENABLED=True,
            NET_REALIZED_PNL_ENABLED=True,
            EXIT_REASON_CLASSIFICATION_V2=True,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
            DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/test/mock",
            LIGHTWEIGHT_MODE=True,
            UPBIT_VERIFIED_ALERTS=True,
            UPBIT_NET_PNL_ENABLED=True,
            UPBIT_MAX_VERIFY_ATTEMPTS=5,
            FILL_VERIFICATION_TIMEOUT_SEC=30.0,
        )
        self.store = NotificationOutboxStore(self.db_path)
        self.order_info = OrderRequest(
            exchange="UPBIT",
            base="BTC",
            quote="KRW",
            side="buy",
            amount=0.01,
            signal_id="sig-d1-test",
        )

    def test_unconfirmed_order_not_terminated_in_lightweight_mode(self):
        """In lightweight mode, orders reaching max attempts transition to recovery, NOT prematurely DONE."""
        dispatch_verified_alert(
            "UPBIT",
            {"uuid": "upbit-pending-d1"},
            self.order_info,
            self.settings,
        )

        client = Mock()
        client.market.return_value = {}
        client.fetch_order.return_value = {
            "uuid": "upbit-pending-d1",
            "state": "wait",
            "volume": "0.01",
            "executed_volume": "0.0",
        }

        # Tick 5 times up to max attempts
        for _ in range(5):
            with self.store._connect() as con:
                con.execute("UPDATE verified_jobs SET due=0")
            verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            job = con.execute("SELECT state, data FROM verified_jobs WHERE data LIKE '%upbit-pending-d1%'").fetchone()
            self.assertIsNotNone(job)
            # Must remain PENDING in recovery phase, NOT marked DONE!
            self.assertEqual(job[0], "PENDING")
            data = json.loads(job[1])
            self.assertEqual(data.get("stage"), "recovery")
            self.assertTrue(data.get("unverified_alert_sent"))

            # Outbox must contain exactly 1 unverified notification
            outbox_rows = con.execute("SELECT payload FROM verified_outbox").fetchall()
            self.assertEqual(len(outbox_rows), 1)
            payload = json.loads(outbox_rows[0][0])
            self.assertEqual(payload["event_type"], "unverified")

    def test_final_fill_alert_sent_after_unconfirmed_recovery(self):
        """When an order that was previously unconfirmed eventually fills, the final Discord alert is sent."""
        dispatch_verified_alert(
            "UPBIT",
            {"uuid": "upbit-recover-d1"},
            self.order_info,
            self.settings,
        )

        client = Mock()
        client.market.return_value = {}
        # Initially pending
        client.fetch_order.return_value = {
            "uuid": "upbit-recover-d1",
            "state": "wait",
            "volume": "0.01",
            "executed_volume": "0.0",
        }

        # 5 ticks: transitions to recovery with unverified alert
        for _ in range(5):
            with self.store._connect() as con:
                con.execute("UPDATE verified_jobs SET due=0")
            verification_tick(self.settings, lambda *a: client)

        # Later: order finally fills on exchange!
        client.fetch_order.return_value = {
            "uuid": "upbit-recover-d1",
            "state": "done",
            "price": "50000000",
            "volume": "0.01",
            "executed_volume": "0.01",
            "trades": [
                {"id": "t-d1-1", "price": 50000000.0, "amount": 0.01, "fee": 250.0, "fee_currency": "KRW", "timestamp": 1000.0}
            ],
        }

        with self.store._connect() as con:
            con.execute("UPDATE verified_jobs SET due=0")
        verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            # Job is now DONE because it authoritatively filled
            job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE '%upbit-recover-d1%'").fetchone()
            self.assertEqual(job[0], "DONE")

            # Outbox now contains 2 events: first "unverified", then "final"
            outbox_rows = con.execute("SELECT payload FROM verified_outbox ORDER BY created_at").fetchall()
            self.assertEqual(len(outbox_rows), 2)
            p1 = json.loads(outbox_rows[0][0])
            p2 = json.loads(outbox_rows[1][0])
            self.assertEqual(p1["event_type"], "unverified")
            self.assertEqual(p2["event_type"], "final")
            self.assertEqual(p2["fill_status"], "FILLED")
            self.assertNotEqual(p1["event_id"], p2["event_id"])


class Defect2EventLoopNonBlockingTests(unittest.TestCase):
    """Defect 2: Event loop non-blockingSAFE_RETRY reconciliation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "d2_safe.db"
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
            PASSWORD="test-password",
            SAFE_RETRY_ENABLED=True,
            ORDER_RECONCILIATION_ENABLED=True,
            DUPLICATE_SIGNAL_PROTECTION=True,
            PERSIST_ORDER_STATE=True,
            SAFE_RETRY_DB_PATH=str(self.db_path),
        )

    def test_reconciliation_offloaded_via_to_thread_does_not_block_event_loop(self):
        """13 pending orders with delayed exchange response do not block the asyncio event loop."""
        # Seed 13 pending orders in store
        for i in range(13):
            cid = f"poa{i:029d}"
            signal = hashlib.sha256(f"sig-{i}".encode()).hexdigest()
            row = dict(
                signal=signal, asset=f"BINANCE:BTC/USDT:{i}", state="UNKNOWN",
                exchange="BINANCE", symbol="BTC/USDT", client_id=cid, order_id=f"oid-{i}",
                created=self.now, reference=50000.0, target="0.01", filled="0",
                remaining="0.01", attempts=1, futures=True, side="buy", close=False
            )
            self.store.claim(row)

        # Mock slow bot that takes 0.05s per query
        def slow_reconcile(*args):
            time.sleep(0.05)
            return {"id": "reconciled", "status": "closed", "filled": 0.01}

        slow_bot = Mock()
        slow_bot.client = Mock()
        slow_bot.client.options = {}
        slow_bot.client.fetch_order.side_effect = slow_reconcile
        slow_bot.client.fetch_open_orders.return_value = []
        slow_bot.client.fetch_closed_orders.return_value = []
        slow_bot.client.fetch_my_trades.return_value = []
        slow_bot.client.fetch_positions.return_value = [{"symbol": "BTC/USDT", "side": "long", "contracts": 1.0}]

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        async def run_test():
            # Start background reconciliation in thread
            recon_task = asyncio.create_task(asyncio.to_thread(reconcile_pending, self.settings, lambda ex: slow_bot))

            # Concurrently run async tasks on event loop measuring lag
            ticks = 0
            for _ in range(5):
                t0 = time.perf_counter()
                await asyncio.sleep(0.02)
                elapsed = time.perf_counter() - t0
                # If event loop was blocked by 13 * 0.05s = 0.65s, elapsed would be > 0.5s!
                self.assertLess(elapsed, 0.15, "Event loop was blocked by reconciliation!")
                ticks += 1

            await recon_task
            self.assertEqual(ticks, 5)

        asyncio.run(run_test())

    def test_reconciliation_does_not_hold_manager_mutex_during_network_calls(self):
        """manager.execute() for new incoming webhook is NOT blocked while slow reconciliation is running."""
        signal = hashlib.sha256(b"slow-recon").hexdigest()
        row = dict(
            signal=signal, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="poa_slow_1234567890123456789012",
            order_id="oid-slow", created=self.now, reference=50000.0, target="0.01",
            filled="0", remaining="0.01", attempts=1, futures=True, side="buy", close=False
        )
        self.store.claim(row)

        network_started = threading.Event()
        can_finish = threading.Event()

        def slow_fetch(*args, **kw):
            network_started.set()
            can_finish.wait(timeout=2.0)
            return {"id": "oid-slow", "status": "closed", "filled": 0.01}

        slow_bot = Mock()
        slow_bot.client = Mock()
        slow_bot.client.options = {}
        slow_bot.client.fetch_order.side_effect = slow_fetch
        slow_bot.client.fetch_open_orders.return_value = []
        slow_bot.client.fetch_closed_orders.return_value = []
        slow_bot.client.fetch_my_trades.return_value = []
        slow_bot.client.fetch_positions.return_value = []

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        t_recon = threading.Thread(target=reconcile_pending, args=(self.settings, lambda ex: slow_bot))
        t_recon.start()

        # Wait until reconciliation is inside the network call
        self.assertTrue(network_started.wait(timeout=1.0))

        # Now, simulate a new incoming webhook executing manager.execute() on a different asset
        order_info = OrderRequest(
            exchange="BINANCE", base="ETH", quote="USDT.P", side="buy",
            amount=0.1, signal_id="incoming-fast-sig", price=3000.0
        )
        fast_client = Mock()
        fast_client.market.return_value = {"contract": True, "contractSize": 1.0, "limits": {}}
        fast_client.fetch_ticker.return_value = {"last": 3000.0}
        fast_client.fetch_positions.return_value = []
        fast_client.create_order.return_value = {"id": "fast-fill", "status": "closed", "filled": 0.1}

        fast_bot = NS(client=fast_client, position_mode="one-way")
        args = ("ETH/USDT:USDT", "market", "buy", 0.1, None, {})

        t_start = time.perf_counter()
        # This MUST succeed immediately without waiting for can_finish!
        res = self.manager.execute(fast_client.create_order, args, order_info, Adapter(fast_bot, order_info))
        elapsed = time.perf_counter() - t_start

        self.assertEqual(res["id"], "fast-fill")
        self.assertLess(elapsed, 0.2, "Incoming webhook order was blocked by ongoing reconciliation query!")

        can_finish.set()
        t_recon.join(timeout=2.0)


class Defect3SignalCollisionTests(unittest.TestCase):
    """Defect 3: Namespaced signal_id collision prevention and backward compatibility."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "d3_safe.db"
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

    def _make_bot(self, api_key="acct-1"):
        client = Mock()
        client.apiKey = api_key
        client.market.return_value = {"contract": True, "contractSize": 1.0, "limits": {}}
        client.fetch_ticker.return_value = {"last": 50000.0}
        client.fetch_positions.return_value = []
        client.create_order.return_value = {"id": "oid-1", "status": "closed", "filled": 0.01}
        return NS(client=client, position_mode="one-way")

    def test_same_signal_resend_blocked(self):
        """Resubmitting the identical signal must be blocked."""
        info = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT.P", side="buy", amount=0.01, signal_id="common-sig-1")
        bot = self._make_bot()
        args = ("BTC/USDT:USDT", "market", "buy", 0.01, None, {})

        self.manager.execute(bot.client.create_order, args, info, Adapter(bot, info))
        self.assertEqual(len(bot.client.create_order.mock_calls), 1)

        # Resubmit identical signal
        with self.assertRaises(SafetyError) as ctx:
            self.manager.execute(bot.client.create_order, args, info, Adapter(bot, info))
        self.assertIn("Signal already recorded", str(ctx.exception))
        self.assertEqual(len(bot.client.create_order.mock_calls), 1)

    def test_same_signal_id_different_symbols_distinguished(self):
        """Same signal_id on different symbols must NOT collide and both execute."""
        info_btc = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT.P", side="buy", amount=0.01, signal_id="shared-sig")
        info_eth = OrderRequest(exchange="BINANCE", base="ETH", quote="USDT.P", side="buy", amount=0.1, signal_id="shared-sig")

        bot_btc = self._make_bot()
        bot_eth = self._make_bot()

        self.manager.execute(bot_btc.client.create_order, ("BTC/USDT:USDT", "market", "buy", 0.01, None, {}), info_btc, Adapter(bot_btc, info_btc))
        self.manager.execute(bot_eth.client.create_order, ("ETH/USDT:USDT", "market", "buy", 0.1, None, {}), info_eth, Adapter(bot_eth, info_eth))

        self.assertEqual(len(bot_btc.client.create_order.mock_calls), 1)
        self.assertEqual(len(bot_eth.client.create_order.mock_calls), 1)

    def test_same_signal_id_different_strategies_distinguished(self):
        """Same signal_id from different strategies on same symbol must NOT collide."""
        info_s1 = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT.P", side="buy", amount=0.01, signal_id="sig-tv", strategy_id="strat-rsi")
        info_s2 = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT.P", side="buy", amount=0.01, signal_id="sig-tv", strategy_id="strat-macd")

        bot1 = self._make_bot()
        bot2 = self._make_bot()

        self.manager.execute(bot1.client.create_order, ("BTC/USDT:USDT", "market", "buy", 0.01, None, {}), info_s1, Adapter(bot1, info_s1))
        self.manager.execute(bot2.client.create_order, ("BTC/USDT:USDT", "market", "buy", 0.01, None, {}), info_s2, Adapter(bot2, info_s2))

        self.assertEqual(len(bot1.client.create_order.mock_calls), 1)
        self.assertEqual(len(bot2.client.create_order.mock_calls), 1)

    def test_same_signal_id_different_accounts_distinguished(self):
        """Same signal_id on different accounts must NOT collide."""
        info1 = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT.P", side="buy", amount=0.01, signal_id="sig-multiacct")
        info2 = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT.P", side="buy", amount=0.01, signal_id="sig-multiacct")

        bot1 = self._make_bot(api_key="account-key-alpha")
        bot2 = self._make_bot(api_key="account-key-beta")

        self.manager.execute(bot1.client.create_order, ("BTC/USDT:USDT", "market", "buy", 0.01, None, {}), info1, Adapter(bot1, info1))
        self.manager.execute(bot2.client.create_order, ("BTC/USDT:USDT", "market", "buy", 0.01, None, {}), info2, Adapter(bot2, info2))

        self.assertEqual(len(bot1.client.create_order.mock_calls), 1)
        self.assertEqual(len(bot2.client.create_order.mock_calls), 1)

    def test_legacy_saved_signal_backward_compatibility(self):
        """Signals stored under legacy sha256(exchange:signal_id) are still recognized and blocked upon resend."""
        legacy_signal_id = "legacy-tv-signal-123"
        legacy_hash = hashlib.sha256(f"BINANCE:{legacy_signal_id}".encode()).hexdigest()

        # Seed legacy record in sqlite
        legacy_row = {
            "signal": legacy_hash,
            "asset": "BINANCE:BTC/USDT:USDT",
            "state": "FILLED",
            "exchange": "BINANCE",
            "symbol": "BTC/USDT:USDT",
            "client_id": "poa_legacy",
            "order_id": "oid-legacy",
            "created": 1000.0,
            "reference": 50000.0,
            "target": "0.01",
            "filled": "0.01",
            "remaining": "0",
            "attempts": 1,
            "futures": True,
            "side": "buy",
            "close": False,
        }
        with self.store.connect() as con:
            con.execute("INSERT INTO safe_orders VALUES (?,?,?,?)", (legacy_hash, legacy_row["asset"], "FILLED", json.dumps(legacy_row)))

        # Now send new order with same signal_id
        info = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT.P", side="buy", amount=0.01, signal_id=legacy_signal_id)
        bot = self._make_bot()
        args = ("BTC/USDT:USDT", "market", "buy", 0.01, None, {})

        with self.assertRaises(SafetyError) as ctx:
            self.manager.execute(bot.client.create_order, args, info, Adapter(bot, info))
        self.assertIn("Signal already recorded", str(ctx.exception))
        self.assertEqual(len(bot.client.create_order.mock_calls), 0)


class Defect4UpbitRealApiSerializationTests(unittest.TestCase):
    """Defect 4: Verify actual Upbit CCXT API HTTP request serialization."""

    def setUp(self):
        self.client = ccxt.upbit({"apiKey": "mock_upbit_key", "secret": "mock_upbit_secret"})
        self.client.markets = {
            "BTC/KRW": {
                "id": "KRW-BTC",
                "symbol": "BTC/KRW",
                "base": "BTC",
                "quote": "KRW",
                "baseId": "BTC",
                "quoteId": "KRW",
                "spot": True,
                "precision": {"amount": 0.00000001, "price": 1000},
                "limits": {"amount": {"min": 0.00005}, "cost": {"min": 5000}},
            }
        }
        self.client.markets_by_id = {"KRW-BTC": self.client.markets["BTC/KRW"]}
        self.client.symbols = ["BTC/KRW"]

        self.captured = {}
        def mock_fetch(url, method="GET", headers=None, body=None):
            self.captured["url"] = url
            self.captured["method"] = method
            self.captured["headers"] = headers
            self.captured["body"] = body
            parsed_body = json.loads(body) if body else {}
            return {
                "uuid": "upbit-http-uuid-1",
                "state": "done",
                "price": parsed_body.get("price"),
                "volume": parsed_body.get("volume"),
                "ord_type": parsed_body.get("ord_type"),
                "side": parsed_body.get("side"),
            }
        self.client.fetch = mock_fetch

    def test_upbit_market_buy_http_request_serialization(self):
        """Market buy sends ord_type=price, price=<KRW total>, NO volume, and identifier."""
        cid = "poa" + "a" * 29
        # amount=0.01 BTC, price=50,000,000 KRW -> total cost = 500,000 KRW
        res = self.client.create_order("BTC/KRW", "market", "buy", 0.01, 50000000.0, {"identifier": cid})

        self.assertIsNotNone(self.captured.get("body"))
        body = json.loads(self.captured["body"])

        self.assertEqual(body["market"], "KRW-BTC")
        self.assertEqual(body["side"], "bid")
        self.assertEqual(body["ord_type"], "price")
        self.assertEqual(body["price"], "500000")
        self.assertNotIn("volume", body, "Upbit market buy must NOT pass volume parameter!")
        self.assertEqual(body["identifier"], cid)

    def test_upbit_market_sell_http_request_serialization(self):
        """Market sell sends ord_type=market, volume=<coin volume>, NO price, and identifier."""
        cid = "poa" + "b" * 29
        res = self.client.create_order("BTC/KRW", "market", "sell", 0.025, None, {"identifier": cid})

        self.assertIsNotNone(self.captured.get("body"))
        body = json.loads(self.captured["body"])

        self.assertEqual(body["market"], "KRW-BTC")
        self.assertEqual(body["side"], "ask")
        self.assertEqual(body["ord_type"], "market")
        self.assertEqual(body["volume"], "0.025")
        self.assertNotIn("price", body, "Upbit market sell must NOT pass price parameter!")
        self.assertEqual(body["identifier"], cid)


class Defect5LightweightModeTests(unittest.TestCase):
    """Defect 5: LIGHTWEIGHT_MODE setting behaviors."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "d5_notifications.sqlite3")
        self.store = NotificationOutboxStore(self.db_path)

    def test_lightweight_mode_false_maintains_legacy_behavior(self):
        """When LONG_TERM_RECOVERY_ENABLED=False, unconfirmed order is held in MANUAL_REVIEW, never DONE."""
        settings = Settings(
            _env_file=None,
            PASSWORD="test-password",
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            DISCORD_OUTBOX_ENABLED=True,
            UPBIT_VERIFIED_ALERTS=True,
            UPBIT_MAX_VERIFY_ATTEMPTS=5,
            LIGHTWEIGHT_MODE=False,
            LONG_TERM_RECOVERY_ENABLED=False,  # Recovery disabled: must be safely held in MANUAL_REVIEW
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
        )
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        dispatch_verified_alert("UPBIT", {"uuid": "ord-d5-legacy"}, order_info, settings)

        client = Mock()
        client.market.return_value = {}
        client.fetch_order.return_value = {"uuid": "ord-d5-legacy", "state": "wait", "volume": "0.01", "executed_volume": "0"}

        for _ in range(5):
            with self.store._connect() as con:
                con.execute("UPDATE verified_jobs SET due=0")
            verification_tick(settings, lambda *a: client)

        with self.store._connect_read() as con:
            job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE '%ord-d5-legacy%'").fetchone()
            self.assertEqual(job[0], "MANUAL_REVIEW")

    def test_lightweight_mode_true_applies_concurrency_and_recovery(self):
        """When LIGHTWEIGHT_MODE=True, max attempts transitions to recovery polling, and concurrency semaphore is configured."""
        settings = Settings(
            _env_file=None,
            PASSWORD="test-password",
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            DISCORD_OUTBOX_ENABLED=True,
            UPBIT_VERIFIED_ALERTS=True,
            UPBIT_MAX_VERIFY_ATTEMPTS=5,
            LIGHTWEIGHT_MODE=True,
            VERIFY_MAX_CONCURRENCY=1,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
        )
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        dispatch_verified_alert("UPBIT", {"uuid": "ord-d5-light"}, order_info, settings)

        client = Mock()
        client.market.return_value = {}
        client.fetch_order.return_value = {"uuid": "ord-d5-light", "state": "wait", "volume": "0.01", "executed_volume": "0"}

        for _ in range(5):
            with self.store._connect() as con:
                con.execute("UPDATE verified_jobs SET due=0")
            verification_tick(settings, lambda *a: client)

        with self.store._connect_read() as con:
            job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE '%ord-d5-light%'").fetchone()
            # In lightweight mode, stays PENDING in recovery
            self.assertEqual(job[0], "PENDING")


if __name__ == "__main__":
    unittest.main()
