"""Unit tests verifying the 2 defect fixes:
1. CCXT query concurrency & shutdown race condition safety (safe_retry.py, main.py)
2. MANUAL_REVIEW Discord alert deduplication for repeated signal_ids (dispatcher.py, outbox.py)
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
from exchange.verified_pnl.dispatcher import (
    dispatch_verified_alert, verification_tick, flush_outbox,
    make_manual_review_payload, enqueue_manual_review_alert
)
from exchange.verified_pnl.models import (
    FillStatus, VerificationStatus, NotificationPayload, ExitReason, PnlResult
)
from exchange.verified_pnl.outbox import (
    NotificationOutboxStore, DiscordSender, build_verified_embed
)


class TwoDefectsVerificationTests(unittest.TestCase):
    def setUp(self):
        reset_reconciliation_state()
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
            ORDER_RECONCILIATION_ENABLED=True,
            DUPLICATE_SIGNAL_PROTECTION=True,
            PERSIST_ORDER_STATE=True,
            BINANCE_KEY="bin_test_key",
            BINANCE_SECRET="bin_test_secret",
            DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/123/fake",
        )
        self.store = NotificationOutboxStore(self.db_path)
        self.safe_store = Store(self.safe_db)
        self.manager = Manager(self.safe_store)

    def tearDown(self):
        reset_reconciliation_state()

    # --- Defect 1: CCXT Concurrency & Shutdown Race Conditions ---

    def test_server_shutdown_while_ccxt_reconciliation_active(self):
        """1. Server shutdown waits for in-flight synchronous CCXT queries before closing clients."""
        client_closed = False
        query_finished = False

        class SlowClient:
            def __init__(self):
                self.options = {"defaultType": "swap"}
            def fetch_order(self, order_id, symbol, params=None):
                nonlocal query_finished
                # Simulate slow exchange network call
                time.sleep(0.15)
                query_finished = True
                return {"id": order_id, "status": "closed", "filled": 1.0}
            def fetch_open_orders(self, *a, **k): return []
            def fetch_closed_orders(self, *a, **k): return []
            def fetch_my_trades(self, *a, **k): return []
            def fetch_positions(self, *a, **k): return []
            def close(self):
                nonlocal client_closed
                # If query is still running when close() is called, this is a race violation!
                if not query_finished:
                    raise RuntimeError("Client was closed while query was still executing!")
                client_closed = True

        sig = hashlib.sha256(b"active-reconcile").hexdigest()
        self.safe_store.claim(dict(
            signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="cid_active_shutdown_" + "x" * 20,
            order_id="oid_active_shutdown", created=time.time(), reference=50000.0, target="1.0",
            filled="0", remaining="1.0", attempts=1, futures=True, side="buy", close=False
        ))

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        mock_slow_client = SlowClient()

        # Run reconciliation in a background thread
        recon_thread = threading.Thread(
            target=reconcile_pending,
            args=(self.settings, lambda ex, fut: mock_slow_client)
        )
        recon_thread.start()

        # Let the query start
        time.sleep(0.03)

        # Trigger shutdown from main thread
        begin_shutdown()
        # Shutdown waits for queries to drain before closing clients
        drained = wait_for_reconciliation_drained(timeout=2.0)
        self.assertTrue(drained, "Active reconciliation queries failed to drain in time")

        mock_slow_client.close()
        recon_thread.join(timeout=2.0)

        self.assertTrue(query_finished)
        self.assertTrue(client_closed)

    def test_slow_api_timeout_and_task_cancellation(self):
        """2. Slow API timeout / error notifies failure safely without permitting duplicate new orders."""
        sig = hashlib.sha256(b"slow-timeout").hexdigest()
        row = dict(
            signal=sig, asset="BINANCE:ETH/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="ETH/USDT", client_id="cid_timeout_" + "x" * 20,
            order_id="oid_timeout", created=time.time(), reference=3000.0, target="1.0",
            filled="0", remaining="1.0", attempts=1, futures=True, side="buy", close=False
        )
        self.safe_store.claim(row)

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        def mock_timeout_client(*args):
            c = Mock()
            c.fetch_order.side_effect = TimeoutError("Exchange request timed out")
            return c

        reconcile_pending(self.settings, mock_timeout_client)

        # Order must remain UNKNOWN (never marked FILLED, nor cleared for resubmission)
        pending = self.safe_store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "UNKNOWN")

    def test_new_reconciliation_blocked_during_shutdown(self):
        """3. New reconciliation requests are blocked when shutdown is initiated."""
        begin_shutdown()
        self.assertTrue(is_shutting_down())

        pool = get_reconciliation_clients(self.settings)
        with self.assertRaises(SafetyError):
            pool.get_client("BINANCE", True)

        sig = hashlib.sha256(b"blocked-recon").hexdigest()
        self.safe_store.claim(dict(
            signal=sig, asset="BINANCE:ADA/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="ADA/USDT", client_id="cid_blocked_" + "x" * 20,
            order_id="oid_blocked", created=time.time(), reference=1.0, target="10.0",
            filled="0", remaining="10.0", attempts=1, futures=True, side="buy", close=False
        ))

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        mock_client = Mock()
        reconcile_pending(self.settings, lambda ex, fut: mock_client)

        # Since shutdown was active, reconcile_pending did not execute queries
        mock_client.fetch_order.assert_not_called()

    def test_client_idempotent_close_and_leak_prevention(self):
        """4. Repeated close calls are idempotent and clean up all resources without exceptions."""
        pool = get_reconciliation_clients(self.settings)
        mock_c1 = Mock()
        mock_c2 = Mock()
        pool.clients[("BINANCE", "swap", "default")] = mock_c1
        pool.clients[("BYBIT", "spot", "default")] = mock_c2

        # First close
        close_reconciliation_clients(timeout=0.5)
        mock_c1.close.assert_called_once()
        mock_c2.close.assert_called_once()
        self.assertEqual(len(pool.clients), 0)

        # Second close: must not raise error and must remain safe
        close_reconciliation_clients(timeout=0.5)
        self.assertEqual(mock_c1.close.call_count, 1)
        self.assertEqual(mock_c2.close.call_count, 1)

    # --- Defect 2: MANUAL_REVIEW Discord Warning Deduplication ---

    def test_same_signal_id_generates_exactly_one_warning(self):
        """5. Re-receiving webhooks without order ID for the same signal_id creates only 1 Discord alert."""
        order_info1 = OrderRequest(
            exchange="BINANCE", base="BTC", quote="USDT", side="buy",
            amount=0.01, signal_id="sig-dedup-1", order_name="전략A"
        )
        order_info2 = OrderRequest(
            exchange="BINANCE", base="BTC", quote="USDT", side="buy",
            amount=0.01, signal_id="sig-dedup-1", order_name="전략A"
        )

        # Dispatch twice without order ID (simulating webhook resend)
        dispatch_verified_alert("BINANCE", {}, order_info1, self.settings)
        dispatch_verified_alert("BINANCE", {}, order_info2, self.settings)

        # Only 1 job in verified_jobs table
        with self.store._connect_read() as con:
            jobs = con.execute("SELECT count(*) FROM verified_jobs").fetchone()[0]
            self.assertEqual(jobs, 1, "Duplicate verified job was created for identical signal_id!")

        mock_client = Mock(market=Mock(return_value={}))
        # Run tick 1
        verification_tick(self.settings, lambda *a: mock_client)

        with self.store._connect_read() as con:
            alerts = con.execute("SELECT count(*) FROM verified_outbox WHERE event_type='manual_review'").fetchone()[0]
            self.assertEqual(alerts, 1)

        # Dispatch again after tick (e.g., late webhook retry)
        dispatch_verified_alert("BINANCE", {}, order_info1, self.settings)

        # Run tick 2
        with self.store._connect() as con:
            con.execute("UPDATE verified_jobs SET due=0")
        verification_tick(self.settings, lambda *a: mock_client)

        with self.store._connect_read() as con:
            alerts = con.execute("SELECT count(*) FROM verified_outbox WHERE event_type='manual_review'").fetchone()[0]
            self.assertEqual(alerts, 1, "Duplicate Discord alert created on subsequent webhook delivery!")

    def test_concurrent_signal_dispatch_and_server_restart(self):
        """6. Concurrent incoming webhooks and server restart maintain exactly 1 alert."""
        order_info = OrderRequest(
            exchange="BYBIT", base="ETH", quote="USDT", side="buy",
            amount=0.5, signal_id="sig-concurrent-1", order_name="전략B"
        )

        # 5 threads dispatch concurrently
        threads = [
            threading.Thread(target=dispatch_verified_alert, args=("BYBIT", {}, order_info, self.settings))
            for _ in range(5)
        ]
        for t in threads: t.start()
        for t in threads: t.join()

        mock_client = Mock(market=Mock(return_value={}))
        verification_tick(self.settings, lambda *a: mock_client)

        with self.store._connect_read() as con:
            alerts = con.execute("SELECT count(*) FROM verified_outbox WHERE event_type='manual_review'").fetchone()[0]
            self.assertEqual(alerts, 1, "Concurrent dispatches produced duplicate alerts!")

        # Simulate crash and restart
        new_store = NotificationOutboxStore(self.db_path)
        new_store.recover()

        # Re-dispatch after restart
        dispatch_verified_alert("BYBIT", {}, order_info, self.settings)
        verification_tick(self.settings, lambda *a: mock_client)

        with self.store._connect_read() as con:
            alerts = con.execute("SELECT count(*) FROM verified_outbox WHERE event_type='manual_review'").fetchone()[0]
            self.assertEqual(alerts, 1, "Server restart caused duplicate alert generation!")

    def test_different_strategy_and_symbol_same_signal_id_distinguished(self):
        """7. Different strategies or symbols sharing the same signal_id string are properly distinguished."""
        # Different strategies
        order_strat1 = OrderRequest(
            exchange="BINANCE", base="SOL", quote="USDT", side="buy",
            amount=1.0, signal_id="sig-shared", order_name="전략1"
        )
        order_strat2 = OrderRequest(
            exchange="BINANCE", base="SOL", quote="USDT", side="buy",
            amount=1.0, signal_id="sig-shared", order_name="전략2"
        )
        dispatch_verified_alert("BINANCE", {}, order_strat1, self.settings)
        dispatch_verified_alert("BINANCE", {}, order_strat2, self.settings)

        # Different symbol
        order_sym = OrderRequest(
            exchange="BINANCE", base="XRP", quote="USDT", side="buy",
            amount=100.0, signal_id="sig-shared", order_name="전략1"
        )
        dispatch_verified_alert("BINANCE", {}, order_sym, self.settings)

        with self.store._connect_read() as con:
            jobs = con.execute("SELECT count(*) FROM verified_jobs").fetchone()[0]
            self.assertEqual(jobs, 3, "Legitimate distinct orders were mistakenly merged!")

        mock_client = Mock(market=Mock(return_value={}))
        verification_tick(self.settings, lambda *a: mock_client)

        with self.store._connect_read() as con:
            alerts = con.execute("SELECT count(*) FROM verified_outbox WHERE event_type='manual_review'").fetchone()[0]
            self.assertEqual(alerts, 3, "Each distinct strategy/symbol must produce its own alert!")

    def test_discord_failure_retry_and_deduplication(self):
        """8. Discord failure maintains retry backoff and never generates duplicate alert on re-dispatch."""
        order_info = OrderRequest(
            exchange="OKX", base="BTC", quote="USDT", side="buy",
            amount=0.01, signal_id="sig-fail-retry", order_name="전략OKX"
        )
        dispatch_verified_alert("OKX", {}, order_info, self.settings)

        mock_client = Mock(market=Mock(return_value={}))
        verification_tick(self.settings, lambda *a: mock_client)

        # Simulate Discord rate-limit / transient retryable failure (429)
        with patch("requests.post", return_value=Mock(status_code=429, headers={"Retry-After": "5"}, text="Rate Limited")):
            flush_outbox(self.settings)

        with self.store._connect_read() as con:
            row = con.execute("SELECT status, attempts FROM verified_outbox WHERE event_type='manual_review'").fetchone()
            self.assertEqual(row[0], "PENDING")
            self.assertEqual(row[1], 1)

        # While waiting for retry, another webhook arrives with the same signal_id
        dispatch_verified_alert("OKX", {}, order_info, self.settings)
        verification_tick(self.settings, lambda *a: mock_client)

        with self.store._connect_read() as con:
            alerts = con.execute("SELECT count(*) FROM verified_outbox WHERE event_type='manual_review'").fetchone()[0]
            self.assertEqual(alerts, 1, "Webhook retry during backoff produced duplicate alert!")

        # Now Discord recovers (204)
        with self.store._connect() as con:
            con.execute("UPDATE verified_outbox SET next_retry_at=0")

        with patch("requests.post", return_value=Mock(status_code=204)):
            flush_outbox(self.settings)

        with self.store._connect_read() as con:
            row = con.execute("SELECT status FROM verified_outbox WHERE event_type='manual_review'").fetchone()
            self.assertEqual(row[0], "SENT")


if __name__ == "__main__":
    unittest.main()
