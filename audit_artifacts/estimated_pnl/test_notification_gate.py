"""Required raw-order notification acceptance at the unchanged production entry.
These cases intentionally fail before permission to extend admission/persistence.
No synthetic NotificationPayload is passed to a renderer.
"""
import ast,copy,json,sqlite3,sys,hashlib,uuid
from pathlib import Path
from unittest.mock import Mock,patch
import pytest
import r6_worker_harness as H
from exchange.verified_pnl.bootstrap import configure_execution_bootstrap
from exchange.verified_pnl.history import configure_history_provider
from exchange.verified_pnl import intake as I

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).parent

def live_log(settings):
 tree=ast.parse((ROOT/'main.py').read_text(encoding='utf-8'))
 func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='log')
 # Isolate the exact production function body from startup/operational DB effects.
 import exchange.verified_pnl as api
 api.dispatch_verified_alert=H.dp.dispatch_verified_alert
 namespace={'settings':settings,'is_verified_exchange':lambda x:x in H.dp.SUPPORTED,'log_order_message':Mock(side_effect=AssertionError('LEGACY_FALLBACK_FORBIDDEN')),'print_alert_message':Mock()}
 exec(compile(ast.Module(body=[func],type_ignores=[]),str(ROOT/'main.py'),'exec'),namespace)
 return namespace['log']

def observed(venue,oid,side,price,fee):
 quote='KRW' if venue=='UPBIT' else 'USDT'
 if venue=='UPBIT':
  return dict(uuid=oid,market='KRW-BTC',side='bid' if side=='buy' else 'ask',state='done',ord_type='limit',volume='1',executed_volume='1',remaining_volume='0',paid_fee=str(fee),trades_count=1,created_at='2026-10-01T00:00:00+00:00',trades=[dict(uuid=str(uuid.uuid5(uuid.NAMESPACE_URL,oid+'-trade')),market='KRW-BTC',side='bid' if side=='buy' else 'ask',price=str(price),volume='1',funds=str(price),created_at='2026-10-01T00:00:01+00:00')])
 return dict(id=oid,status='closed',symbol='BTC/USDT',side=side,type='limit',amount='1',filled='1',remaining='0',average=str(price),price='999',fee=dict(cost=str(fee),currency=quote),trades=[dict(id=oid+'-trade',order=oid,symbol='BTC/USDT',side=side,price=str(price),amount='1',fee=dict(cost=str(fee),currency=quote),timestamp=1790812801000)])

@pytest.mark.parametrize('venue',['BINANCE','BYBIT','OKX','UPBIT'])
def test_actual_notification_entry_raw_fill_requires_estimate(tmp_path,venue):
 configure_execution_bootstrap(None);configure_history_provider(None)
 st=H.settings(tmp_path/'notifications.sqlite');st.UPBIT_VERIFIED_ALERTS=True;st.UPBIT_NET_PNL_ENABLED=True
 setattr(st,venue+'_KEY','MOCK_HOST_ACCOUNT_REFERENCE_ONLY')
 quote='KRW' if venue=='UPBIT' else 'USDT';buyprice=100000000 if venue=='UPBIT' else 150;sellprice=110000000 if venue=='UPBIT' else 160;buyfee=50000 if venue=='UPBIT' else 1;sellfee=55000 if venue=='UPBIT' else 1
 handler=live_log(st);results=[];client=Mock()
 for side,price,fee in [('buy',buyprice,buyfee),('sell',sellprice,sellfee)]:
  info=H.t.OrderRequest(exchange=venue,base='BTC',quote=quote,side=side,amount=1,order_name='strategy-A',strategy_id='strategy-A')
  info.is_futures=False;info.is_coinm=False;info.is_buy=side=='buy';info.is_sell=side=='sell';info.is_close=False
  oid=str(uuid.uuid5(uuid.NAMESPACE_URL,venue+'-mock-'+side)) if venue in ('UPBIT','BYBIT') else str((100000000000000000 if venue=='OKX' else 1000)+(1 if side=='buy' else 2))
  raw=observed(venue,oid,side,price,fee);results.append(raw)
  handler(venue,raw,info)
  H.dp.verification_tick(st,lambda *a:client)
 with patch('requests.post',return_value=Mock(status_code=200,json=Mock(return_value={'id':'mock-message'}))) as post:H.dp.flush_outbox(st)
 store=H.ob.NotificationOutboxStore(st.VERIFIED_NOTIFICATIONS_DB_PATH);sidecar=I.IntakeStore(st.VERIFIED_NOTIFICATIONS_DB_PATH)
 with store._connect_read() as c:
  jobs=c.execute('select state,financial_state,data from vpnl_jobs').fetchall()
  roots=c.execute('select count(*) from vpnl_roots').fetchone()[0];deliveries=c.execute('select count(*) from vpnl_deliveries').fetchone()[0]
 with sidecar.connect() as c:inputs=c.execute('select status,request from notification_intake order by seq').fetchall()
 proof=dict(venue=venue,entry='main.log -> dispatcher.dispatch_verified_alert -> IntakeStore/replay -> verification_tick -> flush_outbox',provider=None,intake_count=len(inputs),intake_states=[x[0] for x in inputs],retained_request_keys=[sorted(json.loads(x[1])['result']) for x in inputs],job_states=[(x[0],x[1]) for x in jobs],job_result_keys=[sorted(json.loads(x[2])['result']) for x in jobs],root_count=roots,delivery_count=deliveries,mock_post_count=post.call_count,query_count=client.fetch_order.call_count,order_create_calls=client.create_order.call_count,dispatcher_file=H.dp.__file__,main_sha256=hashlib.sha256((ROOT/'main.py').read_bytes()).hexdigest())
 (OUT/(venue.lower()+'_entrypoint.json')).write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
 with sqlite3.connect(st.VERIFIED_NOTIFICATIONS_DB_PATH) as c,sqlite3.connect(OUT/(venue.lower()+'_fixture.sqlite')) as dst:c.backup(dst)
 with sqlite3.connect(sidecar.path) as c,sqlite3.connect(OUT/(venue.lower()+'_intake_fixture.sqlite')) as dst:c.backup(dst)
 assert post.call_count==1,'RAW_OBSERVED_ESTIMATE_NOT_DELIVERED: '+json.dumps({k:proof[k] for k in ('intake_states','job_states','root_count','delivery_count','mock_post_count')})
 assert '추정' in post.call_args.kwargs['data'].decode('utf-8')
