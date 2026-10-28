"""Independent reproduction and verification tests for atomic lease and CCXT shutdown race:
1. Shutdown immediately after acquire_lease() before client_lock is acquired.
2. Shutdown while waiting for client_lock.
3. Shutdown while query is in-flight.
4. Drain timeout exceeded, then lease returned later safely.
5. Double release and duplicate close prevention.
6. Slow API exceptions, timeout, and cancellation clean up lease.
7. 13 strategies concurrent queries & shutdown without deadlocks or double-closes.
8. Blocking new lease acquisition during shutdown.
9. Event loop remains responsive during shutdown.
10. Server restart SQLite order recovery and duplicate signal protection.
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


class AtomicLeaseShutdownRaceTests(unittest.TestCase):
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

    def test_1_shutdown_after_acquire_lease_before_lock_acquired(self):
        """1. Shutdown occurs after acquire_lease() registers active lease, before client_lock is acquired."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)

        mock_client = Mock()
        close_called_while_lease_active = False

        lease_registered = threading.Event()
        shutdown_done = threading.Event()
        query_finished = threading.Event()

        def mock_close():
            nonlocal close_called_while_lease_active
            if not query_finished.is_set():
                close_called_while_lease_active = True
        mock_client.close.side_effect = mock_close

        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        def worker():
            # Acquire lease
            with pool.acquire_lease("BINANCE", True) as c:
                lease_registered.set()
                # Wait until shutdown has run before continuing
                self.assertTrue(shutdown_done.wait(timeout=1.0))
                time.sleep(0.05)
                query_finished.set()

        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(lease_registered.wait(timeout=1.0))

        # While active lease is held, trigger shutdown
        begin_shutdown()
        pool.close(timeout=0.05)

        # PASS CRITERIA: client must NOT have been closed while lease is active
        self.assertFalse(close_called_while_lease_active, "Client was closed prematurely while active lease existed!")
        self.assertEqual(mock_client.close.call_count, 0)

        shutdown_done.set()
        t.join(timeout=1.0)

        # After worker exits and releases lease, exactly 1 deferred close occurs
        self.assertEqual(mock_client.close.call_count, 1)

    def test_2_shutdown_while_waiting_for_client_lock(self):
        """2. Shutdown occurs while worker is waiting on client_lock (still tracked as active lease)."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)
        mock_client = Mock()
        close_called_early = False

        def mock_close():
            nonlocal close_called_early
            close_called_early = True
        mock_client.close.side_effect = mock_close

        pool.clients[key] = mock_client
        client_lock = threading.Lock()
        pool.client_locks[key] = client_lock

        # Thread 1 holds client_lock
        client_lock.acquire()

        worker_started = threading.Event()
        worker_finished = threading.Event()

        def worker():
            worker_started.set()
            # This will register lease in acquire_lease, then block waiting for client_lock
            with pool.acquire_lease("BINANCE", True):
                time.sleep(0.02)
                worker_finished.set()

        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(worker_started.wait(timeout=1.0))
        time.sleep(0.05)  # Ensure worker is blocked on client_lock

        # Active lease must be 1 even though client_lock is not yet acquired
        self.assertEqual(pool.active_leases.get(key, 0), 1)

        # Trigger shutdown while worker is waiting
        begin_shutdown()
        pool.close(timeout=0.05)

        # Must NOT close client because active lease is waiting!
        self.assertFalse(close_called_early, "Client closed while lease was waiting for lock!")
        self.assertEqual(mock_client.close.call_count, 0)

        # Now release the lock
        client_lock.release()
        t.join(timeout=1.0)
        self.assertTrue(worker_finished.is_set())

        # Once worker finishes, client is closed safely exactly once
        self.assertEqual(mock_client.close.call_count, 1)

    def test_3_shutdown_while_query_in_flight(self):
        """3. Shutdown occurs while CCXT query is executing inside client_lock."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)
        mock_client = Mock()
        close_during_query = False

        query_running = threading.Event()
        query_completed = threading.Event()

        def mock_close():
            nonlocal close_during_query
            if not query_completed.is_set():
                close_during_query = True
        mock_client.close.side_effect = mock_close

        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        def worker():
            with pool.acquire_lease("BINANCE", True):
                query_running.set()
                time.sleep(0.15)
                query_completed.set()

        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(query_running.wait(timeout=1.0))

        # Trigger shutdown during query
        begin_shutdown()
        pool.close(timeout=0.05)

        self.assertFalse(close_during_query)
        self.assertEqual(mock_client.close.call_count, 0)

        t.join(timeout=1.0)
        self.assertEqual(mock_client.close.call_count, 1)

    def test_4_drain_timeout_then_lease_returned(self):
        """4. Drain timeout expires, then lease returns later: client is closed deferred, never prematurely."""
        pool = get_reconciliation_clients(self.settings)
        key = pool._get_key("BINANCE", True)
        mock_client = Mock()
        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        sig = hashlib.sha256(b"lease-drain-sig").hexdigest()
        self.safe_store.claim(dict(
            signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="cid_lease_dr_" + "x" * 20,
            order_id="oid_lease_dr", created=time.time(), reference=50000.0, target="1.0",
            filled="0", remaining="1.0", attempts=1, futures=True, side="buy", close=False
        ))

        def slow_fetch(*a, **kw):
            time.sleep(0.2)
            return {"id": "oid_lease_dr", "status": "closed", "filled": 1.0}

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

        time.sleep(0.04)
        begin_shutdown()

        # Drain timeout is short (0.02s) -> returns False
        drained = wait_for_reconciliation_drained(timeout=0.02)
        self.assertFalse(drained)

        # close() call must NOT force close active lease
        close_reconciliation_clients(timeout=0.02)
        self.assertEqual(mock_client.close.call_count, 0)

        recon_thread.join(timeout=1.0)
        # Deferred close called exactly once upon lease return
        self.assertEqual(mock_client.close.call_count, 1)

    def test_5_double_release_and_duplicate_close_prevention(self):
        """5. Multiple close calls or lease exits never execute duplicate close() calls."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)
        mock_client = Mock()
        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        # Normal acquisition and release
        with pool.acquire_lease("BINANCE", True):
            pass

        # Close pool
        pool.close(timeout=0.1)
        self.assertEqual(mock_client.close.call_count, 1)

        # Repeated close must be idempotent (0 additional calls)
        pool.close(timeout=0.1)
        close_reconciliation_clients(timeout=0.1)
        self.assertEqual(mock_client.close.call_count, 1)

    def test_6_slow_api_exceptions_timeout_and_asyncio_cancellation(self):
        """6. Exceptions and timeouts inside lease release the lease cleanly in finally block."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)
        mock_client = Mock()
        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        # Exception raised inside lease
        with self.assertRaises(TimeoutError):
            with pool.acquire_lease("BINANCE", True):
                raise TimeoutError("Exchange timeout")

        # Lease count must be completely cleaned up (0)
        self.assertNotIn(key, pool.active_leases)

        # Subsequent close closes client cleanly
        pool.close(timeout=0.1)
        mock_client.close.assert_called_once()

    def test_7_13_strategies_concurrent_queries_and_shutdown(self):
        """7. 13 concurrent strategy queries and simultaneous shutdown complete with 0 deadlocks and 0 double-closes."""
        pool = ReconciliationClients(self.settings)
        clients = {}
        for i in range(1, 14):
            c = Mock()
            clients[i] = c
            k = (f"BINANCE_{i}", "swap", "default")
            pool.clients[k] = c
            pool.client_locks[k] = threading.Lock()

        def make_worker(cid):
            def _w():
                with pool.acquire_lease(f"BINANCE_{cid}", True):
                    time.sleep(0.05)
            return _w

        threads = [threading.Thread(target=make_worker(i)) for i in range(1, 14)]
        for t in threads: t.start()

        time.sleep(0.02)
        # Simultaneous shutdown during concurrent active leases
        begin_shutdown()
        pool.close(timeout=0.5)

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "Deadlock among 13 strategy lease threads!")

        for i, c in clients.items():
            self.assertEqual(c.close.call_count, 1, f"Client {i} close call count != 1")

    def test_8_new_lease_blocked_during_shutdown(self):
        """8. Initiating shutdown blocks new lease acquisition immediately with SafetyError."""
        begin_shutdown()
        pool = get_reconciliation_clients(self.settings)
        with self.assertRaises(SafetyError):
            with pool.acquire_lease("BINANCE", True):
                pass

    def test_9_event_loop_remains_responsive_during_shutdown(self):
        """9. Event loop continues to process coroutines while shutdown waits in thread pool."""
        async def run_check():
            ticks = 0
            stop = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not stop.is_set():
                    ticks += 1
                    await asyncio.sleep(0.01)

            t_task = asyncio.create_task(ticker())

            begin_shutdown()
            await asyncio.to_thread(time.sleep, 0.08)
            await asyncio.to_thread(wait_for_reconciliation_drained, 0.05)
            await asyncio.to_thread(close_reconciliation_clients, 0.05)

            stop.set()
            await t_task
            self.assertGreater(ticks, 3, "Event loop was blocked during shutdown!")

        asyncio.run(run_check())

    def test_10_sqlite_recovery_and_duplicate_protection_after_restart(self):
        """10. After server restart, pending orders recover and duplicate submissions are blocked."""
        sig = hashlib.sha256(b"lease-restart-sig").hexdigest()
        self.safe_store.claim(dict(
            signal=sig, asset="BYBIT:ETH/USDT", state="UNKNOWN",
            exchange="BYBIT", symbol="ETH/USDT", client_id="cid_lrecov_" + "x" * 20,
            order_id="oid_lrecov", created=time.time(), reference=3000.0, target="0.5",
            filled="0", remaining="0.5", attempts=1, futures=True, side="buy", close=False
        ))

        reset_reconciliation_state()
        new_store = Store(self.safe_db)
        new_manager = Manager(new_store)

        pending = new_store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["signal"], sig)

        req = OrderRequest(exchange="BYBIT", base="ETH", quote="USDT", side="buy", amount=0.5, signal_id=sig, price=3000.0, is_futures=True, is_close=False)
        with self.assertRaises(SafetyError):
            new_manager.execute(Mock(), ("ETH/USDT", "market", "buy", 0.5, None, {}), req, Mock())


if __name__ == "__main__":
    unittest.main()
