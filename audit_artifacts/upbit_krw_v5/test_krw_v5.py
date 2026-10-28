"""Native Upbit REST + independent archived cash legs; no synthetic finance signer."""
import copy,json,hashlib,sqlite3,base64
from pathlib import Path
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from test_upbit_read import raw_order,U,T
import r6_worker_harness as H
from bundle5_wire_observer import FrozenWireObserver
from exchange.verified_pnl import evidence as E
from exchange.verified_pnl.bootstrap import *
from exchange.verified_pnl.upbit_spot_read import *
from exchange.verified_pnl.history import *
from exchange.verified_pnl.verifier import FillVerifier
from exchange.verified_pnl.models import NotificationPayload

@pytest.fixture(autouse=True)
def reset_provider():
 configure_execution_bootstrap(None);configure_history_provider(None)
 yield
 configure_execution_bootstrap(None);configure_history_provider(None)

class CashArchive:
 def __init__(self,record,ids):self.record=record;self.order_ids=ids;self.error=None
 def __call__(self,scope):
  if self.error:raise RuntimeError(self.error)
  return copy.deepcopy(self.record)

@pytest.fixture
def krw(tmp_path):
 pocket=U('v5-pocket');scope=SpotScope('UPBIT','pinned-krw-host-account','pocket:'+pocket,H.identity('UPBIT','unconfigured'),'BTC/KRW','KRW-BTC','KRW','archived-flat-cycle','physical')
 buy=raw_order('v5-buy','bid',['100000000'],T);sell=raw_order('v5-sell','ask',['110000000'],T+1000)
 originals={buy['uuid']:copy.deepcopy(buy),sell['uuid']:copy.deepcopy(sell)};orders=copy.deepcopy(originals)
 state={'orders':orders,'external':(),'owners':(scope.book,),'anchor':True,'revision':1,'history_missing':False,'read_error':None}
 def get(path,params):
  if state['read_error']:raise RuntimeError(state['read_error'])
  assert path=='/v1/order'
  return copy.deepcopy(orders[params['uuid']])
 def identity(c):return AuthenticatedAccount('UPBIT',scope.account,scope.subaccount,frozenset({'read'}))
 client=UpbitSpotReads(scope,get,identity,pocket_uuid=pocket,as_of_ms=T+10000)
 expected=[]
 for oid in originals:
  d=client.read_order(client,oid,scope.symbol).order;s=d['observation_summary'];t=d['trades'][0]
  expected.append(dict(order_id=oid,trade_id=t['id'],side=d['side'],quantity=str(decimal(t['amount'])),price=str(decimal(t['price'])),paid_fee=str(decimal(s['paid_fee'])),currency='KRW',created_us=s['created_us'],filled_us=utc_us(t['timestamp']),native_digest=E.digest(d)))
 # Independent historic source: these three balances are explicit test-oracle
 # archive values, NOT current /accounts responses or copied paid_fee records.
 points=(KrwCashCheckpoint((T-100)*1000,'200000000','0','archive-c0'),KrwCashCheckpoint((T+10)*1000,'99950000','1','archive-c1'),KrwCashCheckpoint((T+1010)*1000,'209895000','0','archive-c2'))
 archive=CashArchive(KrwCashAudit(scope,E.digest(expected),points,(),(scope.book,),'independent-cash-archive-revision1'),tuple(originals))
 cap=KrwCashAuditCapability(scope,'registered-independent-cash-archive',archive)
 port=client.enable_krw_contract(cap)
 key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
 authority=E.EvidenceAuthority('krw-fixture-issuer',key)
 boot=SpotExecutionBootstrap(scope,port,authority,{authority.key_id:public},{authority.key_id:hashlib.sha256(public).hexdigest()})
 assert configure_execution_bootstrap(boot)
 st=H.settings(tmp_path/'krw-v5.sqlite');st.UPBIT_VERIFIED_ALERTS=True;st.UPBIT_NET_PNL_ENABLED=True
 info=request('sell');context=E.context_for('UPBIT',scope.account,info,client);bound=E.strict_object(context.scope_json)
 facts=[]
 for i,o in enumerate(expected):
  facts.append(dict(sequence=i+1,order_id=o['order_id'],side=o['side'],status='FILLED',exchange_status='closed',requested='1',remaining='0',quantity='1',average=o['price'],trades=[dict(id=o['trade_id'],amount='1',price=o['price'],fee=json.loads(E.canonical(D(o['paid_fee']))),currency='KRW',timestamp=o['filled_us']/1000000)]))
 digest=hashlib.sha256(''.join(E.canonical(f)+'\n' for f in facts).encode()).hexdigest()
 def coverage(c,s):
  current=hashlib.sha256(''.join(E.canonical(f)+'\n' for f in state.get('history_facts',facts)).encode()).hexdigest()
  return Coverage(bound,(T-100)/1000,(T+1010)/1000,2,2,current,'independent-order-coverage',state['revision'])
 def balance(c,s,k):return BalanceEvidence(bound,(T-100)/1000 if k=='opening' else (T+1010)/1000,'0','independent-flat-'+k) if state['anchor'] else None
 def inventory(c,s):return InventoryEvidence(bound,(T-100)/1000,(T+1010)/1000,'0','0',state['external'],state['owners'],'independent-inventory')
 def page(c,s,after,limit):return HistoryPage(bound,after,tuple(HistoryOrder(i+1,o['order_id'],o['side'],scope.book) for i,o in enumerate(expected) if i+1>after)[:limit] if not state['history_missing'] else (), 'independent-history-page')
 provider=TrustedSpotHistoryProvider(boot,HistoryReadAdapter('independent-archived-flat-upbit-v1',coverage,balance,inventory,page));configure_history_provider(provider)
 return NS(scope=scope,client=client,st=st,info=info,boot=boot,key=key,public=public,archive=archive,cap=cap,state=state,expected=expected,context=context,provider=provider,buy=buy['uuid'],sell=sell['uuid'])

def request(side):
 i=H.t.OrderRequest(exchange='UPBIT',base='BTC',quote='KRW',side=side,amount=1)
 i.is_futures=False;i.is_coinm=False;i.is_buy=side=='buy';i.is_sell=side=='sell';i.is_close=False
 return i

def execute(k,oid=None):
 oid=oid or k.sell
 return FillVerifier().verify_order_result(NS(client=k.client),'UPBIT','BTC/KRW',{'id':oid,'verified':True},request('buy' if oid==k.buy else 'sell'),k.context)

def store(k):return H.ob.NotificationOutboxStore(k.st.VERIFIED_NOTIFICATIONS_DB_PATH)

def publish(k):
 accepted=H.dp.dispatch_verified_alert('UPBIT',{'id':k.sell},request('sell'),k.st)
 assert accepted is not None
 H.dp.verification_tick(k.st,k.boot.client_for)
 with store(k)._connect_read() as c:rows=c.execute('select payload from vpnl_revisions order by rowid').fetchall()
 return [NotificationPayload(**json.loads(x[0])) for x in rows]

def positive(k):
 ps=publish(k);assert ps
 p=ps[-1];assert E.payload_valid(p),[(q.net_pnl,q.pnl_verification_status) for q in ps]
 assert D(p.cost_basis)==D('100050000') and D(p.net_pnl.split()[0])==D('9895000')
 assert D('9.89')<D(p.roi_pct.rstrip('%'))<D('9.90')
 assert E.payload_acquisition_price(p)==D('100000000')
 return p

def test_full_native_trusted_path_oracle_embed_wire_restart(krw):
 k=krw;p=positive(k)
 for receipt,kind in [(p.execution_receipt,'EXECUTION'),(p.accounting_receipt,'ACCOUNTING'),(p.financial_receipt,'FINANCIAL')]:
  assert receipt['version']==5 and receipt['profile']==E.KRW_PROFILE and E.verify(receipt,kind)
 desc=H.de.build_mobile_verified_embed(p).to_dict()['description'];assert '미확정' not in desc and '+9,895,000원 (+9.89%)' in desc and '매수가: 100,000,000원' in desc
 with FrozenWireObserver(store(k)) as obs:H.dp.flush_outbox(k.st)
 wire=obs.finish();assert len(wire)==1 and wire[0]['ack']['status']=='SENT'
 before=wire[0]
 # Restart trusted bootstrap using the pinned public key + archive configuration.
 assert configure_execution_bootstrap(k.boot)
 assert E.payload_valid(p) and generation_valid(store(k),p.financial_receipt['body']['generation_id'])
 publish(k)
 with FrozenWireObserver(store(k)) as obs:H.dp.flush_outbox(k.st)
 assert obs.finish()==[]
 with store(k)._connect_read() as c:
  row=c.execute('select wire,status,message_id from vpnl_deliveries where id=?',(before['delivery'],)).fetchone()
 assert base64.b64encode(row[0]).decode()==before['raw_b64'] and row[1]=='SENT' and row[2]==before['ack']['message_id']
 out=Path(__file__).parent
 (out/'positive_payload.json').write_text(E.canonical(p.dict()),encoding='utf-8')
 (out/'test_public_pin.json').write_text(json.dumps(dict(test_only=True,key_id=k.boot.authority.key_id,public_key_b64=base64.b64encode(k.public).decode(),public_sha256=hashlib.sha256(k.public).hexdigest(),scope=p.financial_receipt['body']['scope'],audit_source_digest=k.client.audit_source_digest()),indent=2),encoding='utf-8')
 (out/'wire_evidence.json').write_text(json.dumps(wire,ensure_ascii=False,indent=2),encoding='utf-8')
 with sqlite3.connect(k.st.VERIFIED_NOTIFICATIONS_DB_PATH) as src,sqlite3.connect(out/'positive_v4_store.sqlite') as dst:src.backup(dst)

@pytest.mark.parametrize('fault',['multi','cancel','paid_missing','paid_conflict','audit_missing','audit_dict','copied_rest','checkpoint_fee_conflict','pocket','archive_orders','archive_external','archive_shared','other_order','timeout','audit_timeout'])
def test_execution_fails_closed(krw,fault):
 k=krw;o=k.state['orders'][k.buy];a=k.archive
 if fault=='multi':
  t=copy.deepcopy(o['trades'][0]);t['uuid']=U('second-v5');o['trades'].append(t);o.update(volume='2',executed_volume='2',trades_count=2)
 elif fault=='cancel':o['state']='cancel'
 elif fault=='paid_missing':o.pop('paid_fee')
 elif fault=='paid_conflict':o['paid_fee']='1'
 elif fault=='audit_missing':k.client._krw_audit_capability=None
 elif fault=='audit_dict':a.record=vars(a.record)
 elif fault=='copied_rest':a.record=copy.deepcopy(o)
 elif fault=='checkpoint_fee_conflict':
  pts=list(a.record.checkpoints);pts[1]=replace(pts[1],krw='99999999');a.record=replace(a.record,checkpoints=tuple(pts))
 elif fault=='pocket':a.record=replace(a.record,scope=replace(k.scope,subaccount='pocket:'+U('other')))
 elif fault=='archive_orders':a.record=replace(a.record,orders_digest='0'*64)
 elif fault=='archive_external':a.record=replace(a.record,external_events=('deposit',))
 elif fault=='archive_shared':a.record=replace(a.record,owners=('other-strategy',))
 elif fault=='other_order':o['uuid']=U('another-order')
 elif fault=='timeout':k.state['read_error']='FAKE_API_KEY_CANARY'
 else:a.error='FAKE_BEARER_CANARY'
 f=execute(k);assert f.execution_receipt is None and f.verification_status.value=='UNVERIFIED'

@pytest.mark.parametrize('fault',['anchor','missing_history','external','shared'])
def test_execution_can_verify_but_history_incomplete_no_finance(krw,fault):
 k=krw
 if fault=='anchor':k.state['anchor']=False
 elif fault=='missing_history':k.state['history_missing']=True
 elif fault=='external':k.state['external']=('deposit',)
 else:k.state['owners']=(k.scope.book,'other')
 assert execute(k).execution_receipt is not None
 ps=publish(k);assert ps and all(not E.payload_valid(p) and p.net_pnl is None for p in ps)

@pytest.mark.parametrize('fault',['entry','entry_tiny','cost','net','roi','issuer','account','order','version','signature','audit','generation'])
def test_tampered_finance_not_confirmed(krw,fault):
 k=krw;p=positive(k);original=p.copy(deep=True)
 if fault=='entry':p.entry_price='100'
 elif fault=='entry_tiny':p.entry_price='99999999.999'
 elif fault=='cost':p.cost_basis='101'
 elif fault=='net':p.net_pnl='+58 KRW'
 elif fault=='roi':p.roi_pct='57.43%'
 elif fault=='issuer':p.financial_receipt['key_id']='unregistered'
 elif fault=='account':p.financial_receipt['body']['scope']['account']='other'
 elif fault=='order':p.order_id=U('other-financial')
 elif fault=='version':p.financial_receipt['version']=4;p.financial_receipt.pop('profile')
 elif fault=='signature':p.financial_receipt['signature']='A'*88
 elif fault=='audit':p.execution_receipt['body']['fee_evidence']['audit']['orders_digest']='0'*64
 else:
  gid=p.financial_receipt['body']['generation_id']
  with store(k)._connect() as c:
   c.execute('drop trigger vpnl_history_trades_UPDATE_sealed');c.execute("update vpnl_history_trades set price='101' where role='ENTRY'")
  assert not generation_valid(store(k),gid);return
 if fault in ('entry','entry_tiny'):
  assert E.payload_acquisition_price(p) is None and E.payload_valid(p)
 elif fault=='audit':assert not E.payload_fill_valid(p) and E.payload_acquisition_price(p) is None
 else:assert not E.payload_valid(p)
 assert E.payload_valid(original)

@pytest.mark.parametrize('status',['UNKNOWN','HELD'])
def test_uncertain_no_automatic_post(krw,status):
 positive(krw);s=store(krw)
 with s._connect() as c:c.execute('update vpnl_deliveries set status=?',(status,))
 with FrozenWireObserver(s) as obs:H.dp.flush_outbox(krw.st)
 assert not obs.finish()
 with s._connect_read() as c:assert c.execute('select status from vpnl_deliveries').fetchone()[0]==status

def test_no_canary_on_read_error(krw,capsys):
 from loguru import logger
 logs=[];sink=logger.add(lambda m:logs.append(str(m)))
 try:
  krw.state['read_error']='FAKE_API_KEY_CANARY Bearer FAKE_BEARER_CANARY https://discord.com/api/webhooks/FAKE_WEBHOOK_CANARY'
  ps=publish(krw)
 finally:logger.remove(sink)
 captured=capsys.readouterr();text=captured.out+captured.err+''.join(logs)+E.canonical([p.dict() for p in ps])
 with store(krw)._connect_read() as c:
  for table in ('vpnl_jobs','vpnl_deliveries'):
   text+=str(c.execute('select * from '+table).fetchall())
 assert not any(v in text for v in ('FAKE_API_KEY_CANARY','FAKE_BEARER_CANARY','FAKE_WEBHOOK_CANARY'))


def test_v4_exact_canonical_signature_preserved():
 key=Ed25519PrivateKey.from_private_bytes(bytes(range(32)));public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
 E.configure_provider(None,{'v4-known-fixture':public})
 body={'scope':{'exchange':'BINANCE','settlement':'USDT'},'amount':D('151.000'),'fee':D('1.00')}
 unsigned={'version':4,'kind':'EXECUTION','key_id':'v4-known-fixture','body':{'scope':{'exchange':'BINANCE','settlement':'USDT'},'amount':'151','fee':'1'}}
 expected=dict(unsigned,signature=base64.b64encode(key.sign(E.canonical(unsigned).encode())).decode())
 actual=E.EvidenceAuthority('v4-known-fixture',key)._attest('EXECUTION',body)
 assert actual==expected and E.canonical(actual)==E.canonical(expected) and E.verify(actual,'EXECUTION',unsigned['body'])

@pytest.mark.parametrize('fault',['downgrade','other_issuer','other_scope','missing_audit','currency','receipt_reuse','fee_sign','fake_source'])
def test_execution_receipt_boundary(krw,fault):
 k=krw;f=execute(k);r=copy.deepcopy(f.execution_receipt);assert r and E.verify(r,'EXECUTION')
 if fault=='downgrade':r['version']=4;r.pop('profile')
 elif fault=='other_issuer':r['key_id']='other-key'
 elif fault=='other_scope':r['body']['scope']['subaccount']='pocket:'+U('cross')
 elif fault=='missing_audit':r['body']['fee_evidence'].pop('audit')
 elif fault=='currency':r['body']['fee_evidence']['orders'][0]['currency']='USDT'
 elif fault=='receipt_reuse':r['body']['fill']['order_id']=U('reuse')
 elif fault=='fee_sign':r['body']['fee_evidence']['orders'][0]['paid_fee']='-50000'
 else:r['body']['fee_evidence']['audit']['source_digest']='0'*64
 # Adversarial signed malformed/downgraded body, NOT a positive financial fixture.
 r['signature']=base64.b64encode(k.key.sign(E.canonical({a:b for a,b in r.items() if a!='signature'}).encode())).decode()
 assert not E.verify(r,'EXECUTION')

def test_pinned_registry_required_after_restart(krw):
 k=krw;f=execute(k);assert E.execution_valid(f,f.scope_data)
 E.configure_provider(None,{k.boot.authority.key_id:k.public})
 assert not E.execution_valid(f,f.scope_data)
 assert configure_execution_bootstrap(k.boot) and E.execution_valid(f,f.scope_data)


def test_numeric_quantity_format_is_not_false_rejection(krw):
 # Native volume formatting is economically identical. The independent archive
 # still binds the exact new snapshot; independent cash totals are unchanged.
 k=krw;o=k.state['orders'][k.buy];o['volume']='1.00000000'
 expected=copy.deepcopy(k.expected);expected[0]['native_digest']=k.client.read_order(k.client,k.buy,k.scope.symbol).snapshot_id
 k.archive.record=replace(k.archive.record,orders_digest=E.digest(expected))
 assert execute(k).execution_receipt is not None

@pytest.mark.parametrize('fault',[None,[],1,'receipt',{'version':5,'kind':'EXECUTION','body':None}])
def test_malformed_receipt_no_raise(fault):
 assert E.verify(fault,'EXECUTION') is False


def test_raw_context_cannot_replace_registered_execution_capability(krw):
 k=krw;f=execute(k);proof=copy.deepcopy(f.execution_receipt['body']['fee_evidence']);f.execution_receipt=None
 context=replace(k.context,execution_reader=None,fee_evidence=proof)
 FillVerifier()._certify_execution(f,context,k.client,request('sell'))
 assert f.execution_receipt is None


def test_krw_signed_history_successor_revokes_and_corrects_preserving_sent(krw):
 import time
 k=krw;p=positive(k);s=store(k)
 with FrozenWireObserver(s) as obs:H.dp.flush_outbox(k.st)
 assert len(obs.finish())==1
 with s._connect_read() as c:
  before=c.execute("select id,wire,wire_digest,status,message_id from vpnl_deliveries where status='SENT'").fetchall()
  facts=[json.loads(x[0]) for x in c.execute('select fact from vpnl_history_orders order by sequence')]
 oldgid=p.financial_receipt['body']['generation_id']
 # Genuine new Mock source revision: REST order AND independent archive change.
 k.state['orders'][k.buy]=raw_order('v5-buy','bid',['101000000'],T)
 expected=copy.deepcopy(k.expected);d=k.client.read_order(k.client,k.buy,k.scope.symbol).order
 expected[0].update(price='101000000',paid_fee='50500.0000',native_digest=E.digest(d))
 points=list(k.archive.record.checkpoints);points[1]=replace(points[1],krw='98949500',reference='successor-c1');points[2]=replace(points[2],krw='208894500',reference='successor-c2')
 k.archive.record=replace(k.archive.record,orders_digest=E.digest(expected),checkpoints=tuple(points),archive_reference='trusted-successor-cash-archive')
 facts[0]['average']='101000000';facts[0]['trades'][0].update(price='101000000',fee='50500');k.state.update(history_facts=facts,revision=2)
 wire=[];now=time.time()
 # Advance a controlled clock so real scheduling selects due jobs; no DB due edits.
 for step in range(1,5):
  with patch('time.time',return_value=now+step*60):
   H.dp.verification_tick(k.st,k.boot.client_for)
   with FrozenWireObserver(s) as obs:H.dp.flush_outbox(k.st)
   wire.extend(obs.finish())
 assert not generation_valid(s,oldgid)
 with s._connect_read() as c:
  for row in before:assert c.execute('select id,wire,wire_digest,status,message_id from vpnl_deliveries where id=?',(row[0],)).fetchone()==row
  heads=[NotificationPayload(**json.loads(x[0])) for x in c.execute('select v.payload from vpnl_roots r join vpnl_revisions v on v.root_id=r.id and v.revision=r.head')]
 assert len(heads)==1 and E.payload_valid(heads[0]) and D(heads[0].net_pnl.split()[0])==D('8894500')
 assert len(wire)==2 and all(x['ack']['status']=='SENT' for x in wire)
 assert heads[0].financial_receipt['body']['generation_id']!=oldgid
 (Path(__file__).parent/'krw_successor_wire.json').write_text(json.dumps(wire,ensure_ascii=False,indent=2),encoding='utf-8')
