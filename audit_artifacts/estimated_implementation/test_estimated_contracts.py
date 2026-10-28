"""New bytes-aware contracts; original tests are retained unchanged."""
import copy,json,sqlite3,threading,time,sys,uuid,hashlib
from pathlib import Path
from decimal import Decimal as D
from unittest.mock import Mock,patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'estimated_pnl'))
import test_notification_gate as G
import r6_worker_harness as H
from bundle5_wire_observer import FrozenWireObserver
from exchange.verified_pnl import evidence as E,intake as I,estimated_pnl as N
from exchange.verified_pnl.bootstrap import configure_execution_bootstrap
from exchange.verified_pnl.history import configure_history_provider
from test_bundle3_bootstrap import rig,reset
from test_bundle4_history import history
VENUES=['BINANCE','BYBIT','OKX','UPBIT']

@pytest.fixture
def env(tmp_path):
 configure_execution_bootstrap(None);configure_history_provider(None)
 st=H.settings(tmp_path/'notice.sqlite');st.UPBIT_VERIFIED_ALERTS=True;st.UPBIT_NET_PNL_ENABLED=True
 for v in VENUES:setattr(st,v+'_KEY','mock-account-A')
 e=Mock();e.st=st;e.log=G.live_log(st);e.seq=0
 yield e
 configure_execution_bootstrap(None);configure_history_provider(None)

def info(v,side,strategy='strategy-A',qty='1'):
 x=H.t.OrderRequest(exchange=v,base='BTC',quote='KRW' if v=='UPBIT' else 'USDT',side=side,amount=float(qty),strategy_id=strategy,order_name=strategy)
 x.is_futures=x.is_coinm=x.is_close=False;x.is_buy=side=='buy';x.is_sell=side=='sell';return x

def emit(e,v,side,price,fee='1',qty='1',strategy='strategy-A',oid=None,change=None,tick=True):
 e.seq+=1
 oid=oid or (str(uuid.uuid5(uuid.NAMESPACE_URL,v+str(e.seq))) if v in ('UPBIT','BYBIT') else str(1000+e.seq))
 r=G.observed(v,oid,side,price,fee if fee is not None else 0)
 if v=='UPBIT':
  r.update(volume=qty,executed_volume=qty);r['trades'][0].update(volume=qty,funds=str(D(str(price))*D(qty)))
  if fee is None:r.pop('paid_fee')
 else:
  r.update(amount=qty,filled=qty);r['trades'][0]['amount']=qty
  if fee is None:r.pop('fee');r['trades'][0].pop('fee')
 if change:change(r)
 result=e.log(v,r,info(v,side,strategy,qty));assert result['accepted'] is True
 if tick:H.dp.verification_tick(e.st,lambda *a: (_ for _ in ()).throw(AssertionError('REST_FORBIDDEN')))
 return oid,r,result

def store(e):return H.ob.NotificationOutboxStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH)
def plans(e):
 with store(e)._connect_read() as c:return [json.loads(x[0]) for x in c.execute('SELECT payload FROM vpnl_revisions ORDER BY rowid')]
def send(e):
 with FrozenWireObserver(store(e)) as obs:H.dp.flush_outbox(e.st)
 return obs.finish()
def estimate(e):return plans(e)[-1]['estimated_pnl']

@pytest.mark.parametrize('venue',VENUES)
@pytest.mark.parametrize('sell,expected',[('160','8'),('140','-12'),('152','0')])
def test_entry_profit_loss_flat(env,venue,sell,expected):
 emit(env,venue,'buy','150');emit(env,venue,'sell',sell)
 r=estimate(env);assert r['status']=='ESTIMATED' and D(r['amount'])==D(expected)
 assert D(r['entry_price'])==150 and D(r['exit_price'])==D(sell)
 wire=send(env);assert len(wire)==1 and '추정' in json.dumps(wire[0]['json'],ensure_ascii=False)
 assert not E.payload_valid(H.dp.NotificationPayload(**plans(env)[-1]))
 assert not any(plans(env)[-1].get(k) for k in ('financial_receipt','accounting_receipt','execution_receipt'))

@pytest.mark.parametrize('venue',VENUES)
@pytest.mark.parametrize('fees,mode',[('missing','MISSING'),('foreign','PARTIAL'),('conflict','PARTIAL')])
def test_fees_no_invention_or_double_deduction(env,venue,fees,mode):
 def change(r):
  if fees=='foreign':
   if venue=='UPBIT':r['fee']={'cost':'2','currency':'BTC'}
   else:r['fee']['currency']='BNB';r['trades'][0]['fee']['currency']='BNB'
  elif fees=='conflict':
   if venue=='UPBIT':r['fee']={'cost':'9','currency':'KRW'}
   else:r['fee']['cost']='9'
 emit(env,venue,'buy','150',fee=None if fees=='missing' else '1',change=change)
 emit(env,venue,'sell','160',fee=None if fees=='missing' else '1')
 r=estimate(env);assert r['fee_mode']==mode
 assert D(r['amount'])==(10 if fees=='missing' else 9)
 wire=send(env);text=json.dumps(wire[0]['json'],ensure_ascii=False)
 assert ('수수료 미반영 추정 차익' if mode=='MISSING' else '일부 수수료 미반영') in text

@pytest.mark.parametrize('venue',VENUES)
def test_multiple_buys_partial_sell_restart(env,venue):
 emit(env,venue,'buy','100',qty='1');emit(env,venue,'buy','200',qty='1')
 emit(env,venue,'sell','160',qty='0.5',fee='0.5');a=estimate(env)
 assert D(a['entry_price'])==150 and D(a['amount'])==4
 assert len(send(env))==1
 # Re-open both stores and replay committed requests, without altering due.
 I.replay(env.st,store=store(env));H.dp.verification_tick(env.st,lambda *a:None)
 emit(env,venue,'sell','170',qty='1.5',fee='1.5');b=estimate(env)
 assert D(b['entry_price'])==150 and D(b['amount'])==27
 assert len(send(env))==1 and len(send(env))==0

@pytest.mark.parametrize('venue',VENUES)
@pytest.mark.parametrize('fault',['no_buy','strategy','account','quantity'])
def test_unavailable_cost_never_borrowed(env,venue,fault):
 if fault!='no_buy':emit(env,venue,'buy','150')
 if fault=='account':setattr(env.st,venue+'_KEY','mock-account-B')
 emit(env,venue,'sell','160',strategy='strategy-B' if fault=='strategy' else 'strategy-A',qty='2' if fault=='quantity' else '1')
 assert estimate(env)['status']=='UNAVAILABLE'
 assert '추정 손익: 계산 불가' in json.dumps(send(env)[0]['json'],ensure_ascii=False)

@pytest.mark.parametrize('venue',VENUES)
@pytest.mark.parametrize('fault',['pending','rejected','zero','missing_price','duplicate_trade','wrong_order'])
def test_no_fictitious_sell(env,venue,fault):
 emit(env,venue,'buy','150')
 def change(r):
  t=r['trades'][0]
  if fault=='pending':r['state' if venue=='UPBIT' else 'status']='wait' if venue=='UPBIT' else 'open'
  if fault=='rejected':r['state' if venue=='UPBIT' else 'status']='rejected'
  if fault=='zero':r['executed_volume' if venue=='UPBIT' else 'filled']='0';r['trades']=[]
  if fault=='missing_price':t.pop('price')
  if fault=='duplicate_trade':r['trades']+=copy.deepcopy(r['trades'])
  if fault=='wrong_order':t['order']='other-order'
 emit(env,venue,'sell','160',change=change)
 assert plans(env)==[] and send(env)==[]

@pytest.mark.parametrize('venue',VENUES)
def test_same_order_repeated_and_status_update(env,venue):
 emit(env,venue,'buy','150')
 oid,r,res=emit(env,venue,'sell','160',change=lambda r:r.update(state='wait') if venue=='UPBIT' else r.update(status='open'))
 # The same order can progress from open to complete; no request-price fallback.
 emit(env,venue,'sell','160',oid=oid);assert len(send(env))==1
 emit(env,venue,'sell','160',oid=oid);assert send(env)==[]
 with store(env)._connect_read() as c:
  assert c.execute('SELECT count(*) FROM vpnl_roots').fetchone()[0]==1
  assert c.execute('SELECT count(*) FROM vpnl_deliveries').fetchone()[0]==1

@pytest.mark.parametrize('state',['UNKNOWN','HELD'])
def test_uncertain_does_not_repost(env,state):
 emit(env,'BINANCE','buy','150');oid,raw,_=emit(env,'BINANCE','sell','160')
 s=store(env)
 with s._connect() as c:c.execute('UPDATE vpnl_deliveries SET status=?',(state,))
 emit(env,'BINANCE','sell','160',oid=oid)
 assert send(env)==[]
 with s._connect_read() as c:assert c.execute('SELECT status FROM vpnl_deliveries').fetchone()[0]==state

def test_crash_after_v4_commit_before_sidecar_ack(env):
 emit(env,'BINANCE','buy','150')
 original=I.IntakeStore.mark
 def fail(self,key,state,*a,**kw):
  if state=='SUBMITTED':raise RuntimeError('mock-crash')
  return original(self,key,state,*a,**kw)
 with patch.object(I.IntakeStore,'mark',fail):oid,r,res=emit(env,'BINANCE','sell','160',tick=False)
 I.replay(env.st);H.dp.verification_tick(env.st,lambda *a:None)
 assert len(send(env))==1
 I.replay(env.st);assert send(env)==[]
 with store(env)._connect_read() as c:assert c.execute('SELECT count(*) FROM vpnl_jobs').fetchone()[0]==2

def test_fake_payload_or_producer_cannot_admit(env):
 emit(env,'BINANCE','buy','150');emit(env,'BINANCE','sell','160')
 p=H.dp.NotificationPayload(**plans(env)[-1]);p.event_id='forged'
 assert not store(env).enqueue(p)
 with pytest.raises(ValueError):N.producer_token(lambda:None)
 with pytest.raises(ValueError):I.IntakeStore(env.st.VERIFIED_NOTIFICATIONS_DB_PATH).record(dict(info=vars(info('BINANCE','sell')),result={'id':'untrusted'},source_account_ref='1'*64),observation={},producer='POA_MAIN_LOG_V1')

def test_real_worker_without_provider(env):
 emit(env,'BINANCE','buy','150',tick=False);emit(env,'BINANCE','sell','160',tick=False)
 with FrozenWireObserver(store(env)) as obs:
  stop=H.dp.start_worker(env.st);assert stop is not None
  try:
   end=time.time()+8
   while not obs.records and time.time()<end:time.sleep(.03)
  finally:
   stop.set()
   end=time.time()+5
   while any(t.name=='verified-notifications' for t in threading.enumerate()) and time.time()<end:time.sleep(.03)
 assert len(obs.finish())==1

def test_legacy_v1_wait_provider_not_retro_sent(env):
 inf=info('BINANCE','sell');H.dp.dispatch_verified_alert('BINANCE',{'id':'legacy-order'},inf,env.st)
 I.replay(env.st)
 with I.IntakeStore(env.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:
  assert c.execute('SELECT status FROM notification_intake').fetchone()[0]=='WAIT_PROVIDER'
 assert send(env)==[]

def test_financial_priority_real_producer(history):
 h=history;r=h.rig;handler=G.live_log(r.st)
 handler('BINANCE',copy.deepcopy(h.state['orders']['sell']),H.t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='sell',amount=1))
 H.dp.verification_tick(r.st,r.boot.client_for)
 with H.ob.NotificationOutboxStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH)._connect_read() as c:rows=c.execute('SELECT payload FROM vpnl_revisions').fetchall()
 assert len(rows)==1
 p=H.dp.NotificationPayload(**json.loads(rows[0][0]));assert E.payload_valid(p) and p.notification_type is None
 assert D(p.net_pnl.split()[0])==8 and D(p.cost_basis)==151
 with FrozenWireObserver(H.ob.NotificationOutboxStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH)) as obs:H.dp.flush_outbox(r.st)
 assert len(obs.finish())==1

def test_original_financial_serialization_omits_added_fields():
 p=H.dp.NotificationPayload(event_id='compat',exchange='BINANCE',symbol='BTC/USDT',side='매도',order_name='test',exit_reason='MANUAL',pnl_result='UNKNOWN',fill_status='UNVERIFIED',event_timestamp=1)
 assert 'notification_type' not in p.dict() and 'estimated_pnl' not in json.loads(p.json())

def test_concurrent_ten_producers_deduplicate(env):
 import concurrent.futures
 emit(env,'BINANCE','buy','150')
 raw=G.observed('BINANCE','1002','sell','160','1');order=info('BINANCE','sell')
 with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:out=list(pool.map(lambda _:env.log('BINANCE',copy.deepcopy(raw),order.copy(deep=True)),range(10)))
 assert all(x['accepted'] for x in out)
 H.dp.verification_tick(env.st,lambda *a:None);assert len(send(env))==1
 with store(env)._connect_read() as c:
  assert c.execute('SELECT count(*) FROM vpnl_jobs').fetchone()[0]==2
  assert c.execute('SELECT count(*) FROM vpnl_roots').fetchone()[0]==1

def test_snapshot_is_append_only_and_sidecar_binding_checked(env):
 emit(env,'BINANCE','buy','150');emit(env,'BINANCE','sell','160')
 with store(env)._connect() as c:
  with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE vpnl_notice_inputs SET snapshot='{}'")
 side=I.IntakeStore(env.st.VERIFIED_NOTIFICATIONS_DB_PATH)
 with side.connect() as c:
  with pytest.raises(sqlite3.IntegrityError):c.execute("DELETE FROM notification_observation_admissions")
 assert len(send(env))==1

def test_log_and_stored_snapshot_canaries_excluded(env,capsys):
 from loguru import logger
 logs=[];sink=logger.add(lambda m:logs.append(str(m)))
 try:
  emit(env,'BINANCE','buy','150',change=lambda r:r.update(apiKey='FAKE_API_KEY_CANARY',authorization='Bearer FAKE_BEARER_CANARY',http='https://FAKE_WEBHOOK_CANARY'))
  emit(env,'BINANCE','sell','160');records=send(env)
  # Malformed external payload rejection must not echo its body.
  outcome=env.log('BINANCE',{'id':'FAKE_API_KEY_CANARY'},info('BINANCE','sell'))
  assert not outcome['accepted']
 finally:logger.remove(sink)
 with store(env)._connect_read() as c:db=''.join(str(x) for x in c.execute('SELECT snapshot FROM vpnl_notice_inputs'))
 with I.IntakeStore(env.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:db+=''.join(str(x) for x in c.execute('SELECT request,error FROM notification_intake'))
 outer=capsys.readouterr();text=db+json.dumps(records)+''.join(logs)+outer.out+outer.err
 assert not any(v in text for v in ('FAKE_API_KEY_CANARY','FAKE_BEARER_CANARY','FAKE_WEBHOOK_CANARY'))

def test_previous_verified_sent_never_reposted_as_estimate(history):
 from test_bundle4_history import run_order,send as old_send,store as hs
 h=history;run_order(h);assert len(old_send(h))==1
 before=None
 with hs(h)._connect_read() as c:before=c.execute('SELECT wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()
 # Same notification key/order; provider-independent new boundary must not duplicate.
 handler=G.live_log(h.rig.st)
 handler('BINANCE',copy.deepcopy(h.state['orders']['sell']),H.t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='sell',amount=1))
 H.dp.verification_tick(h.rig.st,h.rig.boot.client_for)
 with FrozenWireObserver(hs(h)) as obs:H.dp.flush_outbox(h.rig.st)
 assert obs.finish()==[]
 with hs(h)._connect_read() as c:assert c.execute('SELECT wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()==before

def test_estimated_sent_blocks_later_verified_duplicate(history):
 from test_bundle3_bootstrap import active
 from test_bundle4_history import request,store as hs
 h=history;r=h.rig
 configure_execution_bootstrap(None);configure_history_provider(None)
 handler=G.live_log(r.st)
 # A concrete source account is configured but remains the bootstrap's stable host reference.
 # Rig uses its unconfigured host reference; resolve it through its supplied host config.
 from exchange.verified_pnl import bootstrap as B
 # Retain provider mapping while disabling HISTORY only: execution stays VERIFIED,
 # accounting is unavailable and the registered boundary chooses ESTIMATED.
 active(r)
 for side in ('buy','sell'):
  handler('BINANCE',copy.deepcopy(h.state['orders'][side]),request(side));H.dp.verification_tick(r.st,r.boot.client_for)
  if side=='buy':
   with FrozenWireObserver(hs(h)) as buywire:H.dp.flush_outbox(r.st)
   assert len(buywire.finish())==1
 with FrozenWireObserver(hs(h)) as obs:H.dp.flush_outbox(r.st)
 assert len(obs.finish())==1
 with hs(h)._connect_read() as c:p=json.loads(c.execute("SELECT payload FROM vpnl_revisions WHERE json_extract(payload,'$.order_id')='sell'").fetchone()[0]);assert p['notification_type']=='ESTIMATED'
 configure_history_provider(h.provider)
 H.dp.dispatch_verified_alert('BINANCE',{'id':'sell'},request('sell'),r.st);H.dp.verification_tick(r.st,r.boot.client_for)
 with FrozenWireObserver(hs(h)) as later:H.dp.flush_outbox(r.st)
 assert later.finish()==[]

def test_estimated_pending_superseded_by_verified(history):
 from test_bundle4_history import request,store as hs
 h=history;r=h.rig;handler=G.live_log(r.st)
 configure_history_provider(None)
 for side in ('buy','sell'):
  handler('BINANCE',copy.deepcopy(h.state['orders'][side]),request(side));H.dp.verification_tick(r.st,r.boot.client_for)
  if side=='buy':
   with FrozenWireObserver(hs(h)) as buywire:H.dp.flush_outbox(r.st)
   assert len(buywire.finish())==1
 with hs(h)._connect_read() as c:assert c.execute("SELECT count(*) FROM vpnl_deliveries WHERE status='PENDING'").fetchone()[0]==1
 configure_history_provider(h.provider)
 H.dp.dispatch_verified_alert('BINANCE',{'id':'sell'},request('sell'),r.st);H.dp.verification_tick(r.st,r.boot.client_for)
 with FrozenWireObserver(hs(h)) as obs:H.dp.flush_outbox(r.st)
 records=obs.finish();assert len(records)==1
 assert '추정' not in json.dumps(records[0]['json'],ensure_ascii=False)
 with hs(h)._connect_read() as c:assert sorted(x[0] for x in c.execute("SELECT d.status FROM vpnl_deliveries d JOIN vpnl_revisions r ON r.root_id=d.root_id AND r.revision=d.revision WHERE json_extract(r.payload,'$.order_id')='sell'"))==['SENT','SUPERSEDED_UNSENT']

@pytest.mark.parametrize('venue',VENUES)
def test_separate_process_replay_to_wire(env,venue,tmp_path):
 import subprocess,sys
 emit(env,venue,'buy','150',tick=False);emit(env,venue,'sell','160',tick=False)
 output=tmp_path/'child_wire.json'
 code="""
import json,sys
from pathlib import Path
import r6_worker_harness as H
from exchange.verified_pnl.intake import replay
from bundle5_wire_observer import FrozenWireObserver
st=H.settings(sys.argv[1]);st.UPBIT_VERIFIED_ALERTS=True;st.UPBIT_NET_PNL_ENABLED=True
for v in ('BINANCE','BYBIT','OKX','UPBIT'):setattr(st,v+'_KEY','mock-account-A')
replay(st)
H.dp.verification_tick(st,lambda *a:(_ for _ in ()).throw(AssertionError('NO_LIVE_CLIENT')))
store=H.ob.NotificationOutboxStore(st.VERIFIED_NOTIFICATIONS_DB_PATH)
with FrozenWireObserver(store) as o:H.dp.flush_outbox(st)
r=o.finish();assert len(r)==1
Path(sys.argv[2]).write_text(json.dumps(r),encoding='utf-8')
"""
 child=subprocess.run([sys.executable,'-c',code,env.st.VERIFIED_NOTIFICATIONS_DB_PATH,str(output)],cwd=Path(__file__).resolve().parents[2],capture_output=True,timeout=25)
 assert child.returncode==0,child.stderr.decode('utf-8','replace')[-400:]
 records=json.loads(output.read_text(encoding='utf-8'));assert records[0]['ack']['status']=='SENT'
 assert send(env)==[]


def test_restore_old_v4_new_observation_sidecar_blocks_post(env,tmp_path):
 from exchange.verified_pnl.intake import restore_preflight
 s=store(env);backup=tmp_path/'old_v4.sqlite'
 with sqlite3.connect(s.db_path) as a,sqlite3.connect(backup) as b:a.backup(b)
 emit(env,'BINANCE','buy','150');emit(env,'BINANCE','sell','160');assert len(send(env))==1
 # Controlled independent SQLite snapshot replacement (no production files).
 with sqlite3.connect(backup) as a,sqlite3.connect(s.db_path) as b:a.backup(b)
 I._BOOT_CHECKED.discard(s.db_path)
 result=restore_preflight(store(env));assert result['blocked']
 assert send(env)==[]


def test_synthetic_strategy_account_symbol_cross_reference_rejected(env):
 emit(env,'BINANCE','buy','150');emit(env,'BINANCE','sell','160')
 s=store(env);original_payload=plans(env)[-1]
 with s._connect() as c:
  key,raw=c.execute("SELECT id,data FROM vpnl_jobs WHERE json_extract(data,'$.info.is_sell')=1").fetchone();job=s.parse_job(raw)
  # Admission authority also checks the actual owned producer, not just a flag.
  p=H.dp.NotificationPayload(**original_payload)
  assert s._estimated_authority(c,p,key,job)
  for field,value in [('strategy_id','another-strategy'),('side','매수'),('symbol','ETH/USDT'),('order_id','other-order')]:
   bad=p.copy(deep=True);setattr(bad,field,value);assert not s._estimated_authority(c,bad,key,job)


def test_duplicate_trade_fee_and_summary_counted_once(env):
 emit(env,'BINANCE','buy','150');emit(env,'BINANCE','sell','160',change=lambda r:r.update(fees=[copy.deepcopy(r['fee'])]))
 assert D(estimate(env)['amount'])==8


def test_upbit_market_buy_total_is_never_unit_price(env):
 emit(env,'UPBIT','buy','100000000',fee='50000',change=lambda r:r.update(ord_type='price',price='1000000000'))
 emit(env,'UPBIT','sell','110000000',fee='55000')
 p=estimate(env);assert D(p['entry_price'])==100000000 and D(p['amount'])==9895000

@pytest.mark.parametrize('venue',VENUES)
def test_explicit_zero_fee_is_not_missing(env,venue):
 emit(env,venue,'buy','150',fee='0');emit(env,venue,'sell','160',fee='0')
 r=estimate(env);assert r['fee_mode']=='COMPLETE' and D(r['amount'])==10

@pytest.mark.parametrize('venue',['BINANCE','BYBIT','OKX'])
def test_normalized_rebate_is_once_not_a_fee_sign_flip(env,venue):
 emit(env,venue,'buy','150',fee='-1');emit(env,venue,'sell','160',fee='-1')
 r=estimate(env);assert r['fee_mode']=='COMPLETE' and D(r['amount'])==12


def test_observation_commit_failure_not_accepted(env):
 from contextlib import contextmanager
 side=I.IntakeStore(env.st.VERIFIED_NOTIFICATIONS_DB_PATH)
 old=I.IntakeStore.connect;calls=[0]
 @contextmanager
 def fail(self):
  calls[0]+=1
  with old(self) as c:
   yield c
   if calls[0]>=2:raise sqlite3.OperationalError('FAKE_API_KEY_CANARY')
 with patch.object(I.IntakeStore,'connect',fail):
  response=env.log('BINANCE',G.observed('BINANCE','1001','buy','150','1'),info('BINANCE','buy'))
 assert response['accepted'] is False
 with side.connect() as c:assert c.execute('SELECT count(*) FROM notification_intake').fetchone()[0]==0


def test_observation_admission_tamper_quarantines_without_post(env):
 emit(env,'BINANCE','buy','150',tick=False);emit(env,'BINANCE','sell','160',tick=False)
 side=I.IntakeStore(env.st.VERIFIED_NOTIFICATIONS_DB_PATH)
 # Simulates corruption, not a trusted producer. Drop protection only in fixture.
 with side.connect() as c:
  c.execute('DROP TRIGGER notice_admission_UPDATE')
  c.execute("UPDATE notification_observation_admissions SET observation_digest=?",('0'*64,))
 I._BOOT_CHECKED.discard(store(env).db_path)
 H.dp.verification_tick(env.st,lambda *a:None);assert send(env)==[]


def test_sold_event_never_changes_sent_bytes_on_new_observation(env):
 emit(env,'BINANCE','buy','150');oid,_,_=emit(env,'BINANCE','sell','160');assert len(send(env))==1
 s=store(env)
 with s._connect_read() as c:before=c.execute('SELECT wire,wire_digest,message_id,status FROM vpnl_deliveries').fetchall()
 emit(env,'BINANCE','sell','161',oid=oid);assert send(env)==[]
 with s._connect_read() as c:assert c.execute('SELECT wire,wire_digest,message_id,status FROM vpnl_deliveries').fetchall()==before
