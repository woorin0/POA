import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock
from harness import ROOT, setup, bot_for, load

schema, legacy, util = setup()
from exchange.safe_retry import Store, Manager, Config, Adapter, SafetyError, classify, CLIENT_KEYS
import ccxt


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "orders.db")
        self.events = []
        self.now = 1000
        self.manager = Manager(self.store, Config(base_delay=0, jitter=False),
                               self.events.append, lambda: self.now, lambda _: None)
        self.info = schema.OrderRequest(exchange="BINANCE", base="BTC", quote="USDT",
                                        side="buy", amount=.01, signal_id="signal-1", price=100)
        self.bot = bot_for("BINANCE", self.info)
        self.adapter = Adapter(self.bot, self.info)
        self.args = ("BTC/USDT", "market", "buy", .01, None, {})

    def run_order(self, func=None):
        return self.manager.execute(func or self.bot.client.create_order, self.args, self.info, self.adapter)

    def rows(self):
        with closing(sqlite3.connect(self.store.path)) as con:
            return [json.loads(r[0]) for r in con.execute("SELECT data FROM safe_orders")]

    def test_success_and_client_ids_all_exchanges(self):
        for ex, key in CLIENT_KEYS.items():
            self.info.exchange = ex
            self.adapter.exchange = ex
            result = self.run_order()
            self.assertEqual(result["filled"], .01)
            cid = self.bot.client.calls[-1][1][5][key]
            self.assertRegex(cid, r"^[a-zA-Z0-9]{32}$")
        self.assertEqual(len(self.rows()), 3)

    def test_confirmed_rejection_rate_retry(self):
        for ex, body in [("BINANCE", '{"code":-1003}'), ("BYBIT", '{"retCode":10006}'), ("OKX", '{"code":"50011"}')]:
            self.info.exchange = self.adapter.exchange = ex
            submit = Mock(side_effect=[ccxt.RateLimitExceeded(body), {"id":"1", "status":"closed", "filled":.01}])
            self.run_order(submit)
            self.assertEqual(submit.call_count, 2)
            self.assertEqual(submit.call_args_list[0].args[5], submit.call_args_list[1].args[5])

    def test_timestamp_repair(self):
        self.bot.client.load_time_difference = Mock()
        submit = Mock(side_effect=[ccxt.InvalidNonce('{"code":-1021}'), {"id":"1", "status":"open", "filled":0}])
        self.run_order(submit)
        self.bot.client.load_time_difference.assert_called_once()
        self.assertEqual(submit.call_count, 2)

    def test_precision_repair_only_down(self):
        self.args = (*self.args[:3], .0109, *self.args[4:])
        submit = Mock(side_effect=[ccxt.InvalidOrder('{"code":-1111}'), {"id":"1", "status":"closed", "filled":.01}])
        self.run_order(submit)
        self.assertEqual(submit.call_args.args[3], .01)
        self.assertEqual(self.rows()[0]["remaining"], "0.0009")

    def test_timeout_filled_reconciles_without_resend(self):
        def submit(*args):
            self.bot.client.fetch_order = Mock(return_value={"id":"1", "clientOrderId":args[5]["newClientOrderId"], "status":"closed", "filled":.01})
            raise ccxt.RequestTimeout("response lost")
        call = Mock(side_effect=submit)
        self.assertEqual(self.run_order(call)["id"], "1")
        self.assertEqual(call.call_count, 1)

    def test_response_loss_open_order_blocks_new_signal(self):
        def submit(*args):
            self.bot.client.fetch_open_orders = Mock(return_value=[{"id":"1", "clientOrderId":args[5]["newClientOrderId"], "status":"open", "filled":0}])
            raise ConnectionResetError()
        self.run_order(submit)
        self.info.signal_id = "different"
        with self.assertRaises(SafetyError):
            self.run_order()

    def test_not_found_is_unknown(self):
        self.bot.client.fetch_order = Mock(side_effect=ccxt.OrderNotFound())
        self.bot.client.fetch_open_orders = Mock(return_value=[])
        self.bot.client.fetch_closed_orders = Mock(return_value=[])
        self.bot.client.fetch_my_trades = Mock(return_value=[])
        submit = Mock(side_effect=ccxt.RequestTimeout())
        with self.assertRaises(SafetyError): self.run_order(submit)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.rows()[0]["state"], "UNKNOWN")
        self.bot.client.fetch_my_trades.assert_called_once()

    def test_fatal_errors_no_retry(self):
        for i, exc in enumerate([ccxt.InsufficientFunds(), ccxt.AuthenticationError(), ccxt.PermissionDenied(), ccxt.InvalidOrder('{"code":-4061}')]):
            self.info.signal_id = str(i)
            submit = Mock(side_effect=exc)
            with self.assertRaises(SafetyError): self.run_order(submit)
            self.assertEqual(submit.call_count, 1)

    def test_limit_open_remains_accepted(self):
        self.args = (self.args[0], "limit", "buy", .01, 100, {})
        self.run_order(Mock(return_value={"id":"1", "status":"open", "filled":0}))
        self.assertEqual(self.rows()[0]["state"], "ACCEPTED")
        self.assertEqual(len(self.store.pending()), 1)

    def test_partial_fill_remaining_no_full_resubmit(self):
        self.run_order(Mock(return_value={"id":"1", "status":"open", "filled":.004}))
        self.assertEqual(self.rows()[0]["remaining"], "0.006")
        with self.assertRaises(SafetyError): self.run_order()
        self.assertEqual(len(self.bot.client.calls), 0)

    def test_duplicate_even_after_filled(self):
        self.run_order()
        with self.assertRaises(SafetyError): self.run_order()
        self.assertEqual(len(self.bot.client.calls), 1)

    def test_concurrent_duplicate(self):
        def send(_):
            try: self.run_order(); return True
            except SafetyError: return False
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(send, range(16)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.bot.client.calls), 1)

    def test_retry_limit(self):
        submit = Mock(side_effect=ccxt.RateLimitExceeded('{"code":-1003}'))
        with self.assertRaises(SafetyError): self.run_order(submit)
        self.assertEqual(submit.call_count, 3)

    def test_ttl_during_retry(self):
        self.manager.sleep = lambda _: setattr(self, "now", 1031)
        submit = Mock(side_effect=ccxt.RateLimitExceeded('{"code":-1003}'))
        with self.assertRaises(SafetyError): self.run_order(submit)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.rows()[0]["state"], "EXPIRED")

    def test_price_during_retry(self):
        self.manager.sleep = lambda _: setattr(self.bot.client, "fetch_ticker", lambda _: {"last":101})
        submit = Mock(side_effect=ccxt.RateLimitExceeded('{"code":-1003}'))
        with self.assertRaises(SafetyError): self.run_order(submit)
        self.assertEqual(submit.call_count, 1)

    def test_crash_after_api_before_ack(self):
        submit = Mock(side_effect=SystemExit())
        with self.assertRaises(SystemExit): self.run_order(submit)
        self.assertEqual(self.rows()[0]["state"], "SUBMITTING")
        Store(self.store.path).recover()
        self.assertEqual(self.rows()[0]["state"], "UNKNOWN")
        with self.assertRaises(SafetyError): self.run_order()

    def test_crash_after_db_before_api(self):
        save = self.store.save
        def crash(row, state, **kwargs):
            save(row, state, **kwargs)
            if state == "SUBMITTING": raise SystemExit()
        self.store.save = crash
        with self.assertRaises(SystemExit): self.run_order()
        Store(self.store.path).recover()
        self.assertEqual(self.rows()[0]["state"], "UNKNOWN")
        self.assertEqual(self.bot.client.calls, [])

    def test_late_fill_during_cancellation(self):
        self.run_order(Mock(return_value={"id":"1", "status":"open", "filled":.004}))
        row = self.store.pending()[0]
        self.manager.record(row, {"id":"1", "status":"canceled", "filled":.007})
        self.assertEqual(self.rows()[0]["remaining"], "0.003")
        with self.assertRaises(SafetyError): self.run_order()

    def test_already_closed_position_and_excess_close(self):
        self.info.is_close, self.info.is_futures, self.info.is_buy = True, True, False
        self.args = ("BTC/USDT:USDT", "market", "sell", .01, None, {"reduceOnly":True})
        self.bot.client.fetch_positions = Mock(return_value=[])
        with self.assertRaises(SafetyError): self.run_order()
        self.assertEqual(self.bot.client.calls, [])

    def test_close_without_reduce_only_blocked(self):
        self.info.is_close = True
        with self.assertRaises(SafetyError): self.run_order()
        self.assertEqual(self.bot.client.calls, [])

    def test_discord_failure_isolated(self):
        self.manager.alert = Mock(side_effect=RuntimeError())
        self.run_order()
        self.assertEqual(self.rows()[0]["state"], "FILLED")

    def test_db_failure_before_send(self):
        self.store.save = Mock(side_effect=sqlite3.OperationalError())
        with self.assertRaises(sqlite3.OperationalError): self.run_order()
        self.assertEqual(self.bot.client.calls, [])

    def test_db_failure_after_send_prevents_replay(self):
        save = self.store.save
        def fail(row, state, **kw):
            if state == "FILLED": raise sqlite3.OperationalError()
            return save(row, state, **kw)
        self.store.save = fail
        with self.assertRaises(sqlite3.OperationalError): self.run_order()
        with self.assertRaises(SafetyError): self.run_order()
        self.assertEqual(len(self.bot.client.calls), 1)

    def test_same_amount_different_signals(self):
        self.run_order()
        self.info.signal_id = "second"
        self.run_order()
        self.assertEqual(len(self.bot.client.calls), 2)

    def test_missing_id_never_retries(self):
        self.info.signal_id = None
        submit = Mock(side_effect=ccxt.RateLimitExceeded('{"code":-1003}'))
        with self.assertRaises(SafetyError): self.run_order(submit)
        self.assertEqual(submit.call_count, 1)

    def test_unknown_500_and_generic_429(self):
        for message in ["HTTP 500", "HTTP 429", "Internal error"]:
            self.assertEqual(classify("BINANCE", Exception(message)), "unknown")

    def test_invalid_ack_blocks(self):
        with self.assertRaises(SafetyError): self.run_order(Mock(return_value={}))
        self.assertEqual(self.rows()[0]["state"], "UNKNOWN")

    def test_backup_and_migration_repeat(self):
        self.run_order()
        backup = Path(self.tmp.name) / "backup.db"
        self.store.backup(backup)
        restored = Store(backup)
        with restored.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM safe_orders").fetchone()[0], 1)
        self.assertEqual(restored.pending(), [])

    def test_two_stores_atomic_claim(self):
        self.run_order(Mock(return_value={"id":"1", "status":"open", "filled":0}))
        row = self.rows()[0]
        other = Store(self.store.path)
        self.assertFalse(other.claim(row)[1])
        with self.assertRaises(SafetyError): other.claim(dict(row, signal="other"))

    def test_illegal_state_transition(self):
        self.run_order()
        with self.assertRaises(SafetyError): self.store.save(self.rows()[0], "SUBMITTING")

    def test_no_secrets_persisted(self):
        self.run_order()
        text = json.dumps(self.rows())
        for word in ["password", "apiKey", "secret", "test-only"]:
            self.assertNotIn(word, text)

    def test_disabled_ignores_store(self):
        self.assertFalse(util.settings.SAFE_RETRY_ENABLED)
        submit = Mock(return_value={"id":"legacy"})
        submit.__name__ = "create_order"
        result = legacy.retry(submit, *self.args, order_info=self.info, instance=self.bot)
        self.assertEqual(result, {"id":"legacy"})

    def test_enabled_route_integration_and_duplicate(self):
        import exchange.safe_retry as safe
        from fastapi.testclient import TestClient
        previous = safe._manager
        safe._manager = self.manager
        util.settings.SAFE_RETRY_ENABLED = True
        self.addCleanup(setattr, safe, "_manager", previous)
        self.addCleanup(setattr, util.settings, "SAFE_RETRY_ENABLED", False)
        main = load("integration_main", ROOT / "main.py")
        main.whitelist.append("testclient")
        main.get_bot = lambda *args: self.bot
        client = TestClient(main.app)
        payload = dict(password="test-only",exchange="BINANCE",base="BTC",quote="USDT",side="buy",amount=.01,signal_id="route")
        self.assertEqual(client.post("/order",json=payload).json(), {"result":"success"})
        self.assertIsNone(client.post("/order",json=payload).json())
        self.assertEqual(len(self.bot.client.calls), 1)

    def test_dependencies_and_exclusive_worker(self):
        import exchange.safe_retry as safe
        prior = safe._manager
        safe._manager = None
        settings = schema.Settings(_env_file=None,PASSWORD="test-only",SAFE_RETRY_ENABLED=True,
                                   SAFE_RETRY_DB_PATH=str(Path(self.tmp.name)/"exclusive.db"))
        with self.assertRaises(SafetyError): safe.get_manager(settings)
        settings.ORDER_RECONCILIATION_ENABLED = settings.PERSIST_ORDER_STATE = settings.DUPLICATE_SIGNAL_PROTECTION = True
        try:
            safe.get_manager(settings)
            code = "from harness import setup; s,_,_=setup(); from exchange.safe_retry import get_manager; import sys; c=s.Settings(_env_file=None,PASSWORD='test-only',SAFE_RETRY_ENABLED=True,ORDER_RECONCILIATION_ENABLED=True,PERSIST_ORDER_STATE=True,DUPLICATE_SIGNAL_PROTECTION=True,SAFE_RETRY_DB_PATH=sys.argv[1]); get_manager(c)"
            child = subprocess.run([sys.executable,"-c",code,settings.SAFE_RETRY_DB_PATH],capture_output=True)
            self.assertNotEqual(child.returncode, 0)
        finally:
            if safe._lock_file:
                safe._lock_file.close()
                safe._lock_file = None
            safe._manager = prior

    def test_real_process_exit_after_submission_intent(self):
        code = """from harness import setup,bot_for
import os,sys
s,_,_=setup()
from exchange.safe_retry import Store,Manager,Adapter
i=s.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=.01,signal_id='crash')
b=bot_for('BINANCE',i)
m=Manager(Store(sys.argv[1]))
m.execute(lambda *a: os._exit(17),('BTC/USDT','market','buy',.01,None,{}),i,Adapter(b,i))
"""
        child = subprocess.run([sys.executable,"-c",code,self.store.path],capture_output=True)
        self.assertEqual(child.returncode, 17, child.stderr)
        self.assertEqual(self.rows()[0]["state"], "SUBMITTING")
        self.store.recover()
        self.assertEqual(self.rows()[0]["state"], "UNKNOWN")

    def test_admin_rollback_gate(self):
        self.run_order(Mock(return_value={"id":"1","status":"open","filled":0}))
        cmd = [sys.executable,str(ROOT / "tools/safe_order_admin.py"),"rollback-check",self.store.path]
        self.assertEqual(subprocess.run(cmd,capture_output=True).returncode, 2)
        row = self.store.pending()[0]
        self.manager.record(row, {"id":"1","status":"closed","filled":.01})
        self.assertEqual(subprocess.run(cmd,capture_output=True).returncode, 0)

    def test_close_quantity_exceeds_position(self):
        self.info.is_close, self.info.is_futures, self.info.is_buy = True, True, False
        self.args = ("BTC/USDT:USDT", "market", "sell", 11, None, {"reduceOnly":True})
        with self.assertRaises(SafetyError): self.run_order()
        self.assertEqual(self.bot.client.calls, [])

    def test_unverified_terminal_fill_remains_unknown(self):
        with self.assertRaises(SafetyError): self.run_order(Mock(return_value={"id":"1","status":"closed"}))
        self.assertEqual(self.rows()[0]["state"], "UNKNOWN")

    def test_enabled_retry_real_exchange_method(self):
        import exchange.safe_retry as safe
        prior = safe._manager
        safe._manager = self.manager
        util.settings.SAFE_RETRY_ENABLED = True
        try:
            original = self.bot.client.create_order
            calls = []
            def create_order(*args):
                calls.append(args)
                if len(calls) == 1: raise ccxt.RateLimitExceeded('{"code":-1003}')
                return original(*args)
            self.bot.client.create_order = create_order
            self.bot.market_buy(self.info)
            self.assertEqual(len(calls), 2)
            self.assertIn("newClientOrderId", calls[1][5])
        finally:
            safe._manager = prior
            util.settings.SAFE_RETRY_ENABLED = False

    def test_existing_database_backed_up_before_schema_addition(self):
        path = Path(self.tmp.name) / "existing.db"
        with closing(sqlite3.connect(path)) as con:
            con.execute("CREATE TABLE legacy (value TEXT)")
            con.execute("INSERT INTO legacy VALUES ('retained')")
            con.commit()
        Store(path)
        backups = list(Path(self.tmp.name).glob("existing.db.pre-safe-*.bak"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as con:
            self.assertEqual(con.execute("SELECT value FROM legacy").fetchone()[0], "retained")
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='safe_orders'").fetchone())
        with closing(sqlite3.connect(path)) as con:
            self.assertEqual(con.execute("SELECT value FROM legacy").fetchone()[0], "retained")

    def test_balance_revalidated_after_rejection(self):
        self.manager.sleep = lambda _: setattr(self.bot.client,"fetch_free_balance",lambda: {"USDT":0})
        submit = Mock(side_effect=ccxt.RateLimitExceeded('{"code":-1003}'))
        with self.assertRaises(SafetyError): self.run_order(submit)
        self.assertEqual(submit.call_count, 1)

    def test_reconciliation_conflicting_observations_hold(self):
        def submit(*args):
            record = {"id":"1","clientOrderId":args[5]["newClientOrderId"],"filled":.004,"status":"open"}
            self.bot.client.fetch_order = Mock(return_value=record)
            self.bot.client.fetch_closed_orders = Mock(return_value=[dict(record,status="canceled",filled=.007)])
            raise ccxt.RequestTimeout()
        with self.assertRaises(SafetyError): self.run_order(submit)
        self.assertEqual(self.rows()[0]["state"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
