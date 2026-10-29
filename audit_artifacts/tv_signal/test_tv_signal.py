import copy,json,sys,threading
from pathlib import Path
from decimal import Decimal as D
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'estimated_implementation'))
import test_estimated_contracts as T
from exchange.verified_pnl import tradingview_signal as S,intake as I
H=T.H;env=T.env

def emit(e,side,price,qty='1',strategy='tv-A',base='BTC',venue='UPBIT',sid=None,oid=None,extra=None,tick=True):
 e.seq+=1;sig=dict(kind='strategy_order_fill',price=str(price),quantity=str(qty),action=side,order_id='entry' if side=='buy' else 'exit',signal_id=sid or 'signal-'+str(e.seq),strategy_id=strategy)
 if extra:sig.update(extra)
 info=H.t.OrderRequest(exchange=venue,base=base,quote='KRW' if venue=='UPBIT' else 'USDT',side=side,amount=0.002,price=999,strategy_id=strategy,order_name=strategy,tv_signal=sig)
 r=e.log(venue,{'id':oid or 'ack-'+str(e.seq)},info)
 if tick:H.dp.verification_tick(e.st,lambda *a: (_ for _ in ()).throw(AssertionError('NO_SIGNAL_GET')))
 return r,info

def estimate(e):return T.plans(e)[-1]['estimated_pnl']

@pytest.mark.parametrize('venue',T.VENUES)
@pytest.mark.parametrize('base',['BTC','XRP'])
def test_main_entrypoint_signal_no_fill(env,venue,base):
 e=env
 with patch.object(H.dp.FillVerifier,'verify_order_result',side_effect=AssertionError('NO_FINANCIAL')):
  a,info=emit(e,'buy','150','2',base=base,venue=venue);assert a['accepted']
  assert info.price==999 and info.amount==0.002
  emit(e,'sell','160','1',base=base,venue=venue)
 r=estimate(e);assert D(r['amount'])==10 and D(r['entry_price'])==150 and D(r['quantity'])==1
 assert abs(D(r['roi'])-D('6.6666666667'))<D('0.000000001')
 p=T.plans(e)[-1];assert p['execution_receipt'] is None and p['financial_receipt'] is None and p['fill_status']=='UNVERIFIED'
 wire=T.send(e);assert len(wire)==1
 desc=json.dumps(wire,ensure_ascii=False);assert 'TradingView' in desc and '미반영' in desc

@pytest.mark.parametrize('sale,profit',[('160','20'),('140','-20'),('150','0')])
def test_weighted_partial_restart(env,sale,profit):
 e=env;emit(e,'buy','100','1');emit(e,'buy','200','1');emit(e,'sell',sale,'1')
 assert D(estimate(e)['amount'])==D(profit)/2
 store=T.store(e);assert len(T.send(e))==1
 # Recreate both stores; second sell uses remaining average 150, not entire principal.
 I.IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH);T.store(e)
 emit(e,'sell',sale,'1');assert D(estimate(e)['amount'])==D(profit)/2
 assert len(T.send(e))==1

@pytest.mark.parametrize('kind',['strategy','account','symbol','no_buy'])
def test_never_borrow_long_hold_or_other_scope(env,kind):
 e=env;emit(e,'buy','1','100',strategy='other' if kind=='strategy' else 'tv-A',base='ETH' if kind=='symbol' else 'BTC')
 if kind=='account':e.st.UPBIT_KEY='different-mock-key'
 if kind=='no_buy':base='XRP'
 else:base='BTC'
 emit(e,'sell','100000000','1',base=base)
 r=estimate(e)
 if kind!='no_buy':assert r['status']=='UNAVAILABLE' and r['amount'] is None
 else:assert r['amount'] is None


def test_explicit_entry_fallback_only(env):
 e=env;emit(e,'sell','110000000',extra={'entry_price':'100000000','entry_source':'strategy.position_avg_price'})
 r=estimate(e);assert D(r['amount'])==10000000 and '명시적 전략' in r['source']

@pytest.mark.parametrize('change',[{'price':'NaN'},{'quantity':'0'},{'price':True},{'price':'Infinity'},{'price':'{{strategy.order.price}}'},{'entry_price':'100'},{'strategy_id':'bearer canary'},{'unknown':'x'}])
def test_schema_reject(env,change):
 with pytest.raises((ValueError,TypeError)):emit(env,'sell','160',extra=change)
 assert not T.plans(env)


def test_action_strategy_mismatch_not_accepted(env):
 e=env
 with pytest.raises(ValueError):emit(e,'buy','150',extra={'action':'sell'})
 assert not T.plans(e)


def test_dedup_same_signal_new_ack(env):
 e=env;emit(e,'buy','150',sid='buy-1');emit(e,'sell','160',sid='sell-1');assert len(T.send(e))==1
 a,_=emit(e,'sell','160',sid='sell-1');assert a['accepted'];assert T.send(e)==[]
 bad,_=emit(e,'sell','170',sid='sell-1');assert not bad['accepted'];assert T.send(e)==[]
 with T.store(e)._connect_read() as c:assert c.execute('SELECT count(*) FROM vpnl_notice_inputs').fetchone()[0]==2


def test_signal_and_execution_cost_never_mix(env):
 e=env;T.emit(e,'UPBIT','buy','1','0',qty='100');emit(e,'sell','160');assert estimate(e)['amount'] is None

@pytest.mark.parametrize('state',['SENT','UNKNOWN','HELD','POSTING'])
def test_existing_delivery_never_rewritten(env,state):
 e=env;emit(e,'buy','150');emit(e,'sell','160',sid='sell-immutable')
 st=T.store(e)
 if state=='SENT':assert len(T.send(e))==1
 else:
  with st._connect() as c:c.execute('UPDATE vpnl_deliveries SET status=?',(state,))
 with st._connect_read() as c:before=c.execute('SELECT * FROM vpnl_deliveries').fetchall();revs=c.execute('SELECT * FROM vpnl_revisions').fetchall()
 emit(e,'sell','160',sid='sell-immutable');assert T.send(e)==[]
 with st._connect_read() as c:assert c.execute('SELECT * FROM vpnl_deliveries').fetchall()==before and c.execute('SELECT * FROM vpnl_revisions').fetchall()==revs


def test_alert_explicit(env):
 e=env;emit(e,'buy','150',extra={'kind':'alert'});emit(e,'sell','160',extra={'kind':'alert'});assert D(estimate(e)['amount'])==10


def test_oversell_only_matched(env):
 e=env;emit(e,'buy','150');emit(e,'sell','160','2');r=estimate(e);assert D(r['quantity'])==1 and D(r['amount'])==10 and D(r['unmatched_quantity'])==1


def test_forged_payload_rejected(env):
 e=env;emit(e,'buy','150');emit(e,'sell','160');p=H.dp.NotificationPayload(**T.plans(e)[-1]);p.estimated_pnl['amount']='999999'
 assert not T.store(e).enqueue(p,max_attempts=1)


def test_concurrent_producer_dedup(env):
 e=env;_,info=emit(e,'buy','150',sid='parallel-buy',tick=False)
 results=[]
 def call():results.append(e.log('UPBIT',{'id':'parallel-ack'},info))
 threads=[threading.Thread(target=call) for _ in range(10)]
 for t in threads:t.start()
 for t in threads:t.join()
 assert len(results)==10 and all(r['accepted'] for r in results)
 assert len(set(r['intake_id'] for r in results))==1
 H.dp.verification_tick(e.st,lambda *a: (_ for _ in ()).throw(AssertionError('NO_GET')))
 emit(e,'sell','160');assert D(estimate(e)['amount'])==10 and len(T.send(e))==1


def test_restart_separate_process_projection(env,tmp_path):
 import subprocess,os
 e=env;emit(e,'buy','100000000');r,_=emit(e,'sell','110000000')
 before=estimate(e);assert len(T.send(e))==1
 code="""import sys,json
import test_estimated_contracts as T
from exchange.verified_pnl.estimated_pnl import calculate
st=T.H.settings(sys.argv[1]);st.UPBIT_VERIFIED_ALERTS=True
store=T.H.ob.NotificationOutboxStore(sys.argv[1])
with store._connect_read() as c:print(json.dumps(calculate(c,sys.argv[2]),sort_keys=True))
T.H.dp.verification_tick(st,lambda *a: (_ for _ in ()).throw(AssertionError('NO_GET')))
with T.FrozenWireObserver(store) as observer:T.H.dp.flush_outbox(st)
assert observer.finish()==[]
"""
 child=subprocess.run([sys.executable,'-c',code,str(e.st.VERIFIED_NOTIFICATIONS_DB_PATH),r['intake_id']],env=os.environ.copy(),capture_output=True,text=True,encoding='utf-8',timeout=30)
 assert child.returncode==0,child.stderr
 assert json.loads(child.stdout.splitlines()[0])==before


def test_invalidated_finance_does_not_supply_signal_basis(env):
 e=env;emit(e,'sell','160');p=T.plans(e)[-1];assert p['estimated_pnl']['amount'] is None and p['financial_receipt'] is None


def test_original_intake_request_retained(env):
 e=env;r,_=emit(e,'buy','150');sc=I.IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH)
 with sc.connect() as c:before=c.execute('SELECT request,request_digest,submission_ack FROM notification_intake WHERE id=?',(r['intake_id'],)).fetchone()
 emit(e,'sell','160')
 with sc.connect() as c:assert c.execute('SELECT request,request_digest,submission_ack FROM notification_intake WHERE id=?',(r['intake_id'],)).fetchone()==before
 assert json.loads(before[0])['observation']['confirmed'] is False


@pytest.mark.parametrize('venue',T.VENUES)
def test_actual_order_route_mock_to_notification(env,venue):
 import ast,asyncio
 from types import SimpleNamespace as NS
 e=env;root=Path.cwd();tree=ast.parse((root/'main.py').read_text(encoding='utf-8-sig'))
 nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in ('order','schedule_order_log')]
 for n in nodes:n.decorator_list=[]
 calls=[]
 class Bot:
  def init_info(self,info):self.order_info=info
  def market_buy(self,info):calls.append(('buy',info.amount,info.price));return {'id':'route-buy'}
  def market_sell(self,info):calls.append(('sell',info.amount,info.price));return {'id':'route-sell'}
 bot=Bot();namespace=dict(settings=e.st,MarketOrder=object,BackgroundTasks=object,get_bot=lambda *a:bot,log=e.log,is_verified_exchange=lambda v:True,get_error=lambda err:(_ for _ in ()).throw(err))
 exec(compile(ast.Module(body=nodes,type_ignores=[]),str(root/'main.py'),'exec'),namespace)
 for side,price in [('buy','150'),('sell','160')]:
  sig=dict(kind='strategy_order_fill',price=price,quantity='2',action=side,order_id=side,signal_id='route-'+side,strategy_id='tv-route')
  info=H.t.OrderRequest(exchange=venue,base='XRP',quote='KRW' if venue=='UPBIT' else 'USDT',side=side,amount=0.002,price=777,strategy_id='tv-route',tv_signal=sig)
  assert asyncio.run(namespace['order'](info,NS(add_task=lambda *a:pytest.fail('legacy_fallback'))))=={'result':'success'}
  H.dp.verification_tick(e.st,lambda *a:pytest.fail('GET_FORBIDDEN'))
 assert calls==[('buy',0.002,777),('sell',0.002,777)]
 assert D(estimate(e)['amount'])==20 and len(T.send(e))==1


def test_producer_capability_cannot_be_flag(env):
 e=env;r,_=emit(e,'buy','150');sc=I.IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH)
 with sc.connect() as c:request=json.loads(c.execute('SELECT request FROM notification_intake WHERE id=?',(r['intake_id'],)).fetchone()[0])
 with pytest.raises(ValueError,match='unregistered'):sc.record(request,observation=request['observation'],producer=True)


def test_payload_financial_labels_forgery_rejected(env):
 e=env;emit(e,'buy','150');emit(e,'sell','160');p=H.dp.NotificationPayload(**T.plans(e)[-1]);p.fill_verification_status='VERIFIED'
 assert not T.store(e).enqueue(p,max_attempts=1)


def test_no_order_or_financial_execution_changes():
 import subprocess,ast
 root=Path.cwd()
 unchanged=['main.py','exchange/safe_retry.py','exchange/verified_pnl/evidence.py','exchange/verified_pnl/verifier.py','exchange/verified_pnl/bootstrap.py','exchange/verified_pnl/history.py','exchange/verified_pnl/ledger.py','exchange/verified_pnl/pnl_calculator.py','exchange/verified_pnl/reconciliation.py','exchange/verified_pnl/outbox.py','exchange/verified_pnl/discord_sender.py']
 for name in unchanged:
  p=root/name
  if not p.exists():continue
  old=subprocess.check_output(['git','show','ae0691e01c926c7247328bd6e6dba1ba3fb1ddca:'+name])
  assert p.read_bytes()==old,name
 name='exchange/verified_pnl/delivery_store.py';before=ast.parse(subprocess.check_output(['git','show','ae0691e01c926c7247328bd6e6dba1ba3fb1ddca:'+name]).decode('utf-8-sig'));after=ast.parse((root/name).read_text(encoding='utf-8-sig'))
 for fn in ('_wire','_attempt','begin_post','ack','claim_next'):
  get=lambda tree:next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==fn)
  assert ast.dump(get(before))==ast.dump(get(after)),fn


def test_failed_commit_not_accepted(env):
 from contextlib import contextmanager
 import sqlite3
 e=env;original=I.IntakeStore.connect
 @contextmanager
 def fail(self):
  with original(self) as c:
   yield c
   raise sqlite3.OperationalError('synthetic_commit_failure')
 with patch.object(I.IntakeStore,'connect',fail):r,_=emit(e,'buy','150',tick=False)
 assert r['accepted'] is False
 assert not T.plans(e)


def test_canary_not_persisted_or_logged(env,capsys):
 e=env
 with pytest.raises(ValueError) as err:emit(e,'buy','150',extra={'signal_id':'Bearer SYNTHETIC_CANARY'})
 assert 'SYNTHETIC_CANARY' not in str(err.value)
 captured=capsys.readouterr();assert 'SYNTHETIC_CANARY' not in captured.out+captured.err
 assert not T.plans(e)


def test_unknown_buy_then_new_strategy_not_account_inventory(env):
 e=env;emit(e,'buy','150',strategy='old-strategy');emit(e,'sell','160',strategy='new-strategy')
 assert estimate(e)['status']=='UNAVAILABLE'


def test_repeated_entry_order_id_is_not_event_id(env):
 e=env;emit(e,'buy','100',sid='distinct-1');emit(e,'buy','200',sid='distinct-2');emit(e,'sell','160')
 assert D(estimate(e)['entry_price'])==150


def test_same_exchange_order_cannot_create_two_signal_buys(env):
 e=env;emit(e,'buy','100',sid='first',oid='same-ack');emit(e,'buy','200',sid='second',oid='same-ack');emit(e,'sell','160')
 assert estimate(e)['status']=='UNAVAILABLE'


def test_webhook_password_and_execution_fields_stay_separate(env):
 from types import SimpleNamespace as NS
 signal=dict(kind='alert',price='150',quantity='2',action='buy',order_id='entry',signal_id='auth-1',strategy_id='auth-strategy')
 fields=dict(exchange='UPBIT',base='XRP',quote='KRW',side='buy',amount=0.002,price=777,strategy_id='auth-strategy',tv_signal=signal)
 with patch.dict(H.t.MarketOrder.__validators__['password'][0].func.__globals__,Settings=lambda:NS(PASSWORD='synthetic-test-password')):
  with pytest.raises(ValueError):H.t.MarketOrder(password='wrong',**fields)
  parsed=H.t.MarketOrder(password='synthetic-test-password',**fields)
 assert parsed.price==777 and parsed.amount==0.002 and json.loads(parsed.tv_signal)['price']=='150'
