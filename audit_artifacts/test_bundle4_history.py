import pytest,copy,json,hashlib,sqlite3
from dataclasses import replace
from types import SimpleNamespace as NS
from decimal import Decimal as D
from unittest.mock import patch,Mock
from test_bundle3_bootstrap import rig,reset,active
from exchange.verified_pnl.history import *
from exchange.verified_pnl.bootstrap import OrderSnapshot,TradePage
from exchange.verified_pnl import evidence as E
import r6_worker_harness as H

@pytest.fixture(autouse=True)
def clean_history():
 configure_history_provider(None);yield;configure_history_provider(None)


def raw_fact(o,sequence):
 return dict(sequence=sequence,order_id=o['id'],side=o['side'],status=('FILLED' if o['status']=='closed' else 'PARTIALLY_FILLED'),exchange_status=o['status'],requested=json.loads(E.canonical(D(o['amount']))),remaining=json.loads(E.canonical(D(o.get('remaining','0')))),quantity=json.loads(E.canonical(D(o['filled']))),average=json.loads(E.canonical(D(o['average']))),trades=[dict(id=t['id'],amount=json.loads(E.canonical(D(t['amount']))),price=json.loads(E.canonical(D(t['price']))),fee=json.loads(E.canonical(D(t['fee']['cost']))),currency=t['fee']['currency'],timestamp=float(t['timestamp']/1000)) for t in o['trades']])

@pytest.fixture
def history(rig):
 buy=H.full_entry();sell=H.native('sell',160,1,ts=1700000001000)
 buy.update(side='buy',symbol='BTC/USDT');sell.update(side='sell',symbol='BTC/USDT')
 state={'orders':{'buy':buy,'sell':sell},'audit':[copy.deepcopy(buy),copy.deepcopy(sell)],'closing':'1','calls':[],'observed':0}
 s=rig.scope
 def read(c,oid,symbol):return OrderSnapshot(copy.deepcopy(state['orders'][oid]),s.account,s.subaccount,'fixed-window')
 def page(c,oid,symbol,cursor,snapshot):return TradePage(copy.deepcopy(state['orders'][oid]['trades']),s.account,s.subaccount,oid,symbol,snapshot,cursor,None)
 rig.boot.adapter=replace(rig.adapter,read_order=read,read_page=page)
 active(rig);context=E.context_for(s.exchange,s.account,rig.info,rig.client);scope=E.strict_object(context.scope_json)
 def coverage(c,requested):
  if state.get('timeout'):raise TimeoutError('FAKE_HISTORY_TOKEN_CANARY')
  facts=[raw_fact(o,i+1) for i,o in enumerate(state['audit'])];h=hashlib.sha256()
  for fact in facts:h.update((E.canonical(fact)+'\n').encode())
  return Coverage(state.get('scope',scope),1699999999.0,state.get('through',1700000001.0),len(facts),sum(len(o['trades']) for o in state['audit']),h.hexdigest(),'coverage-ref',state.get('revision',1),state['observed'])
 def balance(c,requested,kind):
  if state.get('missing_anchor') and kind=='opening':return None
  return BalanceEvidence(scope,1699999999.0 if kind=='opening' else state.get('through',1700000001.0),state.get('opening','0') if kind=='opening' else state['closing'],'flat-ref' if kind=='opening' else 'closing-ref')
 def inventory(c,requested):return InventoryEvidence(scope,1699999999.0,state.get('through',1700000001.0),state.get('opening','0'),state.get('inventory_closing',state['closing']),state.get('external',()),state.get('owners',(scope['book'],)),'inventory-ref')
 def history_page(c,requested,after,limit):
  state['calls'].append((after,limit))
  source=state.get('listed',state['audit'])
  values=tuple(HistoryOrder(i+1,o['id'],o['side'],scope['book']) for i,o in enumerate(source) if i+1>after)[:limit]
  return HistoryPage(scope,after,values,'page-ref')
 adapter=HistoryReadAdapter('independent-flat-account-audit-fixture-v1',coverage,balance,inventory,history_page)
 provider=TrustedSpotHistoryProvider(rig.boot,adapter);configure_history_provider(provider)
 return NS(rig=rig,state=state,provider=provider,scope=scope)


def request(side,qty=1):
 return H.t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side=side,amount=qty)

def run_order(h,oid='sell',side='sell',qty=1):
 r=h.rig;info=request(side,qty);H.dp.dispatch_verified_alert('BINANCE',{'id':oid},info,r.st)
 H.dp.verification_tick(r.st,r.boot.client_for)
 with sqlite3.connect(r.st.VERIFIED_NOTIFICATIONS_DB_PATH) as c:
  rows=c.execute('select payload from vpnl_revisions order by rowid').fetchall()
 return [json.loads(v[0]) for v in rows]


def test_history_generation_port_exists():
 assert history_readiness()=='HISTORY_NOT_READY'


def test_normal_151_8_roi_and_generation(history):
 values=run_order(history);assert values
 p=values[-1];assert E.verify(p['financial_receipt'],'FINANCIAL')
 assert D(p['net_pnl'].split()[0])==8 and D(p['cost_basis'])==151
 assert D('5.29')<D(p['roi_pct'].rstrip('%'))<D('5.31')
 assert p['financial_receipt']['body']['generation_id']
 assert history_readiness()=='HISTORY_COMPLETE'

@pytest.mark.parametrize('fault',['anchor','opening','first_order','missing_page','vwap','duplicate','account','symbol','wrong_order','external','inventory','owners','fee','paid_fee'])
def test_incomplete_or_contradictory_history_no_financial(history,fault):
 h=history;s=h.state
 if fault=='anchor':s['missing_anchor']=True
 elif fault=='opening':s['opening']='1'
 elif fault=='first_order':s['listed']=[s['audit'][1]]
 elif fault=='missing_page':s['listed']=[]
 elif fault=='vwap':s['orders']['buy']['average']='149'
 elif fault=='duplicate':s['orders']['buy']['trades'][1]['id']=s['orders']['buy']['trades'][0]['id']
 elif fault=='account':s['scope']=dict(h.scope,account='other-account')
 elif fault=='symbol':s['orders']['buy']['symbol']='ETH/USDT'
 elif fault=='wrong_order':s['orders']['buy']['trades'][0]['order']='other-order'
 elif fault=='external':s['external']=('unexplained-transfer',)
 elif fault=='inventory':s['inventory_closing']='2'
 elif fault=='owners':s['owners']=(h.scope['book'],'other-strategy')
 elif fault=='fee':s['orders']['buy']['fees']=[dict(cost='999',currency='USDT')]
 else:s['orders']['buy']['paid_fee']='999'
 values=run_order(h)
 assert all(v['financial_receipt'] is None and v['net_pnl'] is None for v in values)
 assert history_readiness()=='HISTORY_NOT_READY'


def store(h):return H.ob.NotificationOutboxStore(h.rig.st.VERIFIED_NOTIFICATIONS_DB_PATH)

def tick(h):
 with store(h)._connect() as c:
  c.execute("update vpnl_jobs set due=0 where state='PENDING'");c.execute('update vpnl_history_refresh set due=0')
 H.dp.verification_tick(h.rig.st,h.rig.boot.client_for)

def send(h,callback=None):
 with patch('requests.post',side_effect=callback,return_value=Mock(status_code=200,json=Mock(return_value={'id':'123'}))) as post:H.dp.flush_outbox(h.rig.st)
 return [call.kwargs['data'] for call in post.call_args_list]

def revisions(h):
 with store(h)._connect_read() as c:return c.execute('select d.revision,d.status,d.wire,v.payload from vpnl_deliveries d join vpnl_revisions v on v.root_id=d.root_id and v.revision=d.revision order by d.rowid').fetchall()

def correct_history(h):
 h.state['revision']=h.state.get('revision',1)+1
 h.state['orders']['buy']['trades'][0]['price']='120';h.state['orders']['buy']['average']='160'
 h.state['audit']=copy.deepcopy(list(h.state['orders'].values()))


def test_same_history_observation_time_has_no_revision_or_post(history):
 run_order(history);initial=send(history);assert len(initial)==1
 history.state['observed']=999999;tick(history);assert send(history)==[]
 with store(history)._connect_read() as c:
  assert c.execute('select count(*) from vpnl_history_generations').fetchone()[0]==1
  assert c.execute('select count(*) from vpnl_revisions').fetchone()[0]==1

@pytest.mark.parametrize('fault',['timeout','anchor'])
def test_temporary_missing_history_never_false_revokes(history,fault):
 run_order(history);before=send(history);assert len(before)==1
 history.state['timeout']=fault=='timeout';history.state['missing_anchor']=fault=='anchor'
 tick(history)
 assert send(history)==[] and len(revisions(history))==1
 with store(history)._connect_read() as c:assert c.execute('select count(*) from vpnl_history_invalidations').fetchone()[0]==0


def test_trusted_history_change_revokes_then_corrects_same_root(history):
 run_order(history);first=send(history);before=revisions(history)[0];assert len(first)==1
 correct_history(history);tick(history)
 after=revisions(history);assert len(after)==2
 assert after[0]==before and json.loads(after[1][3])['event_type']=='revoked'
 wires=send(history);assert len(wires)==1 and wires[0]==after[1][2]
 tick(history);corrected=revisions(history);assert len(corrected)==3
 assert D(json.loads(corrected[-1][3])['net_pnl'].split()[0])==-2
 assert len(send(history))==1
 with store(history)._connect_read() as c:
  assert c.execute('select count(*) from vpnl_roots').fetchone()[0]==1
  assert c.execute('select count(*) from vpnl_history_generations').fetchone()[0]==2
  assert c.execute('select count(*) from vpnl_history_invalidations').fetchone()[0]==1
  rows=c.execute("select a.revision,a.wire_digest,d.wire from vpnl_delivery_attempts a join vpnl_deliveries d on d.id=a.delivery_id where a.phase='SENT'").fetchall()
  assert len(rows)==3 and all(hashlib.sha256(r[2]).hexdigest()==r[1] for r in rows)

@pytest.mark.parametrize('state',['UNKNOWN','HELD'])
def test_uncertain_delivery_remains_manual_after_revocation(history,state):
 run_order(history)
 with store(history)._connect() as c:c.execute('update vpnl_deliveries set status=?',(state,))
 correct_history(history);tick(history);tick(history)
 assert send(history)==[]
 assert all(row[1] in ('UNKNOWN','HELD') for row in revisions(history))


def test_generation_and_cache_immutable_and_restart_valid(history):
 p=run_order(history)[-1];gid=p['financial_receipt']['body']['generation_id'];s=store(history)
 assert generation_valid(s,gid)
 with s._connect() as c:
  with pytest.raises(sqlite3.IntegrityError):c.execute("update vpnl_history_generations set body='{}'")
  with pytest.raises(sqlite3.IntegrityError):c.execute("update vpnl_history_trades set price='1'")
 assert generation_valid(store(history),gid)
 # Even bypassing a trigger as an offline corrupt fixture cannot retag cached cost.
 with s._connect() as c:
  c.execute('drop trigger vpnl_history_trades_UPDATE_sealed');c.execute("update vpnl_history_trades set price='1' where role='ENTRY'")
 assert not generation_valid(store(history),gid)


def test_stale_generation_cannot_repromote_after_restart(history):
 old=run_order(history)[-1];gid=old['financial_receipt']['body']['generation_id'];send(history)
 correct_history(history);tick(history);assert not generation_valid(store(history),gid)
 history.state['orders']['buy']['trades'][0]['price']='100';history.state['orders']['buy']['average']='150';history.state['audit']=copy.deepcopy(list(history.state['orders'].values()))
 tick(history)
 assert not generation_valid(store(history),gid)
 assert all(v['financial_receipt'] is None or v['financial_receipt']['body']['generation_id']!=gid for v in [json.loads(r[3]) for r in revisions(history)[1:]])


def test_revoked_key_is_fail_closed(history):
 p=run_order(history)[-1];gid=p['financial_receipt']['body']['generation_id']
 E.configure_provider(None,{})
 assert not generation_valid(store(history),gid)


def test_fake_incomplete_buy_cannot_yield_net58(history):
 h=history;buy=h.state['orders']['buy'];buy.update(amount='1',filled='1',average='100');buy['trades']=buy['trades'][:1];buy['fee']['cost']='1'
 values=run_order(h)
 assert all(v['net_pnl'] is None and v['financial_receipt'] is None for v in values)

@pytest.mark.parametrize('rebate',[False,True])
def test_interleaved_moving_average_and_rebates(history,rebate):
 h=history;orders=[]
 for i,(oid,side,price) in enumerate([('b1','buy',100),('b2','buy',200),('s1','sell',160),('b3','buy',200),('s2','sell',200)]):
  o=H.native(oid,price,1,ts=1700000000000+i*1000);o.update(side=side,symbol='BTC/USDT')
  if rebate:o['fee']['cost']='-1';o['trades'][0]['fee']['cost']='-1'
  orders.append(o)
 h.state.update(orders={o['id']:o for o in orders},audit=copy.deepcopy(orders),closing='1',through=1700000004.0)
 one=run_order(h,'s1')[-1];two=run_order(h,'s2')[-1]
 assert D(one['net_pnl'].split()[0])==(12 if rebate else 8)
 assert D(two['net_pnl'].split()[0])==(27 if rebate else 23)
 assert D(one['cost_basis'])==(149 if rebate else 151)


def test_large_history_staging_is_bounded_and_restartable(history):
 h=history;orders=[]
 for i in range(80):
  o=H.native(f'b{i:03}',150,1,ts=1700000000000+i*1000);o.update(side='buy',symbol='BTC/USDT');orders.append(o)
 close=H.native('sell',160,1,ts=1700000080000);close.update(side='sell',symbol='BTC/USDT');orders.append(close)
 h.state.update(orders={o['id']:o for o in orders},audit=copy.deepcopy(orders),closing='79',through=1700000080.0)
 initial=run_order(h)[-1];assert initial['financial_receipt'] is None
 cursors=[]
 for _ in range(5):
  with store(h)._connect_read() as c:cursors.append(c.execute('select cursor from vpnl_history_candidates').fetchone()[0])
  # New store instance each iteration; progress lives in SQLite rather than a Python list.
  tick(h)
 with store(h)._connect_read() as c:
  assert c.execute('select count(*) from vpnl_history_orders').fetchone()[0]==81
  assert c.execute('select count(*) from vpnl_history_generations').fetchone()[0]==1
  latest=json.loads(c.execute('select payload from vpnl_revisions order by rowid desc limit 1').fetchone()[0])
 assert all(0<=b-a<=20 for a,b in zip([0]+cursors,cursors))
 assert D(latest['net_pnl'].split()[0])==8 and D(latest['cost_basis'])==151
 assert all(limit<=40 for _,limit in h.state['calls'])


def test_generation_cannot_be_reused_for_other_scope(history):
 p=run_order(history)[-1];gid=p['financial_receipt']['body']['generation_id']
 from exchange.verified_pnl.models import VerifiedFill
 receipt=p['execution_receipt'];f=VerifiedFill(**receipt['body']['fill']);f.execution_receipt=receipt
 f.scope_data=dict(f.scope_data,account='another-account')
 assert not generation_valid(store(history),gid,f)


def test_old_accounting_cannot_be_reused_without_generation_store(history):
 p=run_order(history)[-1]
 from exchange.verified_pnl.models import VerifiedFill
 from exchange.verified_pnl.pnl_calculator import PnlCalculator
 f=VerifiedFill(**p['execution_receipt']['body']['fill']);f.execution_receipt=p['execution_receipt']
 # Financial receipt references the immutable accounting digest; obtain it by normal provider allocation.
 context=E.context_for(history.rig.scope.exchange,history.rig.scope.account,NS(**dict(vars(history.rig.info),side='sell',is_buy=False,is_sell=True)),history.rig.client)
 with store(history)._connect_read() as c:context=replace(context,history_receipt=json.loads(c.execute('select receipt from vpnl_history_generations').fetchone()[0]))
 entry,fee=accounting_for_generation(store(history),f,context)
 info=NS(is_sell=True,is_futures=False,is_close=False)
 assert PnlCalculator().calculate_pnl(f,info,entry_price=entry,allocated_entry_fee=fee,allocated_margin=D(151)).net_realized_pnl is None


def test_legacy_bad_cost_is_not_a_generation_source(history,tmp_path):
 from exchange.verified_pnl import migrations as M
 h=history;p=h.rig.st.VERIFIED_NOTIFICATIONS_DB_PATH
 with sqlite3.connect(p) as c:
  c.execute('create table verified_trades(id text primary key,scope text,data text)')
  c.execute('insert into verified_trades values(?,?,?)',('arbitrary-poison-id','unknown',json.dumps({'order':'buy','price':100,'amount':1,'fee':1})))
 s=H.ob.NotificationOutboxStore(p,migration_mode=True)
 M.verify_backup(s,tmp_path/'backup.sqlite',writer_quiesced=True);M.approve_cutover(s,approved=True,actor='fixture')
 while s.import_legacy_batch():pass
 M.finish_cutover(s)
 value=run_order(h)[-1]
 assert D(value['cost_basis'])==151 and D(value['net_pnl'].split()[0])==8
 with sqlite3.connect(p) as c:assert c.execute('select count(*) from verified_trades').fetchone()[0]==1


def test_generation_dependency_blocks_stale_publish_and_preserves_sent(history):
 from exchange.verified_pnl.models import NotificationPayload
 p=run_order(history)[-1];send(history);before=revisions(history)[0]
 correct_history(history);tick(history)
 stale=NotificationPayload(**p);stale.event_version=99
 assert store(history).enqueue(stale) is False
 with store(history)._connect() as c:
  root=c.execute('select id from vpnl_roots').fetchone()[0]
  with pytest.raises(sqlite3.IntegrityError,match='history_dependency_unavailable'):
   c.execute('insert into vpnl_revisions values(?,?,?,?,?,?)',(root,99,E.canonical(stale.dict()),E.digest(H.ob.NotificationOutboxStore._facts(stale)),'CORRECTION',0))
 assert revisions(history)[0]==before


def test_posting_race_keeps_actual_wire_revision_and_ack(history):
 run_order(history);wires=[]
 def post(*a,**kw):
  wires.append(kw['data'])
  if len(wires)==1:correct_history(history);tick(history)
  return Mock(status_code=200,json=Mock(return_value={'id':'123'}))
 send(history,post)
 actual=revisions(history)
 assert actual[0][1]=='SENT' and actual[0][2]==wires[0]
 with store(history)._connect_read() as c:
  for revision,digest,wire in c.execute("select a.revision,a.wire_digest,d.wire from vpnl_delivery_attempts a join vpnl_deliveries d on d.id=a.delivery_id where a.phase='SENT'"):
   assert hashlib.sha256(wire).hexdigest()==digest and any(wire==w for w in wires)


def test_canary_history_timeout_has_no_logs_or_revocation(history,capsys):
 from loguru import logger
 run_order(history);send(history);logs=[];sink=logger.add(lambda msg:logs.append(str(msg)))
 try:
  history.state['timeout']=True;tick(history)
  with store(history)._connect_read() as c:text='\n'.join(c.iterdump())
  capture=capsys.readouterr();assert 'FAKE_HISTORY_TOKEN_CANARY' not in ''.join(logs)+capture.out+capture.err+text
  assert len(revisions(history))==1
 finally:logger.remove(sink)


def test_same_financial_fact_new_coverage_does_not_starve_real_revocation(history):
 h=history;run_order(h);assert len(send(h))==1
 extra=H.native('late-buy',200,1,ts=1700000002000);extra.update(side='buy',symbol='BTC/USDT')
 h.state['orders']['late-buy']=extra;h.state['audit']=copy.deepcopy(list(h.state['orders'].values()));h.state['through']=1700000002.0;h.state['closing']='2'
 h.state['revision']=2
 tick(h);assert len(revisions(h))==1 and send(h)==[]
 with store(h)._connect_read() as c:assert c.execute('select count(*) from vpnl_history_generations').fetchone()[0]==2
 correct_history(h);tick(h)
 values=revisions(h);assert len(values)==2 and json.loads(values[-1][3])['event_type']=='revoked'
 assert len(send(h))==1


def test_no_history_provider_keeps_execution_only(history):
 configure_history_provider(None)
 value=run_order(history)[-1]
 assert E.verify(value['execution_receipt'],'EXECUTION') and value['financial_receipt'] is None
 assert value['cost_basis'] is None and value['net_pnl'] is None and value['roi_pct'] is None


def test_generation_dependencies_are_not_pruned(history):
 run_order(history);send(history);s=store(history)
 assert s.prune_completed(max_age_seconds=-1)==0
 with s._connect_read() as c:
  assert c.execute('select count(*) from vpnl_jobs').fetchone()[0]==1
  assert c.execute('select count(*) from vpnl_history_generations').fetchone()[0]==1


def test_rotated_unpinned_key_cannot_reuse_old_generation(history):
 from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
 from cryptography.hazmat.primitives import serialization
 p=run_order(history)[-1];gid=p['financial_receipt']['body']['generation_id']
 replacement=Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
 E.configure_provider(None,{'new-key':replacement})
 assert not generation_valid(store(history),gid)


def test_numeric_format_only_is_same_generation(history):
 h=history;run_order(h);send(h)
 for o in h.state['orders'].values():
  for k in ('amount','filled','average'):o[k]=format(D(o[k]),'.4f')
  o['fee']['cost']=format(D(o['fee']['cost']),'.4f')
  for t in o['trades']:
   for k in ('amount','price'):t[k]=format(D(t[k]),'.4f')
   t['fee']['cost']=format(D(t['fee']['cost']),'.4f')
 h.state['closing']='1.000';h.state['audit']=copy.deepcopy(list(h.state['orders'].values()))
 tick(h);assert send(h)==[]
 with store(h)._connect_read() as c:assert c.execute('select count(*) from vpnl_history_generations').fetchone()[0]==1


def test_rotated_issuer_cannot_forge_old_issuer_withdrawal(history):
 from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
 from cryptography.hazmat.primitives import serialization
 from exchange.verified_pnl.bootstrap import configure_execution_bootstrap
 h=history;run_order(h);assert len(send(h))==1;before=revisions(h)[0]
 newkey=Ed25519PrivateKey.generate();public=newkey.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
 h.rig.boot.authority=E.EvidenceAuthority('rotated-fixture-key',newkey)
 h.rig.boot.public_keys['rotated-fixture-key']=public;h.rig.boot.pin_fingerprints['rotated-fixture-key']=hashlib.sha256(public).hexdigest()
 assert configure_execution_bootstrap(h.rig.boot)
 correct_history(h);tick(h)
 assert send(h)==[] and revisions(h)[0]==before and len(revisions(h))==1
 assert history_readiness()=='HISTORY_KEY_REVIEW'
 with store(h)._connect_read() as c:assert c.execute("select count(*) from vpnl_history_revocations where state='KEY_REVIEW'").fetchone()[0]==1


def test_process_restart_verifies_generation_and_dependencies_with_public_pin(history):
 import subprocess,sys,os
 h=history;p=run_order(h)[-1];gid=p['financial_receipt']['body']['generation_id']
 program="""import sys,json;import r6_worker_harness as H;from exchange.verified_pnl import evidence as E;from exchange.verified_pnl.history import generation_valid;E.configure_provider(None,{'fixture-only':bytes.fromhex(sys.argv[2])});s=H.ob.NotificationOutboxStore(sys.argv[1]);valid=generation_valid(s,sys.argv[3]);c=__import__('sqlite3').connect(sys.argv[1]);count=c.execute('select count(*) from vpnl_history_dependencies').fetchone()[0];c.close();print(json.dumps({'valid':valid,'dependencies':count}))"""
 def child():
  result=subprocess.run([sys.executable,'-c',program,str(h.rig.st.VERIFIED_NOTIFICATIONS_DB_PATH),h.rig.public.hex(),gid],cwd=str(H.ROOT),env=os.environ.copy(),capture_output=True,text=True)
  assert result.returncode==0;return json.loads(result.stdout)
 first=child();assert first['valid'] is True and first['dependencies']==1
 correct_history(h);tick(h)
 second=child();assert second['valid'] is False and second['dependencies']==1


def test_actual_start_worker_uses_history_generation_pipeline(history):
 h=history;r=h.rig;H.dp.dispatch_verified_alert('BINANCE',{'id':'sell'},request('sell'),r.st);iterations=[0]
 class OneRun:
  def __init__(self,target,**kw):self.target=target
  def start(self):self.target()
 class Stop:
  def is_set(self):return iterations[0]>0
  def wait(self,*args):iterations[0]+=1
  def set(self):iterations[0]=1
 with patch.object(H.dp,'threading',NS(Thread=OneRun,Event=Stop)),patch('requests.post',return_value=Mock(status_code=200,json=Mock(return_value={'id':'123'}))) as post:
  assert H.dp.start_worker(r.st,execution_bootstrap=r.boot) is not None
 assert post.call_count==1 and isinstance(post.call_args.kwargs['data'],bytes)
 p=json.loads(revisions(h)[0][3]);assert D(p['net_pnl'].split()[0])==8 and p['financial_receipt']['body']['generation_id']


def test_pending_cache_tamper_cannot_be_signed_complete(history):
 h=history;orders=[]
 for i in range(21):
  o=H.native(f'b{i:03}',150,1,ts=1700000000000+i*1000);o.update(side='buy',symbol='BTC/USDT');orders.append(o)
 close=H.native('sell',160,1,ts=1700000021000);close.update(side='sell',symbol='BTC/USDT');orders.append(close)
 h.state.update(orders={o['id']:o for o in orders},audit=copy.deepcopy(orders),closing='20',through=1700000021.0)
 run_order(h)
 with store(h)._connect() as c:c.execute("update vpnl_history_trades set price='1' where order_id='b000'")
 tick(h)
 with store(h)._connect_read() as c:assert c.execute('select count(*) from vpnl_history_generations').fetchone()[0]==0
 assert all(json.loads(r[3])['financial_receipt'] is None for r in revisions(h))


def test_generation_head_rollback_is_detected(history):
 h=history;p=run_order(h)[-1];old=p['financial_receipt']['body']['generation_id'];send(h)
 extra=H.native('late-buy',200,1,ts=1700000002000);extra.update(side='buy',symbol='BTC/USDT')
 h.state['orders']['late-buy']=extra;h.state['audit']=copy.deepcopy(list(h.state['orders'].values()));h.state['through']=1700000002.0;h.state['closing']='2';h.state['revision']=2;tick(h)
 assert generation_valid(store(h),old)
 with store(h)._connect() as c:c.execute('update vpnl_history_heads set generation_id=?',(old,))
 assert not generation_valid(store(h),old)
 tick(h);assert send(h)==[] and history_readiness()=='HISTORY_NOT_READY'

@pytest.mark.parametrize('revision',[None,0,1])
def test_missing_or_stale_source_revision_cannot_correct_finance(history,revision):
 h=history;run_order(h);send(h);before=revisions(h)[0]
 correct_history(h);h.state['revision']=revision;tick(h)
 assert len(revisions(h))==1 and revisions(h)[0]==before and send(h)==[]


def test_source_revision_only_change_reuses_generation(history):
 h=history;run_order(h);send(h);h.state['revision']=2;tick(h)
 with store(h)._connect_read() as c:assert c.execute('select count(*) from vpnl_history_generations').fetchone()[0]==1
 assert send(h)==[] and len(revisions(h))==1


def test_ambiguous_entry_close_timestamp_cannot_guess_cost(history):
 h=history;h.state['orders']['sell']['trades'][0]['timestamp']=1700000000000;h.state['audit']=copy.deepcopy(list(h.state['orders'].values()))
 values=run_order(h)
 assert all(v['financial_receipt'] is None and v['net_pnl'] is None for v in values)


def test_multiple_flat_cycles_are_not_combined(history):
 h=history;buy=H.native('buy',150,1);buy.update(side='buy',symbol='BTC/USDT')
 sell=H.native('sell',160,1,ts=1700000001000);sell.update(side='sell',symbol='BTC/USDT')
 again=H.native('again',200,1,ts=1700000002000);again.update(side='buy',symbol='BTC/USDT')
 h.state.update(orders={o['id']:o for o in (buy,sell,again)},audit=copy.deepcopy([buy,sell,again]),closing='1',through=1700000002.0)
 values=run_order(h)
 assert all(v['financial_receipt'] is None for v in values)


def test_provenance_references_do_not_store_raw_http_secrets(history):
 h=history;a=h.provider.adapter
 def wrap(fn,label):
  def read(*args):return replace(fn(*args),reference='https://FAKE_SOURCE_HTTP_CANARY/'+label+'?token=FAKE_REFERENCE_TOKEN_CANARY')
  return read
 h.provider.adapter=replace(a,coverage=wrap(a.coverage,'coverage'),balance=lambda c,s,k:replace(a.balance(c,s,k),reference='FAKE_REFERENCE_TOKEN_CANARY-'+k),inventory=wrap(a.inventory,'inventory'))
 value=run_order(h)[-1];assert D(value['net_pnl'].split()[0])==8
 with store(h)._connect_read() as c:encoded='\n'.join(c.iterdump())
 assert 'FAKE_SOURCE_HTTP_CANARY' not in encoded and 'FAKE_REFERENCE_TOKEN_CANARY' not in encoded


def test_fractional_entry_fee_keeps_verified_accounting_precision(history):
 h=history;buy=H.native('buy',150,3);buy.update(side='buy',symbol='BTC/USDT');buy['fee']['cost']='1';buy['trades'][0]['fee']['cost']='1'
 sell=H.native('sell',160,1,ts=1700000001000);sell.update(side='sell',symbol='BTC/USDT')
 h.state.update(orders={'buy':buy,'sell':sell},audit=copy.deepcopy([buy,sell]),closing='2')
 p=run_order(h)[-1]
 assert E.verify(p['financial_receipt'],'FINANCIAL')
 from decimal import localcontext
 with localcontext() as precision:
  precision.prec=512
  assert D(p['net_pnl'].split()[0])==D(26)/D(3)
 wire=send(h)[0];description=json.loads(wire)['embeds'][0]['description']
 assert '+8.6667 USDT' in description and '손익 검증 완료' in description
 (H.ROOT/'audit_artifacts/bundle4_fractional_display.json').write_text(json.dumps({'description_length':len(description),'full_precision_matches':p['net_pnl'] in description,'rounded_8_6667_present':'+8.6667 USDT' in description}),encoding='utf8')
 assert D('150.33')<D(p['cost_basis'])<D('150.34')
