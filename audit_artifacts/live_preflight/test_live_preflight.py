import ast,asyncio,copy,hashlib,json,os,subprocess,sys,time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'upbit_get_host'))
import test_get_host as T
from exchange.verified_pnl import upbit_notification_local as L
from exchange.verified_pnl import upbit_notification_bootstrap as B

@pytest.fixture
def local(tmp_path,monkeypatch):
 f=T.setup.__wrapped__(tmp_path);r=next(f)
 ev=dict(source='operator-reviewed-record',reference='external-reference',digest='a'*64,expires_at=time.time()+600)
 op=dict(actor_digest='b'*64,approved_at=time.time()-1,allowed_ips=['192.0.2.10'],permission_review_digest='c'*64)
 record=dict(format='POA_UPBIT_OPERATOR_BINDING_V1',config=asdict(r.cfg),evidence=ev,operator=op,permissions=['order_query'],diagnostic_order=dict(uuid=r.buy,market='KRW-BTC',side='buy'))
 state=dict(reads=[],http=[],fault=None,record=record)
 def encode():return json.dumps(state['record'],separators=(',',':')).encode()
 env={'POA_UPBIT_ESTIMATED_READ_ENABLED':'true','POA_UPBIT_ONE_SHOT_GET_ENABLED':'true','POA_UPBIT_READ_BINDING_SHA256':hashlib.sha256(encode()).hexdigest()}
 def read(target):
  state['reads'].append(target)
  if target==L.BINDING_TARGET:return encode()
  if state['fault']=='secret':raise RuntimeError('FAKE_SECRET_KEY_CANARY')
  return json.dumps(dict(access_key=r.credentials.access_key,secret_key=r.credentials.secret_key)).encode()
 def tx(req):
  state['http'].append(req)
  if state['fault']=='timeout':raise TimeoutError('FAKE_SECRET_KEY_CANARY')
  if type(state['fault']) is int:return T.response({},state['fault'])
  oid=req.url.split('uuid=')[1];raw=copy.deepcopy(r.state['orders'][oid])
  if state['fault']=='market':raw['market']='KRW-ETH'
  if state['fault']=='uuid':raw['uuid']=r.sell
  if state['fault']=='side':raw['side']='ask'
  return T.response(raw)
 monkeypatch.setattr(L,'_vault_read',read);monkeypatch.setattr(L,'HTTPSOrderGETTransport',lambda:tx)
 r.local=state;r.env=env;r.repin=lambda:env.update(POA_UPBIT_READ_BINDING_SHA256=hashlib.sha256(encode()).hexdigest())
 yield r
 try:next(f)
 except StopIteration:pass


def test_disabled_does_not_open_vault_or_network(local):
 r=local
 for env in ({},{'POA_UPBIT_ESTIMATED_READ_ENABLED':'false'}):assert L.configure_local_upbit_sources(environment=env)=='DISABLED'
 assert r.local['reads']==r.local['http']==[]

@pytest.mark.parametrize('fault',['missing_pin','wrong_pin','expired','permissions','account','pocket','strategy','ip','evidence','actor','format','duplicate'])
def test_operator_binding_fail_closed(local,fault):
 r=local;d=r.local['record']
 if fault=='missing_pin':r.env.pop('POA_UPBIT_READ_BINDING_SHA256')
 elif fault=='wrong_pin':r.env['POA_UPBIT_READ_BINDING_SHA256']='f'*64
 elif fault=='expired':d['evidence']['expires_at']=time.time()-1;r.repin()
 elif fault=='permissions':d['permissions']=['order_query','order'];r.repin()
 elif fault in ('account','pocket'):d['config'][fault+'_uuid']='bad';r.repin()
 elif fault=='strategy':d['config']['strategy']='default';r.repin()
 elif fault=='ip':d['operator']['allowed_ips']=[];r.repin()
 elif fault=='evidence':d['evidence']['digest']='';r.repin()
 elif fault=='actor':d['operator']['actor_digest']='';r.repin()
 elif fault=='format':d['format']='verified=True';r.repin()
 else:
  raw=b'{"format":1,"format":2}';r.env['POA_UPBIT_READ_BINDING_SHA256']=hashlib.sha256(raw).hexdigest()
  with patch.object(L,'_vault_read',return_value=raw):assert L.configure_local_upbit_sources(environment=r.env)=='LOCAL_SOURCES_UNAVAILABLE'
  return
 assert L.configure_local_upbit_sources(environment=r.env)=='LOCAL_SOURCES_UNAVAILABLE'
 assert L.CREDENTIAL_TARGET not in r.local['reads'] and not r.local['http']


def test_registered_binding_is_local_not_exchange_identity(local):
 r=local;assert L.configure_local_upbit_sources(environment=r.env).startswith('LOCAL_SOURCES_CONFIGURED')
 p=B.prepare_upbit_notification_host(environment=r.env);assert p.host is not None
 assert L.CREDENTIAL_TARGET not in r.local['reads'] and r.local['http']==[]
 assert p.host.reader.scope.account==r.cfg.account_uuid and p.host.reader.scope.book==r.cfg.strategy

@pytest.mark.parametrize('fault',['secret',401,403,429,500,'timeout','market','uuid','side'])
def test_one_shot_errors_never_retry_or_leak(local,fault,capsys):
 r=local;r.local['fault']=fault
 with pytest.raises(L.PreflightError) as exc:L.one_shot_summary(r.buy,approved=True,environment=r.env)
 assert len(r.local['http'])==(0 if fault=='secret' else 1)
 assert 'CANARY' not in str(exc.value)
 captured=capsys.readouterr();assert 'CANARY' not in captured.out+captured.err


def test_one_shot_only_pinned_uuid_and_exact_approval(local):
 r=local
 for oid,approved in [(r.sell,True),(r.buy,'true'),(r.buy,1),(r.buy,False)]:
  with pytest.raises(L.PreflightError):L.one_shot_summary(oid,approved=approved,environment=r.env)
 assert r.local['http']==[]


def test_one_shot_summary_minimal_and_no_issuance(local):
 r=local;value=L.one_shot_summary(r.buy,approved=True,environment=r.env)
 assert T.T.D(value['vwap'])==100000000 and T.T.D(value['paid_fee'])==50000 and value['state']=='done' and len(r.local['http'])==1
 text=json.dumps(value);assert all(s not in text for s in (r.buy,r.cfg.account_uuid,r.cfg.pocket_uuid,'CANARY','Authorization'))
 assert value['identity']=='OPERATOR_REGISTERED_NOT_EXCHANGE_VERIFIED'


def test_registry_change_after_host_creation_refused_before_keys(local):
 r=local;assert L.configure_local_upbit_sources(environment=r.env).startswith('LOCAL_SOURCES_CONFIGURED')
 host=B.prepare_upbit_notification_host(environment=r.env).host;r.local['record']['config']['strategy']='different'
 with pytest.raises(Exception):host.reader.read_notification_order(host.reader,r.buy,'BTC/KRW')
 assert L.CREDENTIAL_TARGET not in r.local['reads'] and not r.local['http']


def test_actual_startup_local_sources_worker_estimated(local):
 import threading
 r=local;root=Path.cwd();node=next(n for n in ast.parse((root/'main.py').read_text(encoding='utf-8-sig')).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='startup');node.decorator_list=[]
 ns=dict(settings=r.st,app=NS(state=NS()),log_message=lambda *a:None,VERSION='mock')
 exec(compile(ast.Module(body=[node],type_ignores=[]),str(root/'main.py'),'exec'),ns)
 # Worker uses the same fixed-origin production transport constructor, replaced only at HTTP boundary.
 import exchange.verified_pnl.upbit_notification_get as G
 T.feed(r,'buy');stop=None
 with patch.dict(os.environ,r.env),patch.object(G,'HTTPSOrderGETTransport',L.HTTPSOrderGETTransport),T.FrozenWireObserver(T.store(r)) as observer:
  try:
   with patch.dict(os.environ,r.env):asyncio.run(ns['startup']())
   stop=ns['app'].state.verified_notifications_stop;assert stop is not None
   deadline=time.time()+10;n=0
   while time.time()<deadline:
    with T.store(r)._connect_read() as c:n=c.execute('SELECT count(*) FROM vpnl_notice_upbit_observations').fetchone()[0]
    if n==1:break
    time.sleep(.02)
   assert n==1;T.feed(r,'sell');deadline=time.time()+10
   while not observer.records and time.time()<deadline:time.sleep(.02)
  finally:
   if stop is not None:stop.set()
   deadline=time.time()+5
   while any(t.name=='verified-notifications' for t in threading.enumerate()) and time.time()<deadline:time.sleep(.02)
  wires=observer.finish()
 assert len(wires)==1 and len(r.local['http'])==2 and T.T.D(T.payload(r)['estimated_pnl']['amount'])==9895000
 assert T.payload(r)['financial_receipt'] is None and T.send(r)==[]
 (Path(__file__).parent/'worker_wire.json').write_text(json.dumps(wires),encoding='utf-8')


def test_direct_diagnostic_disabled_without_package_db_init(local):
 r=local;env=dict(os.environ);env['POA_UPBIT_ESTIMATED_READ_ENABLED']='false'
 p=subprocess.run([sys.executable,'exchange/verified_pnl/upbit_notification_local.py','--order-uuid',r.buy,'--approved-once'],capture_output=True,env=env,timeout=15)
 assert p.returncode==2 and p.stdout==b'' and p.stderr.decode().strip()=='UPBIT_LOCAL_PREFLIGHT_FAILED'


def test_cli_invalid_arguments_do_not_echo_canary(local):
 r=local;p=subprocess.run([sys.executable,'exchange/verified_pnl/upbit_notification_local.py','--FAKE_SECRET_KEY_CANARY'],capture_output=True,timeout=15)
 assert p.returncode==2 and b'CANARY' not in p.stderr+p.stdout


def test_disabled_startup_preserves_worker_arguments_without_vault(local):
 r=local;root=Path.cwd();node=next(n for n in ast.parse((root/'main.py').read_text(encoding='utf-8-sig')).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='startup');node.decorator_list=[]
 ns=dict(settings=r.st,app=NS(state=NS()),log_message=lambda *a:None,VERSION='mock');exec(compile(ast.Module(body=[node],type_ignores=[]),str(root/'main.py'),'exec'),ns)
 with patch.dict(os.environ,{'POA_UPBIT_ESTIMATED_READ_ENABLED':'false'}),patch.object(T.H.dp,'start_worker',return_value=None) as start:
  asyncio.run(ns['startup']());assert start.call_args.kwargs=={}
 assert not r.local['reads'] and not r.local['http']


def test_vault_access_key_pin_mismatch_no_network(local):
 r=local;read=L._vault_read
 def changed(target):
  if target==L.CREDENTIAL_TARGET:return b'{"access_key":"OTHER_CANARY","secret_key":"OTHER_SECRET_CANARY"}'
  return read(target)
 with patch.object(L,'_vault_read',changed):
  with pytest.raises(L.PreflightError):L.one_shot_summary(r.buy,approved=True,environment=r.env)
 assert not r.local['http']


def test_native_vault_layout_copy_zero_free_without_real_vault(monkeypatch):
 import ctypes
 blob=ctypes.create_string_buffer(b'{"fixture":true}');seen={}
 def read(target,kind,flags,out):
  cls=out._obj._type_;c=cls();c.Type=1;c.TargetName=target;c.CredentialBlobSize=len(blob.raw)-1;c.CredentialBlob=ctypes.cast(blob,ctypes.POINTER(ctypes.c_ubyte));seen['c']=c
  ctypes.cast(out,ctypes.POINTER(ctypes.POINTER(cls)))[0]=ctypes.pointer(c);return 1
 def free(pointer):seen['freed']=True
 monkeypatch.setattr(L.ctypes,'WinDLL',lambda *a,**k:NS(CredReadW=read,CredFree=free))
 value=L._vault_read(L.BINDING_TARGET)
 assert value==b'{"fixture":true}' and seen['freed'] and set(blob.raw)=={0}
