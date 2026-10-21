import json
import tempfile
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from harness import setup, ROOT, load, bot_for
s,_,util=setup()
from exchange.verified_pnl.verifier import FillVerifier
from exchange.verified_pnl.models import *
from exchange.verified_pnl.pnl_calculator import PnlCalculator
from exchange.verified_pnl.exit_reason import classify_exit_reason,classify_pnl_result
from exchange.verified_pnl.outbox import NotificationOutboxStore,DiscordSender,format_kst
from exchange.verified_pnl.dispatcher import dispatch_verified_alert,verification_tick,flush_outbox
from exchange.verified_pnl.ledger import allocate


class Integrity(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings=s.Settings(_env_file=None,PASSWORD='test-only',VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,DISCORD_OUTBOX_ENABLED=True,DISCORD_RETRY_ENABLED=True,
            NET_REALIZED_PNL_ENABLED=True,EXIT_REASON_CLASSIFICATION_V2=True,
            VERIFIED_NOTIFICATIONS_DB_PATH=str(Path(self.tmp.name)/'alerts.db'),
            DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/test/mock')
        self.store=NotificationOutboxStore(self.settings.VERIFIED_NOTIFICATIONS_DB_PATH)
        self.info=s.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT.P',side='close/sell',amount=.01,exit_type='TP')

    def fill(self,**kw):
        return VerifiedFill(exchange='BINANCE',symbol='BTC/USDT:USDT',order_id='o',market_type='linear',position_side='LONG',
            status=FillStatus.FILLED,filled_quantity=D('.01'),average_fill_price=D('102000'),
            fees={'USDT':D('.51')},fees_complete=True,verification_status=VerificationStatus.VERIFIED,**kw)

    def calculate(self,fill=None,**kw):
        return PnlCalculator().calculate_pnl(fill or self.fill(),self.info,entry_price=D('100000'),**kw)

    def payload(self,key='e'):
        return NotificationPayload(event_id=key,exchange='BINANCE',symbol='BTC/USDT',side='롱 종료',order_name='주문',
            exit_reason=ExitReason.TP,pnl_result=PnlResult.UNKNOWN,fill_status=FillStatus.PENDING,event_timestamp=time.time())

    def status(self,key='e'):
        with self.store._connect() as c:
            return c.execute('SELECT status FROM verified_outbox WHERE event_id=?',(key,)).fetchone()[0]

    def test_missing_funding_is_unknown(self):
        result=self.calculate(allocated_entry_fee=D('.5'))
        self.assertIsNone(result.net_realized_pnl)
        self.assertEqual(classify_pnl_result(result),PnlResult.UNKNOWN)

    def test_missing_entry_fee_is_unknown(self):
        self.assertIsNone(self.calculate(allocated_funding=D(0)).net_realized_pnl)

    def test_missing_exit_fee_is_unknown(self):
        fill=self.fill(); fill.fees={}; fill.fees_complete=False
        self.assertIsNone(self.calculate(fill,allocated_entry_fee=D(0),allocated_funding=D(0)).net_realized_pnl)

    def test_currency_mismatch_not_added(self):
        fill=self.fill(); fill.fees['BNB']=D('.1')
        self.assertIsNone(self.calculate(fill,allocated_entry_fee=D(0),allocated_funding=D(0)).net_realized_pnl)

    def test_funding_paid_and_received(self):
        for funding,expected in [(D('-1'),D('17.99')),(D('1'),D('19.99'))]:
            self.assertEqual(self.calculate(allocated_entry_fee=D('.5'),allocated_funding=funding).net_realized_pnl,expected)

    def test_already_included_costs_not_double_counted(self):
        fill=self.fill(accounting_basis='net',realized_pnl=D('18.99'),pnl_includes_entry_fee=True,pnl_includes_exit_fee=True,pnl_includes_funding=True)
        self.assertEqual(self.calculate(fill,allocated_entry_fee=D('50'),allocated_funding=D('10')).net_realized_pnl,D('18.99'))

    def test_closed_price_fallback_forbidden(self):
        fill=FillVerifier().verify_order_result(None,'BINANCE','BTC/USDT:USDT',{'id':'o','status':'closed','filled':.01,'price':999999,'close_price':999999},self.info)
        self.assertIsNone(fill.average_fill_price)
        self.assertNotEqual(fill.status,FillStatus.FILLED)

    def test_zero_filled_cannot_complete(self):
        fill=FillVerifier().verify_order_result(None,'BINANCE','BTC/USDT:USDT',{'id':'o','status':'closed','filled':0,'average':100},self.info)
        self.assertNotEqual(fill.status,FillStatus.FILLED)

    def test_deduplicate_trades_and_fee_not_twice(self):
        t={'id':'t','order':'o','price':100,'amount':.01,'fee':{'cost':0,'currency':'USDT'}}
        fill=FillVerifier().verify_order_result(None,'BINANCE','BTC/USDT:USDT',{'id':'o','status':'closed','filled':.01,'trades':[t,t],'fee':{'cost':1,'currency':'USDT'}},self.info)
        self.assertEqual(fill.filled_quantity,D('.01'))
        self.assertEqual(fill.fees,{'USDT':D(0)})
        self.assertTrue(fill.fees_complete)

    def test_okx_all_quantities_in_base(self):
        self.info.contract_size=.01
        fill=FillVerifier().verify_order_result(None,'OKX','BTC/USDT:USDT',{'id':'o','status':'open','amount':10,'filled':4,'remaining':6,'average':100},self.info)
        self.assertEqual((fill.requested_quantity,fill.filled_quantity,fill.remaining_quantity),(D('.1'),D('.04'),D('.06')))

    def test_okx_unknown_contract_never_assume_one(self):
        fill=FillVerifier().verify_order_result(None,'OKX','BTC/USDT:USDT',{'id':'o','status':'closed','filled':4,'average':100},self.info)
        self.assertEqual(fill.verification_status,VerificationStatus.UNVERIFIED)

    def test_missing_order_id_never_matches_account_trades(self):
        client=Mock()
        fill=FillVerifier().verify_order_result(NS(client=client),'BINANCE','BTC/USDT:USDT',None,self.info)
        client.fetch_my_trades.assert_not_called()
        self.assertEqual(fill.status,FillStatus.UNVERIFIED)

    def test_trades_without_terminal_order_are_partial(self):
        client=Mock();client.fetch_order.side_effect=RuntimeError()
        client.fetch_my_trades.return_value=[{'id':'t','order':'o','price':100,'amount':.01}]
        fill=FillVerifier().verify_order_result(NS(client=client),'BINANCE','BTC/USDT:USDT',{'id':'o'},self.info)
        self.assertEqual(fill.status,FillStatus.PARTIALLY_FILLED)

    def test_native_gross_semantics(self):
        for ex,field in [('BINANCE','realizedPnl'),('OKX','fillPnl')]:
            self.info.contract_size=1
            fill=FillVerifier().verify_order_result(None,ex,'BTC/USDT:USDT',{'id':'o','status':'closed','filled':.01,
                'trades':[{'id':'t','price':100,'amount':.01,'info':{field:'2'},'fee':{'currency':'USDT','cost':'.1'}}]},self.info)
            self.assertEqual(fill.realized_pnl,D(2));self.assertEqual(fill.accounting_basis,'gross')

    def test_exit_reason_matrix(self):
        for reason,net,outcome in [('TP',1,'PROFIT'),('TP',-1,'LOSS'),('SL',-1,'LOSS'),('SL',1,'PROFIT'),('TRAIL',1,'PROFIT'),('BE',-1,'LOSS'),('TIME',1,'PROFIT'),('MANUAL',None,'UNKNOWN')]:
            self.info.exit_type=reason
            self.assertEqual(classify_exit_reason(self.info).value,reason)
            self.assertEqual(classify_pnl_result(PnlCalculationResult(net_realized_pnl=net)).value,outcome)

    def test_stop_market_does_not_prove_strategy_intent(self):
        self.info.exit_type='TRAIL'
        self.assertEqual(classify_exit_reason(self.info,{'type':'STOP_MARKET'}),ExitReason.TRAIL)
        self.info.exit_type=None;self.info.order_name='best strategy'
        self.assertEqual(classify_exit_reason(self.info),ExitReason.UNKNOWN)

    def test_ledger_split_entry_and_three_closes(self):
        def record(oid,tid,qty,price,fee,stamp,close):
            info=NS(is_close=close,strategy_id='one')
            fill=VerifiedFill(exchange='BINANCE',symbol='BTC/USDT:USDT',market_type='linear',position_side='LONG',order_id=oid,
                filled_quantity=D(qty),pnl_currency='USDT',trades=[VerifiedTrade(id=tid,price=D(price),amount=D(qty),fee=D(fee),fee_currency='USDT',timestamp=stamp)])
            return allocate(self.store,fill,info,'acct',True)
        record('e1','t1','.005','100000','.5',1,False)
        record('e2','t2','.005','102000','.5',2,False)
        fees=[]
        for index,qty in enumerate(['.004','.003','.003']):
            avg,fee=record('c'+str(index),'c'+str(index),qty,'103000','.2',3+index,True)
            self.assertEqual(avg,D('101000'));fees.append(fee)
        self.assertEqual(fees,[D('.4'),D('.3'),D('.3')])
        avg,fee=record('c2','c2','.003','103000','.2',5,True)
        self.assertEqual(fee,D('.3'))

    def test_queue_and_worker_separate_and_restart(self):
        with patch('requests.post',side_effect=AssertionError('must not send')):
            dispatch_verified_alert('BINANCE',{'id':'o','price':99999},self.info,self.settings,Mock())
        client=Mock();client.market.return_value={}
        client.fetch_order.return_value={'id':'o','status':'closed','amount':.01,'filled':.01,'average':102000}
        client.fetch_my_trades.return_value=[]
        verification_tick(self.settings,lambda *a:client)
        verification_tick(self.settings,lambda *a:client)
        with self.store._connect() as c:
            rows=c.execute('SELECT payload FROM verified_outbox').fetchall()
            self.assertEqual(len(rows),1)
            payload=json.loads(rows[0][0]);self.assertEqual(payload['exit_price'],'102,000')
            self.assertIsNone(payload['net_pnl']);self.assertEqual(payload['pnl_result'],'UNKNOWN')

    def test_permanent_error_worker_no_retry(self):
        self.store.enqueue(self.payload())
        with patch('requests.post',return_value=Mock(status_code=404)) as post:
            flush_outbox(self.settings);flush_outbox(self.settings)
        self.assertEqual(post.call_count,1);self.assertEqual(self.status(),'FAILED')

    def test_timeout_unknown_not_resent(self):
        import requests
        self.store.enqueue(self.payload())
        with patch('requests.post',side_effect=requests.exceptions.Timeout()): flush_outbox(self.settings)
        self.assertEqual(self.status(),'UNKNOWN')
        with patch('requests.post') as post: flush_outbox(self.settings);post.assert_not_called()

    def test_crash_sending_recovered_unknown(self):
        self.store.enqueue(self.payload());self.assertTrue(self.store.claim('e'))
        with self.store._connect() as c:c.execute("UPDATE verified_outbox SET updated_at=0")
        NotificationOutboxStore(self.store.db_path).recover()
        self.assertEqual(self.status(),'UNKNOWN')

    def test_atomic_claim(self):
        self.store.enqueue(self.payload())
        second=NotificationOutboxStore(self.store.db_path)
        self.assertTrue(self.store.claim('e'));self.assertFalse(second.claim('e'))

    def test_retry_after_header_priority(self):
        response=Mock(status_code=429,headers={'Retry-After':'12'});response.json.return_value={'retry_after':1}
        with patch('requests.post',return_value=response):
            self.assertEqual(DiscordSender(self.settings.DISCORD_WEBHOOK_URL).send_payload(self.payload())[2],12)

    def test_500_pending_retry(self):
        self.store.enqueue(self.payload())
        with patch('requests.post',return_value=Mock(status_code=500)):flush_outbox(self.settings)
        # P2-5: HTTP 500 is held as UNKNOWN by default to prevent duplicate sends when post-delivery response fails
        self.assertEqual(self.status(),'UNKNOWN')
        self.assertEqual(self.store.requeue_unknown('e', confirm_duplicate_risk=True),1)
        self.assertEqual(self.status(),'PENDING')

    def test_invalid_url_no_request(self):
        with patch('requests.post') as post:
            result=DiscordSender('https://discord.attacker.example/api/webhooks/x').send_payload(self.payload())
            self.assertFalse(result[0]);post.assert_not_called()

    def test_db_failure_no_legacy_fallback_or_raise(self):
        with patch('exchange.verified_pnl.dispatcher.NotificationOutboxStore',side_effect=OSError('token secret')):
            dispatch_verified_alert('BINANCE',{'id':'o'},self.info,self.settings)

    def test_kst_epoch(self):
        self.assertEqual(format_kst(0),'70-01-01 09:00:00')

    def test_flag_off_ignores_db(self):
        self.settings.VERIFIED_DISCORD_ALERTS=False
        with patch('exchange.verified_pnl.dispatcher.NotificationOutboxStore') as db:
            dispatch_verified_alert('BINANCE',{'id':'o'},self.info,self.settings)
            db.assert_not_called()

    def test_unverified_fill_cannot_produce_net(self):
        fill=self.fill();fill.verification_status=VerificationStatus.UNVERIFIED
        self.assertIsNone(self.calculate(fill,allocated_entry_fee=D(0),allocated_funding=D(0)).net_realized_pnl)

    def test_routes_order_arguments_unchanged_with_db_failure(self):
        from fastapi.testclient import TestClient
        main=load('verified_route_test',ROOT/'main.py')
        main.whitelist.append('testclient')
        client=TestClient(main.app,base_url='http://127.0.0.1')
        main.settings=self.settings
        for exchange in ['BINANCE','BYBIT','OKX']:
            for route in ['/','/order']:
                for side in ['entry/buy','close/sell']:
                    payload=dict(password='test-only',exchange=exchange,base='BTC',quote='USDT.P',
                                 side=side,amount=.01,leverage=2,margin_mode='isolated')
                    observations=[]
                    for enabled in [False,True]:
                        self.settings.VERIFIED_DISCORD_ALERTS=enabled
                        bot=bot_for(exchange,s.OrderRequest(**payload))
                        main.get_bot=lambda *args:bot
                        with patch('exchange.verified_pnl.dispatcher.NotificationOutboxStore',side_effect=OSError('db offline')):
                            response=client.post(route,json=payload)
                        observations.append((response.status_code,response.json(),bot.client.calls))
                    self.assertEqual(observations[0],observations[1],(exchange,route,side))
                    self.assertEqual(observations[1][1],{'result':'success'})

    def test_other_exchange_keeps_legacy_alert(self):
        main=load('other_route_test',ROOT/'main.py');main.settings=self.settings
        with patch.object(main,'log_order_message') as legacy,patch.object(main,'print_alert_message'):
            main.log('UPBIT',{},self.info)
            legacy.assert_called_once()

    def test_outbox_success_records_discord_message_id(self):
        self.store.enqueue(self.payload())
        response=Mock(status_code=200);response.json.return_value={'id':'message-1'}
        with patch('requests.post',return_value=response):flush_outbox(self.settings)
        self.assertEqual(self.status(),'SENT')
        with self.store._connect() as c:
            self.assertEqual(c.execute('SELECT discord_message_id FROM verified_outbox').fetchone()[0],'message-1')

    def test_backup_and_hold_preserve_pending_rows(self):
        import subprocess,sys,sqlite3
        self.store.enqueue(self.payload())
        script=str(ROOT/'scripts/notification_db.py')
        backup=Path(self.tmp.name)/'copy.db'
        subprocess.run([sys.executable,script,'backup',self.store.db_path,'--destination',str(backup)],check=True,capture_output=True)
        subprocess.run([sys.executable,script,'hold',self.store.db_path],check=True,capture_output=True)
        self.assertEqual(self.status(),'HELD')
        self.assertEqual(self.store.get_pending(),[])
        from contextlib import closing
        with closing(sqlite3.connect(backup)) as c:
            self.assertEqual(c.execute('SELECT status FROM verified_outbox').fetchone()[0],'PENDING')



class RequeueSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "alerts.db")
        self.store = NotificationOutboxStore(self.db_path)
        self.settings = s.Settings(
            _env_file=None,
            PASSWORD="test-only",
            VERIFIED_DISCORD_ALERTS=True,
            DISCORD_OUTBOX_ENABLED=True,
            DISCORD_RETRY_ENABLED=True,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
            DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/99999/SECRET_CANARY_TOKEN_DO_NOT_LEAK",
        )

    def _insert_outbox_row(self, event_id: str, status: str, payload_str: Optional[str] = None):
        if payload_str is None:
            payload = NotificationPayload(
                event_id=event_id,
                exchange="BINANCE",
                symbol="BTC/USDT",
                side="매도",
                order_name="주문",
                exit_reason=ExitReason.TP,
                pnl_result=PnlResult.UNKNOWN,
                fill_status=FillStatus.PENDING,
                event_timestamp=time.time(),
            )
            payload_str = payload.json()
        now = time.time()
        with self.store._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO verified_outbox (
                    event_id, exchange, symbol, order_id, client_order_id,
                    event_type, version, payload, status, attempts,
                    max_attempts, next_retry_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 5, 0, ?, ?)
                """,
                (event_id, "BINANCE", "BTC/USDT", "ord_" + event_id, "cid_" + event_id,
                 "ORDER_ALERT", 1, payload_str, status, now, now),
            )

    def _get_status(self, event_id: str) -> Optional[str]:
        with self.store._connect() as con:
            row = con.execute("SELECT status FROM verified_outbox WHERE event_id=?", (event_id,)).fetchone()
            return row[0] if row else None

    def test_requeue_no_event_id_bulk_blocked_by_default(self):
        self._insert_outbox_row("u1", OutboxStatus.UNKNOWN.value)
        with self.assertRaises(ValueError) as ctx:
            self.store.requeue_unknown(confirm_duplicate_risk=True)
        self.assertIn("Bulk requeue blocked by default", str(ctx.exception))
        self.assertEqual(self._get_status("u1"), OutboxStatus.UNKNOWN.value)

    def test_requeue_without_explicit_risk_acknowledgment_blocked(self):
        self._insert_outbox_row("u1", OutboxStatus.UNKNOWN.value)
        with self.assertRaises(ValueError) as ctx1:
            self.store.requeue_unknown("u1")
        self.assertIn("operator must explicitly confirm duplicate alert risk", str(ctx1.exception))

        with self.assertRaises(ValueError) as ctx2:
            self.store.requeue_unknown("u1", confirm_duplicate_risk=False)
        self.assertIn("operator must explicitly confirm duplicate alert risk", str(ctx2.exception))
        self.assertEqual(self._get_status("u1"), OutboxStatus.UNKNOWN.value)

    def test_requeue_specific_unknown_event_success(self):
        self._insert_outbox_row("u1", OutboxStatus.UNKNOWN.value)
        count = self.store.requeue_unknown("u1", confirm_duplicate_risk=True)
        self.assertEqual(count, 1)
        self.assertEqual(self._get_status("u1"), OutboxStatus.PENDING.value)

    def test_requeue_specific_held_event_success(self):
        self._insert_outbox_row("h1", OutboxStatus.HELD.value)
        count = self.store.requeue_unknown("h1", confirm_duplicate_risk=True)
        self.assertEqual(count, 1)
        self.assertEqual(self._get_status("h1"), OutboxStatus.PENDING.value)

    def test_requeue_sent_event_blocked(self):
        self._insert_outbox_row("s1", OutboxStatus.SENT.value)
        count = self.store.requeue_unknown("s1", confirm_duplicate_risk=True)
        self.assertEqual(count, 0)
        self.assertEqual(self._get_status("s1"), OutboxStatus.SENT.value)

    def test_requeue_nonexistent_event_returns_zero(self):
        count = self.store.requeue_unknown("nonexistent_event", confirm_duplicate_risk=True)
        self.assertEqual(count, 0)

    def test_requeue_preserves_other_unknown_events(self):
        self._insert_outbox_row("u1", OutboxStatus.UNKNOWN.value)
        self._insert_outbox_row("u2", OutboxStatus.UNKNOWN.value)
        count = self.store.requeue_unknown("u1", confirm_duplicate_risk=True)
        self.assertEqual(count, 1)
        self.assertEqual(self._get_status("u1"), OutboxStatus.PENDING.value)
        self.assertEqual(self._get_status("u2"), OutboxStatus.UNKNOWN.value)

    def test_bulk_requeue_explicit_approval_and_scope(self):
        self._insert_outbox_row("u1", OutboxStatus.UNKNOWN.value)
        self._insert_outbox_row("u2", OutboxStatus.UNKNOWN.value)
        self._insert_outbox_row("h1", OutboxStatus.HELD.value)
        self._insert_outbox_row("s1", OutboxStatus.SENT.value)
        self._insert_outbox_row("p1", OutboxStatus.PENDING.value)

        # Bulk requeue without confirm_duplicate_risk is rejected
        with self.assertRaises(ValueError):
            self.store.requeue_unknown(allow_bulk=True, confirm_duplicate_risk=False)

        # Bulk requeue with both flags transitions only UNKNOWN and HELD
        count = self.store.requeue_unknown(allow_bulk=True, confirm_duplicate_risk=True)
        self.assertEqual(count, 3)
        self.assertEqual(self._get_status("u1"), OutboxStatus.PENDING.value)
        self.assertEqual(self._get_status("u2"), OutboxStatus.PENDING.value)
        self.assertEqual(self._get_status("h1"), OutboxStatus.PENDING.value)
        self.assertEqual(self._get_status("s1"), OutboxStatus.SENT.value)
        self.assertEqual(self._get_status("p1"), OutboxStatus.PENDING.value)

    def test_concurrent_requeue_state_consistency(self):
        import threading
        self._insert_outbox_row("u_concurrent", OutboxStatus.UNKNOWN.value)
        results = []
        def worker():
            try:
                c = self.store.requeue_unknown("u_concurrent", confirm_duplicate_risk=True)
                results.append(c)
            except Exception as ex:
                results.append(ex)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread should transition the row (1), others should transition 0
        total_transitions = sum(r for r in results if isinstance(r, int))
        self.assertEqual(total_transitions, 1)
        self.assertEqual(self._get_status("u_concurrent"), OutboxStatus.PENDING.value)

    def test_audit_log_contains_no_secrets(self):
        self._insert_outbox_row("u_audit", OutboxStatus.UNKNOWN.value)
        logged_warnings = []
        with patch("exchange.verified_pnl.outbox.logger.warning", side_effect=lambda msg: logged_warnings.append(str(msg))):
            self.store.requeue_unknown("u_audit", confirm_duplicate_risk=True)

        self.assertTrue(len(logged_warnings) >= 1)
        log_text = " ".join(logged_warnings)
        self.assertIn("AUDIT_ALERT_REQUEUE", log_text)
        self.assertIn("event_id=u_audit", log_text)
        self.assertNotIn("SECRET_CANARY_TOKEN_DO_NOT_LEAK", log_text)
        self.assertNotIn("https://discord.com", log_text)
        self.assertNotIn("test-only", log_text)

    def test_backward_compatibility_with_legacy_payload(self):
        # Legacy payload lacking v2 specific fields (e.g. no fill_verification_status)
        legacy_raw_json = json.dumps({
            "event_id": "legacy_evt_1",
            "exchange": "BINANCE",
            "symbol": "BTC/USDT",
            "side": "매도",
            "order_name": "주문",
            "exit_reason": "TP",
            "pnl_result": "PROFIT",
            "fill_status": "FILLED",
            "event_timestamp": time.time(),
        })
        self._insert_outbox_row("legacy_evt_1", OutboxStatus.UNKNOWN.value, payload_str=legacy_raw_json)
        self.assertEqual(self._get_status("legacy_evt_1"), OutboxStatus.UNKNOWN.value)

        # Requeue succeeds
        count = self.store.requeue_unknown("legacy_evt_1", confirm_duplicate_risk=True)
        self.assertEqual(count, 1)
        self.assertEqual(self._get_status("legacy_evt_1"), OutboxStatus.PENDING.value)

        # Flush succeeds with mocked sender
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = {"id": "legacy_sent_msg_1"}
        with patch("requests.post", return_value=mock_resp):
            flush_outbox(self.settings)

        self.assertEqual(self._get_status("legacy_evt_1"), OutboxStatus.SENT.value)


if __name__=='__main__':unittest.main()
