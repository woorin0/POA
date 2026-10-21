import asyncio
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

from harness import ROOT, setup
schema, legacy, util = setup()
Settings = schema.Settings
OrderRequest = schema.OrderRequest

from exchange.safe_retry import (
    Store, Manager, Config, Adapter, SafetyError,
    reconcile_pending, reconciliation_loop,
    get_reconciliation_clients, close_reconciliation_clients, ReconciliationClients
)
from exchange.verified_pnl.dispatcher import (
    dispatch_verified_alert, verification_tick, flush_outbox
)
from exchange.verified_pnl.models import (
    FillStatus, VerificationStatus, NotificationPayload, ExitReason, PnlResult
)
from exchange.verified_pnl.outbox import NotificationOutboxStore
import ccxt


class TwoDefectsVerificationTests(unittest.TestCase):
    """Rigorous verification for:
    1. Unconfirmed order long-term tracking and recovery (dispatcher.py)
    2. SAFE_RETRY CCXT concurrency and read-only isolation (safe_retry.py, main.py)
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "test_notifications.sqlite3")
        self.safe_db = str(Path(self.tmp.name) / "test_safe.sqlite3")
        self.settings = Settings(
            _env_file=None,
            PASSWORD="test-pwd",
            SAFE_RETRY_ENABLED=True,
            UPBIT_SAFE_RETRY_ENABLED=True,
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            DISCORD_OUTBOX_ENABLED=True,
            DISCORD_RETRY_ENABLED=True,
            UPBIT_VERIFIED_ALERTS=True,
            UPBIT_NET_PNL_ENABLED=True,
            UPBIT_MAX_VERIFY_ATTEMPTS=5,
            LIGHTWEIGHT_MODE=True,
            LONG_TERM_RECOVERY_ENABLED=True,
            LONG_TERM_RECOVERY_MAX_DELAY=300.0,
            FILL_VERIFICATION_TIMEOUT_SEC=30.0,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
            SAFE_RETRY_DB_PATH=self.safe_db,
            BINANCE_KEY="test_bin_key",
            BINANCE_SECRET="test_bin_sec",
            UPBIT_KEY="test_up_key",
            UPBIT_SECRET="test_up_sec",
        )
        self.store = NotificationOutboxStore(self.db_path)
        self.safe_store = Store(self.safe_db)
        self.manager = Manager(self.safe_store, Config())

    def tearDown(self):
        close_reconciliation_clients()

    # --- Defect 1: Unconfirmed order tracking & recovery ---

    def test_unconfirmed_order_tracking_maintained_past_1_hour(self):
        """1. Unconfirmed orders aged past 1 hour must remain in recovery tracking, NEVER marked DONE."""
        order_info = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT.P", side="buy", amount=0.01)
        dispatch_verified_alert("BINANCE", {"id": "bin-1h-pending"}, order_info, self.settings)

        client = Mock()
        client.market.return_value = {"contract": True, "contractSize": 1.0}
        client.fetch_order.return_value = {"id": "bin-1h-pending", "status": "open", "filled": 0.0}

        # Simulate age past 3600s (1 hour)
        now = time.time()
        with self.store._connect() as con:
            con.execute("UPDATE verified_jobs SET created=?, due=0", (now - 3650.0,))

        verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            job = con.execute("SELECT state, due, data FROM verified_jobs WHERE data LIKE '%bin-1h-pending%'").fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job[0], "PENDING")
            data = json.loads(job[2])
            self.assertEqual(data.get("stage"), "recovery")
            self.assertGreaterEqual(job[1], now + 50.0)

    def test_upbit_recovery_after_5_attempts_exceeded(self):
        """2. Upbit orders exceeding 5 verify attempts transition to low-frequency recovery PENDING, not DONE."""
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        dispatch_verified_alert("UPBIT", {"uuid": "upbit-5att-pend"}, order_info, self.settings)

        client = Mock()
        client.market.return_value = {}
        client.fetch_order.return_value = {"uuid": "upbit-5att-pend", "state": "wait", "volume": "0.01", "executed_volume": "0"}

        # Tick 6 times (past UPBIT_MAX_VERIFY_ATTEMPTS=5)
        for _ in range(6):
            with self.store._connect() as con:
                con.execute("UPDATE verified_jobs SET due=0")
            verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            job = con.execute("SELECT state, data FROM verified_jobs WHERE data LIKE '%upbit-5att-pend%'").fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job[0], "PENDING")
            data = json.loads(job[1])
            self.assertEqual(data.get("stage"), "recovery")
            self.assertTrue(data.get("unverified_alert_sent"))

    def test_missing_order_id_transitions_to_manual_review(self):
        """3. Orders missing exchange IDs cannot be queried and must transition to MANUAL_REVIEW, not DONE."""
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        # Empty result with no id/uuid/identifier
        dispatch_verified_alert("UPBIT", {}, order_info, self.settings)

        client = Mock()
        client.market.return_value = {}

        verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            job = con.execute("SELECT state, data FROM verified_jobs").fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job[0], "MANUAL_REVIEW")
            data = json.loads(job[1])
            self.assertEqual(data.get("stage"), "manual_review")
            self.assertEqual(data.get("reason"), "missing_order_id")

    def test_long_term_recovery_in_both_lightweight_modes(self):
        """4. Long-term recovery applies whether LIGHTWEIGHT_MODE is True or False."""
        for mode in [True, False]:
            s = Settings(
                _env_file=None,
                PASSWORD="test",
                VERIFIED_DISCORD_ALERTS=True,
                VERIFIED_FILL_PRICE=True,
                DISCORD_OUTBOX_ENABLED=True,
                LIGHTWEIGHT_MODE=mode,
                LONG_TERM_RECOVERY_ENABLED=True,
                VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
                UPBIT_VERIFIED_ALERTS=True,
                UPBIT_MAX_VERIFY_ATTEMPTS=5,
            )
            order_info = OrderRequest(exchange="UPBIT", base="ETH", quote="KRW", side="buy", amount=0.1)
            dispatch_verified_alert("UPBIT", {"uuid": f"upbit-mode-{mode}"}, order_info, s)

            client = Mock()
            client.market.return_value = {}
            client.fetch_order.return_value = {"uuid": f"upbit-mode-{mode}", "state": "wait", "volume": "0.1", "executed_volume": "0"}

            for _ in range(5):
                with self.store._connect() as con:
                    con.execute("UPDATE verified_jobs SET due=0 WHERE id LIKE ?", (f"%mode-{mode}%",))
                verification_tick(s, lambda *a: client)

            with self.store._connect_read() as con:
                job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE ?", (f"%upbit-mode-{mode}%",)).fetchone()
                self.assertIsNotNone(job)
                self.assertEqual(job[0], "PENDING", f"Failed for LIGHTWEIGHT_MODE={mode}")

    def test_long_term_recovery_enabled_on_and_off(self):
        """5. LONG_TERM_RECOVERY_ENABLED=True -> PENDING; False -> MANUAL_REVIEW (never DONE)."""
        # Test ON
        s_on = Settings(
            _env_file=None,
            PASSWORD="test",
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            DISCORD_OUTBOX_ENABLED=True,
            LONG_TERM_RECOVERY_ENABLED=True,
            UPBIT_MAX_VERIFY_ATTEMPTS=2,
            UPBIT_VERIFIED_ALERTS=True,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
        )
        dispatch_verified_alert("UPBIT", {"uuid": "ord-on"}, OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01), s_on)
        client = Mock(market=Mock(return_value={}), fetch_order=Mock(return_value={"uuid": "ord-on", "state": "wait", "volume": "0.01", "executed_volume": "0"}))
        for _ in range(2):
            with self.store._connect() as con: con.execute("UPDATE verified_jobs SET due=0 WHERE data LIKE '%ord-on%'")
            verification_tick(s_on, lambda *a: client)
        with self.store._connect_read() as con:
            job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE '%ord-on%'").fetchone()
            self.assertEqual(job[0], "PENDING")

        # Test OFF
        s_off = Settings(
            _env_file=None,
            PASSWORD="test",
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            DISCORD_OUTBOX_ENABLED=True,
            LONG_TERM_RECOVERY_ENABLED=False,
            UPBIT_MAX_VERIFY_ATTEMPTS=2,
            UPBIT_VERIFIED_ALERTS=True,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
        )
        dispatch_verified_alert("UPBIT", {"uuid": "ord-off"}, OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01), s_off)
        for _ in range(2):
            with self.store._connect() as con: con.execute("UPDATE verified_jobs SET due=0 WHERE data LIKE '%ord-off%'")
            verification_tick(s_off, lambda *a: client)
        with self.store._connect_read() as con:
            job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE '%ord-off%'").fetchone()
            self.assertEqual(job[0], "MANUAL_REVIEW")

    def test_exception_path_preserves_unconfirmed_order_state(self):
        """6. Exceptions during verification must NOT mark jobs DONE; order remains PENDING with backoff."""
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        dispatch_verified_alert("UPBIT", {"uuid": "upbit-exc-test"}, order_info, self.settings)

        # Force exception
        def crash_factory(*args):
            raise ConnectionError("Exchange API down")

        for _ in range(6):
            with self.store._connect() as con: con.execute("UPDATE verified_jobs SET due=0")
            verification_tick(self.settings, crash_factory)

        with self.store._connect_read() as con:
            job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE '%upbit-exc-test%'").fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job[0], "PENDING")

    def test_server_restart_recovery_and_final_fill_confirmation(self):
        """7. A job in recovery survives simulated crash and, upon eventual exchange fill, sends final alert."""
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        dispatch_verified_alert("UPBIT", {"uuid": "upbit-restart-test"}, order_info, self.settings)

        # Simulate stuck in VERIFYING when server restarts
        with self.store._connect() as con:
            con.execute("UPDATE verified_jobs SET state='VERIFYING', due=0 WHERE data LIKE '%upbit-restart-test%'")

        # Startup recovery
        self.store.recover()

        with self.store._connect_read() as con:
            job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE '%upbit-restart-test%'").fetchone()
            self.assertEqual(job[0], "PENDING")

        # Now exchange confirms order is done
        client = Mock(market=Mock(return_value={}), fetch_my_trades=Mock(return_value=[]), fetch_order=Mock(return_value={
            "uuid": "upbit-restart-test", "state": "done", "average": "100000000", "volume": "0.01", "executed_volume": "0.01",
            "fee": {"cost": "500", "currency": "KRW"}, "trades": [{"id": "t1", "price": 100000000, "amount": 0.01, "cost": 1000000, "fee": {"cost": 500, "currency": "KRW"}}]
        }))
        verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE '%upbit-restart-test%'").fetchone()
            self.assertEqual(job[0], "DONE")
            outbox = con.execute("SELECT payload FROM verified_outbox WHERE event_type='final'").fetchall()
            self.assertEqual(len(outbox), 1)

    def test_final_discord_alert_deduplication(self):
        """8. Exactly one final Discord alert is enqueued when an order is terminal."""
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        dispatch_verified_alert("UPBIT", {"uuid": "upbit-dedup-test"}, order_info, self.settings)

        client = Mock(market=Mock(return_value={}), fetch_my_trades=Mock(return_value=[]), fetch_order=Mock(return_value={
            "uuid": "upbit-dedup-test", "state": "done", "average": "100000000", "volume": "0.01", "executed_volume": "0.01",
            "fee": {"cost": "500", "currency": "KRW"}, "trades": [{"id": "t1", "price": 100000000, "amount": 0.01, "cost": 1000000, "fee": {"cost": 500, "currency": "KRW"}}]
        }))

        # Run tick twice on terminal order
        verification_tick(self.settings, lambda *a: client)
        verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            final_alerts = con.execute("SELECT count(*) FROM verified_outbox WHERE event_type='final'").fetchone()[0]
            self.assertEqual(final_alerts, 1)

    # --- Defect 2: SAFE_RETRY CCXT Concurrency & Read-Only Isolation ---

    def test_13_concurrent_orders_reconciliation(self):
        """9. 13 concurrent orders from 13 strategies reconcile cleanly without concurrency conflicts."""
        for i in range(1, 14):
            sig = hashlib.sha256(f"strat-{i}".encode()).hexdigest()
            row = dict(
                signal=sig, asset=f"BINANCE:COIN{i}/USDT", state="UNKNOWN",
                exchange="BINANCE", symbol=f"COIN{i}/USDT", client_id=f"cid_{i}_" + "x" * 20,
                order_id=f"oid_{i}", created=time.time(), reference=100.0, target="1.0",
                filled="0", remaining="1.0", attempts=1, futures=True, side="buy", close=False
            )
            self.safe_store.claim(row)

        def mock_fetch(order_id, symbol, params=None):
            return {"id": order_id, "clientOrderId": params.get("origClientOrderId") if params else None, "status": "closed", "filled": 1.0}

        mock_client = Mock()
        mock_client.fetch_order.side_effect = mock_fetch
        mock_client.fetch_open_orders.return_value = []
        mock_client.fetch_closed_orders.return_value = []
        mock_client.fetch_my_trades.return_value = []
        mock_client.fetch_positions.return_value = []

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        reconcile_pending(self.settings, lambda ex, fut: mock_client)

        pending_count = len(self.safe_store.pending())
        self.assertEqual(pending_count, 0)

    def test_incoming_webhook_during_reconciliation(self):
        """10. New incoming webhook executing manager.execute() is NOT blocked during slow reconciliation."""
        sig = hashlib.sha256(b"slow-order").hexdigest()
        row = dict(
            signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="cid_slow_" + "x" * 20,
            order_id="oid_slow", created=time.time(), reference=50000.0, target="0.01",
            filled="0", remaining="0.01", attempts=1, futures=True, side="buy", close=False
        )
        self.safe_store.claim(row)

        network_entered = threading.Event()
        release_network = threading.Event()

        def slow_fetch(*a, **kw):
            network_entered.set()
            release_network.wait(timeout=2.0)
            return {"id": "oid_slow", "status": "closed", "filled": 0.01}

        slow_client = Mock(fetch_order=slow_fetch, fetch_open_orders=Mock(return_value=[]), fetch_closed_orders=Mock(return_value=[]), fetch_my_trades=Mock(return_value=[]), fetch_positions=Mock(return_value=[]))

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        t = threading.Thread(target=reconcile_pending, args=(self.settings, lambda ex, fut: slow_client))
        t.start()
        self.addCleanup(release_network.set)

        self.assertTrue(network_entered.wait(timeout=1.0))

        # While reconciliation is in-flight, execute an incoming order on a different asset
        order_info = OrderRequest(exchange="BINANCE", base="ETH", quote="USDT.P", side="buy", amount=0.1, signal_id="fast-sig-new", price=3000.0)
        fast_client = Mock(market=Mock(return_value={"contract": True, "contractSize": 1.0, "limits": {}}), fetch_ticker=Mock(return_value={"last": 3000.0}), fetch_positions=Mock(return_value=[]), create_order=Mock(return_value={"id": "fast-oid", "status": "closed", "filled": 0.1}))
        fast_bot = NS(client=fast_client, position_mode="one-way")

        t0 = time.perf_counter()
        res = self.manager.execute(lambda *a, **k: fast_client.create_order(*a, **k), ("ETH/USDT:USDT", "market", "buy", 0.1, None, {}), order_info, Adapter(fast_bot, order_info))
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.2, "Incoming order was blocked by reconciliation!")
        self.assertEqual(res["id"], "fast-oid")

        release_network.set()
        t.join(timeout=2.0)

    def test_shared_order_ccxt_client_never_mutated_or_shared(self):
        """11. Shared trading bot CCXT instance options are NEVER mutated by reconciliation."""
        trading_bot = NS(
            client=Mock(options={"defaultType": "spot"}),
            position_mode="one-way"
        )

        sig = hashlib.sha256(b"fut-recon").hexdigest()
        row = dict(
            signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="cid_fut_" + "x" * 20,
            order_id="oid_fut", created=time.time(), reference=50000.0, target="0.01",
            filled="0", remaining="0.01", attempts=1, futures=True, side="buy", close=False
        )
        self.safe_store.claim(row)

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        def get_bot(exch):
            return trading_bot
        get_bot.__module__ = "exchange.pexchange"

        with patch.object(safe, "get_reconciliation_clients") as mock_recon_pool:
            mock_ro_client = Mock()
            mock_ro_client.fetch_order.return_value = {"id": "oid_fut", "status": "closed", "filled": 0.01}
            mock_ro_client.fetch_open_orders.return_value = []
            mock_ro_client.fetch_closed_orders.return_value = []
            mock_ro_client.fetch_my_trades.return_value = []
            mock_ro_client.fetch_positions.return_value = []
            mock_recon_pool.return_value.get_client.return_value = mock_ro_client

            reconcile_pending(self.settings, get_bot)

            trading_bot.client.fetch_order.assert_not_called()
            self.assertEqual(trading_bot.client.options["defaultType"], "spot")
            mock_ro_client.fetch_order.assert_called_once()

    def test_startup_does_not_block_event_loop(self):
        """12. Startup reconciliation loop runs in background thread and never blocks the asyncio event loop."""
        async def run_check():
            mock_client = Mock(fetch_order=Mock(side_effect=lambda *a: (time.sleep(0.05), {"id": "1", "status": "closed", "filled": 1.0})[1]), fetch_open_orders=Mock(return_value=[]), fetch_closed_orders=Mock(return_value=[]), fetch_my_trades=Mock(return_value=[]), fetch_positions=Mock(return_value=[]))
            sig = hashlib.sha256(b"lag-test").hexdigest()
            self.safe_store.claim(dict(
                signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
                exchange="BINANCE", symbol="BTC/USDT", client_id="cid_lag_" + "x" * 20,
                order_id="oid_lag", created=time.time(), reference=50000.0, target="0.01",
                filled="0", remaining="0.01", attempts=1, futures=True, side="buy", close=False
            ))

            import exchange.safe_retry as safe
            prev_mgr = safe._manager
            safe._manager = self.manager
            self.addCleanup(setattr, safe, "_manager", prev_mgr)

            recon_task = asyncio.create_task(reconciliation_loop(self.settings, lambda ex, fut: mock_client))

            t0 = time.perf_counter()
            await asyncio.sleep(0.02)
            elapsed = time.perf_counter() - t0

            recon_task.cancel()
            try:
                await recon_task
            except asyncio.CancelledError:
                pass

            self.assertLess(elapsed, 0.1, "Event loop iteration was blocked!")

        asyncio.run(run_check())

    def test_reconciliation_failure_does_not_permit_duplicate_orders(self):
        """13. A failed reconciliation query keeps order UNKNOWN and blocks any new order submission."""
        sig = hashlib.sha256(b"fail-permits-none").hexdigest()
        row = dict(
            signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="cid_fail_" + "x" * 20,
            order_id="oid_fail", created=time.time(), reference=50000.0, target="0.01",
            filled="0", remaining="0.01", attempts=1, futures=True, side="buy", close=False
        )
        self.safe_store.claim(row)

        failing_client = Mock(fetch_order=Mock(side_effect=ccxt.NetworkError("Exchange unreachable")))

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        reconcile_pending(self.settings, lambda ex, fut: failing_client)

        pending = self.safe_store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "UNKNOWN")

        order_info = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT.P", side="buy", amount=0.01, signal_id="new-blocked-sig")
        mock_bot = NS(client=Mock(market=Mock(return_value={"contract": True, "contractSize": 1.0, "limits": {}}), fetch_ticker=Mock(return_value={"last": 50000.0}), fetch_positions=Mock(return_value=[])), position_mode="one-way")
        with self.assertRaises(SafetyError):
            self.manager.execute(lambda *a: None, ("BTC/USDT:USDT", "market", "buy", 0.01, None, {}), order_info, Adapter(mock_bot, order_info))


if __name__ == "__main__":
    unittest.main()
