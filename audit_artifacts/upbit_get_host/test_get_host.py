import ast,base64,copy,hashlib,hmac,json,os,sys,time,ssl
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'estimated_implementation'))
import test_estimated_contracts as T
from test_upbit_read import raw_order,U,T as STAMP
from exchange.verified_pnl.upbit_notification_get import *
from exchange.verified_pnl.upbit_notification_bootstrap import *
from exchange.verified_pnl.upbit_spot_read import configure_upbit_notification_host,current_upbit_notification_host
from exchange.verified_pnl.bootstrap import configure_execution_bootstrap
from exchange.verified_pnl.history import configure_history_provider
from bundle5_wire_observer import FrozenWireObserver
H=T.H;OUT=Path(__file__).parent

class Clock:
 def __init__(self):self.t=0;self.sleeps=[]
 def now(self):return self.t
 def sleep(self,n):self.sleeps.append(n);self.t+=n

def response(raw,status=200,headers=None):return OrderGETResponse(status,json.dumps(raw).encode(),headers or {'content-type':'application/json'})

@pytest.fixture
def setup(tmp_path):
 configure_execution_bootstrap(None);configure_history_provider(None);configure_upbit_notification_host(None);configure_upbit_read_sources(None)
 credentials=ReadCredentials('FAKE_ACCESS_KEY_CANARY','FAKE_SECRET_KEY_CANARY')
 cfg=UpbitReadConfig(U('approved-account'),U('approved-pocket'),H.identity('UPBIT','mock-host'),'strategy-A',credentials.fingerprint())
 buy=raw_order('get-buy','bid',['100000000'],STAMP);sell=raw_order('get-sell','ask',['110000000'],STAMP+1000)
 state={'orders':{buy['uuid']:buy,sell['uuid']:sell},'requests':[],'key_calls':0,'binding_calls':0,'proof_fault':None,'status':200}
 def keys():state['key_calls']+=1;return credentials
 def resolve(c):
  state['binding_calls']+=1
  proof=ExternalAccountBinding(c.account_uuid,c.pocket_uuid,c.source_account_ref,c.strategy,c.read_key_fingerprint,c.producer,frozenset({'order_query'}),'approved-test-registry','test-record-reference','a'*64,time.time()+600)
  if state['proof_fault']=='none':return None
  if state['proof_fault']=='flag':return {'verified':True}
  if state['proof_fault']=='bool':return True
  if state['proof_fault']=='account':return replace(proof,account_uuid=U('other'))
  if state['proof_fault']=='pocket':return replace(proof,pocket_uuid=U('other'))
  if state['proof_fault']=='permissions':return replace(proof,permissions=frozenset({'order_query','withdraw'}))
  if state['proof_fault']=='expired':return replace(proof,expires_at=time.time()-1)
  return proof
 def transport(req):
  validate_request(req);state['requests'].append(req)
  oid=req.url.split('uuid=')[1]
  return response(state['orders'][oid],state['status'])
 sources=UpbitReadSources(cfg,keys,resolve,transport)
 assert configure_upbit_read_sources(sources)
 st=H.settings(tmp_path/'notification.sqlite');st.UPBIT_VERIFIED_ALERTS=True;st.UPBIT_NET_PNL_ENABLED=True;st.UPBIT_KEY='mock-host'
 r=NS(cfg=cfg,credentials=credentials,sources=sources,state=state,st=st,buy=buy['uuid'],sell=sell['uuid'],log=T.G.live_log(st))
 yield r
 configure_upbit_notification_host(None);configure_upbit_read_sources(None);configure_execution_bootstrap(None);configure_history_provider(None)

def prepared(r):
 p=prepare_upbit_notification_host(environment={'POA_UPBIT_ESTIMATED_READ_ENABLED':'true'})
 assert p.host is not None and configure_upbit_notification_host(p.host)
 return p.host

def feed(r,side):assert r.log('UPBIT',{'id':r.buy if side=='buy' else r.sell},T.info('UPBIT',side))['accepted']
def tick(r):H.dp.verification_tick(r.st,lambda *a:(_ for _ in ()).throw(AssertionError('NO_FINANCIAL_CLIENT')))
def store(r):return H.ob.NotificationOutboxStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH)
def send(r):
 with FrozenWireObserver(store(r)) as o:H.dp.flush_outbox(r.st)
 return o.finish()
def payload(r):
 with store(r)._connect_read() as c:return json.loads(c.execute('SELECT payload FROM vpnl_revisions').fetchone()[0])

def decode_part(t):return json.loads(base64.urlsafe_b64decode(t+'='*(-len(t)%4)))

def test_host_build_no_key_no_api_and_jwt_matches_exact_wire(setup):
 r=setup;host=prepared(r)
 assert r.state['key_calls']==0 and r.state['requests']==[]
 for side in ('buy','sell'):feed(r,side);tick(r)
 assert len(r.state['requests'])==2
 nonces=[]
 for req in r.state['requests']:
  a,b,sig=req.headers['Authorization'].removeprefix('Bearer ').split('.')
  assert decode_part(a)['alg']=='HS512'
  claims=decode_part(b);nonces.append(claims['nonce'])
  assert claims['access_key']==r.credentials.access_key and claims['query_hash_alg']=='SHA512'
  assert claims['query_hash']==hashlib.sha512(req.url.split('?')[1].encode()).hexdigest()
  expected=base64.urlsafe_b64encode(hmac.digest(r.credentials.secret_key.encode(),(a+'.'+b).encode(),'sha512')).rstrip(b'=').decode()
  assert hmac.compare_digest(expected,sig)
 assert len(set(nonces))==2
 with store(r)._connect_read() as c:plans=[json.loads(x[0]) for x in c.execute('SELECT payload FROM vpnl_revisions')]
 assert len(plans)==2 and {p['side'] for p in plans}=={'매수','매도'}
 assert next(p for p in plans if p['side']=='매수')['estimated_pnl']['amount'] is None
 p=next(p for p in plans if p['side']=='매도');assert T.D(p['estimated_pnl']['amount'])==9895000 and T.D('9.88')<T.D(p['estimated_pnl']['roi'])<T.D('9.90')
 assert p['financial_receipt'] is None and p['execution_receipt'] is None
 assert len(send(r))==2

@pytest.mark.parametrize('fault',['none','flag','bool','account','pocket','permissions','expired'])
def test_external_binding_required_not_self_verified_flag(setup,fault):
 r=setup;r.state['proof_fault']=fault
 p=prepare_upbit_notification_host(environment={'POA_UPBIT_ESTIMATED_READ_ENABLED':'true'})
 assert p.host is None and r.state['key_calls']==0 and r.state['requests']==[]


def test_default_disabled_does_not_load_keys_or_binding(setup):
 r=setup
 for env in ({},{'POA_UPBIT_ESTIMATED_READ_ENABLED':'false'}):
  p=prepare_upbit_notification_host(environment=env);assert not p.enabled and p.host is None
 assert r.state['key_calls']==r.state['binding_calls']==0 and r.state['requests']==[]

@pytest.mark.parametrize('kwargs',[{'method':'POST'},{'method':'DELETE'},{'origin':'http://api.upbit.com'},{'origin':'https://evil.invalid'}])
def test_only_fixed_https_get(setup,kwargs):
 r=setup;c=UpbitNotificationGET(r.sources.credential_provider,lambda f:None,transport=r.sources.transport)
 with pytest.raises(UpbitGETError):c.get('/v1/order',{'uuid':r.buy},**kwargs)
 assert r.state['requests']==[] and r.state['key_calls']==0

@pytest.mark.parametrize('path,params',[('/v1/accounts',{}),('/v1/order',{'identifier':'x'}),('/v1/order',{'uuid':'../x'}),('/v1/order',{'uuid':'123','extra':'x'})])
def test_get_scope_rejects_before_credentials(setup,path,params):
 r=setup;c=UpbitNotificationGET(r.sources.credential_provider,lambda f:None,transport=r.sources.transport)
 with pytest.raises(UpbitGETError):c(path,params)
 assert r.state['key_calls']==0 and not r.state['requests']

@pytest.mark.parametrize('status,code,max_calls',[(401,'AUTHENTICATION_REJECTED',1),(403,'PERMISSION_REJECTED',1),(429,'RATE_LIMITED',2),(418,'READ_BLOCKED',1),(500,'UPBIT_SERVER_UNAVAILABLE',2),(503,'UPBIT_SERVER_UNAVAILABLE',2),(302,'REDIRECT_REJECTED',1)])
def test_http_errors_bounded_and_secret_safe(setup,status,code,max_calls):
 r=setup;clock=Clock();calls=[]
 def tx(req):calls.append(req);return response({'error':'FAKE_SECRET_KEY_CANARY'},status,{'retry-after':'1','location':'https://evil.invalid'})
 c=UpbitNotificationGET(r.sources.credential_provider,lambda f:None,transport=tx,monotonic=clock.now,sleep=clock.sleep)
 with pytest.raises(UpbitGETError) as exc:c('/v1/order',{'uuid':r.buy})
 assert exc.value.code==code and len(calls)==max_calls and 'CANARY' not in str(exc.value)
 if status in (401,403):
  with pytest.raises(UpbitGETError):c('/v1/order',{'uuid':r.buy})
  assert len(calls)==1
 if len(calls)==2:assert calls[0].headers['Authorization']!=calls[1].headers['Authorization']

@pytest.mark.parametrize('fault',['timeout','exception','bad_json','large','uuid','market','error200'])
def test_transport_and_response_validation(setup,fault):
 r=setup;calls=[];clock=Clock()
 def tx(req):
  calls.append(req)
  if fault=='timeout':raise TimeoutError('FAKE_ACCESS_KEY_CANARY')
  if fault=='exception':raise RuntimeError('Bearer FAKE_SECRET_KEY_CANARY')
  if fault=='bad_json':return OrderGETResponse(200,b'{FAKE_SECRET_KEY_CANARY',{})
  if fault=='large':return OrderGETResponse(200,b'x'*1048577,{})
  raw=copy.deepcopy(r.state['orders'][r.buy])
  if fault=='uuid':raw['uuid']=U('other')
  if fault=='market':raw['market']='KRW-ETH'
  if fault=='error200':raw['error']={'message':'FAKE_SECRET_KEY_CANARY'}
  return response(raw)
 c=UpbitNotificationGET(r.sources.credential_provider,lambda f:None,transport=tx,monotonic=clock.now,sleep=clock.sleep)
 with pytest.raises(UpbitGETError) as exc:c('/v1/order',{'uuid':r.buy})
 assert len(calls)<=2 and 'CANARY' not in repr(exc.value)


def test_missing_fees_and_missing_buy(setup):
 r=setup
 for value in r.state['orders'].values():value.pop('paid_fee')
 prepared(r)
 for side in ('buy','sell'):feed(r,side);tick(r)
 with store(r)._connect_read() as c:plans=[json.loads(x[0]) for x in c.execute('SELECT payload FROM vpnl_revisions ORDER BY rowid')]
 sell=next(p for p in plans if p['side']=='매도')
 assert len(plans)==2 and {p['side'] for p in plans}=={'매수','매도'}
 assert sell['estimated_pnl']['fee_mode']=='MISSING' and T.D(sell['estimated_pnl']['amount'])==10000000
 assert all(p['financial_receipt'] is None and p['execution_receipt'] is None for p in plans)
 records=send(r);assert len(records)==2 and all(x['ack']['status']=='SENT' for x in records)
 assert '수수료 미반영 추정 차익' in json.dumps(records[1]['json'],ensure_ascii=False)
 assert send(r)==[]


def test_sell_only_unavailable(setup):
 r=setup;prepared(r);feed(r,'sell');tick(r)
 assert payload(r)['estimated_pnl']['status']=='UNAVAILABLE' and '계산 불가' in json.dumps(send(r)[0]['json'],ensure_ascii=False)


def test_key_pin_guard_prevents_trading_key_substitution(setup):
 r=setup;bad=replace(r.sources,credential_provider=lambda:ReadCredentials('OTHER_KEY','OTHER_SECRET'))
 assert configure_upbit_read_sources(bad);prepared(r);feed(r,'sell');tick(r)
 assert r.state['requests']==[] and send(r)==[]


def test_main_startup_optin_only_injects_explicit_host(setup):
 import exchange.verified_pnl.dispatcher as dp
 import asyncio
 r=setup;root=Path(__file__).resolve().parents[2];tree=ast.parse((root/'main.py').read_text(encoding='utf-8'));node=next(x for x in tree.body if isinstance(x,ast.AsyncFunctionDef) and x.name=='startup');node.decorator_list=[]
 ns={'settings':r.st,'app':NS(state=NS()),'log_message':lambda *a:None,'VERSION':'mock'}
 exec(compile(ast.Module(body=[node],type_ignores=[]),str(root/'main.py'),'exec'),ns)
 # Replace only external vault/binding discovery; platform preparation and host
 # construction/registration remain production implementations.
 def discover(**kwargs):
  assert configure_upbit_read_sources(r.sources)
  return 'LOCAL_SOURCES_CONFIGURED_EXTERNAL_IDENTITY_UNVERIFIED'
 try:
  with patch('exchange.verified_pnl.upbit_notification_local.configure_local_upbit_sources',side_effect=discover),patch('exchange.verified_pnl.upbit_notification_platform._linux_sources',return_value=r.sources),patch.object(dp,'start_worker',return_value=None) as start:
   with patch.dict(os.environ,{'POA_UPBIT_ESTIMATED_READ_ENABLED':'false'}):asyncio.run(ns['startup']())
   assert start.call_args.kwargs=={} and r.state['key_calls']==r.state['binding_calls']==0
   with patch.dict(os.environ,{'POA_UPBIT_ESTIMATED_READ_ENABLED':'true'}):asyncio.run(ns['startup']())
   assert start.call_args.kwargs['upbit_observation_host'] is not None
   assert r.state['binding_calls']>0 and r.state['key_calls']==0 and not r.state['requests']
 finally:
  stop=getattr(ns['app'].state,'operational_alert_stop',None)
  if stop is not None:stop.set();getattr(stop,'worker_thread',NS(join=lambda **k:None)).join(timeout=2)

@pytest.mark.parametrize('fault',['ok','redirect','oversize','encoding','tls','timeout'])
def test_real_transport_fixed_tls_and_bounds(setup,fault):
 import exchange.verified_pnl.upbit_notification_get as module
 r=setup;seen={};raw=json.dumps(r.state['orders'][r.buy]).encode()
 class Response:
  status=302 if fault=='redirect' else 200
  def getheaders(self):return [('Content-Length',str(1048577 if fault=='oversize' else len(raw))),('Content-Encoding','gzip' if fault=='encoding' else 'identity')]
  def read1(self,n):
   if fault=='timeout':raise TimeoutError('FAKE_SECRET_KEY_CANARY')
   part=seen.get('unread',raw)[:n];seen['unread']=seen.get('unread',raw)[n:];return part
 class Conn:
  def __init__(self,host,port,timeout,context):
   seen.update(host=host,port=port,timeout=timeout,context=context);self.sock=NS(settimeout=lambda n:None)
  def set_debuglevel(self,n):seen['debug']=n
  def connect(self):
   if fault=='tls':raise ssl.SSLError('FAKE_SECRET_KEY_CANARY')
  def request(self,method,target,headers):seen.update(method=method,target=target,headers=headers)
  def getresponse(self):return Response()
  def close(self):seen['closed']=True
 req=OrderGETRequest('GET',ORIGIN+PATH+'?uuid='+r.buy,{'Authorization':'Bearer synthetic','Accept':'application/json','Accept-Encoding':'identity'})
 with patch.object(module.http.client,'HTTPSConnection',Conn):
  if fault in ('ok','redirect'):
   result=HTTPSOrderGETTransport()(req);assert result.status==(200 if fault=='ok' else 302)
   assert result.body==(raw if fault=='ok' else b'')
  else:
   with pytest.raises(UpbitGETError) as exc:HTTPSOrderGETTransport()(req)
   assert 'CANARY' not in str(exc.value)
 assert seen['host']=='api.upbit.com' and seen['port']==443 and seen['debug']==0 and seen['closed']
 assert seen['context'].check_hostname and seen['context'].verify_mode==ssl.CERT_REQUIRED
 if fault!='tls':assert seen['method']=='GET' and seen['target']==PATH+'?uuid='+r.buy

@pytest.mark.parametrize('first',[429,503,'timeout'])
def test_retry_then_success_new_nonce_and_bounded(setup,first):
 r=setup;clock=Clock();calls=[]
 def tx(req):
  calls.append(req)
  if len(calls)==1:
   if first=='timeout':raise TimeoutError('FAKE_SECRET_KEY_CANARY')
   return response({},first,{'retry-after':'1'})
  return response(r.state['orders'][r.buy])
 c=UpbitNotificationGET(r.sources.credential_provider,lambda f:None,transport=tx,monotonic=clock.now,sleep=clock.sleep)
 assert c(PATH,{'uuid':r.buy})['uuid']==r.buy and len(calls)==2
 assert calls[0].headers['Authorization']!=calls[1].headers['Authorization']


def test_long_rate_limit_does_not_busy_retry(setup):
 r=setup;clock=Clock();calls=[]
 def tx(req):calls.append(req);return response({},429,{'retry-after':'60'})
 c=UpbitNotificationGET(r.sources.credential_provider,lambda f:None,transport=tx,monotonic=clock.now,sleep=clock.sleep)
 for _ in range(2):
  with pytest.raises(UpbitGETError):c(PATH,{'uuid':r.buy})
 assert len(calls)==1 and clock.sleeps==[]


def test_unconfigured_optin_fails_closed(setup):
 r=setup;configure_upbit_read_sources(None)
 p=prepare_upbit_notification_host(environment={'POA_UPBIT_ESTIMATED_READ_ENABLED':'true'})
 assert p.host is None and not r.state['requests'] and r.state['key_calls']==0


def test_secret_canary_never_persisted_or_logged(setup,capsys):
 from loguru import logger
 r=setup;logs=[];sink=logger.add(lambda m:logs.append(str(m)))
 try:
  prepared(r)
  for side in ('buy','sell'):feed(r,side);tick(r)
  wires=send(r)
  r.state['status']=401
  with pytest.raises(UpbitGETError) as exc:raise UpbitGETError('Bearer FAKE_SECRET_KEY_CANARY')
  assert str(exc.value)=='READ_FAILURE'
 finally:logger.remove(sink)
 captured=capsys.readouterr()
 with store(r)._connect_read() as c:dump='\n'.join(c.iterdump())
 content=dump+json.dumps(wires)+''.join(logs)+captured.out+captured.err
 assert not any(value in content for value in ('FAKE_ACCESS_KEY_CANARY','FAKE_SECRET_KEY_CANARY','Authorization'))
 for req in r.state['requests']:assert req.headers['Authorization'] not in content


def test_real_startup_child_process_replay_and_no_extra_post(setup,tmp_path):
 import subprocess
 r=setup;prepared(r)
 for side in ('buy','sell'):feed(r,side);tick(r)
 s=store(r)
 with s._connect_read() as c:before=c.execute('SELECT record,record_digest FROM vpnl_notice_upbit_observations ORDER BY intake_id').fetchall()
 output=tmp_path/'child_wire.json'
 code="""
import sys,json,time,ast,asyncio,os
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import test_get_host as T
f=T.setup.__wrapped__(Path(sys.argv[1]).parent);r=next(f)
root=Path.cwd();tree=ast.parse((root/'main.py').read_text(encoding='utf-8'));node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='startup');node.decorator_list=[]
ns={'settings':r.st,'app':NS(state=NS()),'log_message':lambda *a:None,'VERSION':'mock'}
exec(compile(ast.Module(body=[node],type_ignores=[]),str(root/'main.py'),'exec'),ns)
def discover(**kwargs):
 assert T.configure_upbit_read_sources(r.sources)
 return 'LOCAL_SOURCES_CONFIGURED_EXTERNAL_IDENTITY_UNVERIFIED'
with T.FrozenWireObserver(T.store(r)) as observer:
 try:
  with patch('exchange.verified_pnl.upbit_notification_local.configure_local_upbit_sources',side_effect=discover),patch('exchange.verified_pnl.upbit_notification_platform._linux_sources',return_value=r.sources),patch.dict(os.environ,{'POA_UPBIT_ESTIMATED_READ_ENABLED':'true'}):asyncio.run(ns['startup']())
  stop=ns['app'].state.verified_notifications_stop;assert stop is not None
  deadline=time.time()+10
  while (len(observer.records)<2 or stop.notification_status['ticks']<1) and time.time()<deadline:time.sleep(.02)
 finally:
  stop=ns['app'].state.verified_notifications_stop;stop.set();stop.worker_thread.join(5)
  ops=ns['app'].state.operational_alert_stop;ops.set();ops.worker_thread.join(2)
 records=observer.finish();assert len(records)==2 and all(x['ack']['status']=='SENT' for x in records)
Path(sys.argv[2]).write_text(json.dumps(records),encoding='utf-8')
assert not r.state['requests'] and r.state['key_calls']==0
"""
 env=dict(os.environ);env['PYTHONPATH']=str(OUT)+os.pathsep+env.get('PYTHONPATH','')
 child=subprocess.run([sys.executable,'-c',code,r.st.VERIFIED_NOTIFICATIONS_DB_PATH,str(output)],capture_output=True,cwd=Path.cwd(),env=env,timeout=25)
 assert child.returncode==0,child.stderr.decode('utf-8','replace')[-800:]
 records=json.loads(output.read_text(encoding='utf-8'));assert len(records)==2 and all(x['ack']['status']=='SENT' for x in records)
 with s._connect_read() as c:
  assert c.execute('SELECT record,record_digest FROM vpnl_notice_upbit_observations ORDER BY intake_id').fetchall()==before
  plans=[json.loads(x[0]) for x in c.execute('SELECT payload FROM vpnl_revisions')]
 assert {p['side'] for p in plans}=={'매수','매도'} and all(p['financial_receipt'] is None and p['execution_receipt'] is None for p in plans)
 assert send(r)==[]


def test_main_startup_worker_fetches_ack_buy_sell_without_finance(setup):
 import asyncio
 r=setup;root=Path.cwd();tree=ast.parse((root/'main.py').read_text(encoding='utf-8'));node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='startup');node.decorator_list=[]
 ns={'settings':r.st,'app':NS(state=NS()),'log_message':lambda *a:None,'VERSION':'mock'}
 exec(compile(ast.Module(body=[node],type_ignores=[]),str(root/'main.py'),'exec'),ns)
 def discover(**kwargs):
  assert configure_upbit_read_sources(r.sources)
  return 'LOCAL_SOURCES_CONFIGURED_EXTERNAL_IDENTITY_UNVERIFIED'
 feed(r,'buy');stop=None
 with FrozenWireObserver(store(r)) as observer:
  try:
   with patch('exchange.verified_pnl.upbit_notification_local.configure_local_upbit_sources',side_effect=discover),patch('exchange.verified_pnl.upbit_notification_platform._linux_sources',return_value=r.sources),patch.dict(os.environ,{'POA_UPBIT_ESTIMATED_READ_ENABLED':'true'}):asyncio.run(ns['startup']())
   stop=ns['app'].state.verified_notifications_stop;assert stop is not None
   deadline=time.monotonic()+10
   while time.monotonic()<deadline:
    with store(r)._connect_read() as c:n=c.execute('SELECT count(*) FROM vpnl_notice_upbit_observations').fetchone()[0]
    if n==1:break
    time.sleep(.02)
   assert n==1
   feed(r,'sell');deadline=time.monotonic()+10
   while (len(observer.records)<2 or stop.notification_status['ticks']<1) and time.monotonic()<deadline:time.sleep(.02)
  finally:
   if stop is not None:stop.set();stop.worker_thread.join(5)
   ops=getattr(ns['app'].state,'operational_alert_stop',None)
   if ops is not None:ops.set();ops.worker_thread.join(2)
  records=observer.finish()
 assert len(records)==2 and all(x['ack']['status']=='SENT' for x in records) and len(r.state['requests'])==2
 with store(r)._connect_read() as c:plans=[json.loads(x[0]) for x in c.execute('SELECT payload FROM vpnl_revisions ORDER BY rowid')]
 sell=next(p for p in plans if p['side']=='매도')
 assert T.D(sell['estimated_pnl']['amount'])==9895000 and all(p['financial_receipt'] is None and p['execution_receipt'] is None for p in plans)
 assert {p['side'] for p in plans}=={'매수','매도'} and send(r)==[]
