import sys,json,copy,time,ast,sqlite3,subprocess,hashlib
from pathlib import Path
from decimal import Decimal as D
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'simple_upbit'))
import test_simple_upbit as S
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tv_signal'))
import test_tv_signal as V
T=V.T;H=V.H;env=T.env;local=S.local

def emit_native(r,asset,side,*,strategy='common-A',order_name=None,wrong=None):
 r.seq+=1;raw=S.T.raw_order(asset+side+str(r.seq),'bid' if side=='buy' else 'ask',['100000000' if side=='buy' else '110000000'],S.T.STAMP+r.seq*1000)
 raw['market']='KRW-'+asset
 for trade in raw['trades']:trade['market']='KRW-'+asset
 if wrong:wrong(raw)
 r.native[raw['uuid']]=raw
 info=H.t.OrderRequest(exchange='UPBIT',base=asset,quote='KRW',side=side,amount=1,strategy_id=strategy,order_name=order_name or ('LL' if side=='buy' else 'Close_HL'))
 result=r.log('UPBIT',{'id':raw['uuid']},info);assert result['accepted'];S.T.tick(r)
 return raw,info,result

@pytest.mark.parametrize('asset',['BTC','XRP','LINK'])
def test_native_three_markets(local,asset):
 r=local;emit_native(r,asset,'buy');emit_native(r,asset,'sell')
 plans=S.payloads(r);assert len(plans)==2 and [p['symbol'] for p in plans]==[asset+'/KRW']*2
 assert D(plans[-1]['estimated_pnl']['amount'])==9895000
 assert len(r.calls)==2 and len(S.T.send(r))==2
 assert all(p['financial_receipt'] is None and p['execution_receipt'] is None for p in plans)

@pytest.mark.parametrize('asset',['BTC','XRP','LINK'])
def test_signal_ll_close_same_strategy(env,asset):
 e=env
 for side,price,name in [('buy','1000','LL'),('sell','1100','Close_HL')]:
  e.seq+=1;sig=dict(kind='strategy_order_fill',price=price,quantity='100',action=side,order_id=name,signal_id='event-'+str(e.seq),strategy_id='common-strategy')
  info=H.t.OrderRequest(exchange='UPBIT',base=asset,quote='KRW',side=side,amount=0.001,strategy_id='common-strategy',order_name=name,tv_signal=sig)
  assert e.log('UPBIT',{'id':'ack-'+str(e.seq)},info)['accepted'];H.dp.verification_tick(e.st,lambda *a:pytest.fail('NO_QUERY'))
 p=T.plans(e)[-1];assert p['estimated_pnl']['basis']=='TRADINGVIEW_SIGNAL' and D(p['estimated_pnl']['amount'])==10000
 assert len(T.send(e))==2


def test_legacy_names_not_silently_merged(local):
 r=local;emit_native(r,'XRP','buy',strategy=None,order_name='LL');emit_native(r,'XRP','sell',strategy=None,order_name='Close_HL')
 assert S.payloads(r)[-1]['estimated_pnl']['amount'] is None

@pytest.mark.parametrize('fault',['market','side','uuid'])
def test_native_wrong_response_not_published(local,fault):
 r=local
 def wrong(raw):
  if fault=='market':raw['market']='KRW-ETH'
  if fault=='side':raw['side']='ask'
  if fault=='uuid':raw['uuid']=S.T.U('wrong-uuid')
 if fault=='uuid':
  # Transport dictionary is indexed by requested UUID, not the substituted response.
  r.seq+=1;raw=S.T.raw_order('original-id','bid',['100000000'],S.T.STAMP+r.seq*1000);oid=raw['uuid'];raw['uuid']=S.T.U('wrong-uuid');raw['market']='KRW-XRP'
  for t in raw['trades']:t['market']='KRW-XRP'
  r.native[oid]=raw
  info=H.t.OrderRequest(exchange='UPBIT',base='XRP',quote='KRW',side='buy',amount=1,strategy_id='common-A')
  assert r.log('UPBIT',{'id':oid},info)['accepted'];S.T.tick(r)
 else:emit_native(r,'XRP','buy',wrong=wrong)
 assert S.payloads(r)==[] and S.T.send(r)==[]



@pytest.mark.parametrize('venue',['BINANCE','BYBIT','OKX'])
def test_usdt_signal_path(env,venue):
 e=env;V.emit(e,'buy','150','2',venue=venue);V.emit(e,'sell','160','1',venue=venue)
 assert D(T.plans(e)[-1]['estimated_pnl']['amount'])==10 and len(T.send(e))==2


def test_multimarket_cache_same_strategy_isolated(local):
 r=local;emit_native(r,'XRP','buy');emit_native(r,'LINK','sell')
 assert S.payloads(r)[-1]['estimated_pnl']['status']=='UNAVAILABLE'
 x=r.host.for_strategy('common-A',symbol='XRP/KRW');l=r.host.for_strategy('common-A',symbol='LINK/KRW')
 assert x is not l and x.binding!=l.binding
 assert not x.accepts(H.t.OrderRequest(exchange='UPBIT',base='LINK',quote='KRW',side='sell',strategy_id='common-A'),r.host.source_account_ref)


def test_financial_reader_still_btc_only(local):
 from exchange.verified_pnl.upbit_spot_read import UpbitSpotReads,UpbitNotificationReads,UpbitReadIncomplete
 r=local;host=r.host.for_strategy('common-A',symbol='XRP/KRW')
 assert type(host.reader) is UpbitNotificationReads
 with pytest.raises(UpbitReadIncomplete):UpbitSpotReads(host.reader.scope,lambda *a:{},lambda *a:None,pocket_uuid=r.host.pocket_uuid,as_of_ms=int(time.time()*1000))
 assert not hasattr(host.reader,'read_order') and not hasattr(host.reader,'closed_orders') and not hasattr(host.reader,'market')


def test_unknown_cost_explained_and_entry_fallback(env):
 e=env;V.emit(e,'sell','1100','100',base='XRP');wire=T.send(e);body=json.dumps(wire,ensure_ascii=False)
 assert 'TV_ACQUISITION_UNAVAILABLE' in body and '주문 결과 확인 필요' in body
 V.emit(e,'sell','1100','100',base='LINK',strategy='fallback',extra={'entry_price':'1000','entry_source':'strategy.position_avg_price'})
 assert D(T.plans(e)[-1]['estimated_pnl']['amount'])==10000


def test_native_vs_signal_dispatch(local):
 r=local;emit_native(r,'LINK','buy');assert S.payloads(r)[0]['estimated_pnl']['basis']=='POA_BUY_OBSERVATION'
 info=H.t.OrderRequest(exchange='UPBIT',base='LINK',quote='KRW',side='sell',strategy_id='tv-only',tv_signal=dict(kind='alert',price='1100',quantity='1',action='sell',order_id='Close_HL',signal_id='separate-signal',strategy_id='tv-only'))
 assert r.log('UPBIT',{'id':S.T.U('signal-only-sell')},info)['accepted'];S.T.tick(r)
 assert S.payloads(r)[-1]['estimated_pnl']['basis']=='TRADINGVIEW_SIGNAL' and S.payloads(r)[-1]['estimated_pnl']['amount'] is None
 assert len(r.calls)==1


def test_pseudonymous_diagnostic_paths(env,tmp_path):
 e=env;V.emit(e,'buy','1000','2',base='XRP');V.emit(e,'sell','1100',base='XRP')
 result=subprocess.run([sys.executable,'scripts/notification_pipeline_status.py','--db',str(e.st.VERIFIED_NOTIFICATIONS_DB_PATH),'--events','2','--server-alias','node-01'],capture_output=True,text=True,encoding='utf-8',timeout=20)
 assert result.returncode==0,result.stderr
 report=json.loads(result.stdout);assert report['server_alias']=='node-01' and report['worker_state'].startswith('UNVERIFIED')
 events=report['outbox']['recent_events'];assert len(events)==2 and all(x['path']=='TV_SIGNAL' for x in events)
 assert all(len(x['order_fingerprint'])==64 and len(x['signal_fingerprint'])==64 for x in events)
 assert all(x['execution_state']=='UNCONFIRMED' and x['delivery_counts']=={'PENDING':1} for x in events)
 assert 'ack-1' not in result.stdout and 'signal-1' not in result.stdout and 'tv-A' not in result.stdout


def test_optional_origin_and_old_frozen_wire(env,tmp_path):
 from exchange.verified_pnl import notification_origin as O
 e=env;e.st.POA_NOTIFICATION_SERVER_ALIAS='node-01'
 # Construct a pre-upgrade message before enqueue, respecting immutable rows.
 with patch.object(O,'stamp',side_effect=lambda payload,settings:payload):V.emit(e,'buy','150')
 p=H.dp.NotificationPayload(**T.plans(e)[0]);assert not O.current_layout(p)
 st=T.store(e)
 with st._connect_read() as c:before=c.execute('SELECT wire FROM vpnl_deliveries').fetchone()[0];revisions=c.execute('SELECT * FROM vpnl_revisions').fetchall()
 assert len(T.send(e))==1
 with st._connect_read() as c:assert c.execute('SELECT wire FROM vpnl_deliveries').fetchone()[0]==before and c.execute('SELECT * FROM vpnl_revisions').fetchall()==revisions
 V.emit(e,'sell','160');p=H.dp.NotificationPayload(**T.plans(e)[-1]);assert O.current_layout(p) and 'node-01' in O.display(p)
 assert len(T.send(e))==1


def test_deployment_record_must_match_actual_bytes(tmp_path):
 from exchange.verified_pnl import notification_origin as O
 main=tmp_path/'main.py';main.write_bytes(b'pass\n');manifest=[dict(Path='main.py',Bytes=5,SHA256=hashlib.sha256(main.read_bytes()).hexdigest().upper())]
 raw=json.dumps(manifest).encode();(tmp_path/'release_manifest_sha256.json').write_bytes(raw)
 (tmp_path/'.poa-deployment.json').write_text(json.dumps(dict(commit='a'*40,manifest_sha256=hashlib.sha256(raw).hexdigest())))
 assert O.deployment(tmp_path)['commit']=='a'*40
 main.write_bytes(b'bad!\n');assert O.deployment(tmp_path)['status']=='UNVERIFIED'


def test_main_order_and_financial_guards_unchanged():
 base='6a84d84b6cbb740a42773f0b89adfe4454407eb3'
 for path in ('main.py','exchange/safe_retry.py','exchange/verified_pnl/bootstrap.py','exchange/verified_pnl/verifier.py','exchange/verified_pnl/evidence.py','exchange/verified_pnl/ledger.py','exchange/verified_pnl/pnl_calculator.py','exchange/verified_pnl/history.py','exchange/verified_pnl/reconciliation.py','exchange/verified_pnl/outbox.py','exchange/verified_pnl/delivery_store.py'):
  if path=='main.py':
   from notification_source_contract import assert_notification_main_source
   assert_notification_main_source()
  elif path=='exchange/verified_pnl/delivery_store.py':
   from notification_source_contract import assert_reviewed_notification_source
   assert_reviewed_notification_source(path)
  else:assert Path(path).read_bytes()==subprocess.check_output(['git','show',base+':'+path]),path


def test_documented_templates_parse_exactly(env):
 import re
 doc=Path('UPBIT_MULTI_MARKET_SIGNAL_GUIDE.md').read_text(encoding='utf-8')
 blocks=re.findall(r'```json\n(.*?)\n```',doc,re.S)
 template=blocks[0]
 for side,name,price in [('buy','LL','1000'),('sell','Close_HL','1100')]:
  mapping={'{{strategy.order.alert_message}}':'"strategy_id":"S203-001","signal_id":"event-'+side+'"','{{syminfo.basecurrency}}':'XRP','{{syminfo.currency}}':'KRW','{{strategy.order.action}}':side,'{{strategy.order.contracts}}':'100','{{strategy.order.id}}':name,'{{strategy.order.price}}':price}
  text=template
  for k,v in mapping.items():text=text.replace(k,v)
  data=json.loads(text);data.pop('password');info=H.t.OrderRequest(**data)
  assert json.loads(info.tv_signal)['strategy_id']==data['strategy_id'] and info.order_name==name
 for block in blocks[1:3]:
  data=json.loads(block);data.pop('password');assert H.t.OrderRequest(**data).tv_signal



def test_xrp_pending_restart_revalidates_same_observation_no_get(local):
 from exchange.verified_pnl.upbit_notification_simple import SimpleUpbitNotificationHosts
 from exchange.verified_pnl.upbit_spot_read import configure_upbit_notification_host
 r=local;emit_native(r,'XRP','buy');emit_native(r,'XRP','sell');before=len(r.calls)
 assert configure_upbit_notification_host(SimpleUpbitNotificationHosts(r.st))
 assert len(S.T.send(r))==2 and len(r.calls)==before
 S.T.tick(r);assert S.T.send(r)==[]

@pytest.mark.parametrize('mode',['unfilled','timeout','account_changed'])
def test_no_fake_completion_on_read_failure(local,mode):
 r=local
 if mode=='account_changed':r.st.UPBIT_KEY='other-test-key'
 if mode=='timeout':
  def wrong(raw):r.host._get._transport=lambda req:(_ for _ in ()).throw(TimeoutError())
 else:
  def wrong(raw):
   if mode=='unfilled':raw['state']='wait'
 emit_native(r,'LINK','buy',wrong=wrong)
 if mode=='account_changed':assert S.payloads(r)==[] and S.T.send(r)==[]
 else:
  plans=S.payloads(r);assert len(plans)==1
  p=plans[0];assert p['estimated_pnl']['basis']=='POA_BUY_ACK' and p['fill_status']=='UNVERIFIED'
  assert p['estimated_pnl']['signal_price'] is None and p['estimated_pnl']['amount'] is None and p['financial_receipt'] is None
  assert len(S.T.send(r))==1 and S.T.send(r)==[]


def test_multi_market_lru_keeps_active_reader(local):
 r=local;active=r.host.for_strategy('active',symbol='XRP/KRW')
 for i in range(40):r.host.for_strategy('other-'+str(i),symbol='LINK/KRW')
 assert len(r.host._children)==32 and r.host.by_binding(active.binding) is active
 assert r.host.for_strategy('active',symbol='XRP/KRW') is active and r.calls==[]


def test_notification_reason_and_origin_do_not_echo_canaries(env):
 from exchange.verified_pnl import notification_origin as O
 e=env;e.st.POA_NOTIFICATION_SERVER_ALIAS='Bearer_SYNTHETIC_CANARY'
 V.emit(e,'sell','1100');p=H.dp.NotificationPayload(**T.plans(e)[0])
 assert O.display(p) is None and 'SYNTHETIC_CANARY' not in json.dumps(T.send(e))
 assert O.reason_code('Bearer_SYNTHETIC_CANARY')=='UNAVAILABLE'



def test_same_signal_different_real_order_preserved_review(env):
 from exchange.verified_pnl.intake import IntakeStore
 e=env;first,_=V.emit(e,'buy','1000','100',sid='same-signal',oid='real-order-1');assert len(T.send(e))==1
 second,_=V.emit(e,'buy','1000','100',sid='same-signal',oid='real-order-2')
 assert second['accepted'] and second['status']=='DURABLE_REVIEW' and second['intake_id']!=first['intake_id']
 with IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:
  rows=c.execute('SELECT request,status,error FROM notification_intake ORDER BY seq').fetchall()
 assert len(rows)==2 and rows[1][1:]==('REVIEW','TV_SIGNAL_ORDER_CONFLICT')
 assert [json.loads(row[0])['result']['id'] for row in rows]==['real-order-1','real-order-2']
 assert T.send(e)==[]
 result=subprocess.run([sys.executable,'scripts/notification_pipeline_status.py','--db',str(e.st.VERIFIED_NOTIFICATIONS_DB_PATH),'--events','10'],capture_output=True,text=True,encoding='utf-8',timeout=15)
 events=json.loads(result.stdout)['intake']['recent_events'];assert len(events)==2
 assert len({x['order_fingerprint'] for x in events})==2 and len({x['signal_fingerprint'] for x in events})==1
 assert any('TV_SIGNAL_ORDER_CONFLICT' in x['missing_codes'] for x in events)
 V.emit(e,'sell','1100','100');assert D(T.plans(e)[-1]['estimated_pnl']['amount'])==10000


def test_exact_order_signal_retry_still_idempotent(env):
 e=env;first,_=V.emit(e,'buy','1000','100',sid='exact-signal',oid='one-order');assert len(T.send(e))==1
 second,_=V.emit(e,'buy','1000','100',sid='exact-signal',oid='one-order')
 assert first['intake_id']==second['intake_id'] and T.send(e)==[]



def test_current_intake_ack_gap_recovers_without_duplicate(env):
 from exchange.verified_pnl import intake as I
 e=env;V.emit(e,'buy','1000');assert len(T.send(e))==1
 original=I.IntakeStore.mark
 def gap(self,key,status,*args,**kwargs):
  if status=='SUBMITTED':raise RuntimeError('isolated_ack_gap')
  return original(self,key,status,*args,**kwargs)
 with patch.object(I.IntakeStore,'mark',gap):result,_=V.emit(e,'sell','1100',tick=False)
 assert result['accepted']
 for _ in range(3):I.replay(e.st);H.dp.verification_tick(e.st,lambda *a:pytest.fail('NO_API'))
 assert len(T.send(e))==1 and T.send(e)==[]
 assert D(T.plans(e)[-1]['estimated_pnl']['amount'])==100



def test_btc_financial_provider_does_not_capture_xrp_notification(local):
 from exchange.verified_pnl import bootstrap as B
 from types import SimpleNamespace as NS
 r=local;unrelated=NS(scope=NS(exchange='UPBIT',symbol='BTC/KRW',legacy_account_reference=r.host.source_account_ref))
 with patch.object(B,'current_bootstrap',return_value=unrelated):emit_native(r,'XRP','buy');emit_native(r,'XRP','sell')
 assert len(S.payloads(r))==2 and D(S.payloads(r)[-1]['estimated_pnl']['amount'])==9895000
 assert all(p['financial_receipt'] is None for p in S.payloads(r))
