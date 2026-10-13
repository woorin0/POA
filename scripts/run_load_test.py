"""Mock load and concurrency stress probe for POA under lightweight target conditions."""
import os
import sys
import time
import json
import sqlite3
import tempfile
import threading
import statistics
import concurrent.futures
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from harness import setup, bot_for, load

def get_process_rss_mb():
    try:
        import subprocess
        pid = os.getpid()
        cmd = ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {pid}).WorkingSet64"]
        out = subprocess.check_output(cmd, timeout=5).decode().strip()
        return round(int(out) / (1024 * 1024), 2)
    except Exception:
        return 0.0

def run_load_probe():
    print("=== STARTING POA CONCURRENCY & MOCK LOAD STRESS PROBE ===")
    start_time = time.perf_counter()
    initial_rss = get_process_rss_mb()
    print(f"Initial Process RSS: {initial_rss} MB")
    
    schema, legacy, util = setup(ROOT)
    import exchange.safe_retry as safe
    from exchange.verified_pnl.dispatcher import dispatch_verified_alert, verification_tick, flush_outbox
    from exchange.verified_pnl.outbox import NotificationOutboxStore
    from exchange.verified_pnl.models import NotificationPayload
    from fastapi.testclient import TestClient

    results = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "initial_rss_mb": initial_rss,
    }

    with tempfile.TemporaryDirectory(prefix="poa-load-probe-") as folder:
        tmp = Path(folder)
        settings = util.settings
        for name in [
            "SAFE_RETRY_ENABLED",
            "ORDER_RECONCILIATION_ENABLED",
            "DUPLICATE_SIGNAL_PROTECTION",
            "PERSIST_ORDER_STATE",
            "VERIFIED_DISCORD_ALERTS",
            "VERIFIED_FILL_PRICE",
            "DISCORD_OUTBOX_ENABLED",
            "DISCORD_RETRY_ENABLED",
        ]:
            setattr(settings, name, True)

        settings.SAFE_RETRY_DB_PATH = str(tmp / "safe_orders.sqlite3")
        settings.VERIFIED_NOTIFICATIONS_DB_PATH = str(tmp / "notifications.sqlite3")
        settings.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/mock/probe"

        safe_store = safe.Store(settings.SAFE_RETRY_DB_PATH)
        safe._manager = safe.Manager(safe_store, safe.Config(base_delay=0, jitter=False))

        main = load("probe_main", ROOT / "main.py")
        main.whitelist.append("testclient")

        created_orders = {}
        order_lock = threading.Lock()

        def get_bot(*args):
            req_info = schema.OrderRequest(
                exchange="BINANCE", base="BTC", quote="USDT", side="buy", amount=0.01
            )
            bot = bot_for("BINANCE", req_info)
            orig_create = bot.client.create_order

            def create_order(*c_args):
                res = orig_create(*c_args)
                with order_lock:
                    res["id"] = str(len(created_orders) + 1)
                    res["symbol"] = "BTC/USDT"
                    created_orders[res["id"]] = res.copy()
                return res

            bot.client.create_order = create_order
            return bot

        main.get_bot = get_bot

        # ---------------------------------------------------------------------
        # Scenario A: Webhook Processing
        # 1. 100 concurrent requests with identical signal_id -> Deduplication
        # 2. 13 concurrent requests for 13 distinct strategies
        # 3. Mixed valid & malformed requests
        # ---------------------------------------------------------------------
        print("\n[Scenario A] Webhook Processing & Concurrency")

        def parallel_posts(payloads):
            gate = threading.Barrier(len(payloads))
            def worker(data):
                client = TestClient(main.app, base_url="http://127.0.0.1")
                try:
                    gate.wait(timeout=10)
                    t0 = time.perf_counter()
                    resp = client.post("/order", json=data)
                    elapsed_ms = (time.perf_counter() - t0) * 1000
                    return resp.status_code, elapsed_ms
                finally:
                    client.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(payloads)) as pool:
                return list(pool.map(worker, payloads))

        # A-1. 100 requests with identical signal_id
        duplicate_payloads = [
            {
                "password": "test-only",
                "exchange": "BINANCE",
                "base": "BTC",
                "quote": "USDT",
                "side": "buy",
                "amount": 0.01,
                "price": 100,
                "signal_id": "probe-dedup-signal-100",
            }
            for _ in range(100)
        ]
        t_a1_start = time.perf_counter()
        resp_100 = parallel_posts(duplicate_payloads)
        t_a1_dur = time.perf_counter() - t_a1_start
        latencies_100 = sorted(r[1] for r in resp_100)
        p50_100 = round(latencies_100[49], 2)
        p95_100 = round(latencies_100[94], 2)
        p99_100 = round(latencies_100[98], 2)

        dedup_pass = (len(created_orders) == 1)
        print(f"  A-1. 100 duplicate requests -> Created orders: {len(created_orders)} (Expected 1). Pass: {dedup_pass}")
        print(f"       Latencies (ms): p50={p50_100}, p95={p95_100}, p99={p99_100}. Total time: {t_a1_dur:.3f}s")

        results["scenario_a_dedup_100"] = {
            "requests": 100,
            "orders_created": len(created_orders),
            "pass": dedup_pass,
            "latency_ms": {"p50": p50_100, "p95": p95_100, "p99": p99_100},
            "duration_sec": round(t_a1_dur, 3),
        }

        # A-2. 13 distinct strategies concurrent requests
        orders_before_13 = len(created_orders)
        strategy_payloads = [
            {
                "password": "test-only",
                "exchange": "BINANCE",
                "base": "BTC",
                "quote": "USDT",
                "side": "buy",
                "amount": 0.01,
                "price": 100,
                "signal_id": f"strategy-signal-{i}",
                "strategy_id": f"strategy-{i}",
            }
            for i in range(13)
        ]
        t_a2_start = time.perf_counter()
        resp_13 = parallel_posts(strategy_payloads)
        t_a2_dur = time.perf_counter() - t_a2_start
        latencies_13 = sorted(r[1] for r in resp_13)
        new_orders = len(created_orders) - orders_before_13
        p50_13 = round(statistics.median(latencies_13), 2)
        p95_13 = round(latencies_13[-1], 2)
        strat_pass = (new_orders == 13)
        print(f"  A-2. 13 distinct strategies -> Created orders: {new_orders} (Expected 13). Pass: {strat_pass}")
        print(f"       Latencies (ms): p50={p50_13}, p95={p95_13}. Total time: {t_a2_dur:.3f}s")

        results["scenario_a_strategies_13"] = {
            "strategies": 13,
            "orders_created": new_orders,
            "pass": strat_pass,
            "latency_ms": {"p50": p50_13, "p95": p95_13},
            "duration_sec": round(t_a2_dur, 3),
        }

        # A-3. Mixed valid & invalid requests
        mixed_payloads = [
            {"password": "test-only", "exchange": "BINANCE", "base": "ETH", "quote": "USDT", "side": "buy", "amount": 0.1, "signal_id": "valid-1"},
            {"password": "test-only", "exchange": "BYBIT", "base": "SOL", "quote": "USDT", "side": "buy", "amount": 1.0, "signal_id": "valid-2"},
            {"password": "test-only", "exchange": "OKX", "base": "BTC", "quote": "USDT", "side": "buy", "amount": 0.05, "signal_id": "valid-3"},
            {"password": "wrong-pwd", "exchange": "BINANCE", "base": "BTC", "quote": "USDT", "side": "buy", "amount": 0.01}, # wrong pwd -> 422
            {"password": "test-only", "exchange": "BINANCE", "base": "BTC", "quote": "USDT", "side": "buy", "amount": "invalid-float"}, # invalid amount -> 422
        ]
        resp_mixed = parallel_posts(mixed_payloads)
        statuses = [r[0] for r in resp_mixed]
        mixed_pass = (statuses == [200, 200, 200, 422, 422])
        print(f"  A-3. Mixed requests statuses: {statuses}. Pass: {mixed_pass}")
        results["scenario_a_mixed"] = {"statuses": statuses, "pass": mixed_pass}

        # ---------------------------------------------------------------------
        # Scenario B: CCXT Reconciliation & Lease Shutdown Concurrency
        # ---------------------------------------------------------------------
        print("\n[Scenario B] CCXT Reconciliation & Lease Management")
        import exchange.safe_retry as safe_module
        clients_pool = safe_module.ReconciliationClients(settings)
        key = ("BINANCE", "swap")
        mock_ccxt = Mock(spec=["close"])
        clients_pool.clients[clients_pool._normalize_key(key)] = mock_ccxt
        
        # Acquire 13 leases concurrently
        barrier_lease = threading.Barrier(13)
        def acquire_worker(idx):
            barrier_lease.wait(timeout=5)
            return clients_pool.acquire_lease(key)

        with concurrent.futures.ThreadPoolExecutor(max_workers=13) as pool:
            active_leases = list(pool.map(acquire_worker, range(13)))

        # Attempt safe_deferred_close while 13 leases are active -> close count must be 0!
        clients_pool.safe_deferred_close(key)
        close_calls_during_active = mock_ccxt.close.call_count
        active_protect_pass = (close_calls_during_active == 0)
        print(f"  B-1. safe_deferred_close during 13 active leases: close() calls = {close_calls_during_active} (Expected 0). Pass: {active_protect_pass}")

        # Return 12 leases -> close count must still be 0
        for l in active_leases[:-1]:
            l.release()
        clients_pool.safe_deferred_close(key)
        close_calls_before_last = mock_ccxt.close.call_count
        twelve_released_pass = (close_calls_before_last == 0)
        print(f"  B-2. After releasing 12/13 leases: close() calls = {close_calls_before_last} (Expected 0). Pass: {twelve_released_pass}")

        # Return final lease -> triggers deferred close exactly once!
        active_leases[-1].release()
        # 13 concurrent safe_deferred_close calls
        barrier_close = threading.Barrier(13)
        def close_worker(idx):
            barrier_close.wait(timeout=5)
            clients_pool.safe_deferred_close(key)

        with concurrent.futures.ThreadPoolExecutor(max_workers=13) as pool:
            list(pool.map(close_worker, range(13)))

        total_close_calls = mock_ccxt.close.call_count
        single_close_pass = (total_close_calls == 1)
        print(f"  B-3. After last lease released + 13 concurrent close() calls: close() calls = {total_close_calls} (Expected 1). Pass: {single_close_pass}")

        results["scenario_b_ccxt_lease"] = {
            "active_leases_tested": 13,
            "close_during_active": close_calls_during_active,
            "close_after_final_lease": total_close_calls,
            "pass": active_protect_pass and twelve_released_pass and single_close_pass,
        }

        # ---------------------------------------------------------------------
        # Scenario C: Discord Outbox & SQLite WAL Concurrency
        # ---------------------------------------------------------------------
        print("\n[Scenario C] Discord Outbox & SQLite WAL Concurrency")
        outbox_store = NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)

        # Enqueue 13 notifications
        for i in range(13):
            payload_item = NotificationPayload(
                event_id=f"evt-{i}",
                exchange="BINANCE",
                symbol="BTC/USDT",
                side="buy",
                order_name=f"strat-{i}",
                exit_reason="UNKNOWN",
                pnl_result="PROFIT",
                fill_status="FILLED",
                event_timestamp=time.time(),
            )
            outbox_store.enqueue(payload_item)

        # Simulated Discord Failure (HTTP 500)
        with patch("requests.post", return_value=Mock(status_code=500)):
            flush_outbox(settings)

        # Verify items remain PENDING in outbox
        with outbox_store._connect_read() as c:
            pending_count = c.execute("SELECT COUNT(*) FROM verified_outbox WHERE status='PENDING'").fetchone()[0]
            journal_mode = c.execute("PRAGMA journal_mode").fetchone()[0]

        # Reset retry delay and simulate Discord Recovery (HTTP 200)
        with outbox_store._connect() as c:
            c.execute("UPDATE verified_outbox SET next_retry_at = 0")

        ok_resp = Mock(status_code=200)
        ok_resp.json.return_value = {"id": "mock-discord-id"}
        with patch("requests.post", return_value=ok_resp):
            flush_outbox(settings)

        with outbox_store._connect_read() as c:
            sent_count = c.execute("SELECT COUNT(*) FROM verified_outbox WHERE status='SENT'").fetchone()[0]

        sqlite_outbox_pass = (pending_count == 13 and sent_count == 13)
        print(f"  C-1. Outbox: Pending after error = {pending_count}, Sent after recovery = {sent_count}, Journal = {journal_mode}. Pass: {sqlite_outbox_pass}")

        # C-2. SQLite WAL concurrent read/write test (20 concurrent threads)
        wal_errors = []
        barrier_wal = threading.Barrier(20)
        def wal_worker(t_id):
            try:
                barrier_wal.wait(timeout=5)
                p = NotificationPayload(
                    event_id=f"wal-probe-{t_id}",
                    exchange="BINANCE",
                    symbol="BTC/USDT",
                    side="buy",
                    order_name=f"wal-{t_id}",
                    exit_reason="UNKNOWN",
                    pnl_result="UNKNOWN",
                    fill_status="FILLED",
                    event_timestamp=time.time(),
                )
                outbox_store.enqueue(p)
                with outbox_store._connect_read() as conn:
                    res = conn.execute("SELECT COUNT(*) FROM verified_outbox").fetchone()[0]
            except Exception as exc:
                wal_errors.append(str(exc))

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(wal_worker, range(20)))

        wal_pass = (len(wal_errors) == 0)
        print(f"  C-2. SQLite WAL 20 concurrent read/write threads: Errors = {len(wal_errors)}. Pass: {wal_pass}")

        results["scenario_c_discord_sqlite"] = {
            "pending_on_failure": pending_count,
            "sent_on_recovery": sent_count,
            "journal_mode": journal_mode,
            "wal_concurrency_errors": len(wal_errors),
            "pass": sqlite_outbox_pass and wal_pass,
        }

        # ---------------------------------------------------------------------
        # Scenario D: Resource & Memory Final Measurement
        # ---------------------------------------------------------------------
        print("\n[Scenario D] Resource & Memory Measurement")
        peak_rss = get_process_rss_mb()
        active_threads = threading.active_count()
        total_duration = time.perf_counter() - start_time
        print(f"  Initial RSS: {initial_rss} MB")
        print(f"  Peak/Current RSS: {peak_rss} MB (Delta: {peak_rss - initial_rss:.2f} MB)")
        print(f"  Active Threads: {active_threads}")
        print(f"  Total Duration: {total_duration:.3f} s")

        results["scenario_d_resources"] = {
            "initial_rss_mb": initial_rss,
            "current_rss_mb": peak_rss,
            "rss_delta_mb": round(peak_rss - initial_rss, 2),
            "active_threads": active_threads,
            "duration_sec": round(total_duration, 3),
            "vultr_512mb_estimated_headroom_mb": round(512 - peak_rss, 2),
        }

    # Output JSON summary
    out_file = ROOT / "load_test_results.json"
    out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nResults successfully written to: {out_file.name}")
    print("=== MOCK LOAD & STRESS PROBE COMPLETED ===")

if __name__ == "__main__":
    run_load_probe()
