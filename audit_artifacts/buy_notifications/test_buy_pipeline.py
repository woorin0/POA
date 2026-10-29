import sys,json,copy,ast,asyncio,time,threading,sqlite3
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from decimal import Decimal as D
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tv_signal'))
import test_tv_signal as V
T=V.T;H=V.H;env=T.env

@pytest.mark.parametrize('venue',T.VENUES)
@pytest.mark.parametrize('kind',['alert','strategy_order_fill'])
def test_buy_outbox_own_admission(env,venue,kind):
 e=env;r,_=V.emit(e,'buy','150','2',venue=venue,extra={'kind':kind})
 plans=T.plans(e);assert len(plans)==1
 p=plans[0];assert p['side']=='매수' and p['fill_status']=='UNVERIFIED'
 assert all(p[k] is None for k in ('net_pnl','roi_pct','execution_receipt','financial_receipt','accounting_receipt'))
 value=p['estimated_pnl'];assert value['virtual_average']=='150' and value['virtual_quantity']=='2'
 assert len(T.send(e))==1


def test_startup_failure_is_diagnosed(env):
 e=env;messages=[];ns=startup_namespace(e.st,messages)
 from loguru import logger
 sink=logger.add(lambda message:messages.append(str(message)),format='{message}')
 try:
  with patch.object(H.dp,'start_worker',side_effect=RuntimeError('Bearer SYNTHETIC_CANARY')):asyncio.run(ns['startup']())
 finally:logger.remove(sink)
 assert ns['app'].state.notification_worker_status=='WORKER_START_FAILED'
 assert any('WORKER_START_FAILED' in str(x) for x in messages)
 assert 'SYNTHETIC_CANARY' not in str(messages)


def startup_namespace(settings,messages):
 root=Path.cwd();tree=ast.parse((root/'main.py').read_text(encoding='utf-8-sig'));node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='startup');node.decorator_list=[]
 ns=dict(app=NS(state=NS()),settings=settings,log_message=messages.append,VERSION='test')
 exec(compile(ast.Module(body=[node],type_ignores=[]),str(root/'main.py'),'exec'),ns)
 return ns


@pytest.mark.parametrize('venue',T.VENUES)
def test_buy_sell_cost_unchanged(env,venue):
 e=env;V.emit(e,'buy','150','2',venue=venue);V.emit(e,'sell','160','1',venue=venue)
 plans=T.plans(e);assert len(plans)==2 and plans[0]['estimated_pnl']['amount'] is None
 assert D(plans[1]['estimated_pnl']['amount'])==10 and D(plans[1]['estimated_pnl']['entry_price'])==150
 assert len(T.send(e))==2


def test_average_partial_sell_then_buy(env):
 e=env;V.emit(e,'buy','100','1');V.emit(e,'buy','200','1');V.emit(e,'sell','160','1');V.emit(e,'buy','140','1')
 plans=T.plans(e);assert D(plans[2]['estimated_pnl']['amount'])==10
 r=plans[3]['estimated_pnl'];assert D(r['virtual_average'])==145 and D(r['virtual_quantity'])==2
 assert len(T.send(e))==4


def test_strategies_accounts_and_long_hold_isolated(env):
 e=env;V.emit(e,'buy','100','100',strategy='long-strategy');V.emit(e,'buy','150','2',strategy='bot-A');V.emit(e,'sell','160','1',strategy='bot-A')
 assert D(T.plans(e)[-1]['estimated_pnl']['amount'])==10
 e.st.UPBIT_KEY='other-test-account';V.emit(e,'sell','999','1',strategy='bot-A')
 assert T.plans(e)[-1]['estimated_pnl']['status']=='UNAVAILABLE'


def test_signal_and_order_duplicates(env):
 e=env;V.emit(e,'buy','150',sid='unique-buy',oid='original-order');assert len(T.send(e))==1
 V.emit(e,'buy','150',sid='unique-buy',oid='second-ack');assert T.send(e)==[]
 V.emit(e,'buy','150',sid='unique-buy',oid='original-order');assert T.send(e)==[]
 V.emit(e,'sell','160');assert D(T.plans(e)[-1]['estimated_pnl']['amount'])==10
 assert len(T.send(e))==1

@pytest.mark.parametrize('state',['SENT','UNKNOWN','HELD','POSTING'])
def test_buy_delivery_state_and_wire_immutable(env,state):
 e=env;V.emit(e,'buy','150',sid='buy-immutable');st=T.store(e)
 if state=='SENT':assert len(T.send(e))==1
 else:
  with st._connect() as c:c.execute('UPDATE vpnl_deliveries SET status=?',(state,))
 with st._connect_read() as c:before=c.execute('SELECT * FROM vpnl_deliveries').fetchall();revisions=c.execute('SELECT * FROM vpnl_revisions').fetchall()
 V.emit(e,'buy','150',sid='buy-immutable');assert T.send(e)==[]
 with st._connect_read() as c:assert c.execute('SELECT * FROM vpnl_deliveries').fetchall()==before and c.execute('SELECT * FROM vpnl_revisions').fetchall()==revisions

@pytest.mark.parametrize('fault',['side','strategy','account','symbol','order','amount','roi','receipt','fill','financial_flag','filled_qty'])
def test_buy_admission_rejects_forgery_with_live_lease(env,fault):
 e=env;V.emit(e,'buy','150');st=T.store(e);p=H.dp.NotificationPayload(**T.plans(e)[0]);iid=p.estimated_pnl['intake_id']
 with st._connect() as c:
  key=c.execute('SELECT job_id FROM vpnl_notice_inputs WHERE intake_id=?',(iid,)).fetchone()[0]
  c.execute("UPDATE vpnl_jobs SET state='VERIFYING',job_lease='test-owned-lease',due=? WHERE id=?",(time.time()+120,key))
 if fault=='side':p.side='매도'
 if fault=='strategy':p.strategy_id='other-strategy'
 if fault=='account':p.estimated_pnl['scope']['account']='0'*64
 if fault=='symbol':p.symbol='ETH/KRW'
 if fault=='order':p.order_id='other-order'
 if fault=='amount':p.estimated_pnl['amount']='1'
 if fault=='roi':p.estimated_pnl['roi']='1'
 if fault=='receipt':p.execution_receipt={'verified':True}
 if fault=='fill':p.fill_status=H.FillStatus.FILLED
 if fault=='financial_flag':p.pnl_verification_status=H.VerificationStatus.VERIFIED
 if fault=='filled_qty':p.filled_qty='999'
 assert st.enqueue(p,max_attempts=1,job_claim=(key,'test-owned-lease')) is False

@pytest.mark.parametrize('venue',T.VENUES)
def test_no_signal_confirmed_order_buy_compatibility(env,venue):
 e=env;T.emit(e,venue,'buy','150');assert len(T.plans(e))==1
 p=T.plans(e)[0];assert p['side']=='매수' and p['execution_receipt'] is None
 assert p['estimated_pnl']['basis']=='POA_BUY_OBSERVATION' and p['estimated_pnl']['amount'] is None
 T.emit(e,venue,'sell','160');assert D(T.plans(e)[-1]['estimated_pnl']['amount'])==8
 assert len(T.send(e))==2


def test_missing_order_id_not_filled(env):
 e=env;info=T.info('UPBIT','buy');r=e.log('UPBIT',{},info)
 assert not any(p['fill_status']=='FILLED' for p in T.plans(e))


def test_worker_configuration_and_store_failure(env):
 e=env;e.st.VERIFIED_FILL_PRICE=False
 assert H.dp.start_worker(e.st) is None
 e.st.VERIFIED_FILL_PRICE=True
 with patch.object(H.dp,'NotificationOutboxStore',side_effect=sqlite3.OperationalError('Bearer SYNTHETIC_CANARY')):
  worker=H.dp.start_worker(e.st);worker.worker_thread.join(3)
 assert worker.notification_status['state']=='FAILED' and worker.notification_status['code'].startswith('WORKER_STORE_')
 assert 'CANARY' not in json.dumps(worker.notification_status)


def test_real_worker_restart_buy_and_sell(env):
 e=env;V.emit(e,'buy','150',tick=False);V.emit(e,'sell','160',tick=False)
 st=T.store(e)
 with T.FrozenWireObserver(st) as observer:
  stop=H.dp.start_worker(e.st)
  try:
   end=time.monotonic()+12
   while len(observer.records)<2 and time.monotonic()<end:time.sleep(.04)
   assert stop.notification_status['ticks']>=1
  finally:stop.set();stop.worker_thread.join(5)
 assert len(observer.finish())==2 and stop.notification_status['code']=='WORKER_STOPPED'
 with T.FrozenWireObserver(st) as observer:
  stop=H.dp.start_worker(e.st)
  try:
   end=time.monotonic()+6
   while stop.notification_status['ticks']<1 and time.monotonic()<end:time.sleep(.04)
  finally:stop.set();stop.worker_thread.join(5)
 assert observer.finish()==[]


def test_startup_disabled_or_none(env):
 e=env;messages=[];ns=startup_namespace(e.st,messages)
 with patch.object(H.dp,'start_worker',return_value=None):asyncio.run(ns['startup']())
 assert ns['app'].state.notification_worker_status=='WORKER_NOT_STARTED'
 e.st.VERIFIED_DISCORD_ALERTS=False
 with patch.object(H.dp,'start_worker',side_effect=AssertionError('disabled')):asyncio.run(ns['startup']())
 assert ns['app'].state.notification_worker_status=='DISABLED'


def test_signal_mobile_colors_and_kst(env):
 e=env;V.emit(e,'buy','100000000','0.005');V.emit(e,'sell','110000000','0.005');wires=T.send(e);assert len(wires)==2
 body=json.dumps(wires,ensure_ascii=False);assert 'KST' in body and '주문 체결 확인 중' in body and '체결 완료' not in body
 assert '예상 손익' in body and '50,000' in body and '10.00' in body
 assert '1483594' in body or str(0x16A34A) in body
 assert str(0xEF4444) in body


def test_diagnostic_readonly_and_redacted(env,tmp_path):
 import subprocess,hashlib
 e=env;V.emit(e,'buy','150');st=T.store(e)
 with st._connect() as c:c.execute("UPDATE vpnl_jobs SET state='Bearer SYNTHETIC_CANARY'")
 db=Path(e.st.VERIFIED_NOTIFICATIONS_DB_PATH);before=hashlib.sha256(db.read_bytes()).hexdigest()
 result=subprocess.run([sys.executable,'scripts/notification_pipeline_status.py','--db',str(db)],capture_output=True,text=True,encoding='utf-8',timeout=10)
 assert result.returncode==0 and 'SYNTHETIC_CANARY' not in result.stdout+result.stderr
 assert 'OTHER' in result.stdout and str(db) not in result.stdout
 assert hashlib.sha256(db.read_bytes()).hexdigest()==before
 missing=tmp_path/'nonexistent.sqlite3'
 result=subprocess.run([sys.executable,'scripts/notification_pipeline_status.py','--db',str(missing)],capture_output=True,text=True,encoding='utf-8',timeout=10)
 assert result.returncode==2 and not missing.exists()


def test_order_and_crypto_contracts_unchanged():
 import subprocess
 base='c99be38640d0b1d665ca7f921612380028443771'
 for name in ('exchange/safe_retry.py','exchange/verified_pnl/verifier.py','exchange/verified_pnl/evidence.py','exchange/verified_pnl/history.py','exchange/verified_pnl/ledger.py','exchange/verified_pnl/bootstrap.py','exchange/verified_pnl/pnl_calculator.py','exchange/verified_pnl/reconciliation.py','exchange/verified_pnl/outbox.py'):
  assert Path(name).read_bytes()==subprocess.check_output(['git','show',base+':'+name]),name
 before=ast.parse(subprocess.check_output(['git','show',base+':main.py']).decode('utf-8-sig'));after=ast.parse(Path('main.py').read_text(encoding='utf-8-sig'))
 for name in ('order','schedule_order_log','log','get_error'):
  node=lambda t:next(n for n in ast.walk(t) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
  assert ast.dump(node(before))==ast.dump(node(after)),name
 name='exchange/model/schemas.py';old=ast.parse(subprocess.check_output(['git','show',base+':'+name]).decode('utf-8-sig'));new=ast.parse(Path(name).read_text(encoding='utf-8-sig'))
 for cls in ('OrderRequest','OrderBase','MarketOrder'):
  get=lambda tree:next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==cls)
  assert ast.dump(get(old))==ast.dump(get(new)),cls
 old_settings=next(n for n in old.body if isinstance(n,ast.ClassDef) and n.name=='Settings')
 new_settings=next(n for n in new.body if isinstance(n,ast.ClassDef) and n.name=='Settings')
 new_settings.body=[n for n in new_settings.body if not (isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id=='POA_NOTIFICATION_SERVER_ALIAS')]
 assert ast.dump(old_settings)==ast.dump(new_settings),'non-notification settings changed'
 name='exchange/verified_pnl/delivery_store.py';before=ast.parse(subprocess.check_output(['git','show',base+':'+name]).decode('utf-8-sig'));after=ast.parse(Path(name).read_text(encoding='utf-8-sig'))
 for fn in ('_wire','_attempt','begin_post','ack','claim_next','_notice_gate'):
  node=lambda t:next(n for n in ast.walk(t) if isinstance(n,ast.FunctionDef) and n.name==fn)
  assert ast.dump(node(before))==ast.dump(node(after)),fn


def test_buy_concurrent_and_separate_process_restart(env):
 import subprocess,os
 e=env;r,info=V.emit(e,'buy','150',sid='parallel-buy',tick=False)
 results=[]
 def producer():results.append(e.log('UPBIT',{'id':'duplicate-ack'},info))
 threads=[threading.Thread(target=producer) for _ in range(10)]
 for t in threads:t.start()
 for t in threads:t.join()
 assert len(results)==10 and all(x['accepted'] for x in results) and len(set(x['intake_id'] for x in results))==1
 H.dp.verification_tick(e.st,lambda *a:pytest.fail('GET_FORBIDDEN'));assert len(T.send(e))==1
 code="""import sys
import test_estimated_contracts as T
st=T.H.settings(sys.argv[1]);st.UPBIT_VERIFIED_ALERTS=True
T.H.dp.verification_tick(st,lambda *a:(_ for _ in ()).throw(AssertionError('NO_GET')))
store=T.H.ob.NotificationOutboxStore(sys.argv[1])
with T.FrozenWireObserver(store) as observer:T.H.dp.flush_outbox(st)
assert observer.finish()==[]
"""
 result=subprocess.run([sys.executable,'-c',code,str(e.st.VERIFIED_NOTIFICATIONS_DB_PATH)],env=os.environ.copy(),capture_output=True,text=True,encoding='utf-8',timeout=30)
 assert result.returncode==0,result.stderr


def test_startup_host_exception_does_not_kill_other_venues(env):
 from exchange.verified_pnl import upbit_notification_platform as P
 e=env;messages=[];ns=startup_namespace(e.st,messages);stop=threading.Event()
 with patch.object(P,'prepare_platform_upbit_host',side_effect=RuntimeError('synthetic')),patch.object(H.dp,'start_worker',return_value=stop) as worker:
  asyncio.run(ns['startup']())
 assert worker.call_count==1 and ns['app'].state.verified_notifications_stop is stop


def test_intake_failure_not_false_accepted(env):
 from exchange.verified_pnl import intake as I
 e=env
 with patch.object(I.IntakeStore,'record',side_effect=sqlite3.OperationalError('synthetic')):r,_=V.emit(e,'buy','150')
 assert r['accepted'] is False and r['status']=='PERSISTENCE_FAILED' and not T.plans(e)


def test_pending_no_signal_buy_cannot_fake_execution(env):
 e=env;info=T.info('BYBIT','buy');r=e.log('BYBIT',{'id':'id-only-no-provider'},info);assert r['accepted']
 H.dp.verification_tick(e.st,lambda *a:pytest.fail('NO_GET'))
 assert T.plans(e)==[]


def test_outbox_admission_failure_is_visible_review(env):
 e=env;st=T.store(e)
 with patch.object(type(st),'enqueue',return_value=False):V.emit(e,'buy','150')
 with st._connect_read() as c:assert c.execute('SELECT state FROM vpnl_jobs').fetchone()[0]=='OBSERVED_REVIEW'
 assert T.plans(e)==[]


sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'simple_upbit'))
import test_simple_upbit as S
local=S.local


def test_upbit_id_only_get_buy_sell_restores_both(local):
 r=local;S.submit(r,'recovered-buy','buy',['100000000']);S.submit(r,'recovered-sell','sell',['110000000'])
 plans=S.payloads(r);assert len(plans)==2 and plans[0]['side']=='매수' and plans[1]['side']=='매도'
 assert D(plans[1]['estimated_pnl']['amount'])==9895000
 assert all(p['execution_receipt'] is None and p['financial_receipt'] is None for p in plans)
 assert len(r.calls)==2 and len(S.T.send(r))==2


def test_native_missing_buy_or_fee_stays_explicit(local):
 r=local;S.submit(r,'unknown-cost-sell','sell',['110000000'])
 assert S.payloads(r)[0]['estimated_pnl']['amount'] is None
 wires=S.T.send(r);assert len(wires)==1 and str(0x64748B) in json.dumps(wires)
 for side,price in [('buy','100000000'),('sell','110000000')]:
  r.seq+=1;raw=S.T.raw_order('gross-'+side,'bid' if side=='buy' else 'ask',[price],S.T.STAMP+r.seq*1000);raw.pop('paid_fee');r.native[raw['uuid']]=raw
  info=S.T.T.info('UPBIT',side,strategy='gross-strategy');assert r.log('UPBIT',{'id':raw['uuid']},info)['accepted'];S.T.tick(r)
 assert S.payloads(r)[-1]['estimated_pnl']['fee_mode']=='MISSING'
 assert D(S.payloads(r)[-1]['estimated_pnl']['amount'])==10000000
 body=json.dumps(S.T.send(r),ensure_ascii=False);assert '수수료 미반영' in body


def test_readonly_diagnostic_schema_failure_redacts(tmp_path):
 import subprocess
 p=tmp_path/'fixture.sqlite3'
 with sqlite3.connect(p) as c:c.execute('CREATE TABLE vpnl_jobs(wrong TEXT)')
 with sqlite3.connect(str(p)+'.intake.sqlite3') as c:c.execute('CREATE TABLE arbitrary(x)')
 result=subprocess.run([sys.executable,'scripts/notification_pipeline_status.py','--db',str(p)],capture_output=True,text=True,timeout=10)
 assert result.returncode==2 and 'UNSUPPORTED' in result.stdout and str(p) not in result.stdout


def test_worker_legacy_cutover_block_is_explicit(env):
 e=env;path=e.st.VERIFIED_NOTIFICATIONS_DB_PATH
 with sqlite3.connect(path) as c:
  c.execute('CREATE TABLE verified_jobs(id TEXT PRIMARY KEY,data TEXT,state TEXT,due REAL,created REAL)')
  c.execute("INSERT INTO verified_jobs VALUES('legacy','{}','HELD',0,0)")
 worker=H.dp.start_worker(e.st);worker.worker_thread.join(4)
 assert worker.notification_status['code']=='WORKER_CUTOVER_REQUIRED'
 assert worker.notification_status['state']=='BLOCKED'
 with sqlite3.connect(path) as c:assert c.execute('SELECT count(*) FROM verified_jobs').fetchone()[0]==1


@pytest.mark.parametrize('venue',T.VENUES)
def test_wrong_symbol_no_new_root_delivery_or_sell_post(env,venue):
 e=env;T.emit(e,venue,'buy','150');assert len(T.send(e))==1
 st=T.store(e)
 with st._connect_read() as c:before=(c.execute('SELECT count(*) FROM vpnl_roots').fetchone()[0],c.execute('SELECT count(*) FROM vpnl_deliveries').fetchone()[0])
 def wrong(raw):
  oid=raw.get('id') or raw.get('uuid');quote='KRW' if venue=='UPBIT' else 'USDT'
  raw.clear();raw.update(id=oid,status='closed',symbol='ETH/'+quote,side='sell',filled='1',amount='1',remaining='0',average='160',trades=[],fee=dict(cost='1',currency=quote))
 T.emit(e,venue,'sell','160',change=wrong)
 assert T.send(e)==[]
 with st._connect_read() as c:assert before==(c.execute('SELECT count(*) FROM vpnl_roots').fetchone()[0],c.execute('SELECT count(*) FROM vpnl_deliveries').fetchone()[0])
 assert not any(p['side']=='매도' for p in T.plans(e))


@pytest.mark.parametrize('message,category',[('database or disk is full','INTAKE_DISK_FULL'),('attempt to write a readonly database','INTAKE_READONLY'),('database disk image is malformed','INTAKE_CORRUPT'),('unable to open database file','INTAKE_UNAVAILABLE')])
def test_python310_worker_db_codes(env,message,category):
 with patch.object(H.dp,'NotificationOutboxStore',side_effect=sqlite3.OperationalError(message)):
  worker=H.dp.start_worker(env.st);worker.worker_thread.join(3)
 assert worker.notification_status['code']=='WORKER_STORE_'+category
