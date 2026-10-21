"""Independent deterministic reproduction and verification tests for double close prevention:
1. Two threads calling close() concurrently -> exactly 1 CCXT close() call.
2. 13 threads calling close() concurrently -> exactly 1 CCXT close() call.
3. Concurrent close() while lease is active -> 0 premature closes, exactly 1 after return.
4. Final lease return and close() occur simultaneously -> exactly 1 close() call.
5. Close during client_lock contention -> 0 premature closes while waiting or active.
6. Repeated/consecutive close() calls -> 0 additional close() calls.
7. Exception during close() -> exactly 1 attempt, no deadlocks, state marked CLOSED.
8. Blocking new lease acquisition during shutdown -> SafetyError raised.
9. Event loop remains fully responsive during shutdown.
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
from unittest.mock import Mock, patch

from harness import ROOT, setup
schema, legacy, util = setup()
Settings = schema.Settings
OrderRequest = schema.OrderRequest

from exchange.safe_retry import (
    Store, Manager, Adapter, SafetyError,
    reconcile_pending, reconciliation_loop,
    get_reconciliation_clients, close_reconciliation_clients,
    begin_shutdown, is_shutting_down, wait_for_reconciliation_drained,
    reset_reconciliation_state, ReconciliationClients,
    STATE_OPEN, STATE_CLOSING, STATE_CLOSED
)


class DoubleClosePreventionTests(unittest.TestCase):
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

    def test_1_two_threads_concurrent_close_exactly_one_close(self):
        """1. Two threads calling close() concurrently -> exactly 1 CCXT close() call."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)

        mock_client = Mock()
        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        barrier = threading.Barrier(2)
        errors = []

        def close_worker():
            try:
                barrier.wait(timeout=2.0)
                pool.close(timeout=0.5)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=close_worker)
        t2 = threading.Thread(target=close_worker)
        t1.start()
        t2.start()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        self.assertEqual(errors, [])
        self.assertEqual(mock_client.close.call_count, 1, "Duplicate close() detected across 2 concurrent threads!")
        self.assertEqual(pool.client_states.get(key), STATE_CLOSED)

    def test_2_thirteen_threads_concurrent_close_exactly_one_close(self):
        """2. 13 threads calling close() concurrently -> exactly 1 CCXT close() call."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)

        mock_client = Mock()
        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        num_threads = 13
        barrier = threading.Barrier(num_threads)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=3.0)
                pool.close(timeout=0.5)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        self.assertEqual(errors, [])
        self.assertEqual(mock_client.close.call_count, 1, "Duplicate close() detected across 13 concurrent threads!")
        self.assertEqual(pool.client_states.get(key), STATE_CLOSED)

    def test_3_concurrent_close_while_lease_active_zero_premature_closes(self):
        """3. Concurrent close() while lease is active -> 0 premature closes, exactly 1 after return."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)

        mock_client = Mock()
        premature_close = False
        lease_active = threading.Event()
        lease_release_allowed = threading.Event()

        def guarded_close():
            nonlocal premature_close
            if lease_active.is_set() and not lease_release_allowed.is_set():
                premature_close = True
        mock_client.close.side_effect = guarded_close

        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        barrier = threading.Barrier(3)
        errors = []

        def lease_holder():
            try:
                with pool.acquire_lease("BINANCE", True):
                    lease_active.set()
                    barrier.wait(timeout=2.0)
                    lease_release_allowed.wait(timeout=2.0)
            except Exception as e:
                errors.append(e)

        def closer():
            try:
                barrier.wait(timeout=2.0)
                pool.close(timeout=0.1)
            except Exception as e:
                errors.append(e)

        t_lease = threading.Thread(target=lease_holder)
        t_c1 = threading.Thread(target=closer)
        t_c2 = threading.Thread(target=closer)

        t_lease.start()
        t_c1.start()
        t_c2.start()

        t_c1.join(timeout=2.0)
        t_c2.join(timeout=2.0)

        self.assertFalse(premature_close, "Client was prematurely closed while lease was active!")
        self.assertEqual(mock_client.close.call_count, 0, "Client closed before lease was returned!")

        # Now permit lease release
        lease_release_allowed.set()
        t_lease.join(timeout=2.0)

        self.assertEqual(errors, [])
        self.assertEqual(mock_client.close.call_count, 1, "Expected exactly 1 deferred close after lease release!")

    def test_4_final_lease_return_and_close_simultaneous_exactly_one_close(self):
        """4. Final lease return and close() occur simultaneously -> exactly 1 close() call."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)

        mock_client = Mock()
        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        barrier = threading.Barrier(2)
        errors = []

        def lease_worker():
            try:
                with pool.acquire_lease("BINANCE", True):
                    # Lease acquired; sync with close_worker before exiting
                    barrier.wait(timeout=2.0)
                # Exiting context manager triggers deferred close in finally
            except Exception as e:
                errors.append(e)

        def close_worker():
            try:
                barrier.wait(timeout=2.0)
                pool.close(timeout=0.5)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=lease_worker)
        t2 = threading.Thread(target=close_worker)
        t1.start()
        t2.start()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        self.assertEqual(errors, [])
        self.assertEqual(mock_client.close.call_count, 1, "Race between lease return and close() caused multiple closes!")

    def test_5_close_during_client_lock_contention_zero_premature_closes(self):
        """5. Close during client_lock contention -> 0 premature closes while waiting or active."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)

        mock_client = Mock()
        client_closed_early = False
        holder_releasing = threading.Event()
        holder_holding = threading.Event()

        def guarded_close():
            nonlocal client_closed_early
            if not holder_releasing.is_set():
                client_closed_early = True
        mock_client.close.side_effect = guarded_close

        pool.clients[key] = mock_client
        lock = pool.get_client_lock("BINANCE", True)

        def direct_holder():
            with lock:
                holder_holding.set()
                time.sleep(0.15)
                holder_releasing.set()

        t_holder = threading.Thread(target=direct_holder)
        t_holder.start()
        self.assertTrue(holder_holding.wait(timeout=1.0))

        # While lock is held, 2 threads attempt close with short timeout
        barrier = threading.Barrier(2)
        def close_racer():
            barrier.wait(timeout=1.0)
            pool.close(timeout=0.03)

        c1 = threading.Thread(target=close_racer)
        c2 = threading.Thread(target=close_racer)
        c1.start()
        c2.start()
        c1.join(timeout=1.0)
        c2.join(timeout=1.0)

        self.assertFalse(client_closed_early, "Client closed while query held lock!")
        self.assertEqual(mock_client.close.call_count, 0)

        t_holder.join(timeout=1.0)
        # Deferred close after holder released lock
        pool.safe_deferred_close(key, mock_client)
        self.assertEqual(mock_client.close.call_count, 1)

    def test_6_repeated_consecutive_close_calls_zero_additional_closes(self):
        """6. Repeated/consecutive close() calls -> 0 additional close() calls."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)
        mock_client = Mock()
        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        # First close
        pool.close()
        self.assertEqual(mock_client.close.call_count, 1)

        # 5 more consecutive close calls
        for _ in range(5):
            pool.close()
            pool.safe_deferred_close(key, mock_client)

        self.assertEqual(mock_client.close.call_count, 1, "Repeated close() invocations must not trigger multiple closes!")

    def test_7_exception_during_close_no_deadlocks_state_closed(self):
        """7. Exception during close() -> exactly 1 attempt, no deadlocks, state marked CLOSED."""
        pool = ReconciliationClients(self.settings)
        key = pool._get_key("BINANCE", True)
        mock_client = Mock()
        mock_client.close.side_effect = RuntimeError("Broken network socket")
        pool.clients[key] = mock_client
        pool.client_locks[key] = threading.Lock()

        barrier = threading.Barrier(2)
        def close_worker():
            barrier.wait(timeout=1.0)
            pool.close()

        t1 = threading.Thread(target=close_worker)
        t2 = threading.Thread(target=close_worker)
        t1.start()
        t2.start()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        # Exactly 1 attempt made
        self.assertEqual(mock_client.close.call_count, 1)
        self.assertEqual(pool.client_states.get(key), STATE_CLOSED)

        # Subsequent close does not retry or re-raise
        pool.close()
        self.assertEqual(mock_client.close.call_count, 1)

    def test_8_blocking_new_lease_acquisition_during_shutdown(self):
        """8. Blocking new lease acquisition during shutdown -> SafetyError raised."""
        pool = ReconciliationClients(self.settings)
        pool.close()

        with self.assertRaises(SafetyError):
            with pool.acquire_lease("BINANCE", True):
                pass

        with self.assertRaises(SafetyError):
            pool.get_client("BINANCE", True)

    def test_9_event_loop_remains_responsive_during_shutdown(self):
        """9. Event loop remains fully responsive during shutdown."""
        async def verify_loop():
            counter = 0
            pool = ReconciliationClients(self.settings)
            key = pool._get_key("BINANCE", True)
            mock_client = Mock()
            pool.clients[key] = mock_client
            pool.client_locks[key] = threading.Lock()

            async def ticker():
                nonlocal counter
                for _ in range(5):
                    await asyncio.sleep(0.01)
                    counter += 1

            t_task = asyncio.create_task(ticker())
            await asyncio.to_thread(pool.close, 0.1)
            await t_task

            self.assertGreaterEqual(counter, 3, "Event loop was blocked during shutdown!")
            self.assertEqual(mock_client.close.call_count, 1)

        asyncio.run(verify_loop())


if __name__ == "__main__":
    unittest.main()
