import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness import setup
schema, _, util = setup(ROOT)
Settings = schema.Settings
OrderRequest = schema.OrderRequest

from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from exchange.verified_pnl.dispatcher import (
    dispatch_verified_alert,
    verification_tick,
    flush_outbox,
    ReadClients,
)
from exchange.verified_pnl.models import (
    FillStatus,
    VerificationStatus,
    NotificationPayload,
    ExitReason,
    PnlResult,
)
from exchange.verified_pnl.outbox import (
    NotificationOutboxStore,
    _INITIALIZED_DBS,
)
from exchange.verified_pnl.verifier import FillVerifier, _VERIFY_SEMAPHORE


class LightweightVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "test_notifications.sqlite3")
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
            VERIFY_MAX_CONCURRENCY=2,
            VERIFY_STAGED_DELAYS=[2.0, 5.0, 10.0, 20.0, 40.0],
            FILL_VERIFICATION_TIMEOUT_SEC=30.0,
        )
        self.store = NotificationOutboxStore(self.db_path)
        self.order_info = OrderRequest(
            exchange="BINANCE",
            base="BTC",
            quote="USDT.P",
            side="close/sell",
            amount=0.01,
            exit_type="TP",
            strategy_id="strat-alpha",
        )

    def test_staged_retry_backoff_and_bounded_polling(self):
        """Verify that open orders back off gracefully (2s, 5s, 10s...) and do not poll 5s forever."""
        dispatch_verified_alert(
            "BINANCE",
            {"id": "ord-open-1"},
            self.order_info,
            self.settings,
        )

        client = Mock()
        client.market.return_value = {}
        # Order is still open and unfilled on exchange
        client.fetch_order.return_value = {
            "id": "ord-open-1",
            "status": "open",
            "amount": 0.01,
            "filled": 0.0,
            "remaining": 0.01,
        }
        client.fetch_my_trades.return_value = []

        # Tick 1 (t=0s check): attempts=1, next due should be ~now + 2s
        t_before = time.time()
        verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            row = con.execute("SELECT state, due, data FROM verified_jobs WHERE data LIKE '%ord-open-1%'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "PENDING")
            data = json.loads(row[2])
            self.assertEqual(data.get("attempts"), 1)
            # Due must be around now + 2s
            self.assertGreaterEqual(row[1], t_before + 1.8)
            self.assertLessEqual(row[1], t_before + 3.0)

        # Fast forward due to trigger Tick 2 (attempts=2, next due should be ~now + 5s)
        with self.store._connect() as con:
            con.execute("UPDATE verified_jobs SET due=0")

        t_before = time.time()
        verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            row = con.execute("SELECT state, due, data FROM verified_jobs WHERE data LIKE '%ord-open-1%'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "PENDING")
            data = json.loads(row[2])
            self.assertEqual(data.get("attempts"), 2)
            # Due must be around now + 5s
            self.assertGreaterEqual(row[1], t_before + 4.8)
            self.assertLessEqual(row[1], t_before + 6.0)

    def test_timeout_transitions_to_reconciliation_not_endless_5s(self):
        """When an order times out past FILL_VERIFICATION_TIMEOUT_SEC, it must not poll every 5s."""
        dispatch_verified_alert(
            "BINANCE",
            {"id": "ord-timeout"},
            self.order_info,
            self.settings,
        )

        client = Mock()
        client.market.return_value = {}
        client.fetch_order.side_effect = lambda oid, *a, **kw: {
            "id": oid,
            "status": "open",
            "amount": 0.01,
            "filled": 0.0,
        }
        client.fetch_my_trades.return_value = []

        # Simulate timeout by aging the job past 30 seconds
        now = time.time()
        with self.store._connect() as con:
            con.execute("UPDATE verified_jobs SET created=?, due=0", (now - 35,))

        verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            row = con.execute("SELECT state, due, data FROM verified_jobs WHERE data LIKE '%ord-timeout%'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "PENDING")
            # Next due should be reconciliation backoff (at least +60s), NOT +5s!
            self.assertGreaterEqual(row[1], now + 50.0)

        # And unverified alert must have been enqueued once
        with self.store._connect_read() as con:
            outbox_rows = con.execute("SELECT count(*) FROM verified_outbox").fetchone()[0]
            self.assertEqual(outbox_rows, 1)

    def test_unnecessary_fetch_my_trades_skipped_when_order_open_zero_fill(self):
        """When fetch_order returns open with filled=0, fetch_my_trades must not be called."""
        client = Mock()
        client.fetch_order.return_value = {
            "id": "ord-zero",
            "status": "open",
            "amount": 0.01,
            "filled": 0.0,
        }
        verifier = FillVerifier()
        bot = NS(client=client)
        fill = verifier.verify_order_result(bot, "BINANCE", "BTC/USDT:USDT", {"id": "ord-zero"}, self.order_info)

        self.assertEqual(fill.status, FillStatus.PENDING)
        client.fetch_order.assert_called_once()
        # fetch_my_trades must be SKIPPED!
        client.fetch_my_trades.assert_not_called()

    def test_unnecessary_fetch_my_trades_skipped_when_order_canceled_zero_fill(self):
        """When fetch_order returns canceled with filled=0, fetch_my_trades must not be called."""
        client = Mock()
        client.fetch_order.return_value = {
            "id": "ord-canceled",
            "status": "canceled",
            "amount": 0.01,
            "filled": 0.0,
        }
        verifier = FillVerifier()
        bot = NS(client=client)
        fill = verifier.verify_order_result(bot, "BINANCE", "BTC/USDT:USDT", {"id": "ord-canceled"}, self.order_info)

        self.assertEqual(fill.status, FillStatus.CANCELED)
        client.fetch_order.assert_called_once()
        client.fetch_my_trades.assert_not_called()

    def test_concurrency_semaphore_limits_parallel_rest_queries(self):
        """Verify that _VERIFY_SEMAPHORE limits concurrent queries to max capacity (2)."""
        active_queries = []
        max_concurrent = [0]
        lock = threading.Lock()

        def slow_fetch(*args, **kwargs):
            with lock:
                active_queries.append(1)
                if len(active_queries) > max_concurrent[0]:
                    max_concurrent[0] = len(active_queries)
            time.sleep(0.05)
            with lock:
                active_queries.pop()
            return {"id": "ord-concurrent", "status": "closed", "filled": 0.01, "average": 100000}

        client = Mock()
        client.fetch_order.side_effect = slow_fetch
        client.fetch_my_trades.return_value = []
        verifier = FillVerifier()
        bot = NS(client=client)

        threads = []
        for _ in range(8):
            t = threading.Thread(
                target=verifier.verify_order_result,
                args=(bot, "BINANCE", "BTC/USDT:USDT", {"id": "ord-concurrent"}, self.order_info),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # At no point should concurrent REST calls exceed semaphore capacity (2)
        self.assertLessEqual(max_concurrent[0], 2)
        self.assertGreater(max_concurrent[0], 0)

    def test_sqlite_ddl_init_cached_and_wal_normal_pragmas(self):
        """Verify that repeated store instantiation does not re-run DDL and uses NORMAL synchronous."""
        self.assertIn(self.db_path, _INITIALIZED_DBS)

        # Create another store pointing to same DB
        store2 = NotificationOutboxStore(self.db_path)
        with store2._connect() as con:
            # Check synchronous pragma: NORMAL is 1 in SQLite
            sync = con.execute("PRAGMA synchronous").fetchone()[0]
            self.assertEqual(sync, 1)  # 1 = NORMAL, 2 = FULL
            # Check journal_mode: WAL
            journal = con.execute("PRAGMA journal_mode").fetchone()[0].upper()
            self.assertEqual(journal, "WAL")

    def test_prune_completed_cleans_old_jobs_and_outbox(self):
        """Verify that prune_completed removes old SENT/FAILED notifications and DONE jobs."""
        with self.store._connect() as con:
            # Insert ancient completed job and outbox record (10 days old)
            old_time = time.time() - (10 * 86400)
            con.execute("INSERT INTO verified_jobs VALUES ('job-old', '{}', 'DONE', ?, ?)", (old_time, old_time))
            con.execute("INSERT INTO verified_jobs VALUES ('job-new', '{}', 'DONE', ?, ?)", (time.time(), time.time()))
            con.execute(
                """
                INSERT INTO verified_outbox (event_id, exchange, symbol, event_type, version, payload, status, created_at, updated_at)
                VALUES ('out-old', 'BINANCE', 'BTC/USDT', 'final', 1, '{}', 'SENT', ?, ?)
                """,
                (old_time, old_time),
            )
            con.execute(
                """
                INSERT INTO verified_outbox (event_id, exchange, symbol, event_type, version, payload, status, created_at, updated_at)
                VALUES ('out-new', 'BINANCE', 'BTC/USDT', 'final', 1, '{}', 'SENT', ?, ?)
                """,
                (time.time(), time.time()),
            )

        deleted = self.store.prune_completed(max_age_seconds=7 * 86400)
        self.assertEqual(deleted, 2)

        with self.store._connect_read() as con:
            jobs = [r[0] for r in con.execute("SELECT id FROM verified_jobs").fetchall()]
            outbox = [r[0] for r in con.execute("SELECT event_id FROM verified_outbox").fetchall()]
            self.assertIn("job-new", jobs)
            self.assertNotIn("job-old", jobs)
            self.assertIn("out-new", outbox)
            self.assertNotIn("out-old", outbox)

    def test_simulation_13_concurrent_strategy_signals(self):
        """Simulate 13 simultaneous TradingView strategies placing orders without lock contention."""
        client = Mock()
        client.market.return_value = {}
        client.fetch_order.side_effect = lambda oid, *a, **kw: {
            "id": oid,
            "status": "closed",
            "amount": 0.01,
            "filled": 0.01,
            "average": 100000.0,
        }
        client.fetch_my_trades.return_value = []

        errors = []

        def place_signal(i):
            try:
                info = OrderRequest(
                    exchange="BINANCE",
                    base="BTC",
                    quote="USDT.P",
                    side="close/sell",
                    amount=0.01,
                    exit_type="TP",
                    strategy_id=f"strategy-{i:02d}",
                )
                dispatch_verified_alert(
                    "BINANCE",
                    {"id": f"ord-strat-{i:02d}"},
                    info,
                    self.settings,
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=place_signal, args=(i,)) for i in range(13)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Encountered errors during 13 strategy dispatch: {errors}")

        with self.store._connect_read() as con:
            pending_jobs = con.execute("SELECT count(*) FROM verified_jobs WHERE state='PENDING'").fetchone()[0]
            self.assertEqual(pending_jobs, 13)

        # Process all 13 in verification tick
        verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            done_jobs = con.execute("SELECT count(*) FROM verified_jobs WHERE state='DONE'").fetchone()[0]
            self.assertEqual(done_jobs, 13)
            outbox_count = con.execute("SELECT count(*) FROM verified_outbox").fetchone()[0]
            self.assertEqual(outbox_count, 13)


if __name__ == "__main__":
    unittest.main()
