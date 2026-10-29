import ast,asyncio,copy,hashlib,importlib.util,json,os,sys,time,threading
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest,yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'upbit_get_host'))
import test_get_host as T
from exchange.verified_pnl import upbit_notification_platform as L,upbit_notification_get as G
from exchange.verified_pnl.upbit_spot_read import configure_upbit_notification_host,current_upbit_notification_host
from exchange.verified_pnl.upbit_notification_simple import SimpleUpbitNotificationHosts
ROOT=Path.cwd();spec=importlib.util.spec_from_file_location('simple_installer',ROOT/'deployment/install_once.py');I=importlib.util.module_from_spec(spec);spec.loader.exec_module(I)

@pytest.fixture
def local(tmp_path,monkeypatch):
 f=T.setup.__wrapped__(tmp_path);r=next(f);r.st.UPBIT_KEY=r.credentials.access_key;r.st.UPBIT_SECRET=r.credentials.secret_key
 env,binding=I.upbit_read_configuration({},dict(UPBIT_KEY=r.st.UPBIT_KEY,UPBIT_SECRET=r.st.UPBIT_SECRET))
 assert binding is None and env['POA_UPBIT_READ_CREDENTIAL_MODE']=='simple'
 for k,v in env.items():setattr(r.st,k,v)
 r.native={};r.calls=[];r.status=200;r.seq=0
 def tx(req):
  r.calls.append(req);oid=req.url.split('uuid=')[1]
  return T.response(r.native[oid],r.status)
 monkeypatch.setattr(L,'sys',NS(platform='linux'));monkeypatch.setattr(G,'HTTPSOrderGETTransport',lambda:tx)
 p=L.prepare_platform_upbit_host(r.st);assert type(p.host) is SimpleUpbitNotificationHosts and configure_upbit_notification_host(p.host)
 r.host=p.host
 yield r
 try:next(f)
 except StopIteration:pass

def submit(r,name,side,prices,strategy='strategy-A'):
 r.seq+=1;raw=T.raw_order(name,'bid' if side=='buy' else 'ask',prices,T.STAMP+r.seq*1000);r.native[raw['uuid']]=raw
 info=T.T.info('UPBIT',side,strategy=strategy,qty=str(len(prices)))
 assert r.log('UPBIT',{'id':raw['uuid']},info)['accepted'];T.tick(r);return raw['uuid']

def payloads(r):
 with T.store(r)._connect_read() as c:return [json.loads(x[0]) for x in c.execute('SELECT payload FROM vpnl_revisions ORDER BY rowid')]


def test_simple_two_strategies_correct_isolated_pnl(local):
 r=local
 submit(r,'A-buy','buy',['100000000']);submit(r,'B-buy','buy',['200000000'],'strategy-B')
 submit(r,'A-sell','sell',['110000000']);submit(r,'B-sell','sell',['190000000'],'strategy-B')
 p=payloads(r);assert len(p)==2
 assert [T.T.D(x['estimated_pnl']['amount']) for x in p]==[9895000,-10195000]
 assert T.T.D('9.88')<T.T.D(p[0]['estimated_pnl']['roi'])<T.T.D('9.90')
 assert all(x['financial_receipt'] is None and x['execution_receipt'] is None for x in p)
 assert len(T.send(r))==2
 with T.store(r)._connect_read() as c:
  scopes=[json.loads(x[0])['scope'] for x in c.execute('SELECT record FROM vpnl_notice_upbit_observations')]
 assert all(x['account'].startswith('poa-estimated-upbit:') and x['position_cycle']=='poa-estimated-internal-stream' for x in scopes)
 assert len({x['account'] for x in scopes})==1 and {x['book'] for x in scopes}=={'strategy-A','strategy-B'}


def test_partial_sell_weighted_cost_and_restart(local):
 r=local;submit(r,'buy2','buy',['100000000','200000000']);submit(r,'sell1','sell',['160000000'])
 p=payloads(r)[0];assert T.T.D(p['estimated_pnl']['entry_price'])==150000000 and T.T.D(p['estimated_pnl']['amount'])==9845000
 before=T.send(r);assert len(before)==1
 root=SimpleUpbitNotificationHosts(r.st);assert configure_upbit_notification_host(root)
 T.tick(r);assert T.send(r)==[]
 with T.store(r)._connect_read() as c:assert c.execute("SELECT count(*) FROM vpnl_deliveries WHERE status='SENT'").fetchone()[0]==1

@pytest.mark.parametrize('kind',['no_buy','other_strategy','generic'])
def test_unknown_acquisition_never_borrow_cost(local,kind):
 r=local
 if kind=='other_strategy':submit(r,'buy-A','buy',['100000000'])
 if kind=='generic':submit(r,'generic-buy','buy',['100000000'],'주문')
 submit(r,'sell-B','sell',['110000000'],'주문' if kind=='generic' else 'strategy-B')
 p=payloads(r)[-1];assert p['estimated_pnl']['status']=='UNAVAILABLE' and p['estimated_pnl']['amount'] is None
 assert len(T.send(r))==1


def test_missing_fee_is_gross(local):
 r=local
 for side,price in [('buy','100000000'),('sell','110000000')]:
  name='no-fee-'+side;r.seq+=1;raw=T.raw_order(name,'bid' if side=='buy' else 'ask',[price],T.STAMP+r.seq*1000);raw.pop('paid_fee');r.native[raw['uuid']]=raw
  assert r.log('UPBIT',{'id':raw['uuid']},T.T.info('UPBIT',side))['accepted'];T.tick(r)
 p=payloads(r)[0];assert p['estimated_pnl']['fee_mode']=='MISSING' and T.T.D(p['estimated_pnl']['amount'])==10000000
 assert '수수료 미반영' in json.dumps(T.send(r)[0]['json'],ensure_ascii=False)


def test_no_arbitrary_order_get_from_host_or_flag(local):
 r=local;h=r.host.for_strategy('forged-strategy')
 with pytest.raises(Exception):h.observe('forged',T.T.info('UPBIT','sell',strategy='forged-strategy'),r.host.source_account_ref,r.sell)
 with pytest.raises(Exception):h.reader.read_notification_order(h.reader,r.sell,'BTC/KRW')
 with pytest.raises(Exception):r.host._get('/v1/order',{'uuid':r.sell})
 assert r.calls==[] and T.send(r)==[]

@pytest.mark.parametrize('fault',['market','uuid','side','unfilled','account'])
def test_mismatch_no_post(local,fault):
 r=local;raw=T.raw_order('mismatch','ask',['110000000']);r.native[raw['uuid']]=raw
 if fault=='market':raw['market']='KRW-ETH'
 if fault=='uuid':raw['uuid']=r.buy
 if fault=='side':raw['side']='bid'
 if fault=='unfilled':raw.update(state='wait',executed_volume='0',remaining_volume='1',trades_count=0,trades=[])
 if fault=='account':r.st.UPBIT_KEY='different-account'
 r.log('UPBIT',{'id':T.U('mismatch')},T.T.info('UPBIT','sell'));T.tick(r);assert T.send(r)==[]

@pytest.mark.parametrize('status',[401,403])
def test_auth_failure_disables_read_only_not_other_logic(local,status):
 r=local;r.status=status;submit(r,'unauth1','sell',['110000000']);assert len(r.calls)==1 and T.send(r)==[]
 submit(r,'unauth2','sell',['110000000'],'strategy-B');assert len(r.calls)==1 and T.send(r)==[]


def test_same_order_relabelled_strategy_is_not_reposted(local):
 r=local;submit(r,'owned-buy','buy',['100000000']);oid=submit(r,'owned-sell','sell',['110000000']);assert len(T.send(r))==1
 r.log('UPBIT',{'id':oid},T.T.info('UPBIT','sell',strategy='strategy-B'));T.tick(r);assert T.send(r)==[]


def test_simple_installer_no_manual_scope_inputs():
 env,record=I.upbit_read_configuration({},dict(UPBIT_KEY='synthetic-access',UPBIT_SECRET='synthetic-secret'))
 assert env['POA_UPBIT_ESTIMATED_READ_ENABLED'] is True and env['POA_UPBIT_READ_CREDENTIAL_MODE']=='simple' and record is None
 assert env['POA_UPBIT_READ_BINDING_SHA256']==''
 assert not I.upbit_read_configuration({}, {})[0]['POA_UPBIT_ESTIMATED_READ_ENABLED']
 cloud=yaml.safe_load((ROOT/'deployment/cloud-config-ubuntu2204.yaml').read_text(encoding='utf-8'));files={x['path']:x for x in cloud['write_files']};inp=json.loads(files['/root/poa-install-input.json']['content'])
 assert 'upbit_estimated_read' not in inp and not any(x in inp for x in ('account_uuid','pocket_uuid','strategy','allowed_ips','evidence'))
 assert files['/usr/local/lib/poa/install_once.py']['content'].rstrip()==(ROOT/'deployment/install_once.py').read_text(encoding='utf-8').rstrip()


def test_actual_startup_routes_two_strategies(local):
 r=local;node=next(n for n in ast.parse((ROOT/'main.py').read_text(encoding='utf-8')).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='startup');node.decorator_list=[]
 ns=dict(settings=r.st,app=NS(state=NS()),log_message=lambda *a:None,VERSION='mock');exec(compile(ast.Module(body=[node],type_ignores=[]),str(ROOT/'main.py'),'exec'),ns)
 def put(name,side,price,strategy):
  r.seq+=1;raw=T.raw_order(name,'bid' if side=='buy' else 'ask',[price],T.STAMP+r.seq*1000);r.native[raw['uuid']]=raw
  assert r.log('UPBIT',{'id':raw['uuid']},T.T.info('UPBIT',side,strategy=strategy))['accepted']
 put('worker-A-buy','buy','100000000','strategy-A');put('worker-B-buy','buy','200000000','strategy-B');stop=None
 with T.FrozenWireObserver(T.store(r)) as observer:
  try:
   asyncio.run(ns['startup']());stop=ns['app'].state.verified_notifications_stop;assert stop is not None
   deadline=time.time()+10;n=0
   while time.time()<deadline:
    with T.store(r)._connect_read() as c:n=c.execute('SELECT count(*) FROM vpnl_notice_upbit_observations').fetchone()[0]
    if n==2:break
    time.sleep(.02)
   assert n==2;put('worker-A-sell','sell','110000000','strategy-A');put('worker-B-sell','sell','190000000','strategy-B');deadline=time.time()+10
   while len(observer.records)<2 and time.time()<deadline:time.sleep(.02)
  finally:
   if stop is not None:stop.set()
   deadline=time.time()+5
   while any(t.name=='verified-notifications' for t in threading.enumerate()) and time.time()<deadline:time.sleep(.02)
  records=observer.finish()
 assert len(records)==2 and len(r.calls)==4 and T.send(r)==[]
 (Path(__file__).parent/'worker_wire.json').write_bytes(json.dumps(records).encode())

@pytest.mark.parametrize('status',['SENT','UNKNOWN','HELD','POSTING'])
def test_simple_sent_unknown_held_and_cas(local,status):
 r=local;submit(r,'state-buy','buy',['100000000']);oid=submit(r,'state-sell','sell',['110000000']);s=T.store(r);claim=None
 if status=='SENT':assert len(T.send(r))==1
 elif status=='POSTING':
  claim=s.claim_next();bad=dict(claim,token='wrong');assert not s.begin_post(bad);assert s.begin_post(claim);assert not s.ack(bad,True,message_id='123')
 else:
  with s._connect() as c:c.execute('UPDATE vpnl_deliveries SET status=?',(status,))
 with s._connect_read() as c:before=c.execute('SELECT wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()
 r.log('UPBIT',{'id':oid},T.T.info('UPBIT','sell'));T.tick(r);assert T.send(r)==[]
 with s._connect_read() as c:assert c.execute('SELECT wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()==before
 if claim:
  with T.FrozenWireObserver(s) as observer:observer.post(data=claim['wire'],headers={'Content-Type':'application/json'})
  assert s.ack(claim,True,message_id='123000') and observer.finish()[0]['ack']['status']=='SENT'


def test_persisted_simple_observations_replay_new_process(local):
 import subprocess
 r=local;submit(r,'process-buy','buy',['100000000']);submit(r,'process-sell','sell',['110000000'])
 output=Path(__file__).parent/'child_wire.json'
 code="""
import sys,time,json,threading
from pathlib import Path
import pytest
import test_simple_upbit as T
m=pytest.MonkeyPatch();f=T.local.__wrapped__(Path(sys.argv[1]).parent,m);r=next(f)
with T.T.FrozenWireObserver(T.T.store(r)) as observer:
 stop=T.T.H.dp.start_worker(r.st,upbit_observation_host=r.host);assert stop is not None
 deadline=time.time()+10
 while not observer.records and time.time()<deadline:time.sleep(.02)
 stop.set();deadline=time.time()+5
 while any(t.name=='verified-notifications' for t in threading.enumerate()) and time.time()<deadline:time.sleep(.02)
 records=observer.finish();assert len(records)==1
Path(sys.argv[2]).write_text(json.dumps(records),encoding='utf-8');assert r.calls==[]
"""
 env=dict(os.environ);env['PYTHONPATH']=str(Path(__file__).parent)+os.pathsep+env['PYTHONPATH']
 child=subprocess.run([sys.executable,'-c',code,r.st.VERIFIED_NOTIFICATIONS_DB_PATH,str(output)],env=env,capture_output=True,timeout=25)
 assert child.returncode==0,child.stderr.decode('utf-8','replace')[-800:]
 assert json.loads(output.read_text())[0]['ack']['status']=='SENT' and T.send(r)==[]

from test_krw_v5 import krw,reset_provider

def test_simple_router_does_not_override_strict_financial(krw):
 r=krw;r.st.UPBIT_KEY='unconfigured';r.st.UPBIT_SECRET='synthetic-not-live-secret';root=SimpleUpbitNotificationHosts(r.st);assert configure_upbit_notification_host(root)
 try:
  handler=T.T.G.live_log(r.st);assert handler('UPBIT',{'id':r.sell},T.T.info('UPBIT','sell'))['accepted']
  T.H.dp.verification_tick(r.st,r.boot.client_for)
  p=payloads(r)[0];assert T.T.E.payload_valid(T.H.dp.NotificationPayload(**p)) and p.get('notification_type') is None
  assert T.T.D(p['net_pnl'].split()[0])==9895000
  with T.store(r)._connect_read() as c:assert c.execute('SELECT count(*) FROM vpnl_notice_upbit_observations').fetchone()[0]==0
  assert len(T.send(r))==1
 finally:configure_upbit_notification_host(None)



def test_sell_exceeding_observed_inventory_is_unavailable(local):
 r=local;submit(r,'insufficient-buy','buy',['100000000']);submit(r,'old-inventory-sell','sell',['110000000','110000000'])
 assert payloads(r)[-1]['estimated_pnl']['status']=='UNAVAILABLE' and payloads(r)[-1]['estimated_pnl']['amount'] is None
 assert len(T.send(r))==1


def test_estimated_sent_then_verified_provider_no_repost(krw,monkeypatch):
 from exchange.verified_pnl.bootstrap import configure_execution_bootstrap
 from exchange.verified_pnl.history import configure_history_provider
 r=krw;r.st.UPBIT_KEY='unconfigured';r.st.UPBIT_SECRET='synthetic-not-live-secret'
 configure_execution_bootstrap(None);configure_history_provider(None)
 root=SimpleUpbitNotificationHosts(r.st)
 calls=[]
 def tx(req):calls.append(req);return T.response(r.state['orders'][req.url.split('uuid=')[1]])
 monkeypatch.setattr(root._get,'_transport',tx);assert configure_upbit_notification_host(root)
 try:
  handler=T.T.G.live_log(r.st)
  for oid,side in ((r.buy,'buy'),(r.sell,'sell')):
   assert handler('UPBIT',{'id':oid},T.T.info('UPBIT',side))['accepted'];T.tick(r)
  assert len(T.send(r))==1
  with T.store(r)._connect_read() as c:before=c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()
  assert configure_execution_bootstrap(r.boot);configure_history_provider(r.provider)
  assert handler('UPBIT',{'id':r.sell},T.T.info('UPBIT','sell'))['accepted'];T.H.dp.verification_tick(r.st,r.boot.client_for)
  assert T.send(r)==[]
  with T.store(r)._connect_read() as c:assert c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()==before
 finally:configure_upbit_notification_host(None)



def test_unattributed_flag_does_not_relax_strict_registered_scope(krw):
 from exchange.verified_pnl.upbit_spot_read import UpbitNotificationHost,UpbitReadIncomplete
 with pytest.raises(UpbitReadIncomplete,match='UPBIT_STRATEGY_BINDING_REQUIRED'):
  UpbitNotificationHost(krw.client,source_account_ref=krw.scope.legacy_account_reference,strategy='주문',allow_unattributed=True)


def test_default_installer_flags_do_not_require_verified_pnl(local):
 r=local
 for k,v in I.FLAGS.items():setattr(r.st,k,v)
 assert r.st.UPBIT_NET_PNL_ENABLED is False and r.st.NET_REALIZED_PNL_ENABLED is False
 submit(r,'default-install-buy','buy',['100000000']);submit(r,'default-install-sell','sell',['110000000'])
 p=payloads(r)[-1];assert T.T.D(p['estimated_pnl']['amount'])==9895000 and p['financial_receipt'] is None
 assert len(T.send(r))==1



def test_lru_eviction_keeps_inflight_host_binding(local):
 import gc
 r=local;active=r.host.for_strategy('active-worker-strategy')
 for i in range(40):r.host.for_strategy('other-strategy-'+str(i))
 gc.collect()
 assert len(r.host._children)==32 and r.host.by_binding(active.binding) is active
 assert r.host.for_strategy(active.strategy) is active
 assert r.calls==[]
