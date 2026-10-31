"""Independent Decimal oracle + real FastAPI route, notification engine and HTTP ACK.
Only exchange execution and physical HTTP transport are replaced by fakes.
"""
import ast,asyncio,copy,hashlib,json,sys,time,uuid,threading
from decimal import Decimal as D,localcontext,ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest,httpx,requests
from fastapi import FastAPI,BackgroundTasks
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'estimated_implementation'))
import test_estimated_contracts as T
H=T.H;env=T.env
PAIRS=[('UPBIT','BTC'),('UPBIT','XRP'),('UPBIT','LINK'),('BINANCE','BTC'),('BINANCE','XRP'),('BYBIT','BTC'),('BYBIT','XRP'),('OKX','BTC'),('OKX','XRP')]

class Oracle:
 def __init__(self):self.qty=D(0);self.cost=D(0)
 def event(self,side,price,qty,entry=None):
  with localcontext() as c:
   c.prec=100;p=D(price);q=D(qty)
   if side=='buy':self.qty+=q;self.cost+=p*q;return dict(average=self.cost/self.qty,holding=self.qty)
   match=min(q,self.qty)
   if self.qty:average=self.cost/self.qty;allocated=self.cost*match/self.qty
   elif entry is not None:average=D(entry);match=q;allocated=average*q
   else:return dict(amount=None,matched=D(0),unmatched=q)
   amount=p*match-allocated
   result=dict(average=average,amount=amount,roi=amount/allocated*100 if allocated else None,matched=match,unmatched=q-match)
   self.cost-=allocated if self.qty else D(0);self.qty-=match if self.qty else D(0)
   if not self.qty:self.cost=D(0)
   return result

def near(actual,expected):
 assert actual is not None and expected is not None
 with localcontext() as c:
  c.prec=1100;assert abs(D(actual)-expected)<=max(D('1e-60'),abs(expected)*D('1e-70')),(actual,str(expected))

class HTTPProbe:
 def __init__(self,store,status=200,error=None):self.store=store;self.status=status;self.error=error;self.records=[]
 def post(self,url,**kw):
  assert 'json' not in kw and isinstance(kw['data'],bytes) and kw['params']=={'wait':'true'} and kw['allow_redirects'] is False
  raw=kw['data'];digest=hashlib.sha256(raw).hexdigest()
  with self.store._connect_read() as c:
   rows=c.execute("SELECT d.id,d.revision,d.wire,a.revision,a.wire_digest,a.epoch,a.token_hash FROM vpnl_deliveries d JOIN vpnl_delivery_attempts a ON a.delivery_id=d.id WHERE d.status='POSTING' AND a.phase='POSTING' AND d.wire_digest=?",(digest,)).fetchall()
  assert len(rows)==1;did,rev,stored,ar,ad,epoch,token=rows[0]
  assert raw==stored and rev==ar and ad==digest and len(token)==64
  self.records.append(dict(delivery=did,revision=rev,digest=digest,epoch=epoch,token_hash=token,wire=json.loads(raw)))
  if self.error:raise self.error
  response=requests.Response();response.status_code=self.status;response.headers={'Content-Type':'application/json','Retry-After':'2'}
  response._content=b'' if self.status==204 else json.dumps({'id':str(987650000+len(self.records))}).encode()
  return response
 def __enter__(self):self.guard=patch('requests.post',side_effect=self.post);self.guard.start();return self
 def __exit__(self,*a):self.guard.stop()

@pytest.fixture
def route(env):
 e=env;root=Path.cwd();tree=ast.parse((root/'main.py').read_text(encoding='utf-8-sig'))
 nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in ('order','log','is_verified_exchange','get_error','schedule_order_log')]
 e.app=FastAPI();e.executions=[];e.post_count=0;e.latest_oid=None;e.result_extra={}
 class FakeOrderAdapter:
  def init_info(self,info):self.order_info=info
  def execute(self,info,method):
   e.executions.append(dict(method=method,exchange=info.exchange,symbol=info.unified_symbol,price=info.price,amount=info.amount,tv=json.loads(info.tv_signal) if info.tv_signal else None))
   return dict(id=e.latest_oid,**e.result_extra)
  def market_buy(self,i):return self.execute(i,'market_buy')
  def market_sell(self,i):return self.execute(i,'market_sell')
  def market_entry(self,i):return self.execute(i,'market_entry')
  def market_close(self,i):return self.execute(i,'market_close')
 bot=FakeOrderAdapter()
 namespace=dict(app=e.app,settings=e.st,MarketOrder=H.t.MarketOrder,BackgroundTasks=BackgroundTasks,get_bot=lambda *a:bot)
 exec(compile(ast.Module(body=nodes,type_ignores=[]),str(root/'main.py'),'exec'),namespace)
 e.namespace=namespace;e.password='isolated-audit-password'
 with patch.dict(H.t.MarketOrder.__validators__['password'][0].func.__globals__,Settings=lambda:NS(PASSWORD=e.password)):yield e

def submit(e,venue,asset,side,price,qty,*,strategy='Audit-Strategy-A',sid=None,oid=None,fallback=None,source_time=None):
 e.seq+=1;sid=sid or 'event-'+str(e.seq);e.latest_oid=oid or str(uuid.uuid5(uuid.NAMESPACE_URL,venue+str(e.seq)))
 sig=dict(kind='strategy_order_fill',price=price,quantity=qty,action=side,order_id='LL' if side=='buy' else 'Close_HL',signal_id=sid,strategy_id=strategy)
 if fallback:sig.update(entry_price=fallback,entry_source='strategy.position_avg_price')
 body=dict(password=e.password,exchange=venue,base=asset,quote='KRW' if venue=='UPBIT' else 'USDT',side=side,amount=0.002,price=777,order_name=sig['order_id'],strategy_id=strategy,signal_id=sid,tv_signal=sig)
 if source_time is not None:body['signal_time']=source_time
 async def call():
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app),base_url='http://isolated.test') as client:return await client.post('/order',json=body)
 response=asyncio.run(call());assert response.status_code==200 and response.json()=={'result':'success'},response.text
 H.dp.verification_tick(e.st,lambda *a:pytest.fail('EXCHANGE_GET_FORBIDDEN'))
 return body,e.latest_oid

def plans(e):return T.plans(e)
def raw_sell(e):return plans(e)[-1]['estimated_pnl']
def send(e,status=200,error=None):
 probe=HTTPProbe(T.store(e),status,error)
 with probe:H.dp.flush_outbox(e.st)
 return probe.records

def line_fields(record):
 embed=record['wire']['embeds'][0]
 return embed,{line.split(': ',1)[0]:line.split(': ',1)[1] for line in embed.get('description','').splitlines() if ': ' in line}

@pytest.mark.parametrize('venue,asset',PAIRS)
def test_full_fastapi_fields_through_wire(route,venue,asset):
 e=route;quote='KRW' if venue=='UPBIT' else 'USDT';b,boid=submit(e,venue,asset,'buy','100','2');s,soid=submit(e,venue,asset,'sell','110','1')
 actual=plans(e);assert len(actual)==2 and [p['order_id'] for p in actual]==[boid,soid]
 for p in actual:
  assert (p['exchange'],p['symbol'],p['strategy_id'])==(venue,asset+'/'+quote,b['strategy_id'])
  assert p['financial_receipt'] is None and p['execution_receipt'] is None and p['fill_status']=='UNVERIFIED'
  assert p['signal_id'] is not None
 oracle=Oracle();oracle.event('buy','100','2');expected=oracle.event('sell','110','1');near(raw_sell(e)['amount'],expected['amount']);near(raw_sell(e)['roi'],expected['roi'])
 records=send(e);assert len(records)==2
 for i,record in enumerate(records):
  embed,fields=line_fields(record);assert asset+'/'+quote in embed['title'] and fields['전략']==b['strategy_id'] and 'KST' in fields['시각']
  assert embed['color']==(0x64748B if i==0 else 0x8B5CF6)
  assert '체결 완료' not in embed['description'] and 'VERIFIED' not in embed['description']
  assert record['wire']['allowed_mentions']=={'parse':[]}
 with T.store(e)._connect_read() as c:
  rows=c.execute('SELECT snapshot FROM vpnl_notice_inputs ORDER BY sequence').fetchall()
  for row,body,oid in zip(rows,(b,s),(boid,soid)):
   snap=json.loads(row[0]);assert snap['order_id']==oid and snap['signal']['signal_id']==body['signal_id']
   assert D(snap['signal']['price'])==D(body['tv_signal']['price']) and D(snap['signal']['quantity'])==D(body['tv_signal']['quantity'])
   assert snap['strategy']==body['strategy_id'] and snap['symbol']==asset+'/'+quote and snap['confirmed'] is False
 assert [x['amount'] for x in e.executions]==[0.002,0.002] and [x['price'] for x in e.executions]==[777,777]

CASES=[('A',[('buy','100000000','1'),('sell','110000000','1')]),('B',[('buy','100000000','1'),('sell','110000000','0.005')]),('C',[('buy','100000000','0.01'),('buy','110000000','0.02'),('sell','120000000','0.015')]),('D',[('buy','1000','100'),('buy','1200','50'),('sell','1300','80')]),('E',[('buy','100','2'),('buy','110','3'),('sell','120','4')]),('F',[('buy','100','10'),('sell','90','10')])]
@pytest.mark.parametrize('name,events',CASES)
def test_independent_decimal_oracle(route,name,events):
 e=route;o=Oracle();venue='BINANCE' if name=='E' else 'UPBIT';asset='XRP' if name=='D' else 'BTC'
 for side,p,q in events:
  submit(e,venue,asset,side,p,q);want=o.event(side,p,q)
  if side=='sell':near(raw_sell(e)['amount'],want['amount']);near(raw_sell(e)['roi'],want['roi']);near(raw_sell(e)['entry_price'],want['average']);near(raw_sell(e)['quantity'],want['matched'])
  else:near(plans(e)[-1]['estimated_pnl']['virtual_average'],want['average'])
 assert len(send(e))==len(events)

@pytest.mark.parametrize('qty',['0.000000001','1000000000000000000000000'])
def test_accepted_quantity_never_zero_or_drops_notification(route,qty):
 e=route;submit(e,'UPBIT','BTC','buy','100',qty);submit(e,'UPBIT','BTC','sell','110',qty)
 records=send(e);assert len(records)==2
 for record,label in zip(records,('매수수량','매도수량')):
  _,fields=line_fields(record);assert D(fields[label].split()[0].replace(',',''))==D(qty)


def test_source_time_and_signal_identifier_preserved(route):
 e=route;body,oid=submit(e,'UPBIT','BTC','buy','100','1',source_time=1790812800)
 from exchange.verified_pnl.intake import IntakeStore
 with IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:r=json.loads(c.execute('SELECT request FROM notification_intake').fetchone()[0])
 assert r['info']['signal_id']==body['signal_id']
 assert D(str(r['info']['signal_time']))==D('1790812800')
 records=send(e);assert '2026-10-01 09:00:00 KST' in json.dumps(records,ensure_ascii=False)


def test_new_cycle_explicit_strategy_entry_fallback(route):
 e=route;submit(e,'UPBIT','BTC','buy','100','1');submit(e,'UPBIT','BTC','sell','110','1');submit(e,'UPBIT','BTC','sell','160','1',fallback='150')
 assert raw_sell(e)['status']=='ESTIMATED';near(raw_sell(e)['amount'],D(10))



def test_signal_time_field_not_dropped(route):
 e=route;submit(e,'UPBIT','BTC','buy','100','1',source_time=1790812800)
 from exchange.verified_pnl.intake import IntakeStore
 with IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:r=json.loads(c.execute('SELECT request FROM notification_intake').fetchone()[0])
 assert r['info'].get('signal_time')==1790812800


def test_full_accepted_strategy_identity_in_wire(route):
 e=route;name='Strategy-'+('A'*80)+'-unique-tail';submit(e,'UPBIT','BTC','buy','100','1',strategy=name)
 _,fields=line_fields(send(e)[0]);assert fields['전략']==name


def test_signal_id_conflict_rejected_before_fake_execution(route):
 e=route;e.latest_oid=str(uuid.uuid4())
 body=dict(password=e.password,exchange='UPBIT',base='BTC',quote='KRW',side='buy',amount=0.002,strategy_id='A',signal_id='execution-event',tv_signal=dict(kind='alert',action='buy',price='100',quantity='1',order_id='LL',strategy_id='A',signal_id='different-notice-event'))
 async def call():
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app),base_url='http://isolated.test') as client:return await client.post('/order',json=body)
 result=asyncio.run(call());assert result.status_code==422 and e.executions==[]


def test_large_valid_roi_does_not_drop_notification(route):
 e=route;submit(e,'BINANCE','XRP','buy','0.00000001','1');submit(e,'BINANCE','XRP','sell','1000000000000000000000000','1')
 assert len(plans(e))==2 and len(send(e))==2


def test_explicit_reverse_source_time_never_invents_later_profit(route):
 e=route;submit(e,'UPBIT','BTC','sell','110','1',source_time=1790812802);submit(e,'UPBIT','BTC','buy','100','1',source_time=1790812801);submit(e,'UPBIT','BTC','sell','120','1',source_time=1790812803)
 assert raw_sell(e)['status']=='UNAVAILABLE' and raw_sell(e)['amount'] is None


@pytest.mark.parametrize('status,expected',[(200,'SENT'),(204,'SENT'),(400,'FAILED'),(401,'FAILED'),(404,'FAILED'),(429,'PENDING'),(500,'UNKNOWN'),(502,'UNKNOWN'),(503,'UNKNOWN')])
def test_real_sender_http_response_and_ack(route,status,expected):
 e=route;submit(e,'BINANCE','BTC','sell','110','1')
 before=plans(e);records=send(e,status);assert len(records)==1
 with T.store(e)._connect_read() as c:
  state,wire,digest,updated,due=c.execute('SELECT status,wire,wire_digest,updated,due FROM vpnl_deliveries').fetchone()
  assert state==expected and hashlib.sha256(wire).hexdigest()==digest
  attempts=c.execute('SELECT phase FROM vpnl_delivery_attempts').fetchall();assert len(attempts)==1
  if status==429:assert due>=updated+1.9
 assert plans(e)==before
 assert send(e)==[]
 if status==429:
  # Advance wall-clock in the test only; never overwrite production due/state.
  with patch('time.time',return_value=due+0.1):assert len(send(e))==1
  assert send(e)==[]

@pytest.mark.parametrize('error',[requests.Timeout('isolated-timeout'),requests.ConnectionError('isolated-connection')])
def test_ambiguous_transport_no_repost_after_reopen(route,error):
 e=route;submit(e,'UPBIT','XRP','sell','1300','1');assert len(send(e,error=error))==1
 from exchange.verified_pnl.intake import replay
 replay(e.st,store=T.store(e));assert send(e)==[]
 with T.store(e)._connect_read() as c:assert c.execute('SELECT status FROM vpnl_deliveries').fetchone()[0]=='UNKNOWN'

@pytest.mark.parametrize('state',['SENT','UNKNOWN','HELD','POSTING'])
def test_reprocess_never_rewrites_protected_delivery(route,state):
 e=route;_,oid=submit(e,'BYBIT','BTC','buy','100','1',sid='same')
 if state=='SENT':send(e)
 elif state=='POSTING':
  store=T.store(e);claim=store.claim_next();assert store.begin_post(claim)
 else:
  # Explicit fixture state, no production release/approval bypass.
  with T.store(e)._connect() as c:c.execute('UPDATE vpnl_deliveries SET status=?',(state,))
 with T.store(e)._connect_read() as c:before=c.execute('SELECT id,status,wire,wire_digest,message_id FROM vpnl_deliveries').fetchall()
 submit(e,'BYBIT','BTC','buy','100','1',sid='same',oid=oid);assert send(e)==[]
 with T.store(e)._connect_read() as c:assert c.execute('SELECT id,status,wire,wire_digest,message_id FROM vpnl_deliveries').fetchall()==before


def test_two_flush_workers_and_ten_same_producers(route):
 from concurrent.futures import ThreadPoolExecutor
 e=route;body,oid=submit(e,'BINANCE','BTC','buy','100','2',sid='stable-buy');order=H.t.MarketOrder(**body)
 with ThreadPoolExecutor(max_workers=10) as pool:results=list(pool.map(lambda _:e.namespace['log']('BINANCE',{'id':oid},order.copy(deep=True)),range(10)))
 H.dp.verification_tick(e.st,lambda *a:pytest.fail('EXCHANGE_GET_FORBIDDEN'))
 probe=HTTPProbe(T.store(e))
 with probe:
  with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:H.dp.flush_outbox(e.st),range(2)))
 assert len(probe.records)==1 and send(e)==[]
 with T.store(e)._connect_read() as c:assert c.execute('SELECT count(*) FROM vpnl_jobs').fetchone()[0]==1


def test_repeated_partial_sells_reentry_and_ten_buys(route):
 e=route;o=Oracle();events=[('buy',str(100+i),'0.000001') for i in range(12)]
 events += [('sell','120','0.000003'),('sell','90','0.000002'),('sell','130','0.000007'),('buy','200','2'),('sell','210','1'),('buy','220','1'),('sell','230','2')]
 for side,p,q in events:
  submit(e,'BINANCE','XRP',side,p,q);want=o.event(side,p,q);got=raw_sell(e)
  if side=='sell':near(got['amount'],want['amount']);near(got['roi'],want['roi'])
  else:near(got['virtual_average'],want['average']);near(got['virtual_quantity'],want['holding'])
 assert len(send(e))==len(events) and send(e)==[]


def test_oversell_reports_only_matched_quantity(route):
 e=route;submit(e,'UPBIT','XRP','buy','1000','100');submit(e,'UPBIT','XRP','sell','1100','150');r=raw_sell(e)
 assert (D(r['quantity']),D(r['unmatched_quantity']),D(r['amount']))==(D(100),D(50),D(10000))
 _,f=line_fields(send(e)[-1]);assert f['매도수량']=='100 XRP' and f['원가 미확인 수량']=='50 XRP'


def test_203_distinct_strategy_keys_no_holdings_mix(route):
 e=route
 from exchange.verified_pnl import estimated_pnl as N,tradingview_signal as V
 # Real admitted immutable requests: batch producers, then bounded worker ticks.
 for i in range(203):
  inf=T.info('BINANCE','buy',strategy='Strategy-'+str(i));inf.tv_signal=json.dumps(dict(kind='alert',price=str(100+i),quantity='1',action='buy',order_id='LL',signal_id='entry-'+str(i),strategy_id=inf.strategy_id))
  assert e.namespace['log']('BINANCE',{'id':'entry-order-'+str(i),'balance':'999999999','average_buy_price':'1'},inf)['accepted']
 with T.store(e)._connect_read() as c:
  assert c.execute('SELECT count(DISTINCT scope) FROM vpnl_notice_inputs').fetchone()[0]==203
 # Worker intentionally claims bounded batches; drain the queued buys before
 # inspecting the later sell revision, rather than reading an unrelated buy.
 for _ in range(11):H.dp.verification_tick(e.st,lambda *a:pytest.fail('EXCHANGE_GET_FORBIDDEN'))
 for i in (0,101,202):
  submit(e,'BINANCE','BTC','sell','400','1',strategy='Strategy-'+str(i));r=raw_sell(e)
  near(r['entry_price'],D(100+i));near(r['amount'],D(300-i))
 # Separate API-account identifier must never use another account's acquisitions.
 e.st.BINANCE_KEY='mock-account-B';submit(e,'BINANCE','BTC','sell','400','1',strategy='Strategy-0');assert raw_sell(e)['status']=='UNAVAILABLE'


@pytest.mark.parametrize('field',['price','quantity'])
@pytest.mark.parametrize('value',['0','-1','NaN','Infinity','-Infinity'])
def test_bad_signal_numbers_rejected_before_execution(route,field,value):
 e=route;e.latest_oid='not-executed';sig=dict(kind='alert',price='100',quantity='1',action='buy',order_id='LL',signal_id='bad',strategy_id='A');sig[field]=value
 body=dict(password=e.password,exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1,strategy_id='A',tv_signal=sig)
 async def call():
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app),base_url='http://isolated.test') as client:return await client.post('/order',json=body)
 response=asyncio.run(call());assert response.status_code==422 and e.executions==[]


def test_full_strategy_unicode_mobile_bounds_and_canaries(route):
 e=route;name='전략_'+'가'*100
 e.result_extra={'apiKey':'SYNTHETIC_CREDENTIAL_DO_NOT_PERSIST','authorization':'Bearer SYNTHETIC_JWT_DO_NOT_PERSIST'}
 submit(e,'UPBIT','LINK','buy','100000000','0.000001',strategy=name);submit(e,'UPBIT','LINK','sell','90000000','0.000001',strategy=name)
 records=send(e)
 for r in records:
  em,fields=line_fields(r);assert fields['전략']==name and len(em['title'])<=256 and len(em['description'])<=4096
  assert 'SYNTHETIC_' not in json.dumps(r)
 from exchange.verified_pnl.intake import IntakeStore
 with IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:
  for row in c.execute('SELECT request FROM notification_intake'):assert 'SYNTHETIC_' not in row[0]


def test_old_v2_pending_frozen_wire_and_result_unchanged(route):
 e=route
 # Replay the exact previous renderer and previous notification snapshot policy.
 from exchange.verified_pnl import tradingview_signal as V,notification_origin as O
 original=V.capture
 def old_capture(*a,**kw):
  s=original(*a,**kw);s['version']=2;return V.validate(s)
 def old_stamp(p,settings):
  p.verification_source=(p.verification_source or '').split(O.PREFIX)[0]+O.PREFIX+json.dumps({'layout':2,'server':None,'commit':None},sort_keys=True,separators=(',',':'));return p
 with patch.object(V,'capture',side_effect=old_capture),patch.object(O,'stamp',side_effect=old_stamp):
  submit(e,'UPBIT','BTC','buy','100','1');submit(e,'UPBIT','BTC','sell','110','1')
 with T.store(e)._connect_read() as c:before=c.execute('SELECT wire,wire_digest FROM vpnl_deliveries ORDER BY rowid').fetchall()
 assert len(send(e))==2
 with T.store(e)._connect_read() as c:assert c.execute('SELECT wire,wire_digest FROM vpnl_deliveries ORDER BY rowid').fetchall()==before


@pytest.mark.parametrize('venue',['BINANCE','BYBIT','OKX'])
@pytest.mark.parametrize('quote',['USDT.P','USD.P'])
def test_futures_and_coinm_not_processed_as_spot_signal(route,venue,quote):
 e=route;e.latest_oid='future-order'
 body=dict(password=e.password,exchange=venue,base='BTC',quote=quote,side='buy',amount=0.002,price=777)
 async def call():
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app),base_url='http://isolated.test') as client:return await client.post('/order',json=body)
 response=asyncio.run(call());assert response.status_code==200 and response.json()=={'result':'success'} and len(e.executions)==1
 assert ':' in e.executions[0]['symbol']
 from exchange.verified_pnl.intake import IntakeStore
 with IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:
  row=json.loads(c.execute('SELECT request FROM notification_intake').fetchone()[0]);assert row['info']['is_futures'] and row.get('observation') is None
 assert plans(e)==[]


def test_new_notifications_only_ast_and_wire_guards_unchanged():
 import subprocess
 base='8e4f8acb12956ca0df6df089700e700f9948a4ef'
 for p in ('main.py','exchange/safe_retry.py','exchange/verified_pnl/outbox.py','exchange/verified_pnl/verifier.py','exchange/verified_pnl/evidence.py','exchange/verified_pnl/bootstrap.py','exchange/verified_pnl/history.py','exchange/verified_pnl/ledger.py','exchange/verified_pnl/pnl_calculator.py','exchange/verified_pnl/reconciliation.py'):
  if p=='main.py':
   from notification_source_contract import assert_notification_main_source
   assert_notification_main_source()
  else:assert Path(p).read_bytes()==subprocess.check_output(['git','show',base+':'+p]),p
 p='exchange/model/schemas.py';before=ast.parse(subprocess.check_output(['git','show',base+':'+p]).decode('utf-8-sig'));after=ast.parse(Path(p).read_text(encoding='utf-8-sig'))
 for tree in (before,after):
  cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='OrderRequest');cls.body=[n for n in cls.body if not(isinstance(n,ast.FunctionDef) and n.name=='validate_tv_binding')]
 assert ast.dump(before)==ast.dump(after),'non-notification schema/validator changed'
 p='exchange/verified_pnl/delivery_store.py';before=ast.parse(subprocess.check_output(['git','show',base+':'+p]).decode('utf-8-sig'));after=ast.parse(Path(p).read_text(encoding='utf-8-sig'))
 from notification_source_contract import assert_reviewed_notification_source
 assert_reviewed_notification_source(p)
 for name in ('_wire','_attempt','begin_post','ack','claim_next'):
  node=lambda t:next(n for n in ast.walk(t) if isinstance(n,ast.FunctionDef) and n.name==name)
  assert ast.dump(node(before))==ast.dump(node(after)),name


def test_tv_restart_in_separate_process(route,tmp_path):
 import subprocess
 e=route;submit(e,'UPBIT','LINK','buy','1000','2');submit(e,'UPBIT','LINK','sell','1100','1')
 output=tmp_path/'mock_ack.json'
 code="""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'audit_artifacts/precision_audit'))
from test_precision_pipeline import H,HTTPProbe,T
from exchange.verified_pnl.intake import replay
st=H.settings(sys.argv[1]);st.UPBIT_VERIFIED_ALERTS=True;st.UPBIT_NET_PNL_ENABLED=True
for v in T.VENUES:setattr(st,v+'_KEY','mock-account-A')
replay(st);H.dp.verification_tick(st,lambda *a:(_ for _ in ()).throw(AssertionError('NO_REAL_GET')))
probe=HTTPProbe(H.ob.NotificationOutboxStore(st.VERIFIED_NOTIFICATIONS_DB_PATH))
with probe:H.dp.flush_outbox(st)
assert len(probe.records)==2
Path(sys.argv[2]).write_bytes(json.dumps(probe.records).encode())
"""
 child=subprocess.run([sys.executable,'-c',code,e.st.VERIFIED_NOTIFICATIONS_DB_PATH,str(output)],capture_output=True,timeout=35)
 assert child.returncode==0,child.stderr.decode('utf-8','replace')[-700:]
 records=json.loads(output.read_bytes());assert len(records)==2 and '100원' in records[-1]['wire']['embeds'][0]['description']
 assert send(e)==[]


def test_same_order_changed_signal_never_double_counts_or_posts(route):
 e=route;_,oid=submit(e,'UPBIT','XRP','buy','1000','2',sid='first');assert len(send(e))==1
 submit(e,'UPBIT','XRP','buy','1100','3',sid='changed',oid=oid);assert send(e)==[]
 with T.store(e)._connect_read() as c:assert c.execute('SELECT count(*) FROM vpnl_notice_reviews').fetchone()[0]>=1
 submit(e,'UPBIT','XRP','sell','1200','1');assert raw_sell(e)['status']=='UNAVAILABLE'


def test_nested_only_signal_id_preserved_and_bound(route):
 e=route;body,_=submit(e,'UPBIT','BTC','buy','100','1')
 body.pop('signal_id');body['tv_signal']['signal_id']='nested-only';e.latest_oid='nested-order'
 async def call():
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app),base_url='http://isolated.test') as client:return await client.post('/order',json=body)
 response=asyncio.run(call());assert response.status_code==200
 H.dp.verification_tick(e.st,lambda *a:None);assert plans(e)[-1]['signal_id']=='nested-only'
 assert len(send(e))==2



def test_previous_v2_intake_digest_and_retry_remain_valid(route):
 e=route
 from exchange.verified_pnl import tradingview_signal as V,intake as I,notification_origin as O
 old_capture=V.capture;old_normalize=I.normalized_request
 def previous_capture(*a,**kw):
  snap=old_capture(*a,**kw);snap['version']=2;return V.validate(snap)
 def previous_normalize(data):
  data=copy.deepcopy(data);data['info']['signal_id']=None;return old_normalize(data)
 def previous_stamp(p,settings):
  p.verification_source=(p.verification_source or '').split(O.PREFIX)[0]+O.PREFIX+json.dumps({'layout':2,'server':None,'commit':None},sort_keys=True,separators=(',',':'));return p
 with patch.object(V,'capture',side_effect=previous_capture),patch.object(I,'normalized_request',side_effect=previous_normalize),patch.object(O,'stamp',side_effect=previous_stamp):
  body,oid=submit(e,'UPBIT','BTC','buy','100','1',sid='v2-record')
 with I.IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:before=c.execute('SELECT id,request,request_digest FROM notification_intake').fetchall()
 submit(e,'UPBIT','BTC','buy','100','1',sid='v2-record',oid=oid)
 with I.IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:assert c.execute('SELECT id,request,request_digest FROM notification_intake').fetchall()==before
 assert len(send(e))==1 and send(e)==[]


def test_restore_snapshot_loss_of_notification_ack_blocks_delivery(route,tmp_path):
 import sqlite3
 from exchange.verified_pnl import intake as I
 e=route;store=T.store(e);old=tmp_path/'isolated_old_v4.sqlite'
 with sqlite3.connect(store.db_path) as a,sqlite3.connect(old) as b:a.backup(b)
 submit(e,'BINANCE','BTC','buy','100','1');submit(e,'BINANCE','BTC','sell','110','1');assert len(send(e))==2
 with sqlite3.connect(old) as a,sqlite3.connect(store.db_path) as b:a.backup(b)
 I._BOOT_CHECKED.discard(store.db_path);preflight=I.restore_preflight(T.store(e));assert preflight['blocked'] and send(e)==[]
 with I.IntakeStore(e.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:assert c.execute("SELECT count(*) FROM notification_intake WHERE status='REVIEW'").fetchone()[0]>0
