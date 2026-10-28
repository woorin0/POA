"""Upbit native REST observations; NO KRW verified financial positive fixture."""
import copy,datetime,hashlib,json,uuid,sqlite3
from decimal import Decimal as D
from dataclasses import replace
from types import SimpleNamespace as NS
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import r6_worker_harness as H
from exchange.verified_pnl import evidence as E
from exchange.verified_pnl.bootstrap import *
from exchange.verified_pnl.models import NotificationPayload,ExitReason,PnlResult,FillStatus
from exchange.verified_pnl.verifier import FillVerifier
from exchange.verified_pnl.pnl_calculator import PnlCalculator
from exchange.verified_pnl.ledger import identity
from exchange.verified_pnl.upbit_spot_read import *
A=Path(__file__).parent
U=lambda text:str(uuid.uuid5(uuid.NAMESPACE_URL,'upbit-test-only-'+text))
T=1790000000000

def at(ms,kst=True):
 tz=datetime.timezone(datetime.timedelta(hours=9)) if kst else datetime.timezone.utc
 return datetime.datetime.fromtimestamp(ms/1000,tz).isoformat()

def raw_order(name,side,prices,stamp=T):
 oid=U(name)
 trades=[dict(market='KRW-BTC',uuid=U(name+'-fill-'+str(i)),price=str(p),volume='1',funds=str(p),created_at=at(stamp+i,True),side=side,trend='up') for i,p in enumerate(prices)]
 total=sum(map(D,prices))
 return dict(uuid=oid,market='KRW-BTC',side=side,ord_type='limit',price=str(max(map(D,prices)) if side=='bid' else min(map(D,prices))),volume=str(len(prices)),remaining_volume='0',executed_volume=str(len(prices)),reserved_fee=str(total*D('0.0005')),remaining_fee='0',paid_fee=str(total*D('0.0005')),locked='0',trades_count=len(trades),state='done',created_at=at(stamp-10),trades=trades,prevented_volume='0',prevented_locked='0',identifier='safe-client-'+name)

class RawGET:
 def __init__(self):
  a=raw_order('buy','bid',['100000000','200000000']);b=raw_order('sell','ask',['160000000'],T+1000)
  self.orders={a['uuid']:a,b['uuid']:b};self.calls=[];self.error=None;self.duplicate=False;self.omit=False;self.summary_conflict=False
 def __call__(self,path,p):
  self.calls.append((path,copy.deepcopy(p)))
  if self.error:raise RuntimeError(self.error+' FAKE_API_KEY_CANARY Bearer FAKE_BEARER_CANARY FAKE_POCKET_TOKEN')
  if path=='/v1/accounts':return [dict(currency=c,balance=b,locked='0',unit_currency='KRW',avg_buy_price='150000000',avg_buy_price_modified=True) for c,b in [('KRW','1000000000'),('BTC','1')]]
  if path=='/v1/order':return copy.deepcopy(self.orders[p['uuid']])
  if path=='/v1/orders/closed':
   rows=[copy.deepcopy(r) for r in self.orders.values() if r['state'] in ('done','cancel') and int(p['start_time'])*1000<=utc_us(r['created_at'])<=int(p['end_time'])*1000]
   rows.sort(key=lambda r:utc_us(r['created_at']),reverse=True);rows=rows[:p['limit']]
   for r in rows:r.pop('trades')
   if self.duplicate and rows:rows.append(copy.deepcopy(rows[0]))
   if self.omit and int(p['end_time'])-int(p['start_time'])<5000:rows=[]
   if self.summary_conflict and rows:rows[0]['paid_fee']='999'
   return rows
  raise AssertionError('GET_ONLY_PATH')

@pytest.fixture(autouse=True)
def clean():
 configure_execution_bootstrap(None)
 yield
 configure_execution_bootstrap(None)

@pytest.fixture
def upbit(tmp_path):
 g=RawGET();pocket=U('pocket')
 scope=SpotScope('UPBIT','registered-host-account','pocket:'+pocket,identity('UPBIT','unconfigured'),'BTC/KRW','KRW-BTC','KRW','cycle-not-proven','physical')
 bound=NS(account=AuthenticatedAccount('UPBIT',scope.account,scope.subaccount,frozenset({'read'})),error=None)
 def registry(c):
  if bound.error:raise RuntimeError(bound.error)
  return bound.account
 client=UpbitSpotReads(scope,g,registry,pocket_uuid=pocket,as_of_ms=T+10000,page_size=2)
 st=H.settings(tmp_path/'upbit.sqlite');st.UPBIT_VERIFIED_ALERTS=True
 return NS(g=g,scope=scope,pocket=pocket,bound=bound,c=client,st=st)


def test_native_order_quantity_price_and_krw_fee_observations(upbit):
 o=upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW').order
 assert o['filled']=='2' and D(o['average'])==150000000 and o['fee']=={'currency':'KRW','cost':'150000.0000'}
 assert len(o['trades'])==2 and all('fee' not in t for t in o['trades'])
 assert o['observation_summary']['paid_fee']=='150000.0000' and 'verification_status' not in o
 assert upbit.c.read_accounts()['BTC']['balance']=='1'

@pytest.mark.parametrize('kind,side',[('limit','bid'),('price','bid'),('market','ask'),('best','bid'),('best','ask')])
def test_native_order_types_without_quote_unit_confusion(upbit,kind,side):
 oid=U('buy') if side=='bid' else U('sell');r=upbit.g.orders[oid];r['ord_type']=kind
 if kind in ('price','best') and side=='bid':r.update(price='300000000',volume=None)
 if kind in ('market','best') and side=='ask':r.pop('price')
 s=upbit.c.read_order(upbit.c,oid,'BTC/KRW').order
 if side=='bid' and kind in ('price','best'):assert s['price'] is None and s['amount'] is None and s['requested_quote']=='300000000' and D(s['average'])==150000000
 else:assert s['amount'] is not None


def test_market_buy_partial_cancel_preserves_reported_quantity(upbit):
 r=upbit.g.orders[U('buy')];r.update(ord_type='price',price='300000000',volume=None,state='cancel',executed_volume='1',remaining_volume='0',trades_count=1,paid_fee='50000')
 r['trades']=r['trades'][:1]
 s=upbit.c.read_order(upbit.c,r['uuid'],'BTC/KRW').order
 assert s['status']=='canceled' and s['filled']=='1' and s['amount'] is None and s['average']=='100000000'

@pytest.mark.parametrize('fault',['trade_count','quantity','funds','duplicate_trade','wrong_uuid','market','trade_market','side','wrong_order','time','negative_fee','missing_fee','nan','infinity','float','smp','trade_fee','bid_fee','kind_side'])
def test_mismatched_source_rejected(upbit,fault):
 r=upbit.g.orders[U('buy')];t=r['trades'][0]
 if fault=='trade_count':r['trades_count']=1
 elif fault=='quantity':r['executed_volume']='1'
 elif fault=='funds':t['funds']='999'
 elif fault=='duplicate_trade':r['trades'][1]['uuid']=t['uuid']
 elif fault=='wrong_uuid':r['uuid']=U('other')
 elif fault=='market':r['market']='USDT-BTC'
 elif fault=='trade_market':t['market']='KRW-ETH'
 elif fault=='side':t['side']='ask'
 elif fault=='wrong_order':t['order_uuid']=U('other')
 elif fault=='time':t['created_at']='2026-10-01T00:00:00'
 elif fault=='negative_fee':r['paid_fee']='-1'
 elif fault=='missing_fee':r.pop('paid_fee')
 elif fault=='nan':t['price']='NaN'
 elif fault=='infinity':t['volume']='Infinity'
 elif fault=='float':t['price']=100.0
 elif fault=='smp':r['prevented_volume']='1'
 elif fault in ('trade_fee','bid_fee'):t[fault]='1'
 else:r['ord_type']='market'
 with pytest.raises(UpbitReadIncomplete):upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW')


def test_zero_paid_fee_and_reserved_fee_not_double_counted(upbit):
 r=upbit.g.orders[U('buy')];r.update(paid_fee='0',reserved_fee='999999',remaining_fee='500')
 o=upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW').order
 assert o['fee']['cost']=='0' and o['paid_fee']=='0' and all('fee' not in t for t in o['trades'])


def test_uuid_and_identifier_are_separate(upbit):
 r=upbit.g.orders[U('buy')];r.pop('identifier')
 assert upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW').order['clientOrderId'] is None
 with pytest.raises(UpbitReadIncomplete):upbit.c.read_order(upbit.c,'client-made-id','BTC/KRW')


def test_same_trade_cannot_belong_to_two_orders(upbit):
 upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW')
 upbit.g.orders[U('sell')]['trades'][0]['uuid']=upbit.g.orders[U('buy')]['trades'][0]['uuid']
 with pytest.raises(UpbitReadIncomplete):upbit.c.read_order(upbit.c,U('sell'),'BTC/KRW')


def test_kst_utc_microseconds_exact():
 assert utc_us('2026-09-21T15:00:00.123456+09:00')==utc_us('2026-09-21T06:00:00.123456Z')
 assert utc_us(iso_utc(1790000000123456))==1790000000123456


def test_seven_day_split_window_no_invented_page_parameter(upbit):
 snaps=upbit.c.closed_orders(T-1000,T+10000)
 assert len(snaps)==2 and {s.order['id'] for s in snaps}=={U('buy'),U('sell')} and 1<upbit.c.last_scan_requests<=40
 assert all('page' not in p and 'cursor' not in p for path,p in upbit.g.calls)

@pytest.mark.parametrize('fault',['duplicate','omitted','summary'])
def test_closed_page_contradiction_no_success(upbit,fault):
 if fault=='duplicate':upbit.g.duplicate=True
 elif fault=='omitted':upbit.g.omit=True
 else:upbit.g.summary_conflict=True
 with pytest.raises(UpbitReadIncomplete):upbit.c.closed_orders(T-1000,T+10000)


def test_dense_same_time_not_declared_complete(upbit):
 # Both orders exceed/equal cap at an indivisible creation time.
 for r in upbit.g.orders.values():r['created_at']=at(T-10)
 with pytest.raises(UpbitReadIncomplete):upbit.c.closed_orders(T-10,T-10)


def test_over_seven_days_rejected(upbit):
 with pytest.raises(UpbitReadIncomplete):upbit.c.closed_orders(T-7*86400000-1,T)

@pytest.mark.parametrize('fault',['account','pocket','write','dict','error'])
def test_host_binding_fail_closed(upbit,fault):
 if fault=='account':upbit.bound.account=replace(upbit.bound.account,account='other')
 elif fault=='pocket':upbit.bound.account=replace(upbit.bound.account,subaccount='pocket:'+U('other'))
 elif fault=='write':upbit.bound.account=replace(upbit.bound.account,permissions=frozenset({'read','trade'}))
 elif fault=='dict':upbit.bound.account={'verified':True,'pocket':upbit.pocket}
 else:upbit.bound.error='FAKE_API_KEY_CANARY'
 with pytest.raises(UpbitReadIncomplete):upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW')
 assert not upbit.g.calls


def test_observation_port_cannot_bootstrap_financial_evidence(upbit):
 k=Ed25519PrivateKey.generate();pub=k.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
 boot=SpotExecutionBootstrap(upbit.scope,upbit.c.execution_port(),E.EvidenceAuthority('fixture',k),{'fixture':pub},{'fixture':hashlib.sha256(pub).hexdigest()})
 assert configure_execution_bootstrap(boot) is False
 assert E.context_for('UPBIT',upbit.scope.account,NS(is_buy=True,is_sell=False,side='buy',is_futures=False,is_coinm=False,unified_symbol='BTC/KRW'),upbit.c) is None
 with pytest.raises(UpbitReadIncomplete):upbit.c.history_port(boot)
 assert upbit.c.readiness()['financial'].startswith('BLOCKED')


def test_fee_flag_cannot_replace_missing_native_per_trade_evidence(upbit):
 k=Ed25519PrivateKey.generate();pub=k.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
 # Adversarial contract label, never used as a financial positive.
 port=replace(upbit.c.execution_port(),fee_contract='ORDER_LIFETIME_QUOTE_V1')
 boot=SpotExecutionBootstrap(upbit.scope,port,E.EvidenceAuthority('fixture',k),{'fixture':pub},{'fixture':hashlib.sha256(pub).hexdigest()})
 assert configure_execution_bootstrap(boot)
 info=H.t.OrderRequest(exchange='UPBIT',base='BTC',quote='KRW',side='sell',amount=1)
 info.is_buy=False;info.is_sell=True;info.is_futures=False;info.is_coinm=False
 ctx=E.context_for('UPBIT',upbit.scope.account,info,upbit.c)
 f=FillVerifier().verify_order_result(NS(client=upbit.c),'UPBIT','BTC/KRW',{'id':U('sell'),'verified':True,'trades_complete':True},info,ctx)
 assert f.execution_receipt is None and f.financial_receipt is None
 assert PnlCalculator().calculate_pnl(f,info).net_realized_pnl is None

@pytest.mark.parametrize('amount',['+8 KRW','+58 KRW','+999 KRW'])
def test_unsigned_krw_pnl_and_entry_price_unconfirmed(upbit,amount):
 p=NotificationPayload(event_id='fixture',exchange='UPBIT',symbol='BTC/KRW',side='매도',order_name='fixture',exit_reason=ExitReason.TP,pnl_result=PnlResult.PROFIT,fill_status=FillStatus.FILLED,event_timestamp=T/1000,order_id=U('sell'),filled_qty='1',entry_price='100000000',exit_price='160000000',net_pnl=amount,roi_pct='+999%',pnl_currency='KRW',cost_basis_currency='KRW',cost_basis='101',verification_status='VERIFIED',pnl_verification_status='VERIFIED',roi_verification_status='VERIFIED')
 assert not E.payload_valid(p) and E.payload_acquisition_price(p) is None
 text=json.dumps(H.de.build_mobile_verified_embed(p).to_dict(),ensure_ascii=False)
 assert '확정손익: 미확정' in text and '수익률: 미확정' in text
 assert ('매수가' not in text or '매수가: 미확정' in text) and '100,000,000' not in text and '100000000' not in text

@pytest.mark.parametrize('err',['timeout','401','429','500','503'])
def test_get_failure_safe(upbit,err,capsys):
 upbit.g.error=err
 with pytest.raises(UpbitReadIncomplete) as e:upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW')
 assert 'CANARY' not in str(e.value)+''.join(capsys.readouterr())


def test_no_public_trade_or_transfer_write_path(upbit):
 for p in ('/v1/trades/ticks','/v1/pockets/transfers','/v1/orders','/v1/withdraws/krw'):
  with pytest.raises(UpbitReadIncomplete):upbit.c._read(p,{})
 assert not upbit.g.calls


def test_ccxt_parser_market_buy_price_is_not_unit_price(upbit):
 import ccxt
 assert ccxt.__version__=='4.4.20'
 r=upbit.g.orders[U('buy')];r.update(ord_type='price',price='300000000',volume=None)
 market=dict(id='KRW-BTC',symbol='BTC/KRW',base='BTC',quote='KRW',baseId='BTC',quoteId='KRW',type='spot',spot=True,contract=False)
 out=ccxt.upbit().parse_order(r,market)
 assert D(str(out['average']))==150000000 and out['fee']['currency']=='KRW'
 assert all(t.get('fee') is None for t in out['trades'])


def test_canary_logs_and_network_operating_db_guard(upbit,capsys):
 from loguru import logger
 messages=[];sink=logger.add(lambda m:messages.append(str(m)))
 try:
  upbit.g.error='timeout'
  with pytest.raises(UpbitReadIncomplete):upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW')
  assert 'CANARY' not in ''.join(messages)+''.join(capsys.readouterr())
 finally:logger.remove(sink)
 import socket
 with pytest.raises(RuntimeError,match='AUDIT_NETWORK_DISABLED'):socket.getaddrinfo('api.upbit.com',443)
 with pytest.raises(RuntimeError,match='AUDIT_PRODUCTION_DB_DISABLED'):sqlite3.connect('D:/Antigravity/operating.sqlite')


def test_done_with_remaining_is_not_normalized_as_complete(upbit):
 r=upbit.g.orders[U('buy')];r.update(executed_volume='1',remaining_volume='1',trades_count=1);r['trades']=r['trades'][:1]
 with pytest.raises(UpbitReadIncomplete):upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW')


@pytest.mark.parametrize('state',['wait','cancel'])
def test_limit_partial_observation_preserves_nonzero_quantity(upbit,state):
 r=upbit.g.orders[U('buy')];r.update(state=state,executed_volume='1',remaining_volume='1',trades_count=1,paid_fee='50000');r['trades']=r['trades'][:1]
 o=upbit.c.read_order(upbit.c,U('buy'),'BTC/KRW').order
 assert o['filled']=='1' and o['amount']=='2' and o['remaining']=='1' and o['average']=='100000000'
 assert 'verification_status' not in o


def test_missing_order_uuid_not_success(upbit):
 with pytest.raises(UpbitReadIncomplete):upbit.c.read_order(upbit.c,U('missing-order'),'BTC/KRW')


def test_equal_numeric_representations_are_not_false_conflicts(upbit):
 original=upbit.c._get
 def get(path,params):
  result=original(path,params)
  if path=='/v1/orders/closed':
   for r in result:
    for key in ('paid_fee','price','volume','remaining_volume','executed_volume'):r[key]=format(D(r[key]),'.8f')
  return result
 upbit.c._get=get
 assert len(upbit.c.closed_orders(T-1000,T+10000))==2


@pytest.mark.parametrize('side,limit',[('bid','90000000'),('ask','170000000')])
def test_fill_price_must_respect_native_limit(upbit,side,limit):
 oid=U('buy') if side=='bid' else U('sell');upbit.g.orders[oid]['price']=limit
 with pytest.raises(UpbitReadIncomplete,match='UPBIT_LIMIT_FILL_CONFLICT'):upbit.c.read_order(upbit.c,oid,'BTC/KRW')
