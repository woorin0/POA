import copy,json,sys,sqlite3
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'estimated_implementation'))
import test_estimated_contracts as T
from test_upbit_read import raw_order,U,T as TIME
from exchange.verified_pnl.bootstrap import SpotScope,AuthenticatedAccount,configure_execution_bootstrap
from exchange.verified_pnl.history import configure_history_provider
from exchange.verified_pnl.upbit_spot_read import UpbitSpotReads,UpbitNotificationHost,configure_upbit_notification_host
from exchange.verified_pnl import evidence as E,intake as I,estimated_pnl as N
from bundle5_wire_observer import FrozenWireObserver
from test_krw_v5 import krw,reset_provider
H=T.H;OUT=Path(__file__).parent

@pytest.fixture
def local(tmp_path):
 configure_execution_bootstrap(None);configure_history_provider(None);configure_upbit_notification_host(None)
 st=H.settings(tmp_path/'notification.sqlite');st.UPBIT_VERIFIED_ALERTS=True;st.UPBIT_NET_PNL_ENABLED=True;st.UPBIT_KEY='mock-host'
 scope=SpotScope('UPBIT','host-account','pocket:'+U('pocket'),H.identity('UPBIT','mock-host'),'BTC/KRW','KRW-BTC','KRW','observation-cycle','physical')
 buy=raw_order('local-buy','bid',['100000000'],TIME);sell=raw_order('local-sell','ask',['110000000'],TIME+1000)
 state={'orders':{buy['uuid']:buy,sell['uuid']:sell},'identity':AuthenticatedAccount('UPBIT',scope.account,scope.subaccount,frozenset({'read'})),'calls':[],'error':False}
 def get(path,params):
  state['calls'].append((path,params.copy()));assert path=='/v1/order'
  if state['error']:raise RuntimeError('FAKE_API_KEY_CANARY Bearer FAKE_BEARER_CANARY')
  return copy.deepcopy(state['orders'][params['uuid']])
 reader=UpbitSpotReads(scope,get,lambda c:state['identity'],pocket_uuid=U('pocket'),as_of_ms=TIME+10000)
 host=UpbitNotificationHost(reader,source_account_ref=scope.legacy_account_reference,strategy='strategy-A');assert configure_upbit_notification_host(host)
 r=NS(st=st,scope=scope,state=state,reader=reader,host=host,buy=buy['uuid'],sell=sell['uuid'],log=T.G.live_log(st))
 yield r
 configure_upbit_notification_host(None);configure_execution_bootstrap(None);configure_history_provider(None)

def feed(r,side,oid=None):
 info=T.info('UPBIT',side);result=r.log('UPBIT',{'id':oid or (r.buy if side=='buy' else r.sell)},info);assert result['accepted'];return result

def tick(r):H.dp.verification_tick(r.st,lambda *a:(_ for _ in ()).throw(AssertionError('FINANCIAL_CLIENT_FORBIDDEN')))
def store(r):return H.ob.NotificationOutboxStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH)
def ps(r):
 with store(r)._connect_read() as c:return [json.loads(x[0]) for x in c.execute('SELECT payload FROM vpnl_revisions')]
def send(r):
 with FrozenWireObserver(store(r)) as o:H.dp.flush_outbox(r.st)
 return o.finish()

def test_no_feeaudit_ack_buy_sell_real_entry(local):
 r=local
 assert not hasattr(r.reader,'_krw_audit_capability')
 for side in ('buy','sell'):feed(r,side);tick(r)
 p=ps(r)[0];e=p['estimated_pnl']
 assert p['notification_type']=='ESTIMATED' and e['status']=='ESTIMATED'
 assert T.D(e['entry_price'])==100000000 and T.D(e['amount'])==9895000 and T.D('9.88')<T.D(e['roi'])<T.D('9.90')
 assert all(p.get(k) is None for k in ('execution_receipt','accounting_receipt','financial_receipt','net_pnl','roi_pct'))
 with store(r)._connect_read() as c:
  assert c.execute('SELECT count(*) FROM vpnl_notice_upbit_observations').fetchone()[0]==2
  assert c.execute('SELECT count(*) FROM vpnl_notice_successors').fetchone()[0]==0
  assert all(not json.loads(x[0])['confirmed'] for x in c.execute('SELECT snapshot FROM vpnl_notice_inputs'))
 wires=send(r);assert len(wires)==1
 (OUT/'normal_wire.json').write_text(json.dumps(wires,ensure_ascii=False,indent=2),encoding='utf-8')


def test_missing_buy_unavailable(local):
 r=local;feed(r,'sell');tick(r);assert ps(r)[0]['estimated_pnl']['status']=='UNAVAILABLE'
 assert '계산 불가' in json.dumps(send(r)[0]['json'],ensure_ascii=False)


def test_missing_paid_fee_gross_not_zero(local):
 r=local
 for raw in r.state['orders'].values():raw.pop('paid_fee')
 for side in ('buy','sell'):feed(r,side);tick(r)
 e=ps(r)[0]['estimated_pnl'];assert e['fee_mode']=='MISSING' and T.D(e['amount'])==10000000
 assert '수수료 미반영 추정 차익' in json.dumps(send(r)[0]['json'],ensure_ascii=False)
 with pytest.raises(ValueError):r.reader.read_order(r.reader,r.buy,'BTC/KRW') # original finance entry remains strict


@pytest.mark.parametrize('fault',['uuid','account','pocket','symbol','side','trades','quantity','funds','wait','cancel','timeout','strategy'])
def test_invalid_scope_or_incomplete_no_post(local,fault):
 r=local;raw=r.state['orders'][r.sell]
 if fault=='uuid':raw['uuid']=U('different')
 elif fault=='account':r.state['identity']=AuthenticatedAccount('UPBIT','other-account',r.scope.subaccount,frozenset({'read'}))
 elif fault=='pocket':r.state['identity']=AuthenticatedAccount('UPBIT',r.scope.account,'pocket:'+U('other'),frozenset({'read'}))
 elif fault=='symbol':raw['market']='KRW-ETH'
 elif fault=='side':raw['side']='bid'
 elif fault=='trades':raw['trades']+=copy.deepcopy(raw['trades'])
 elif fault=='quantity':raw['executed_volume']='2'
 elif fault=='funds':raw['trades'][0]['funds']='110000001'
 elif fault=='wait':raw.update(state='wait',remaining_volume='1',executed_volume='0',trades_count=0,trades=[])
 elif fault=='cancel':raw['state']='cancel'
 elif fault=='timeout':r.state['error']=True
 elif fault=='strategy':
  result=r.log('UPBIT',{'id':r.sell},T.info('UPBIT','sell',strategy='other'));assert result['accepted'];tick(r);assert send(r)==[];return
 feed(r,'sell');tick(r)
 assert ps(r)==[] and send(r)==[]


def test_multifill_done_vwap_not_trade_fees(local):
 r=local
 r.state['orders'][r.buy]=raw_order('local-buy','bid',['100000000','200000000'],TIME)
 r.state['orders'][r.sell]=raw_order('local-sell','ask',['160000000'],TIME+1000)
 for side in ('buy','sell'):feed(r,side);tick(r)
 e=ps(r)[0]['estimated_pnl'];assert T.D(e['entry_price'])==150000000 and T.D(e['amount'])==9845000
 assert len(send(r))==1


def test_registration_and_forged_observation_cannot_admit(local):
 from dataclasses import replace
 r=local
 assert not r.host.owns({'kind':'UPBIT_ESTIMATED_READ_V1','verified':True})
 result=feed(r,'buy');i=T.info('UPBIT','buy')
 obs=r.host.observe(result['intake_id'],i,r.scope.legacy_account_reference,r.buy)
 changed=obs.facts;changed['average']='1';changed['trades'][0]['price']='1';changed['trades'][0]['funds']='1'
 fake=replace(obs,_facts_json=E.canonical(changed))
 assert r.host.owns(obs) and not r.host.owns(fake)
 # Returned facts are copies, not mutable authority.
 other=obs.facts;other['filled']='999';assert obs.facts['filled']=='1'
 assert not store(r).record_upbit_observation('unknown','lease',{},fake)
 for x in ({'reader':r.reader},lambda:None,object()):assert not configure_upbit_notification_host(x)
 assert configure_upbit_notification_host(r.host)


def test_repeat_query_and_restart_keep_originals(local):
 r=local
 for side in ('buy','sell'):feed(r,side);tick(r)
 s=store(r)
 with s._connect_read() as c:before=c.execute('SELECT intake_id,record,record_digest FROM vpnl_notice_upbit_observations ORDER BY intake_id').fetchall()
 wires=send(r);assert len(wires)==1
 I._BOOT_CHECKED.discard(s.db_path);I.replay(r.st,store=store(r))
 for side in ('buy','sell'):feed(r,side)
 tick(r);assert send(r)==[]
 with s._connect_read() as c:assert c.execute('SELECT intake_id,record,record_digest FROM vpnl_notice_upbit_observations ORDER BY intake_id').fetchall()==before


def test_conflicting_reread_is_review_not_overwrite(local):
 r=local;res=feed(r,'buy');tick(r);s=store(r)
 with s._connect_read() as c:key,encoded=c.execute('SELECT id,data FROM vpnl_jobs').fetchone();before=c.execute('SELECT record FROM vpnl_notice_upbit_observations').fetchone()
 data=s.parse_job(encoded)
 with s._connect() as c:c.execute("UPDATE vpnl_jobs SET state='VERIFYING',job_lease='fixture-lease',due=? WHERE id=?",(__import__('time').time()+120,key))
 raw=r.state['orders'][r.buy];raw['price']='101000000';raw['trades'][0]['price']='101000000';raw['trades'][0]['funds']='101000000'
 obs=r.host.observe(res['intake_id'],T.info('UPBIT','buy'),r.scope.legacy_account_reference,r.buy)
 assert not s.record_upbit_observation(key,'fixture-lease',data,obs)
 with s._connect_read() as c:
  assert c.execute('SELECT record FROM vpnl_notice_upbit_observations').fetchone()==before
  assert c.execute('SELECT reason FROM vpnl_notice_reviews').fetchone()[0]=='upbit_observation_conflict'

@pytest.mark.parametrize('status',['SENT','UNKNOWN','HELD','POSTING'])
def test_states_and_wire_cas_no_repost(local,status):
 r=local
 for side in ('buy','sell'):feed(r,side);tick(r)
 s=store(r);claim=None
 if status=='SENT':assert len(send(r))==1
 elif status=='POSTING':
  claim=s.claim_next();bad=dict(claim,token='wrong');assert not s.begin_post(bad);assert s.begin_post(claim);assert not s.ack(bad,True,message_id='123')
 else:
  with s._connect() as c:c.execute('UPDATE vpnl_deliveries SET status=?',(status,))
 with s._connect_read() as c:before=c.execute('SELECT wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()
 feed(r,'sell');tick(r);assert send(r)==[]
 with s._connect_read() as c:assert c.execute('SELECT wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()==before
 if claim:
  with FrozenWireObserver(s) as o:o.post(data=claim['wire'],headers={'Content-Type':'application/json'})
  assert s.ack(claim,True,message_id='123000') and o.finish()[0]['ack']['status']=='SENT'


def test_trade_id_cannot_move_order_after_reader_restart(local):
 r=local;feed(r,'buy');tick(r)
 # Clearing the reader cache simulates process restart; persisted ownership remains.
 r.reader._owners={}
 sell=r.state['orders'][r.sell];sell['trades'][0]['uuid']=r.state['orders'][r.buy]['trades'][0]['uuid']
 feed(r,'sell');tick(r);assert ps(r)==[] and send(r)==[]
 with store(r)._connect_read() as c:assert c.execute('SELECT reason FROM vpnl_notice_reviews').fetchone()[0]=='upbit_trade_owner_conflict'


def test_market_buy_request_total_not_fill_price(local):
 r=local;raw=r.state['orders'][r.buy];raw.update(ord_type='price',price='110000000',volume=None)
 for side in ('buy','sell'):feed(r,side);tick(r)
 assert T.D(ps(r)[0]['estimated_pnl']['entry_price'])==100000000


def test_no_registered_host_retains_ack(local):
 r=local;configure_upbit_notification_host(None)
 feed(r,'sell');tick(r)
 with store(r)._connect_read() as c:assert c.execute('SELECT state FROM vpnl_jobs').fetchone()[0]=='OBSERVED_WAIT_PROVIDER'
 assert send(r)==[]


def test_new_observation_table_is_immutable(local):
 r=local;feed(r,'buy');tick(r)
 with store(r)._connect() as c:
  with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE vpnl_notice_upbit_observations SET record='{}'")
  with pytest.raises(sqlite3.IntegrityError):c.execute('DELETE FROM vpnl_notice_upbit_observations')


def test_source_canary_not_stored_or_logged(local,capsys):
 from loguru import logger
 r=local;logs=[];sink=logger.add(lambda x:logs.append(str(x)))
 try:
  raw=r.state['orders'][r.buy];raw['authorization']='Bearer FAKE_API_KEY_CANARY';raw['info']={'key':'FAKE_API_KEY_CANARY'}
  for side in ('buy','sell'):feed(r,side);tick(r)
  wires=send(r)
 finally:logger.remove(sink)
 with store(r)._connect_read() as c:db=''.join(x[0] for x in c.execute('SELECT record FROM vpnl_notice_upbit_observations'))
 captured=capsys.readouterr();assert 'FAKE_API_KEY_CANARY' not in db+json.dumps(wires)+''.join(logs)+captured.out+captured.err


def test_new_process_worker_without_financial_provider(local,tmp_path):
 import subprocess,sys
 r=local
 for side in ('buy','sell'):feed(r,side);tick(r)
 s=store(r)
 with s._connect_read() as c:before=c.execute('SELECT record,record_digest FROM vpnl_notice_upbit_observations ORDER BY intake_id').fetchall()
 output=OUT/'child_worker_wire.json'
 code="""
import sys,json,time,threading
from pathlib import Path
import test_upbit_estimated as T
from exchange.verified_pnl.bootstrap import current_bootstrap
f=T.local.__wrapped__(Path(sys.argv[1]).parent);r=next(f)
assert current_bootstrap() is None
with T.FrozenWireObserver(T.store(r)) as observer:
 stop=T.H.dp.start_worker(r.st,upbit_observation_host=r.host);assert stop is not None
 deadline=time.time()+8
 while not observer.records and time.time()<deadline:time.sleep(.02)
 stop.set()
 deadline=time.time()+5
 while any(x.name=='verified-notifications' for x in threading.enumerate()) and time.time()<deadline:time.sleep(.02)
 records=observer.finish();assert len(records)==1
Path(sys.argv[2]).write_text(json.dumps(records),encoding='utf-8')
assert r.state['calls']==[] # replay uses committed observations, no new GET
"""
 child=subprocess.run([sys.executable,'-c',code,r.st.VERIFIED_NOTIFICATIONS_DB_PATH,str(output)],capture_output=True,cwd=Path.cwd(),timeout=25)
 assert child.returncode==0,child.stderr.decode('utf-8','replace')[-500:]
 assert json.loads(output.read_text(encoding='utf-8'))[0]['ack']['status']=='SENT'
 with s._connect_read() as c:assert c.execute('SELECT record,record_digest FROM vpnl_notice_upbit_observations ORDER BY intake_id').fetchall()==before
 assert send(r)==[]


def test_missing_host_after_restart_preserves_pending(local):
 r=local
 for side in ('buy','sell'):feed(r,side);tick(r)
 configure_upbit_notification_host(None)
 assert send(r)==[]
 with store(r)._connect_read() as c:assert c.execute('SELECT status FROM vpnl_deliveries').fetchone()[0]=='PENDING'


def test_registered_host_does_not_override_valid_v5(krw):
 r=krw
 host=UpbitNotificationHost(r.client,source_account_ref=r.scope.legacy_account_reference,strategy='strategy-A')
 assert configure_upbit_notification_host(host)
 try:
  handler=T.G.live_log(r.st);assert handler('UPBIT',{'id':r.sell},T.info('UPBIT','sell'))['accepted']
  H.dp.verification_tick(r.st,r.boot.client_for)
  obj=NS(st=r.st);p=ps(obj)[0]
  assert E.payload_valid(H.dp.NotificationPayload(**p)) and p.get('notification_type') is None
  assert T.D(p['net_pnl'].split()[0])==9895000
  with store(obj)._connect_read() as c:assert c.execute('SELECT count(*) FROM vpnl_notice_upbit_observations').fetchone()[0]==0
  assert len(send(obj))==1
 finally:configure_upbit_notification_host(None)


def test_same_actual_order_cannot_repost_under_new_source_reference(local):
 from dataclasses import replace
 r=local
 for side in ('buy','sell'):feed(r,side);tick(r)
 assert len(send(r))==1
 with store(r)._connect_read() as c:before=c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()
 # Explicit host reconfiguration must REVIEW an already observed physical order,
 # not silently make a new event under another credential reference.
 r.st.UPBIT_KEY='mock-host-reconfigured'
 r.reader.scope=replace(r.scope,legacy_account_reference=H.identity('UPBIT',r.st.UPBIT_KEY))
 newhost=UpbitNotificationHost(r.reader,source_account_ref=r.reader.scope.legacy_account_reference,strategy='strategy-A')
 assert configure_upbit_notification_host(newhost)
 for side in ('buy','sell'):feed(r,side);tick(r)
 assert send(r)==[]
 with store(r)._connect_read() as c:
  assert c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries').fetchall()==before
  assert c.execute('SELECT count(*) FROM vpnl_notice_reviews').fetchone()[0]==2
