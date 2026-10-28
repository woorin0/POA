import copy,json,hashlib,sqlite3,threading
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
from decimal import Decimal
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import r6_worker_harness as H
from exchange.verified_pnl import evidence as E
from exchange.verified_pnl.bootstrap import *
from exchange.verified_pnl.verifier import FillVerifier
from exchange.verified_pnl.models import VerificationStatus,FillStatus
from exchange.verified_pnl.ledger import identity

@pytest.fixture(autouse=True)
def reset():
 configure_execution_bootstrap(None);yield;configure_execution_bootstrap(None)

@pytest.fixture
def rig(tmp_path):
 st=H.settings(tmp_path/'bootstrap.sqlite');client=Mock()
 client.market.return_value=dict(id='BTCUSDT',symbol='BTC/USDT',spot=True,contractSize=1,quote='USDT')
 info=H.t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1)
 info.is_futures=False;info.is_coinm=False;info.is_sell=False
 key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
 authority=E.EvidenceAuthority('fixture-only',key)
 scope=SpotScope('BINANCE','pinned-account','pinned-subaccount',identity('BINANCE',getattr(st,'BINANCE_KEY',None) or 'unconfigured'),'BTC/USDT','BTCUSDT','USDT','externally-bound-scope','physical')
 order=H.native('order-1',100,1);order.update(symbol='BTC/USDT',side='buy')
 state={'order':order,'identity':AuthenticatedAccount('BINANCE',scope.account,scope.subaccount,frozenset({'read'})),'calls':[],'snapshot':'stable-read-window','rejection':None}
 def account(c):
  assert c is client
  if 'identity_error' in state:raise state['identity_error']
  return state['identity']
 def read(c,oid,symbol):
  assert c is client;state['calls'].append(('order',oid,symbol))
  if 'order_error' in state:raise state['order_error']
  data=copy.deepcopy(state['order']);return OrderSnapshot(data,state.get('account',scope.account),state.get('subaccount',scope.subaccount),state['snapshot'],state['rejection'])
 def page(c,oid,symbol,cursor,snapshot):
  assert c is client;state['calls'].append(('page',oid,symbol,cursor))
  if 'page_error' in state:raise state['page_error']
  if state.get('raw_page'):return dict(trades=state['order']['trades'],trades_complete=True,source='EXCHANGE_REST_QUERY')
  if 'pages' in state:trades,nxt=state['pages'][cursor]
  else:trades,nxt=state['order']['trades'],None
  return TradePage(copy.deepcopy(trades),state.get('page_account',scope.account),scope.subaccount,state.get('page_order',oid),state.get('page_symbol',symbol),state.get('page_snapshot',snapshot),state.get('page_cursor',cursor),nxt)
 adapter=SpotReadAdapter(client,'externally-reviewed-venue-fixture-v1',account,read,page,'ORDER_LIFETIME_QUOTE_V1',{'E_FINAL':'INSUFFICIENT_BALANCE'})
 boot=SpotExecutionBootstrap(scope,adapter,authority,{'fixture-only':public},{'fixture-only':hashlib.sha256(public).hexdigest()})
 return NS(st=st,client=client,info=info,key=key,public=public,authority=authority,scope=scope,adapter=adapter,boot=boot,state=state)

def fill(r):
 ctx=E.context_for('BINANCE',r.scope.account,r.info,r.client)
 return FillVerifier().verify_order_result(NS(client=r.client),'BINANCE','BTC/USDT',{'id':'order-1','verified':True,'trades_complete':True},r.info,ctx)

def active(r):assert configure_execution_bootstrap(r.boot)

def test_unsigned_rest_is_not_verified():
 info=H.t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1)
 order=H.native('buy',100,1);client=Mock();client.fetch_order.return_value=order
 result=FillVerifier().verify_order_result(NS(client=client),'BINANCE','BTC/USDT',order,info)
 assert result.verification_status==VerificationStatus.UNVERIFIED and result.execution_receipt is None

def test_bootstrap_port_exists():
 assert execution_readiness()['execution']=='PROVIDER_NOT_CONFIGURED'
 assert execution_readiness()['history']=='HISTORY_NOT_READY'

@pytest.mark.parametrize('fault',['no_keys','unknown_key','wrong_pin','revoked_key','wrong_signer'])
def test_key_fail_closed(rig,fault):
 if fault=='no_keys':rig.boot.public_keys={};rig.boot.pin_fingerprints={}
 if fault=='unknown_key':rig.boot.public_keys={'other':rig.public};rig.boot.pin_fingerprints={'other':hashlib.sha256(rig.public).hexdigest()}
 if fault=='wrong_pin':rig.boot.pin_fingerprints={'fixture-only':'0'*64}
 if fault=='revoked_key':rig.boot.revoked_keys=frozenset({'fixture-only'})
 if fault=='wrong_signer':rig.boot.authority=E.EvidenceAuthority('fixture-only',Ed25519PrivateKey.generate())
 assert not configure_execution_bootstrap(rig.boot)
 assert execution_readiness()['execution'] in {'KEY_UNAVAILABLE','READ_CLIENT_UNVERIFIED'}
 assert E.context_for('BINANCE',rig.scope.account,rig.info,rig.client) is None

@pytest.mark.parametrize('fault',['account','subaccount','exchange','permissions','raw_identity','timeout'])
def test_read_client_identity_fail_closed(rig,fault):
 from dataclasses import replace
 if fault in {'account','subaccount','exchange'}:rig.state['identity']=replace(rig.state['identity'],**{fault:'other'})
 elif fault=='permissions':rig.state['identity']=replace(rig.state['identity'],permissions=frozenset({'read','trade'}))
 elif fault=='raw_identity':rig.state['identity']={'account':rig.scope.account,'verified':True}
 else:rig.state['identity_error']=TimeoutError('FAKE_API_KEY_CANARY')
 assert not configure_execution_bootstrap(rig.boot)
 assert execution_readiness()['execution']=='READ_CLIENT_UNVERIFIED'

@pytest.mark.parametrize('fault',['exchange','account','symbol','futures','client'])
def test_context_scope_reuse_rejected(rig,fault):
 active(rig);exchange='BINANCE';account=rig.scope.account;info=copy.copy(rig.info);client=rig.client
 if fault=='exchange':exchange='BYBIT'
 elif fault=='account':account='different-account'
 elif fault=='symbol':info=NS(is_futures=False,is_coinm=False,unified_symbol='ETH/USDT')
 elif fault=='futures':info=NS(is_futures=True,unified_symbol='BTC/USDT')
 else:client=Mock()
 assert E.context_for(exchange,account,info,client) is None


def test_valid_execution_but_no_finance_or_history(rig):
 active(rig);result=fill(rig)
 assert E.execution_valid(result,result.scope_data) and result.verification_status==VerificationStatus.VERIFIED
 assert result.filled_quantity==1 and result.average_fill_price==100
 assert result.accounting_receipt is None and result.financial_receipt is None
 ctx=E.context_for('BINANCE',rig.scope.account,rig.info,rig.client);assert ctx.history_receipt is None
 assert execution_readiness()==dict(execution='EXECUTION_READY_TESTED',history='HISTORY_NOT_READY',live_account_connected=False)
 rig.client.fetch_order.assert_not_called();rig.client.create_order.assert_not_called()

@pytest.mark.parametrize('status',['open','canceled','expired'])
def test_partial_fill_quantity_preserved(rig,status):
 active(rig);o=rig.state['order'];o.update(status=status,filled='0.25',remaining='0.75',fee={'cost':'0.25','currency':'USDT'})
 o['trades'][0].update(amount='0.25',fee={'cost':'0.25','currency':'USDT'})
 result=fill(rig);assert E.execution_valid(result,result.scope_data)
 assert result.status==FillStatus.PARTIALLY_FILLED and result.filled_quantity==Decimal('0.25')
 assert result.exchange_status==status

@pytest.mark.parametrize('fault',['qty','vwap','order','symbol','page_account','page_order','page_symbol','page_snapshot','page_cursor','raw_page','duplicate','fee_currency','fee_total','missing_fee','missing_timestamp','timeout','missing_page'])
def test_bad_evidence_is_not_signed(rig,fault):
 active(rig);o=rig.state['order']
 if fault=='qty':o['trades'][0]['amount']='0.5'
 elif fault=='vwap':o['average']='101'
 elif fault=='order':o['trades'][0]['order']='another'
 elif fault=='symbol':o['trades'][0]['symbol']='ETH/USDT'
 elif fault.startswith('page_'):rig.state[fault]='foreign'
 elif fault=='raw_page':rig.state['raw_page']=True
 elif fault=='duplicate':o['trades'].append(copy.deepcopy(o['trades'][0]))
 elif fault=='fee_currency':o['trades'][0]['fee']['currency']='BNB'
 elif fault=='fee_total':o['fee']['cost']='2'
 elif fault=='missing_fee':o['trades'][0].pop('fee')
 elif fault=='missing_timestamp':o['trades'][0].pop('timestamp')
 elif fault=='timeout':rig.state['order_error']=TimeoutError('FAKE_API_KEY_CANARY')
 else:rig.state['pages']={None:(o['trades'],'missing')}
 result=fill(rig)
 assert result.execution_receipt is None and result.verification_status==VerificationStatus.UNVERIFIED
 assert execution_readiness()['execution']=='EXECUTION_EVIDENCE_INCOMPLETE'


def test_real_final_rejection_distinct_from_zero_fill(rig):
 active(rig);o=rig.state['order'];o.update(status='rejected',filled='0',remaining='1',average=None,trades=[],fee={'cost':'0','currency':'USDT'},info={'code':'E_FINAL'})
 assert not E.outcome_rejected(fill(rig))
 rig.state['rejection']='E_FINAL';result=fill(rig)
 assert E.outcome_rejected(result) and result.execution_receipt is None
 assert result.verification_status==VerificationStatus.UNVERIFIED
 rig.state['rejection']=None;o['status']='canceled'
 assert not E.outcome_rejected(fill(rig))


def test_timeout_then_authenticated_requery(rig):
 active(rig);rig.state['order_error']=TimeoutError('FAKE_BEARER_CANARY')
 assert fill(rig).execution_receipt is None
 del rig.state['order_error'];result=fill(rig)
 assert E.execution_valid(result,result.scope_data)


def test_two_pages_are_reconciled(rig):
 active(rig);t=rig.state['order']['trades'][0];one=copy.deepcopy(t);two=copy.deepcopy(t)
 one.update(id='one',amount='0.4',fee={'cost':'0.4','currency':'USDT'});two.update(id='two',amount='0.6',fee={'cost':'0.6','currency':'USDT'})
 rig.state['pages']={None:([one],'next'), 'next':([two],None)}
 result=fill(rig);assert E.execution_valid(result,result.scope_data) and len(result.trades)==2
 assert [x[-1] for x in rig.state['calls'] if x[0]=='page']==[None,'next']


def test_restart_public_key_and_revocation(rig):
 active(rig);result=fill(rig);receipt=copy.deepcopy(result.execution_receipt)
 configure_execution_bootstrap(None);assert not E.verify(receipt,'EXECUTION')
 # Restart verification uses only externally pinned public key; no private key is loaded.
 E.configure_provider(None,{'fixture-only':rig.public});assert E.verify(receipt,'EXECUTION')
 E.configure_provider(None,{});assert not E.verify(receipt,'EXECUTION')


def test_actual_worker_wiring_execution_only_and_same_fact_post_zero(rig):
 posts=[];real_tick=H.dp.verification_tick;iterations=[0]
 class OneRun:
  def __init__(self,target,**kwargs):self.target=target
  def start(self):self.target()
 class Stop:
  def is_set(self):return iterations[0]>0
  def wait(self,*args):iterations[0]+=1
  def set(self):iterations[0]=1
 def tick(settings,factory):
  H.dp.dispatch_verified_alert('BINANCE',{'id':'order-1'},rig.info,settings)
  real_tick(settings,factory)
 with patch.object(H.dp,'threading',NS(Event=Stop,Thread=OneRun)),patch.object(H.dp,'ReadClients',side_effect=AssertionError('legacy client path forbidden')),patch.object(H.dp,'verification_tick',side_effect=tick),patch('requests.post',side_effect=lambda *a,**kw:posts.append(kw['data']) or Mock(status_code=200,json=Mock(return_value={'id':'1'}))):
  assert H.dp.start_worker(rig.st,execution_bootstrap=rig.boot) is not None
 assert len(posts)==1 and isinstance(posts[0],bytes)
 with sqlite3.connect(rig.st.VERIFIED_NOTIFICATIONS_DB_PATH) as c:
  payload=json.loads(c.execute('select payload from vpnl_revisions').fetchone()[0]);assert E.verify(payload['execution_receipt'],'EXECUTION')
  assert payload['net_pnl'] is None and payload['roi_pct'] is None and payload['cost_basis'] is None
  assert payload['financial_receipt'] is None
  c.execute("update vpnl_jobs set due=0 where state='PENDING'")
 with patch('requests.post') as post:real_tick(rig.st,rig.boot.client_for);H.dp.flush_outbox(rig.st)
 assert post.call_count==0
 assert execution_readiness()['history']=='HISTORY_NOT_READY'


def test_no_provider_worker_does_not_create_client(rig):
 with patch.object(H.dp,'ReadClients') as clients,patch('requests.post') as post:
  assert H.dp.start_worker(rig.st) is None
 assert clients.call_count==0 and post.call_count==0


def test_canary_no_log_wire_or_database_leaks(rig,capsys):
 from loguru import logger
 active(rig);seen=[];sink=logger.add(lambda m:seen.append(str(m)))
 try:
  rig.state['order_error']=RuntimeError('FAKE_API_KEY_CANARY FAKE_BEARER_CANARY\n\x1b[31m https://FAKE_WEBHOOK_CANARY')
  assert fill(rig).execution_receipt is None
  del rig.state['order_error'];rig.state['order']['info']={'HTTP':'FAKE_BEARER_CANARY','signature':'FAKE_SIGNATURE_CANARY'}
  result=fill(rig);assert E.execution_valid(result,result.scope_data)
  H.dp.dispatch_verified_alert('BINANCE',{'id':'order-1'},rig.info,rig.st);H.dp.verification_tick(rig.st,rig.boot.client_for)
  with patch('requests.post',return_value=Mock(status_code=200,json=Mock(return_value={'id':'1'}))) as post:H.dp.flush_outbox(rig.st)
  with sqlite3.connect(rig.st.VERIFIED_NOTIFICATIONS_DB_PATH) as c:database='\n'.join(c.iterdump())
  capture=capsys.readouterr();text=''.join(seen)+capture.out+capture.err+database+str(post.call_args.kwargs)
  for canary in ('FAKE_API_KEY_CANARY','FAKE_BEARER_CANARY','FAKE_WEBHOOK_CANARY','FAKE_SIGNATURE_CANARY'):assert canary not in text
 finally:logger.remove(sink)

@pytest.mark.parametrize('field',['order_id','filled_quantity','average_fill_price','fees'])
def test_receipt_mutation_does_not_validate(rig,field):
 active(rig);result=fill(rig);assert E.execution_valid(result,result.scope_data)
 changed=result.copy(deep=True)
 setattr(changed,field,{'order_id':'other-order','filled_quantity':Decimal('0.5'),'average_fill_price':Decimal('999'),'fees':{'USDT':Decimal('999')}}[field])
 assert not E.execution_valid(changed,changed.scope_data)

@pytest.mark.parametrize('fee',['-0.000000004','0.00000000000000000000000000000001'])
def test_rebate_and_small_identical_fee(rig,fee):
 active(rig);rig.state['order']['fee']['cost']=fee;rig.state['order']['trades'][0]['fee']['cost']=fee
 result=fill(rig);assert E.execution_valid(result,result.scope_data)
 assert result.fees=={'USDT':Decimal(fee)}


def test_tiny_nonzero_fee_mismatch_is_rejected(rig):
 active(rig);rig.state['order']['fee']['cost']='1.0000000000000000000000000000000000000001'
 assert fill(rig).execution_receipt is None


def test_forged_flags_do_not_replace_real_trade_pages(rig):
 active(rig);rig.state['order'].update(verified=True,trades_complete=True,source='EXCHANGE_REST_QUERY')
 rig.state['pages']={None:([],None)}
 assert fill(rig).execution_receipt is None


def test_changing_snapshot_is_rejected(rig):
 from dataclasses import replace
 original=rig.adapter.read_page
 def changing(*args):
  result=original(*args);rig.state['order']['average']='101';return result
 rig.boot.adapter=replace(rig.adapter,read_page=changing)
 active(rig);assert fill(rig).execution_receipt is None


def test_page_cycle_and_bound(rig):
 active(rig);rig.state['pages']={None:([],'same'),'same':([],'same')}
 assert fill(rig).execution_receipt is None
 assert len([x for x in rig.state['calls'] if x[0]=='page'])==2

@pytest.mark.parametrize('field,value',[('id','OTHER'),('symbol','ETH/USDT'),('spot',False)])
def test_market_metadata_cannot_be_reused(rig,field,value):
 active(rig);rig.client.market.return_value[field]=value
 assert fill(rig).execution_receipt is None


def test_unspecified_market_or_fee_scope_not_ready(rig):
 from dataclasses import replace
 active(rig);info=copy.copy(rig.info);info.is_futures=None
 assert E.context_for('BINANCE',rig.scope.account,info,rig.client) is None
 rig.boot.adapter=replace(rig.adapter,fee_contract='UNSPECIFIED')
 assert not configure_execution_bootstrap(rig.boot)
 assert execution_readiness()['execution']=='READ_CLIENT_UNVERIFIED'


def test_hsm_failure_does_not_expose_exception(rig,capsys):
 active(rig)
 with patch.object(rig.authority,'_attest',side_effect=RuntimeError('FAKE_API_KEY_CANARY')):
  assert fill(rig).execution_receipt is None
 assert execution_readiness()['execution']=='KEY_UNAVAILABLE'
 captured=capsys.readouterr();assert 'FAKE_API_KEY_CANARY' not in captured.out+captured.err


def test_revocation_between_context_and_query(rig):
 active(rig);ctx=E.context_for('BINANCE',rig.scope.account,rig.info,rig.client)
 E.configure_provider(None,{})
 result=FillVerifier().verify_order_result(NS(client=rig.client),'BINANCE','BTC/USDT',{'id':'order-1'},rig.info,ctx)
 assert result.execution_receipt is None and result.verification_status==VerificationStatus.UNVERIFIED
 assert execution_readiness()['execution']=='KEY_UNAVAILABLE'

def test_verbose_authenticated_client_is_not_accepted(rig):
 rig.client.verbose=True
 assert not configure_execution_bootstrap(rig.boot)
 assert execution_readiness()['execution']=='READ_CLIENT_UNVERIFIED'

@pytest.mark.parametrize('quote',[None,'BNB'])
def test_missing_or_wrong_settlement_metadata_is_not_verified(rig,quote):
 active(rig);rig.client.market.return_value['quote']=quote
 assert fill(rig).execution_receipt is None


def test_account_scope_missing_is_not_ready(rig):
 from dataclasses import replace
 rig.boot.scope=replace(rig.scope,position_cycle='')
 assert not configure_execution_bootstrap(rig.boot)
 assert execution_readiness()['execution']=='ACCOUNT_SCOPE_UNVERIFIED'

def test_signing_failure_after_authenticated_query_is_fail_closed(rig,capsys):
 active(rig);ctx=E.context_for('BINANCE',rig.scope.account,rig.info,rig.client)
 with patch.object(rig.authority,'_attest',side_effect=TimeoutError('FAKE_SIGNER_TOKEN_CANARY')):
  result=FillVerifier().verify_order_result(NS(client=rig.client),'BINANCE','BTC/USDT',{'id':'order-1'},rig.info,ctx)
 assert result.execution_receipt is None and result.outcome_receipt is None
 assert result.verification_status==VerificationStatus.UNVERIFIED
 assert execution_readiness()['execution']=='KEY_UNAVAILABLE'
 capture=capsys.readouterr();assert 'FAKE_SIGNER_TOKEN_CANARY' not in capture.out+capture.err

@pytest.mark.parametrize('fault',['both_buy_sell','neither_buy_sell','side_flag_conflict'])
def test_conflicting_direction_flags_cannot_issue_receipt(rig,fault):
 active(rig)
 if fault=='both_buy_sell':rig.info.is_sell=True
 elif fault=='neither_buy_sell':rig.info.is_buy=False
 else:rig.info.side='sell'
 assert E.context_for('BINANCE',rig.scope.account,rig.info,rig.client) is None


def test_configured_bootstrap_does_not_bypass_bundle2_cutover(rig):
 from exchange.verified_pnl.delivery_store import NotificationOutboxStore
 with sqlite3.connect(rig.st.VERIFIED_NOTIFICATIONS_DB_PATH) as c:
  c.execute('create table verified_jobs(id text primary key,data text,state text,due real,created real)')
  c.execute("insert into verified_jobs values('held','{}','HELD',0,0)")
 store=NotificationOutboxStore(rig.st.VERIFIED_NOTIFICATIONS_DB_PATH,migration_mode=True)
 class OneRun:
  def __init__(self,target,**kw):self.target=target
  def start(self):self.target()
 with patch.object(H.dp,'threading',NS(Event=threading.Event,Thread=OneRun)),patch.object(H.dp,'verification_tick') as tick,patch('requests.post') as post:
  H.dp.start_worker(rig.st,execution_bootstrap=rig.boot)
 assert tick.call_count==0 and post.call_count==0
 assert not store.cutover_ready() and rig.state['calls']==[]
 with sqlite3.connect(rig.st.VERIFIED_NOTIFICATIONS_DB_PATH) as c:assert c.execute('select state from verified_jobs').fetchone()[0]=='HELD'


def test_foreign_job_account_does_not_query_bound_account_orders(rig):
 active(rig);H.dp.dispatch_verified_alert('BINANCE',{'id':'order-1'},rig.info,rig.st)
 with sqlite3.connect(rig.st.VERIFIED_NOTIFICATIONS_DB_PATH) as c:
  key,encoded=c.execute('select id,data from vpnl_jobs').fetchone();data=json.loads(encoded);data['account']='foreign-account'
  c.execute('update vpnl_jobs set data=? where id=?',(json.dumps(data),key))
 H.dp.verification_tick(rig.st,rig.boot.client_for)
 assert rig.state['calls']==[]
 rig.client.fetch_order.assert_not_called();rig.client.fetch_my_trades.assert_not_called()
 with sqlite3.connect(rig.st.VERIFIED_NOTIFICATIONS_DB_PATH) as c:
  for encoded, in c.execute('select payload from vpnl_revisions'):
   payload=json.loads(encoded);assert payload['execution_receipt'] is None and payload['financial_receipt'] is None
