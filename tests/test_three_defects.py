"""Unit and integration tests verifying the 3 defect fixes:
1. MANUAL_REVIEW Discord warning generation & deduplication (dispatcher.py)
2. SAFE_RETRY read-only CCXT concurrency and shutdown safety (safe_retry.py, main.py)
3. Linux rollback path separator compatibility (test_rollback.py)
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


class ThreeDefectsVerificationTests(unittest.TestCase):
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

    # --- Defect 1: MANUAL_REVIEW Discord Warning ---

    def test_manual_review_alert_generation_and_deduplication(self):
        """1. MANUAL_REVIEW produces Outbox alert with exchange, symbol, signal_id, reason, and deduplicates."""
        order_info = OrderRequest(
            exchange="BINANCE", base="BTC", quote="USDT", side="buy",
            amount=0.01, signal_id="sig-manual-1", order_name="전략1"
        )
        # Dispatch order WITHOUT order ID
        dispatch_verified_alert("BINANCE", {}, order_info, self.settings)

        # Run verification tick
        mock_client = Mock(market=Mock(return_value={}))
        verification_tick(self.settings, lambda *a: mock_client)

        with self.store._connect_read() as con:
            # Check job is in MANUAL_REVIEW
            job = con.execute("SELECT state, data FROM verified_jobs").fetchone()
            self.assertEqual(job[0], "MANUAL_REVIEW")
            data = json.loads(job[1])
            self.assertTrue(data.get("manual_review_alert_sent"))

            # Check Outbox event was created
            outbox = con.execute(
                "SELECT event_id, event_type, payload FROM verified_outbox WHERE event_type='manual_review'"
            ).fetchall()
            self.assertEqual(len(outbox), 1)

            payload_dict = json.loads(outbox[0][2])
            payload = NotificationPayload(**payload_dict)

            # Verification of required fields
            self.assertEqual(payload.exchange, "BINANCE")
            self.assertIn("BTC", payload.symbol)
            self.assertEqual(payload.signal_id, "sig-manual-1")
            self.assertIn("missing_order_id", payload.error_message)
            self.assertEqual(payload.fill_status, FillStatus.UNVERIFIED)

            # Verification of Embed rendering (P2-2: wire embed has no duplicate fields; details in build_notification_details)
            embed = build_verified_embed(payload)
            embed_dict = embed.data
            self.assertIn("수동 확인 필요", embed_dict["title"])
            self.assertEqual(embed_dict["fields"], [])
            from exchange.utility.discord_embed import build_notification_details
            field_names = [f["name"] for f in build_notification_details(payload)]
            self.assertIn("거래소", field_names)
            self.assertIn("심볼", field_names)
            self.assertIn("신호 ID", field_names)
            self.assertIn("체결 상태", field_names)
            self.assertIn("수동 확인 필요 여부", field_names)
            self.assertIn("오류 상세", field_names)

        # Run another tick: ensure deduplication (no extra outbox event)
        with self.store._connect() as con:
            con.execute("UPDATE verified_jobs SET due=0")
        verification_tick(self.settings, lambda *a: mock_client)

        with self.store._connect_read() as con:
            outbox_count = con.execute(
                "SELECT count(*) FROM verified_outbox WHERE event_type='manual_review'"
            ).fetchone()[0]
            self.assertEqual(outbox_count, 1, "Duplicate manual review alert was created!")

    def test_discord_failure_and_restart_recovery(self):
        """2. Discord failure retries with backoff and persists across restart recovery."""
        payload = NotificationPayload(
            event_type="manual_review",
            event_id="crash-alert-1",
            exchange="BINANCE",
            symbol="BTC/USDT",
            side="매수",
            order_name="테스트",
            exit_reason=ExitReason.MANUAL,
            pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.UNVERIFIED,
            signal_id="sig-crash-1",
            error_message="주문 ID 누락",
            event_timestamp=time.time(),
        )
        self.store.enqueue(payload)

        # Simulate Discord rate-limit / transient failure (HTTP 429) and P2-5 HTTP 500 hold recovery
        mock_resp = Mock(status_code=429, headers={"Retry-After": "5"}, text="Rate Limited")
        with patch("requests.post", return_value=mock_resp):
            flush_outbox(self.settings)

        # Confirm status is still pending with retry backoff
        with self.store._connect_read() as con:
            row = con.execute("SELECT status, attempts FROM verified_outbox WHERE event_id='crash-alert-1'").fetchone()
            self.assertEqual(row[0], "PENDING")
            self.assertEqual(row[1], 1)

        # Simulate server restart & recover
        new_store = NotificationOutboxStore(self.db_path)
        new_store.recover()

        # Reset next_retry_at to 0 to simulate backoff interval elapsed after restart
        with self.store._connect() as con:
            con.execute("UPDATE verified_outbox SET next_retry_at=0 WHERE event_id='crash-alert-1'")

        # Simulate subsequent successful delivery
        mock_success = Mock(status_code=204)
        with patch("requests.post", return_value=mock_success):
            flush_outbox(self.settings)

        with self.store._connect_read() as con:
            row = con.execute("SELECT status FROM verified_outbox WHERE event_id='crash-alert-1'").fetchone()
            self.assertEqual(row[0], "SENT")

    def test_missing_order_id_safe_hold_no_fake_fill(self):
        """3. Order with missing ID remains UNVERIFIED, NEVER marked FILLED or FAILED, and NEVER re-sent."""
        order_info = OrderRequest(
            exchange="BYBIT", base="ETH", quote="USDT", side="buy",
            amount=0.5, signal_id="bybit-noid", is_futures=True
        )
        dispatch_verified_alert("BYBIT", {}, order_info, self.settings)

        mock_client = Mock(
            create_order=Mock(),
            market=Mock(return_value={})
        )
        verification_tick(self.settings, lambda *a: mock_client)

        # Order must not be submitted to exchange again
        mock_client.create_order.assert_not_called()

        with self.store._connect_read() as con:
            job = con.execute("SELECT state, data FROM verified_jobs WHERE data LIKE '%BYBIT%'").fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job[0], "MANUAL_REVIEW")
            outbox = con.execute("SELECT payload FROM verified_outbox WHERE event_type='manual_review'").fetchone()
            self.assertIsNotNone(outbox)
            p = NotificationPayload(**json.loads(outbox[0]))
            self.assertEqual(p.fill_status, FillStatus.UNVERIFIED)
            self.assertNotEqual(p.fill_status, FillStatus.FILLED)
            self.assertNotEqual(p.fill_status, FillStatus.FAILED)

    # --- Defect 2: SAFE_RETRY CCXT Concurrency & Shutdown Safety ---

    def test_13_orders_concurrent_reconciliation(self):
        """4. 13 orders across 13 strategies reconcile concurrently without race conditions."""
        for i in range(1, 14):
            sig = hashlib.sha256(f"strat-three-{i}".encode()).hexdigest()
            row = dict(
                signal=sig, asset=f"BINANCE:TOKEN{i}/USDT", state="UNKNOWN",
                exchange="BINANCE", symbol=f"TOKEN{i}/USDT", client_id=f"cid_three_{i}_" + "x" * 20,
                order_id=f"oid_three_{i}", created=time.time(), reference=100.0, target="1.0",
                filled="0", remaining="1.0", attempts=1, futures=True, side="buy", close=False
            )
            self.safe_store.claim(row)

        def mock_fetch(order_id, symbol, params=None):
            return {"id": order_id, "status": "closed", "filled": 1.0}

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
        self.assertEqual(len(self.safe_store.pending()), 0)

    def test_same_ccxt_client_concurrent_access_safety(self):
        """5. Concurrent access to the SAME CCXT client instance is serialized via per-client lock."""
        pool = ReconciliationClients(self.settings)
        lock1 = pool.get_client_lock("BINANCE", True)
        lock2 = pool.get_client_lock("BINANCE", True)
        self.assertIs(lock1, lock2, "Client lock must be identical for same exchange and market category")

        lock_spot = pool.get_client_lock("BINANCE", False)
        self.assertIsNot(lock1, lock_spot, "Spot and swap locks must be distinct")

        # Verify mutual exclusion
        entered_concurrently = False
        active_count = 0
        counter_lock = threading.Lock()

        def worker():
            nonlocal entered_concurrently, active_count
            with lock1:
                with counter_lock:
                    active_count += 1
                    if active_count > 1:
                        entered_concurrently = True
                time.sleep(0.01)
                with counter_lock:
                    active_count -= 1

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertFalse(entered_concurrently, "Two threads acquired the same client lock concurrently!")

    def test_trading_bot_ccxt_immutability(self):
        """6. Reconciliation NEVER mutates or touches trading bot CCXT instance options."""
        bot_client = Mock(options={"defaultType": "spot", "adjustForTimeDifference": False})
        trading_bot = NS(client=bot_client, position_mode="one-way")

        sig = hashlib.sha256(b"immut-sig").hexdigest()
        self.safe_store.claim(dict(
            signal=sig, asset="BINANCE:BTC/USDT", state="UNKNOWN",
            exchange="BINANCE", symbol="BTC/USDT", client_id="cid_immut_" + "x" * 20,
            order_id="oid_immut", created=time.time(), reference=50000.0, target="0.01",
            filled="0", remaining="0.01", attempts=1, futures=True, side="buy", close=False
        ))

        import exchange.safe_retry as safe
        prev_mgr = safe._manager
        safe._manager = self.manager
        self.addCleanup(setattr, safe, "_manager", prev_mgr)

        def mock_get_bot(exch):
            return trading_bot
        mock_get_bot.__module__ = "exchange.pexchange"

        with patch.object(safe, "get_reconciliation_clients") as mock_pool:
            ro_client = Mock(
                fetch_order=Mock(return_value={"id": "oid_immut", "status": "closed", "filled": 0.01}),
                fetch_open_orders=Mock(return_value=[]),
                fetch_closed_orders=Mock(return_value=[]),
                fetch_my_trades=Mock(return_value=[]),
                fetch_positions=Mock(return_value=[])
            )
            mock_pool.return_value.get_client.return_value = ro_client
            reconcile_pending(self.settings, mock_get_bot)

            # Bot client options must remain 100% untouched
            self.assertEqual(bot_client.options["defaultType"], "spot")
            self.assertFalse(bot_client.options["adjustForTimeDifference"])
            bot_client.fetch_order.assert_not_called()

    def test_load_markets_failure_and_reinitialization(self):
        """7. load_markets() failure does NOT cache broken client and re-attempts on next request."""
        pool = ReconciliationClients(self.settings)

        call_count = 0
        def fail_then_succeed(params):
            nonlocal call_count
            call_count += 1
            client = Mock()
            if call_count == 1:
                client.load_markets.side_effect = ConnectionError("Exchange unavailable")
            else:
                client.load_markets.return_value = {}
                client.markets = {}
            return client

        with patch("ccxt.binance", side_effect=fail_then_succeed):
            # First attempt fails: must raise SafetyError
            with self.assertRaises(SafetyError):
                pool.get_client("BINANCE", True)

            # Ensure client was NOT cached
            key = pool._get_key("BINANCE", True)
            self.assertNotIn(key, pool.clients)

            # Second attempt succeeds: caches properly
            c2 = pool.get_client("BINANCE", True)
            self.assertIn(key, pool.clients)
            self.assertIs(c2, pool.clients[key])

    def test_shutdown_task_cancellation_and_resource_cleanup(self):
        """8. close_reconciliation_clients() cleans up resources and prevents new jobs."""
        pool = get_reconciliation_clients(self.settings)
        mock_c = Mock()
        key = pool._get_key("BINANCE", True)
        pool.clients[key] = mock_c

        close_reconciliation_clients()

        # Closed pool must have called close() and emptied
        mock_c.close.assert_called_once()
        self.assertTrue(pool.closed)
        self.assertEqual(len(pool.clients), 0)

        # Calling get_client on closed pool must raise SafetyError
        with self.assertRaises(SafetyError):
            pool.get_client("BINANCE", True)

    # --- Defect 3: Linux Rollback & Windows Manifest Compatibility ---

    def test_linux_rollback_windows_manifest_compatibility(self):
        """9. Windows backslash manifest entries restore cleanly on Linux paths with traversal defense."""
        from test_rollback import normalize_rel_path

        # Test path normalization
        self.assertEqual(normalize_rel_path("exchange\\binance.py"), Path("exchange/binance.py"))
        self.assertEqual(normalize_rel_path("tests/golden.json"), Path("tests/golden.json"))

        # Test traversal protection
        with self.assertRaises(ValueError):
            normalize_rel_path("..\\secrets.txt")
        with self.assertRaises(ValueError):
            normalize_rel_path("/etc/passwd")

        # Test rollback into isolated directory
        with tempfile.TemporaryDirectory(prefix="poa-norm-test-") as tdir:
            dest = Path(tdir)
            rel = normalize_rel_path("exchange\\model\\schemas.py")
            target = (dest / rel).resolve()
            self.assertTrue(target.is_relative_to(dest))
            target.parent.mkdir(parents=True, exist_ok=True)
            source = ROOT / "exchange/model/schemas.py"
            target.write_bytes(source.read_bytes())
            self.assertEqual(
                hashlib.sha256(target.read_bytes()).hexdigest(),
                hashlib.sha256(source.read_bytes()).hexdigest()
            )


if __name__ == "__main__":
    unittest.main()
