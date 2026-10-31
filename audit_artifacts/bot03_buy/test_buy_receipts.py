import sys,copy,json,time,sqlite3,hashlib
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace as NS
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'simple_upbit'))
import test_simple_upbit as S
from exchange.verified_pnl import upbit_notification_get as G,upbit_spot_read as U,estimated_pnl as N
from exchange.verified_pnl.intake import IntakeStore
local=S.local
H=S.T.T.H


def feed(r,side='buy',name='link-buy',state='done',tick=True):
 raw=S.T.raw_order(name,'bid' if side=='buy' else 'ask',['100000000' if side=='buy' else '110000000'],S.T.STAMP+r.seq*1000)
 r.seq+=1;raw['market']='KRW-LINK'
 for t in raw['trades']:t['market']='KRW-LINK'
 if state=='wait':
  raw.update(state='wait',executed_volume='0',remaining_volume=raw['volume'],trades_count=0,trades=[],paid_fee='0')
 r.native[raw['uuid']]=raw
 # Existing JSON: no tv_signal/strategy_id/signal_id. Preserve LL vs Close_LL.
 info=H.t.OrderRequest(exchange='UPBIT',base='LINK',quote='KRW',side=side,amount=1,order_name='LL' if side=='buy' else 'Close_LL')
 out=r.log('UPBIT',{'id':raw['uuid']},info);assert out['accepted']
 if tick:S.T.tick(r)
 return raw,info


def pending(r):
 p=S.payloads(r);assert len(p)==1
 assert p[0]['estimated_pnl']['basis']=='POA_BUY_ACK' and p[0]['financial_receipt'] is None and p[0]['execution_receipt'] is None
 assert p[0]['estimated_pnl']['amount'] is None and p[0]['estimated_pnl']['roi'] is None
 wires=S.T.send(r);assert len(wires)==1
 text=json.dumps(wires[0]['json'],ensure_ascii=False);assert '매수 주문 접수' in text and '체결 확인 대기' in text and '체결 완료' not in text
 return p[0]


def test_native_link_done_no_extra_tv_fields(local):
 r=local;raw,info=feed(r)
 p=S.payloads(r);assert len(p)==1 and p[0]['estimated_pnl']['basis']=='POA_BUY_OBSERVATION'
 assert p[0]['symbol']=='LINK/KRW' and p[0]['order_name']=='LL' and p[0]['financial_receipt'] is None
 assert len(S.T.send(r))==1
 with S.T.store(r)._connect_read() as c:
  assert json.loads(c.execute('SELECT data FROM vpnl_jobs').fetchone()[0])['notice_snapshot']['confirmed'] is False
  assert json.loads(c.execute('SELECT record FROM vpnl_notice_upbit_observations').fetchone()[0])['snapshot']['confirmed'] is True


@pytest.mark.parametrize('status,code',[(401,'UPBIT_READ_AUTH_REJECTED'),(403,'UPBIT_READ_PERMISSION_REJECTED'),(429,'UPBIT_READ_RATE_LIMITED'),(500,'UPBIT_READ_SERVER_ERROR')])
def test_get_error_retains_review_and_ack(local,status,code):
 r=local;r.status=status;feed(r)
 p=pending(r)
 with S.T.store(r)._connect_read() as c:
  state,reason=c.execute('SELECT state,financial_state FROM vpnl_jobs').fetchone();assert (state,reason)==('OBSERVED_REVIEW',code)
 with IntakeStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:assert c.execute('SELECT status FROM notification_intake').fetchone()[0]=='SUBMITTED'
 assert S.T.send(r)==[]


@pytest.mark.parametrize('fault,code',[('timeout','UPBIT_READ_TIMEOUT'),('ip','UPBIT_READ_IP_REJECTED'),('schema','UPBIT_READ_SCHEMA_INVALID'),('quantity','UPBIT_READ_QUANTITY_CONFLICT'),('trades','UPBIT_READ_FILL_INCOMPLETE'),('uuid','UPBIT_READ_ORDER_UUID'),('market','UPBIT_READ_SCOPE_MISMATCH'),('db','UPBIT_OBSERVATION_DB_FAILED')])
def test_classified_failures_no_fabricated_fill(local,fault,code):
 r=local;raw,_=feed(r,tick=False)
 if fault=='schema':r.native[raw['uuid']].pop('reserved_fee')
 if fault=='quantity':r.native[raw['uuid']]['executed_volume']='2'
 if fault=='trades':r.native[raw['uuid']]['trades']=[]
 if fault=='uuid':r.native[raw['uuid']]['uuid']=S.T.raw_order('other','bid',['1'],S.T.STAMP)['uuid']
 if fault=='market':r.native[raw['uuid']]['market']='KRW-BTC'
 if fault in ('timeout','ip'):
  err=G.UpbitGETError('READ_TIMEOUT' if fault=='timeout' else 'IP_REJECTED')
  with patch.object(G.UpbitNotificationGET,'__call__',side_effect=err):S.T.tick(r)
 elif fault=='db':
  with patch.object(H.ob.NotificationOutboxStore,'record_upbit_observation',side_effect=sqlite3.OperationalError('SYNTHETIC_SECRET_DO_NOT_LOG')):S.T.tick(r)
 else:S.T.tick(r)
 if fault in ('uuid','market'):assert S.payloads(r)==[] and S.T.send(r)==[]
 else:pending(r)
 with S.T.store(r)._connect_read() as c:assert c.execute('SELECT financial_state FROM vpnl_jobs').fetchone()[0]==code


@pytest.mark.parametrize('state',['SENT','UNKNOWN','HELD','POSTING','PENDING'])
def test_pending_to_observed_confirmation_preserves_parent(local,state):
 r=local;raw,info=feed(r,state='wait');p=S.payloads(r);assert len(p)==1
 store=S.T.store(r)
 if state=='SENT':assert len(S.T.send(r))==1
 elif state=='POSTING':
  claim=store.claim_next();assert store.begin_post(claim)
 elif state in ('UNKNOWN','HELD'):
  with store._connect() as c:c.execute('UPDATE vpnl_deliveries SET status=?',(state,))
 with store._connect_read() as c:before=c.execute('SELECT id,wire,wire_digest,message_id FROM vpnl_deliveries').fetchone()
 done=copy.deepcopy(raw);done.update(state='done',remaining_volume='0',executed_volume='1',trades_count=1,paid_fee='50000')
 original=S.T.raw_order('link-buy','bid',['100000000'],S.T.STAMP);done['trades']=original['trades']
 for t in done['trades']:t['market']='KRW-LINK'
 r.native[raw['uuid']]=done
 with store._connect_read() as c:due=c.execute('SELECT due FROM vpnl_jobs').fetchone()[0]
 with patch('time.time',return_value=due+.01):S.T.tick(r)
 def send_due():
  with patch('time.time',return_value=due+.1):return S.T.send(r)
 if state in ('UNKNOWN','HELD'):
  assert len(S.payloads(r))==1 and send_due()==[]
  with store._connect_read() as c:assert c.execute('SELECT state FROM vpnl_jobs').fetchone()[0]=='OBSERVED_REVIEW'
 elif state=='POSTING':
  with store._connect_read() as c:assert c.execute('SELECT status FROM vpnl_deliveries WHERE revision=2').fetchone()[0]=='BLOCKED_PREDECESSOR'
  assert store.ack(claim,True,'987654321000000001');assert len(send_due())==1
 else:
  assert len(S.payloads(r))==2
  wires=send_due();assert len(wires)==1 and '매수 체결 확인' in json.dumps(wires,ensure_ascii=False)
 with store._connect_read() as c:
  assert c.execute('SELECT id,wire,wire_digest,message_id FROM vpnl_deliveries ORDER BY revision LIMIT 1').fetchone()==(before if state!='POSTING' else (*before[:3],'987654321000000001'))
  assert c.execute('SELECT count(*) FROM vpnl_roots').fetchone()[0]==1
 assert S.T.send(r)==[]


def test_duplicate_receipt_after_restart(local):
 r=local;r.status=401;raw,info=feed(r);pending(r)
 from exchange.verified_pnl.intake import replay
 assert r.log('UPBIT',{'id':raw['uuid']},info)['accepted'];replay(r.st);S.T.tick(r)
 assert S.T.send(r)==[]
 with S.T.store(r)._connect_read() as c:assert c.execute('SELECT count(*) FROM vpnl_deliveries').fetchone()[0]==1


def test_arbitrary_exception_is_not_logged(local,capsys):
 r=local;feed(r,tick=False);records=[]
 from loguru import logger
 sink=logger.add(lambda m:records.append(str(m)))
 try:
  with patch.object(U.UpbitNotificationHost,'observe',side_effect=RuntimeError('SYNTHETIC_BEARER_SECRET')):S.T.tick(r)
 finally:logger.remove(sink)
 assert 'SYNTHETIC_BEARER_SECRET' not in ''.join(records)
 pending(r)


@pytest.mark.parametrize('venue',['UPBIT','BINANCE','BYBIT','OKX'])
def test_common_id_only_buy_receipt_no_provider(env,venue):
 import uuid
 e=env;oid=str(uuid.uuid4()) if venue=='UPBIT' else 'accepted-'+venue
 info=S.T.T.info(venue,'buy');info.strategy_id=None;info.order_name='HL'
 assert e.log(venue,{'id':oid},info)['accepted']
 H.dp.verification_tick(e.st,lambda *a:pytest.fail('NO_EXCHANGE_GET'))
 plans=S.T.T.plans(e);assert len(plans)==1 and plans[0]['estimated_pnl']['basis']=='POA_BUY_ACK'
 assert len(S.T.T.send(e))==1
 assert e.log(venue,{'id':oid},info)['accepted'];H.dp.verification_tick(e.st,lambda *a:None);assert S.T.T.send(e)==[]
 assert plans[0]['order_name']=='HL' and plans[0]['financial_receipt'] is None


env=S.T.T.env

@pytest.mark.parametrize('fault',['no_id','bad_uuid','wrong_symbol','wrong_side','rejected'])
def test_no_false_order_acceptance_or_fill(env,fault):
 import uuid
 e=env;raw={'id':str(uuid.uuid4())}
 if fault=='no_id':raw={}
 if fault=='bad_uuid':raw={'id':'not-a-uuid'}
 if fault=='wrong_symbol':raw['symbol']='ETH/KRW'
 if fault=='wrong_side':raw['side']='sell'
 if fault=='rejected':raw['status']='rejected'
 e.log('UPBIT',raw,S.T.T.info('UPBIT','buy'));H.dp.verification_tick(e.st,lambda *a:None)
 assert S.T.T.plans(e)==[] and S.T.T.send(e)==[]


def test_pending_ten_producers_two_workers(env):
 from concurrent.futures import ThreadPoolExecutor
 from bundle5_wire_observer import FrozenWireObserver
 e=env;info=S.T.T.info('BYBIT','buy')
 with ThreadPoolExecutor(max_workers=10) as pool:out=list(pool.map(lambda _:e.log('BYBIT',{'id':'one-receipt'},info.copy(deep=True)),range(10)))
 assert all(x['accepted'] for x in out);H.dp.verification_tick(e.st,lambda *a:None)
 with FrozenWireObserver(S.T.T.store(e)) as obs:
  with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:H.dp.flush_outbox(e.st),range(2)))
 assert len(obs.finish())==1 and S.T.T.send(e)==[]


def test_ip_http_error_uses_name_only(local,capsys):
 r=local;raw,_=feed(r,tick=False)
 r.host._get._transport=lambda req:G.OrderGETResponse(401,json.dumps({'error':{'name':'no_authorization_ip','message':'SYNTHETIC_PRIVATE_VALUE'}}).encode(),{})
 S.T.tick(r);pending(r)
 with S.T.store(r)._connect_read() as c:assert c.execute('SELECT financial_state FROM vpnl_jobs').fetchone()[0]=='UPBIT_READ_IP_REJECTED'
 captured=capsys.readouterr();assert 'SYNTHETIC_PRIVATE_VALUE' not in captured.out+captured.err


def test_admission_rejection_retains_receipt_and_review(local):
 r=local;feed(r,tick=False)
 with patch.object(H.ob.NotificationOutboxStore,'record_upbit_observation',return_value=False):S.T.tick(r)
 pending(r)
 with S.T.store(r)._connect_read() as c:assert c.execute('SELECT state,financial_state FROM vpnl_jobs').fetchone()==('OBSERVED_REVIEW','UPBIT_OBSERVATION_ADMISSION_FAILED')


rig=S.T.T.rig

def test_signed_provider_confirmation_keeps_accounting_path(rig):
 from dataclasses import replace
 from exchange.verified_pnl.bootstrap import SpotExecutionBootstrap,configure_execution_bootstrap
 from exchange.verified_pnl.ledger import identity
 from bundle5_wire_observer import FrozenWireObserver
 r=rig;r.st.BINANCE_KEY='mock-provider-account'
 scope=replace(r.scope,legacy_account_reference=identity('BINANCE',r.st.BINANCE_KEY))
 boot=SpotExecutionBootstrap(scope,r.adapter,r.authority,{'fixture-only':r.public},{'fixture-only':hashlib.sha256(r.public).hexdigest()})
 configure_execution_bootstrap(None)
 info=H.t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1,order_name='LL',strategy_id='physical')
 log=S.T.T.G.live_log(r.st);assert log('BINANCE',{'id':'order-1'},info)['accepted']
 H.dp.verification_tick(r.st,lambda *a:pytest.fail('NO_PROVIDER_YET'))
 store=H.ob.NotificationOutboxStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH)
 with FrozenWireObserver(store) as obs:H.dp.flush_outbox(r.st)
 assert len(obs.finish())==1
 assert configure_execution_bootstrap(boot)
 try:
  with patch.object(H.dp,'allocate',wraps=H.dp.allocate) as allocate:
   H.dp.verification_tick(r.st,boot.client_for)
  assert allocate.call_count==1
  with store._connect_read() as c:
   rows=[json.loads(v[0]) for v in c.execute('SELECT payload FROM vpnl_revisions ORDER BY revision')]
   assert len(rows)==2 and rows[-1]['estimated_pnl']['basis']=='POA_BUY_OBSERVATION'
   assert c.execute('SELECT count(*) FROM vpnl_notice_successors').fetchone()[0]==1
  with FrozenWireObserver(store) as obs:H.dp.flush_outbox(r.st)
  assert len(obs.finish())==1
 finally:configure_execution_bootstrap(None)


def test_posting_unknown_does_not_release_confirmation(local):
 r=local;raw,info=feed(r,state='wait');store=S.T.store(r);claim=store.claim_next();assert store.begin_post(claim)
 done=S.T.raw_order('link-buy','bid',['100000000'],S.T.STAMP);done['market']='KRW-LINK'
 for t in done['trades']:t['market']='KRW-LINK'
 r.native[raw['uuid']]=done
 with store._connect_read() as c:due=c.execute('SELECT due FROM vpnl_jobs').fetchone()[0]
 with patch('time.time',return_value=due+.1):S.T.tick(r)
 assert store.ack(claim,False,error='delivery_unknown')
 with store._connect_read() as c:assert c.execute('SELECT status FROM vpnl_deliveries ORDER BY revision').fetchall()==[('UNKNOWN',),('HELD',)]
 assert S.T.send(r)==[]



def test_delegated_transition_authority_cannot_be_silently_changed():
 from notification_source_contract import ROOT,assert_reviewed_notification_source
 original=Path.read_bytes
 def altered(p):
  raw=original(p)
  return raw.replace(b'def transition(c,p,job):',b'def transition(c,p,job):\n    return True') if p.resolve()==ROOT/'exchange/verified_pnl/buy_ack.py' else raw
 with patch.object(Path,'read_bytes',altered):
  with pytest.raises(AssertionError):assert_reviewed_notification_source('exchange/verified_pnl/delivery_store.py')


@pytest.mark.parametrize('fault',['confirmed','side','receipt','account','order'])
def test_pending_receipt_cannot_promote_forged_confirmation(local,fault):
 r=local;feed(r,state='wait');store=S.T.store(r);p=H.dp.NotificationPayload(**S.payloads(r)[0])
 with store._connect() as c:
  key=c.execute('SELECT id FROM vpnl_jobs').fetchone()[0]
  c.execute("UPDATE vpnl_jobs SET state='VERIFYING',job_lease='fixture-lease',due=? WHERE id=?",(time.time()+120,key))
 if fault=='confirmed':p.event_version=2;p.estimated_pnl['basis']='POA_BUY_OBSERVATION';p.estimated_pnl['signal_price']='100000000'
 if fault=='side':p.side='매도'
 if fault=='receipt':p.financial_receipt={'verified':True}
 if fault=='account':p.estimated_pnl['scope']['account']='0'*64
 if fault=='order':p.order_id='other-order'
 assert store.enqueue(p,job_claim=(key,'fixture-lease')) is False
 with store._connect_read() as c:assert c.execute('SELECT count(*) FROM vpnl_revisions').fetchone()[0]==1
