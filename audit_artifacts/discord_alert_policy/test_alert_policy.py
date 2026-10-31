import sys,copy,json,time,ast,threading,hashlib,sqlite3,asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import pytest,requests
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'estimated_implementation'))
import test_estimated_contracts as T
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tv_signal'))
import test_tv_signal as V
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot03_buy'))
import test_buy_receipts as B
from exchange.verified_pnl import operational_alerts as O,alert_presentation as C
H=T.H;env=T.env;local=B.local;history=T.history;rig=T.rig;reset=T.reset

@pytest.fixture(autouse=True)
def stop_reporters():
 yield
 for stop in list(O._RUNNING.values()):
  stop.set();stop.worker_thread.join(timeout=2)
 O._RUNNING.clear()

class Probe:
 def __init__(self,st,status=200,error=None):self.st=st;self.status=status;self.error=error;self.records=[]
 def post(self,url,**kw):
  assert isinstance(kw['data'],bytes) and 'json' not in kw and kw['allow_redirects'] is False
  raw=kw['data'];wd=hashlib.sha256(raw).hexdigest()
  j=O.Journal(self.st)
  with j.connect() as c:row=c.execute("SELECT wire,digest,status FROM ops_events WHERE digest=?",(wd,)).fetchone()
  assert row==(raw,wd,'POSTING')
  payload=json.loads(raw);assert payload['allowed_mentions']=={'parse':[]}
  assert payload['embeds'][0]['color'] in (C.ERROR,C.PENDING)
  assert '금융 VERIFIED' not in json.dumps(payload,ensure_ascii=False)
  self.records.append(payload)
  if self.error:raise self.error
  response=requests.Response();response.status_code=self.status;response.headers={'Retry-After':'1'};response._content=json.dumps({'id':'987654321000000001'}).encode();return response
 def __enter__(self):self.p=patch('requests.post',side_effect=self.post);self.p.start();return self
 def __exit__(self,*a):self.p.stop()


def info():return NS(exchange='UPBIT',unified_symbol='LINK/KRW',side='buy',is_buy=True,order_name='HL',strategy_id=None,signal_id=None)

@pytest.mark.parametrize('venue',T.VENUES)
def test_four_venues_observed_buy_sell_blue(env,venue):
 e=env;T.emit(e,venue,'buy','150');T.emit(e,venue,'sell','160')
 records=T.send(e);assert len(records)==2
 assert [r['json']['embeds'][0]['color'] for r in records]==[C.CONFIRMED_FILL]*2
 assert T.D(T.plans(e)[-1]['estimated_pnl']['amount'])==8
 assert all(p['financial_receipt'] is None for p in T.plans(e))

@pytest.mark.parametrize('venue',T.VENUES)
def test_signal_profit_purple_signal_buy_pending_gray(env,venue):
 V.emit(env,'buy','100','1',venue=venue);V.emit(env,'sell','110','1',venue=venue)
 records=T.send(env);assert [r['json']['embeds'][0]['color'] for r in records]==[C.PENDING,C.ESTIMATED]
 assert T.D(T.plans(env)[-1]['estimated_pnl']['amount'])==10
 assert '실제 체결' in json.dumps(records[-1]['json'],ensure_ascii=False)


def test_signed_fill_blue_and_financial_proof_unchanged(history):
 h=history;r=h.rig;log=T.G.live_log(r.st)
 log('BINANCE',copy.deepcopy(h.state['orders']['sell']),H.t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='sell',amount=1));H.dp.verification_tick(r.st,r.boot.client_for)
 store=H.ob.NotificationOutboxStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH)
 with store._connect_read() as c:p=H.dp.NotificationPayload(**json.loads(c.execute('SELECT payload FROM vpnl_revisions').fetchone()[0]))
 assert T.E.payload_valid(p) and T.D(p.net_pnl.split()[0])==8
 from bundle5_wire_observer import FrozenWireObserver
 with FrozenWireObserver(store) as observer:H.dp.flush_outbox(r.st)
 assert observer.finish()[0]['json']['embeds'][0]['color']==C.CONFIRMED_FILL


def test_native_sell_without_cost_blue_but_no_pnl(env):
 T.emit(env,'BINANCE','sell','160');wire=T.send(env)[0]['json']['embeds'][0]
 assert wire['color']==C.CONFIRMED_FILL and '계산 불가' in wire['description']
 assert T.plans(env)[0]['estimated_pnl']['amount'] is None


@pytest.mark.parametrize('code,phase',[('ORDER_AUTH_ERROR','ORDER'),('ORDER_PERMISSION_ERROR','ORDER'),('ORDER_PARAMETERS_ERROR','ORDER'),('ORDER_TIMEOUT','ORDER'),('ORDER_NETWORK_ERROR','ORDER'),('UPBIT_OBSERVATION_REVIEW','OBSERVATION'),('UPBIT_READ_IP_REJECTED','OBSERVATION'),('UPBIT_READ_RESPONSE_SCOPE','OBSERVATION'),('UPBIT_READ_FILL_INCOMPLETE','OBSERVATION'),('UPBIT_READ_RATE_LIMITED','OBSERVATION'),('UPBIT_OBSERVATION_DB_FAILED','OBSERVATION'),('WORKER_START_FAILED','WORKER'),('WORKER_TICK_INTAKE_CORRUPT','WORKER'),('BUY_ADMISSION_REJECTED','OUTBOX'),('SIGNAL_SELL_ADMISSION_REJECTED','OUTBOX'),('NOTIFICATION_INTAKE_REVIEW','INTAKE'),('INTAKE_SAVE_FAILED','INTAKE'),('RESTORE_PREFLIGHT_BLOCKED','RESTORE'),('DISCORD_DELIVERY_UNKNOWN','OUTBOX')])
def test_registered_operational_errors_are_red(env,code,phase):
 e=env;e.st.POA_NOTIFICATION_SERVER_ALIAS='BOT03'
 assert O.report(e.st,code,phase=phase,info=info(),order_id='test-order-1')
 with Probe(e.st) as observer:
  with patch('time.time',return_value=time.time()+6):assert O.flush(e.st)
 message=observer.records[0]['embeds'][0]
 assert message['color']==C.ERROR and code in message['description'] and '체결 여부 미확정' in message['description'] and 'BOT03' in message['description']
 assert '주문 실패 확정' not in message['description']


def test_one_hundred_duplicates_restart_recovery_recurrence(env):
 e=env
 assert sum(bool(O.report(e.st,'UPBIT_READ_TIMEOUT',phase='OBSERVATION',info=info(),order_id='order-one')) for _ in range(100))==1
 with Probe(e.st) as observer:
  assert O.flush(e.st);assert not O.flush(e.st)
  j=O.Journal(e.st)
  with j.connect() as c:assert c.execute('SELECT count(*) FROM ops_events').fetchone()[0]==1
  O.resolve(e.st,phase='OBSERVATION',info=info(),order_id='order-one')
  O.report(e.st,'UPBIT_READ_TIMEOUT',phase='OBSERVATION',info=info(),order_id='order-one')
  with patch('time.time',return_value=time.time()+11):assert O.flush(e.st)
  with patch('time.time',return_value=time.time()+22):assert O.flush(e.st)
  assert not O.flush(e.st)
 assert len(observer.records)==3
 assert observer.records[1]['embeds'][0]['color']==C.PENDING
 with O.Journal(e.st).connect() as c:assert c.execute('SELECT episode,active FROM ops_incidents').fetchone()==(2,1)


def test_global_rate_limit_and_multiple_reporters(env):
 e=env;start=time.time()
 for i in range(100):O.report(e.st,'ORDER_TIMEOUT',phase='ORDER',info=info(),order_id='order-'+str(i))
 with Probe(e.st) as observer:
  for second in range(60):
   with patch('time.time',return_value=start+second):
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:O.flush(e.st),range(2)))
 assert len(observer.records)==6


@pytest.mark.parametrize('status,error,state',[(200,None,'SENT'),(204,None,'SENT'),(401,None,'HELD'),(500,None,'UNKNOWN'),(200,requests.Timeout('SYNTHETIC_CREDENTIAL'),'UNKNOWN')])
def test_failure_is_nonrecursive_and_ambiguous_not_retried(env,status,error,state):
 e=env;O.report(e.st,'WORKER_START_FAILED')
 with Probe(e.st,status,error) as observer:
  O.flush(e.st)
  with patch('time.time',return_value=time.time()+100):assert not O.flush(e.st)
 assert len(observer.records)==1
 with O.Journal(e.st).connect() as c:
  assert c.execute('SELECT status FROM ops_events').fetchone()[0]==state
  assert c.execute('SELECT count(*) FROM ops_events').fetchone()[0]==1


def test_crash_after_post_intent_is_unknown(env):
 e=env;O.report(e.st,'WORKER_NOT_STARTED');j=O.Journal(e.st);claim=j.claim();assert claim
 with patch('time.time',return_value=time.time()+31):
  with Probe(e.st) as observer:assert not O.flush(e.st)
 assert observer.records==[]
 with j.connect() as c:assert c.execute('SELECT status FROM ops_events').fetchone()[0]=='UNKNOWN'
 assert not j.ack(claim,True,'987',None,None)


def test_bot03_review_creates_red_error_and_gray_receipt(local):
 r=local;r.status=401;B.feed(r)
 ordinary=B.S.T.send(r);assert len(ordinary)==1 and ordinary[0]['json']['embeds'][0]['color']==C.PENDING
 with Probe(r.st) as observer:O.flush(r.st)
 assert len(observer.records)==1 and observer.records[0]['embeds'][0]['color']==C.ERROR
 with B.S.T.store(r)._connect_read() as c:assert c.execute('SELECT state FROM vpnl_jobs').fetchone()[0]=='OBSERVED_REVIEW'
 assert not O.report(r.st,'UPBIT_READ_AUTH_REJECTED',phase='OBSERVATION',info=B.H.t.OrderRequest(exchange='UPBIT',base='LINK',quote='KRW',side='buy',amount=1,order_name='LL'),order_id=list(r.native)[0])


def test_worker_not_started_reported_independently(env):
 e=env;tree=ast.parse(Path('main.py').read_bytes());node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='startup');node.decorator_list=[]
 ns=dict(app=NS(state=NS()),settings=e.st,log_message=lambda *a:None,VERSION='test')
 exec(compile(ast.Module(body=[node],type_ignores=[]),str(Path('main.py').resolve()),'exec'),ns)
 import exchange.verified_pnl.upbit_notification_platform as platform
 with Probe(e.st) as observer:
  with patch.object(H.dp,'start_worker',return_value=None),patch.object(platform,'prepare_platform_upbit_host',return_value=NS(status='DISABLED',host=None)):
   asyncio.run(ns['startup']())
  stop=ns['app'].state.operational_alert_stop
  end=time.time()+4
  while not observer.records and time.time()<end:time.sleep(.02)
  stop.set();stop.worker_thread.join(timeout=2)
 assert len(observer.records)==1 and 'WORKER_NOT_STARTED' in observer.records[0]['embeds'][0]['description']
 assert ns['app'].state.verified_notifications_stop is None


def test_no_secret_or_financial_payload_can_use_operational_route(env):
 e=env;bad=NS(exchange='UPBIT',unified_symbol='LINK/KRW',side='buy',order_name='Bearer SYNTHETIC_CREDENTIAL',strategy_id=None,signal_id=None,apiKey='SYNTHETIC_API_KEY',password='SYNTHETIC_PASSWORD')
 assert O.report(e.st,'ORDER_TIMEOUT',phase='ORDER',info=bad,order_id='SYNTHETIC_ORDER_RAW')
 assert not O.report(e.st,'verified=True',phase='ORDER',info=bad)
 assert not O.report(e.st,'UPBIT_READ_FILL_PENDING',phase='OBSERVATION',info=bad)
 with Probe(e.st) as observer:O.flush(e.st)
 text=json.dumps(observer.records,ensure_ascii=False)
 with O.Journal(e.st).connect() as c:text+=''.join(str(r) for r in c.execute('SELECT context,wire FROM ops_events'))
 assert 'SYNTHETIC_' not in text and 'Bearer' not in text and 'password' not in text


def test_legacy_frozen_wire_not_recoloured(env):
 e=env
 with patch.object(C,'admit',side_effect=lambda c,p,job:p):
  T.emit(e,'BINANCE','buy','150');T.emit(e,'BINANCE','sell','160')
 with T.store(e)._connect_read() as c:before=c.execute('SELECT wire,wire_digest FROM vpnl_deliveries ORDER BY rowid').fetchall()
 records=T.send(e);assert [r['json']['embeds'][0]['color'] for r in records]==[0x16A34A,0xEF4444]
 with T.store(e)._connect_read() as c:assert c.execute('SELECT wire,wire_digest FROM vpnl_deliveries ORDER BY rowid').fetchall()==before
 assert T.send(e)==[]



def test_ops_429_retry_is_bounded(env):
 e=env;O.report(e.st,'WORKER_START_FAILED');start=time.time()
 with Probe(e.st,429) as observer:
  for second in (0,11,22,33):
   with patch('time.time',return_value=start+second):O.flush(e.st)
 assert len(observer.records)==3
 with O.Journal(e.st).connect() as c:assert c.execute('SELECT status,attempts FROM ops_events').fetchone()==('HELD',3)


def test_invalid_wire_and_ack_token_are_rejected(env):
 e=env;O.report(e.st,'WORKER_START_FAILED');j=O.Journal(e.st)
 with j.connect() as c:
  with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE ops_events SET wire=x'00'")
 claim=j.claim();bad=dict(claim,token='forged');assert not j.ack(bad,True,'123',None,None)
 assert j.ack(claim,False,None,'delivery_unknown',None)


def test_normal_fill_wait_is_not_an_error(local):
 r=local;B.feed(r,state='wait')
 j=O.Journal(r.st)
 with j.connect() as c:assert c.execute('SELECT count(*) FROM ops_events').fetchone()[0]==0
 records=B.S.T.send(r);assert len(records)==1 and records[0]['json']['embeds'][0]['color']==C.PENDING


def test_unrelated_order_cannot_resolve_observation_error(local):
 r=local;raw,_=B.feed(r,tick=False)
 host=r.host.for_strategy('LL',symbol='LINK/KRW');original=host.observe
 with patch.object(host,'observe',side_effect=RuntimeError('unsafe detail')):B.S.T.tick(r)
 j=O.Journal(r.st)
 with j.connect() as c:assert c.execute('SELECT active FROM ops_incidents').fetchone()[0]==1
 # An unrelated order cannot resolve the original incident.
 O.resolve(r.st,phase='OBSERVATION',info=info(),order_id='another-order')
 with j.connect() as c:assert c.execute('SELECT active FROM ops_incidents').fetchone()[0]==1


def test_operation_journal_failure_cannot_escape_to_order_route(env,capsys):
 sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'precision_audit'))
 import test_precision_pipeline as P
 fixture=P.route.__wrapped__(env);e=next(fixture);e.latest_oid='unused'
 try:
  import ccxt
  bot=e.namespace['get_bot']('BINANCE',None)
  body=dict(password=e.password,exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1,order_name='HL')
  async def run():
   import httpx
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app),base_url='http://isolated.test') as client:return await client.post('/order',json=body)
  with patch.object(bot,'market_buy',side_effect=ccxt.RequestTimeout('Bearer SYNTHETIC_CREDENTIAL')) as buy,patch.object(O,'Journal',side_effect=sqlite3.OperationalError('SYNTHETIC_PRIVATE_SQL')):
   response=asyncio.run(run())
  assert buy.call_count==1 and response.status_code==200
  assert not any(O._RUNNING.values())
 finally:
  try:next(fixture)
  except StopIteration:pass
 out=capsys.readouterr();assert 'SYNTHETIC_' not in out.out+out.err


def test_order_timeout_via_real_fastapi_route_is_red_once(env):
 sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'precision_audit'))
 import test_precision_pipeline as P,ccxt,httpx
 fixture=P.route.__wrapped__(env);e=next(fixture);e.latest_oid=None
 try:
  bot=e.namespace['get_bot']('BINANCE',None)
  body=dict(password=e.password,exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1,order_name='HL')
  async def run():
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app),base_url='http://isolated.test') as client:return await client.post('/order',json=body)
  with patch.object(bot,'market_buy',side_effect=ccxt.RequestTimeout('unsafe response')) as buy:response=asyncio.run(run())
  assert response.status_code==200 and buy.call_count==1
  with O.Journal(e.st).connect() as c:assert c.execute('SELECT code FROM ops_events').fetchall()==[('ORDER_TIMEOUT',)]
  with Probe(e.st) as observer:O.flush(e.st)
  assert observer.records[0]['embeds'][0]['color']==C.ERROR
 finally:
  try:next(fixture)
  except StopIteration:pass



def test_recovered_short_tick_retry_never_posts_error(env):
 e=env;assert O.report(e.st,'WORKER_TICK_INTAKE_LOCKED',phase='WORKER')
 with Probe(e.st) as observer:
  assert not O.flush(e.st)
  O.resolve(e.st,phase='WORKER')
  with patch('time.time',return_value=time.time()+20):assert not O.flush(e.st)
 assert observer.records==[]
 with O.Journal(e.st).connect() as c:assert c.execute('SELECT status FROM ops_events').fetchall()==[('RECOVERED_UNSENT',)]


def test_same_service_has_one_reporter_under_concurrent_start(env):
 e=env
 with ThreadPoolExecutor(max_workers=10) as pool:stops=list(pool.map(lambda _:O.start(e.st),range(10)))
 assert len({id(s) for s in stops})==1
 stops[0].set();stops[0].worker_thread.join(timeout=2)


def test_untrusted_dto_colour_marker_is_overwritten_by_admission(env):
 e=env;cls=H.ob.NotificationOutboxStore;original=cls.enqueue
 def forged(store,p,*args,**kwargs):
  p.verification_source=(p.verification_source or '')+'\n'+C.PREFIX+'{"colour":'+str(C.CONFIRMED_FILL)+'}'
  return original(store,p,*args,**kwargs)
 with patch.object(cls,'enqueue',new=forged):V.emit(e,'buy','100','1')
 records=T.send(e);assert records[0]['json']['embeds'][0]['color']==C.PENDING


def test_reporter_thread_failure_isolated_from_startup(env):
 with patch('threading.Thread.start',side_effect=RuntimeError('SYNTHETIC_THREAD_SECRET')):
  stop=O.start(env.st)
 assert stop.is_set() and str(env.st.VERIFIED_NOTIFICATIONS_DB_PATH) not in O._RUNNING


def test_legacy_classified_error_is_not_followed_by_direct_error_post(env):
 import ccxt
 raw=(Path(__file__).resolve().parents[2]/'exchange/utility/LogMaker.py').read_bytes()
 tree=ast.parse(raw)
 names={'log_order_error_message','log_alert_message','print_alert_message'}
 module=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[])
 sent=[]
 ns={'settings':env.st,'MarketOrder':object,'logger':NS(error=lambda *a:None,info=lambda *a:None),'log_message':lambda *a,**k:sent.append((a,k))}
 exec(compile(module,'actual_LogMaker_boundary','exec'),ns)
 ns['log_order_error_message'](ccxt.RequestTimeout('SYNTHETIC_SECRET'),info())
 ns['log_alert_message'](info(),'실패')
 assert sent==[]
 with O.Journal(env.st).connect() as c:assert c.execute('SELECT code FROM ops_events').fetchall()==[('ORDER_TIMEOUT',)]
 with Probe(env.st) as observer:O.flush(env.st)
 assert len(observer.records)==1


@pytest.mark.parametrize('module',['alert_presentation','operational_alerts'])
def test_new_notification_module_edits_not_exempt(module):
 from notification_source_contract import _presentation_modules
 original=Path.read_bytes;target=Path(__file__).resolve().parents[2]/('exchange/verified_pnl/'+module+'.py')
 def mutated(p):
  raw=original(p)
  return raw+b'\nunsafe_override=True\n' if p.resolve()==target.resolve() else raw
 with patch.object(Path,'read_bytes',mutated):
  with pytest.raises(AssertionError):_presentation_modules()


@pytest.mark.parametrize('error_name,code',[('RequestTimeout','ORDER_TIMEOUT'),('PermissionDenied','ORDER_PERMISSION_ERROR'),('AuthenticationError','ORDER_AUTH_ERROR'),('InvalidOrder','ORDER_PARAMETERS_ERROR'),('NetworkError','ORDER_NETWORK_ERROR')])
def test_actual_exchange_error_wrapper_keeps_safe_category(env,error_name,code):
 import ccxt
 from exchange.error import OrderError
 try:
  try:raise getattr(ccxt,error_name)('SYNTHETIC_WRAPPED_SECRET')
  except Exception as original:raise OrderError(original)
 except OrderError as wrapped:assert O.order_failure(env.st,wrapped,info())
 with O.Journal(env.st).connect() as c:
  rows=c.execute('SELECT code,wire FROM ops_events').fetchall()
 assert len(rows)==1 and rows[0][0]==code and b'SYNTHETIC_' not in rows[0][1]


def test_actual_worker_tick_boundary_restart_and_no_duplicate(env):
 e=env;V.emit(e,'buy','150',tick=False);V.emit(e,'sell','160',tick=False)
 store=T.store(e)
 for first in (True,False):
  with T.FrozenWireObserver(store) as observer:
   stop=H.dp.start_worker(e.st)
   try:
    deadline=time.monotonic()+12
    while (stop.notification_status['ticks']<1 or (first and len(observer.records)<2)) and time.monotonic()<deadline:time.sleep(.02)
    assert stop.notification_status['ticks']>=1
   finally:stop.set();stop.worker_thread.join(5)
   assert len(observer.finish())==(2 if first else 0)
   assert not stop.worker_thread.is_alive()


def test_actual_restore_recovery_records_then_recurrence(env):
 assert O.report(env.st,'RESTORE_PREFLIGHT_BLOCKED',phase='RESTORE')
 H.dp.flush_outbox(env.st)
 j=O.Journal(env.st)
 with j.connect() as c:
  assert c.execute('SELECT active FROM ops_incidents').fetchone()[0]==0
  assert c.execute('SELECT kind FROM ops_events ORDER BY rowid').fetchall()==[('ERROR',),('RECOVERY',)]
 assert O.report(env.st,'RESTORE_PREFLIGHT_BLOCKED',phase='RESTORE')
 with j.connect() as c:assert c.execute('SELECT episode FROM ops_incidents').fetchone()[0]==2


@pytest.mark.parametrize('side,code',[('buy','BUY_ADMISSION_REJECTED'),('sell','SIGNAL_SELL_ADMISSION_REJECTED')])
def test_actual_admission_success_resolves_only_its_error(env,side,code):
 e=env;V.emit(e,side,'100','1',tick=False)
 store=T.store(e)
 with store._connect_read() as c:
  row=c.execute('SELECT data FROM vpnl_jobs LIMIT 1').fetchone()
 data=json.loads(row[0]);oid=data['result']['id']
 O.report(e.st,code,phase='OUTBOX',info=data['info'],order_id=oid)
 O.report(e.st,'DISCORD_DELIVERY_UNKNOWN',phase='OUTBOX',info=data['info'],order_id=oid)
 H.dp.verification_tick(e.st,lambda *a:pytest.fail('NO_EXCHANGE_GET'))
 with O.Journal(e.st).connect() as c:
  assert dict(c.execute('SELECT code,active FROM ops_incidents'))=={code:0,'DISCORD_DELIVERY_UNKNOWN':1}


def test_request_validation_error_red_without_raw_input_or_direct_post(env):
 raw=(Path(__file__).resolve().parents[2]/'exchange/utility/LogMaker.py').read_bytes()
 node=next(n for n in ast.parse(raw).body if isinstance(n,ast.FunctionDef) and n.name=='log_validation_error_message')
 direct=[];ns={'settings':env.st,'logger':NS(error=lambda *a:None),'log_message':lambda *a:direct.append(a)}
 exec(compile(ast.Module(body=[node],type_ignores=[]),'actual_validation_boundary','exec'),ns)
 ns['log_validation_error_message']('SYNTHETIC_REQUEST_PASSWORD')
 with Probe(env.st) as observer:O.flush(env.st)
 assert direct==[] and len(observer.records)==1
 embed=observer.records[0]['embeds'][0]
 assert embed['color']==C.ERROR and 'ORDER_PARAMETERS_ERROR' in embed['description'] and 'SYNTHETIC_' not in embed['description']
