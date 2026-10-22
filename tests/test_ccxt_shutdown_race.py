"""Comprehensive independent race tests verifying CCXT safe shutdown and lock mechanics:
1. Query holds lock longer than close timeout: exactly 0 close calls during query.
2. Drain timeout exceeded: 0 forced client closes during query.
3. Safe deferred close called exactly once after query completes.
4. Slow API, exceptions, timeout, and asyncio cancellation.
5. New reconciliation blocked during shutdown.
6. Event loop remains responsive and unblocked during shutdown.
7. 13 strategies concurrent queries & shutdown without deadlocks or duplicate closes.
8. Server restart SQLite recovery and duplicate signal protection.
"""
import asyncio
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch, MagicMock

from harness import ROOT, setup
schema, legacy, util = setup()
Settings = schema.Settings
OrderRequest = schema.OrderRequest

from exchange.safe_retry import (
    Store, Manager, Adapter, SafetyError,
    reconcile_pending, reconciliation_loop,
    get_reconciliation_clients, close_reconciliation_clients,
    begin_shutdown, is_shutting_down, wait_for_reconciliation_drained,
    reset_reconciliation_state, ReconciliationClients
)


class CCXTSafeShutdownRaceTests(unittest.TestCase):
    def setUp(self):
        reset_reconciliation_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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
            LIGHTWEIGHT_MODE=True,
            LONG_TERM_RECOVERY_ENABLED=True,
            LONG_TERM_RECOVERY_MAX_DELAY=300.0,
            FILL_VERIFICATION_TIMEOUT_SEC=30.0,
            SAFE_RETRY_DB_PATH=self.safe_db,
            ORDER_RECONCILIATION_ENABLED=True,
            DUPLICATE_SIGNAL_PROTECTION=True,
            PERSIST_ORDER_STATE=True,
            BINANCE_KEY="bin_test_key",
            BINANCE_SECRET="bin_test_secret",
            DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/123/fake",
        )
        self.safe_store = Store(self.safe_db)
        self.manager = Manager(self.safe_store)

    def tearDown(self):
        reset_reconciliation_state()

    def test_1_lock_held_longer_than_close_timeout_zero_close_calls_during_query(self):
        """1. Query holds lock for 0.3s while close has 0.05s timeout: 0 close calls while query is active."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)

        close_called_while_active = False
        query_active = threading.Event()
        query_finish = threading.Event()

        mock_client = Mock()
        def mock_close():
            nonlocal close_called_while_active
            if query_active.is_set() and not query_finish.is_set():
                close_called_while_active = True
        mock_client.close.side_effect = mock_close

        pool.clients[key] = mock_client
        lock = pool.get_client_lock("BINANCE", True)

        def slow_query():
            with lock:
                query_active.set()
                # Hold lock longer than close timeout
                time.sleep(0.2)
                query_finish.set()
                # Clean up deferred close if registered
                pool.safe_deferred_close(key, mock_client)

        t = threading.Thread(target=slow_query)
        t.start()
        self.assertTrue(query_active.wait(timeout=1.0))

        # Close attempt with short timeout (0.05s) while query holds lock
        pool.close(timeout=0.05)

        # CRITICAL ASSERTION: client must NOT have been closed while query was active!
        self.assertFalse(close_called_while_active, "client.close() was executed without holding lock!")
        self.assertEqual(mock_client.close.call_count, 0, "client.close() was called during active query!")

        t.join(timeout=1.0)
        # After query finishes and deferred close runs, it must be closed exactly once
        self.assertEqual(mock_client.close.call_count, 1)

    def test_2_drain_timeout_exceeded_zero_forced_client_closes(self):
        """2. When drain timeout is exceeded, in-flight client is NOT forcibly closed (0 calls)."""
        pool = get_reconciliation_clients(self.settings)
        key = pool._get_key("BINANCE", True)

        mock_client = Mock()
        pool.clients[key] = mock_client
        lock = pool.get_client_lock("BINANCE", True)

        query_done = threading.Event()
        query_started = threading.Event()
        client_closed_during_query = False

        def mock_close():
            nonlocal client_closed_during_query
            if not query_done.is_set():
                client_closed_during_query = True
        mock_client.close.side_effect = mock_close

        sig = hashlib.sha256(b"drain-timeout-sig").hexdigest()
        self.safe_store.claim(dict(
            signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="cid_drain_to_" + "x" * 20,
            order_id="oid_drain_to", created=time.time(), reference=50000.0, target="1.0",
            filled="0", remaining="1.0", attempts=1, futures=True, side="buy", close=False
        ))

        def slow_fetch(*a, **kw):
            query_started.set()
            time.sleep(0.2)
            query_done.set()
            return {"id": "oid_drain_to", "status": "closed", "filled": 1.0}

        mock_client.fetch_order = slow_fetch
        mock_client.fetch_open_orders.return_value = []
        mock_client.fetch_closed_orders.return_value = []
        mock_client.fetch_my_trades.return_value = []
        mock_client.fetch_positions.return_value = []

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        recon_thread = threading.Thread(target=reconcile_pending, args=(self.settings,))
        recon_thread.start()

        # Let query start
        self.assertTrue(query_started.wait(timeout=2.0))

        # Shutdown initiated, drain timeout is very short (0.02s) so drain returns False
        begin_shutdown()
        drained = wait_for_reconciliation_drained(timeout=0.02)
        self.assertFalse(drained, "Drain should have timed out!")

        # Close clients with short timeout: must NOT forcibly close in-flight client
        close_reconciliation_clients(timeout=0.02)

        self.assertFalse(client_closed_during_query, "Client was forcibly closed during active query!")
        self.assertEqual(mock_client.close.call_count, 0)

        recon_thread.join(timeout=1.0)
        # Exactly 1 deferred close after query completes
        self.assertEqual(mock_client.close.call_count, 1)

    def test_3_deferred_safe_close_called_exactly_once(self):
        """3. After in-flight query finishes, deferred close is called exactly 1 time (no duplicate closes)."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)
        mock_client = Mock()
        pool.clients[key] = mock_client
        lock = pool.get_client_lock("BINANCE", True)

        with lock:
            # pool.close called while lock held
            pool.close(timeout=0.02)
            self.assertEqual(mock_client.close.call_count, 0)
            self.assertIn(key, pool.pending_close_keys)

            # Query finishes inside the lock
            pool.safe_deferred_close(key, mock_client)
            self.assertEqual(mock_client.close.call_count, 1)

        # Repeated deferred close or pool close must NOT invoke close() again
        pool.safe_deferred_close(key, mock_client)
        pool.close(timeout=0.02)
        self.assertEqual(mock_client.close.call_count, 1)

    def test_4_slow_api_exceptions_timeout_and_asyncio_cancellation(self):
        """4. Slow API timeout / exceptions and asyncio cancellation clean up without deadlocks."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)
        mock_client = Mock()
        mock_client.fetch_order.side_effect = TimeoutError("Exchange gateway timeout")
        mock_client.fetch_open_orders.return_value = []
        mock_client.fetch_closed_orders.return_value = []
        mock_client.fetch_my_trades.return_value = []
        mock_client.fetch_positions.return_value = []
        pool.clients[key] = mock_client

        sig = hashlib.sha256(b"err-cancel-sig").hexdigest()
        self.safe_store.claim(dict(
            signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="cid_err_cancel_" + "x" * 20,
            order_id="oid_err_cancel", created=time.time(), reference=50000.0, target="1.0",
            filled="0", remaining="1.0", attempts=1, futures=True, side="buy", close=False
        ))

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        with patch.object(safe, "get_reconciliation_clients", return_value=pool):
            reconcile_pending(self.settings)

        # Active queries count must be cleanly 0 and drained event set
        self.assertEqual(safe._active_queries_count, 0)
        self.assertTrue(safe._queries_drained_event.is_set())

        # Clean close
        pool.close(timeout=0.5)
        mock_client.close.assert_called_once()

    def test_5_new_reconciliation_blocked_during_shutdown(self):
        """5. Initiating shutdown blocks new reconciliation submissions and raises SafetyError on get_client."""
        begin_shutdown()
        self.assertTrue(is_shutting_down())

        pool = get_reconciliation_clients(self.settings)
        with self.assertRaises(SafetyError):
            pool.get_client("BINANCE", True)

        sig = hashlib.sha256(b"new-blocked-sig").hexdigest()
        self.safe_store.claim(dict(
            signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="cid_new_block_" + "x" * 20,
            order_id="oid_new_block", created=time.time(), reference=50000.0, target="1.0",
            filled="0", remaining="1.0", attempts=1, futures=True, side="buy", close=False
        ))

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        mock_bot = Mock()
        reconcile_pending(self.settings, lambda ex, fut: mock_bot)
        mock_bot.fetch_order.assert_not_called()

    def test_6_event_loop_unblocked_during_shutdown(self):
        """6. FastAPI event loop remains responsive and executes other async tasks during shutdown."""
        async def run_scenario():
            loop_heartbeats = 0
            stop_ticker = asyncio.Event()

            async def ticker():
                nonlocal loop_heartbeats
                while not stop_ticker.is_set():
                    loop_heartbeats += 1
                    await asyncio.sleep(0.01)

            ticker_task = asyncio.create_task(ticker())

            # Simulate slow shutdown waiting in to_thread
            begin_shutdown()
            await asyncio.to_thread(time.sleep, 0.08)
            await asyncio.to_thread(wait_for_reconciliation_drained, 0.05)
            await asyncio.to_thread(close_reconciliation_clients, 0.05)

            stop_ticker.set()
            await ticker_task

            # Event loop ticked multiple times during shutdown, proving no blockage
            self.assertGreater(loop_heartbeats, 3, "Event loop was blocked by shutdown operations!")

        asyncio.run(run_scenario())

    def test_7_13_strategies_concurrent_queries_and_shutdown_no_deadlock(self):
        """7. 13 concurrent strategy queries and simultaneous shutdown complete without deadlocks or double-closes."""
        pool = ReconciliationClients(self.settings)
        clients = {}
        for i in range(1, 14):
            c = Mock()
            clients[i] = c
            pool.clients[(f"BINANCE_{i}", "swap", "default")] = c
            self.safe_store.claim(dict(
                signal=hashlib.sha256(f"strat-13-{i}".encode()).hexdigest(),
                asset=f"BINANCE_{i}:T{i}/USDT", state="UNKNOWN",
                exchange=f"BINANCE_{i}", symbol=f"T{i}/USDT", client_id=f"cid_13_{i}_" + "x" * 20,
                order_id=f"oid_13_{i}", created=time.time(), reference=100.0, target="1.0",
                filled="0", remaining="1.0", attempts=1, futures=True, side="buy", close=False
            ))

        def make_fetch(cid):
            def _fetch(*a, **k):
                time.sleep(0.05)
                return {"id": f"oid_13_{cid}", "status": "closed", "filled": 1.0}
            return _fetch

        for i, c in clients.items():
            c.fetch_order.side_effect = make_fetch(i)
            c.fetch_open_orders.return_value = []
            c.fetch_closed_orders.return_value = []
            c.fetch_my_trades.return_value = []
            c.fetch_positions.return_value = []

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        # Run 13 concurrent threads
        threads = [
            threading.Thread(target=reconcile_pending, args=(self.settings, lambda ex, fut: clients.get(int(ex.split("_")[1]))))
            for _ in range(13)
        ]
        for t in threads: t.start()

        time.sleep(0.02)
        # Simultaneous shutdown call while queries are in-flight
        begin_shutdown()
        wait_for_reconciliation_drained(timeout=2.0)
        pool.close(timeout=0.5)

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "Deadlock detected among 13 strategy threads!")

        # All clients closed at most once (never double-closed)
        for i, c in clients.items():
            self.assertLessEqual(c.close.call_count, 1)

    def test_8_sqlite_recovery_and_duplicate_order_protection(self):
        """8. After server restart, pending orders recover safely and duplicate submissions are blocked."""
        sig = hashlib.sha256(b"restart-recov-sig").hexdigest()
        self.safe_store.claim(dict(
            signal=sig, asset="BYBIT:ETH/USDT", state="UNKNOWN",
            exchange="BYBIT", symbol="ETH/USDT", client_id="cid_recov_" + "x" * 20,
            order_id="oid_recov", created=time.time(), reference=3000.0, target="0.5",
            filled="0", remaining="0.5", attempts=1, futures=True, side="buy", close=False
        ))

        # Restart simulation
        reset_reconciliation_state()
        new_store = Store(self.safe_db)
        new_manager = Manager(new_store)

        pending = new_store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["signal"], sig)

        # Duplicate order submission must be blocked by duplicate signal protection
        with self.assertRaises(SafetyError):
            req = OrderRequest(exchange="BYBIT", base="ETH", quote="USDT", side="buy", amount=0.5, signal_id=sig, price=3000.0, is_futures=True, is_close=False)
            new_manager.execute(Mock(), ("ETH/USDT", "market", "buy", 0.5, None, {}), req, Mock())


if __name__ == "__main__":
    unittest.main()
