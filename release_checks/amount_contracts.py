import copy, hashlib, inspect, sys
from decimal import Decimal as D
from types import SimpleNamespace as NS
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from exchange.verified_pnl import evidence as E, alert_presentation as A
from exchange.verified_pnl.bootstrap import (SpotScope, AuthenticatedAccount, OrderSnapshot, TradePage, SpotReadAdapter, SpotExecutionBootstrap)
from exchange.verified_pnl.verifier import FillVerifier
from exchange.verified_pnl.models import NotificationPayload, ExitReason, PnlResult
from exchange.utility.discord_embed import Embed

def build(side='buy',mutate=None):
    raw=dict(id='order-1',symbol='BTC/USDT',side=side,status='closed',amount='3',filled='3',remaining='0',average='150',cost='450',cost_currency='USDT',fee={'cost':'2','currency':'USDT'},
        trades=[dict(id='a',order='order-1',symbol='BTC/USDT',price='100',amount='1',cost='100',cost_currency='USDT',timestamp=1700000000000,fee={'cost':'1','currency':'USDT'}),
                dict(id='b',order='order-1',symbol='BTC/USDT',price='175',amount='2',cost='350',cost_currency='USDT',timestamp=1700000000001,fee={'cost':'1','currency':'USDT'})])
    if mutate:mutate(raw)
    class ReadClient:
        verbose=False
        def market(self,symbol):return dict(id='BTCUSDT',symbol=symbol,spot=True,contractSize=1,quote='USDT')
    client=ReadClient();key=Ed25519PrivateKey.generate()
    public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    authority=E.EvidenceAuthority('independent-test-only',key)
    scope=SpotScope('BINANCE','test-account','test-subaccount','test-source','BTC/USDT','BTCUSDT','USDT','test-cycle','physical')
    adapter=SpotReadAdapter(client,'independent-archive-v1',
        lambda c:AuthenticatedAccount('BINANCE',scope.account,scope.subaccount,frozenset({'read'})),
        lambda c,o,s:OrderSnapshot(copy.deepcopy(raw),scope.account,scope.subaccount,'snapshot'),
        lambda c,o,s,cursor,snapshot:TradePage(copy.deepcopy(raw['trades']),scope.account,scope.subaccount,o,s,snapshot,cursor,None),
        'ORDER_LIFETIME_QUOTE_V1',{})
    boot=SpotExecutionBootstrap(scope,adapter,authority,{authority.key_id:public},{authority.key_id:hashlib.sha256(public).hexdigest()})
    assert boot.activate()
    info=NS(is_buy=side=='buy',is_sell=side=='sell',side=side,is_futures=False,is_coinm=False,is_close=False,unified_symbol=scope.symbol)
    context=E.context_for('BINANCE',scope.account,info,client)
    fill=FillVerifier().verify_order_result(NS(client=client),'BINANCE',scope.symbol,{'id':'order-1'},info,context)
    p=NotificationPayload(event_id='independent',exchange='BINANCE',symbol=scope.symbol,side='매수' if side=='buy' else '매도',order_name='test',exit_reason=ExitReason.UNKNOWN,pnl_result=PnlResult.UNKNOWN,fill_status=fill.status,order_id=fill.order_id,filled_qty=str(fill.filled_quantity),exit_price=str(fill.average_fill_price) if fill.average_fill_price is not None else None,execution_receipt=fill.execution_receipt,verification_source=A.AMOUNT_ONLY_MARKER,event_timestamp=0)
    return p,fill,authority

@pytest.mark.parametrize('side',['buy','sell'])
def test_real_normalize_sign_render(side):
    p,f,_=build(side);assert E.execution_valid(f,f.scope_data) and E.payload_fill_valid(p)
    assert A._verified_executed_amount(p)==(D('450'),'USDT','spot')
    text=A.apply_amount_only(Embed(title='test'),p).data['description']
    assert '450' in text and not any(x in text.lower() for x in ('손익','roi','수익률'))

@pytest.mark.parametrize('fault',['order_cost','trade_cost','currency','trade_currency','order_id','symbol','duplicate','qty','vwap','partial','nan','inf','negative','precision'])
def test_conflicting_sources_never_sign(fault):
    def bad(o):
        if fault=='order_cost':o['cost']='999'
        elif fault=='trade_cost':o['trades'][0]['cost']='999'
        elif fault=='currency':o['cost_currency']='KRW'
        elif fault=='trade_currency':o['trades'][0]['cost_currency']='KRW'
        elif fault=='order_id':o['trades'][0]['order']='other'
        elif fault=='symbol':o['trades'][0]['symbol']='ETH/USDT'
        elif fault=='duplicate':o['trades'][1]['id']='a'
        elif fault=='qty':o['filled']='4'
        elif fault=='vwap':o['average']='149'
        elif fault=='partial':o['trades'].pop()
        else:o['trades'][0]['price']={'nan':'NaN','inf':'Infinity','negative':'-1','precision':'1e-10000'}[fault]
    p,f,_=build(mutate=bad)
    assert f.execution_receipt is None and A._verified_executed_amount(p) is None
    text=A.apply_amount_only(Embed(title='test'),p).data['description']
    assert '확인 중' in text and not any(x in text.lower() for x in ('손익','roi','수익률'))

@pytest.mark.parametrize('fault',['signature','issuer','order','symbol','scope'])
def test_real_ed25519_rejects_tamper(fault):
    p,_,_=build();assert E.payload_fill_valid(p)
    if fault=='signature':p.execution_receipt['signature']='A'*88
    elif fault=='issuer':p.execution_receipt['key_id']='unregistered'
    elif fault=='order':p.order_id='other'
    elif fault=='symbol':p.symbol='ETH/USDT'
    else:p.execution_receipt['body']['scope']['account']='other'
    assert not E.payload_fill_valid(p) and A._verified_executed_amount(p) is None

def test_optional_cost_is_absent_not_zero():
    p,f,_=build(mutate=lambda o:(o.pop('cost'),[t.pop('cost') for t in o['trades']]))
    assert E.payload_fill_valid(p) and A._verified_executed_amount(p)[0]==450

def test_bridge_absent_and_validation_functions_are_real():
    assert not any(k.endswith('conftest') and 'tests' in str(getattr(v,'__file__','')) and 'release_checks' not in str(getattr(v,'__file__','')) for k,v in sys.modules.items())
    for func in (E.verify,E.payload_fill_valid,FillVerifier._parse_order_dict,A._verified_executed_amount):
        assert '/exchange/' in inspect.getsourcefile(func).replace('\\','/')
        assert 'unittest.mock' not in type(func).__module__

@pytest.mark.parametrize('side',['buy','sell'])
def test_okx_contract_multiplier_exactly_once(side):
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    auth=E.EvidenceAuthority('linear-test-only',key);E.configure_provider(None,{auth.key_id:public})
    scope=dict(exchange='OKX',account='test-account',market_type='linear',symbol='BTC/USDT:USDT',instrument='BTC-USDT-SWAP',settlement='USDT',contract_size='0.01',position_side='LONG',position_cycle='test-cycle',book='physical')
    class Client:
        def market(self,symbol):return dict(id=scope['instrument'],symbol=symbol,linear=True,contractSize='0.01')
    client=Client();info=NS(is_futures=True,is_coinm=False,is_buy=side=='buy',is_sell=side=='sell',is_close=side=='sell',contract_size=D('.01'))
    raw=dict(id='linear-order',symbol=scope['symbol'],side=side,status='closed',amount='2',filled='2',remaining='0',average='100',cost='2',cost_currency='USDT',fee={'cost':'0.01','currency':'USDT'},trades=[dict(id='linear-trade',order='linear-order',symbol=scope['symbol'],price='100',amount='2',cost='2',cost_currency='USDT',timestamp=1700000000000,fee={'cost':'0.01','currency':'USDT'})])
    verifier=FillVerifier();fill=verifier._parse_order_dict('OKX',scope['symbol'],raw,info)
    verifier._certify_execution(fill,E.ObservationContext(auth,client,E.canonical(scope)),client,info)
    assert E.execution_valid(fill,scope) and fill.filled_quantity==D('.02')
    p=NotificationPayload(event_id='linear',exchange='OKX',symbol=scope['symbol'],side='롱 진입' if side=='buy' else '롱 종료',order_name='test',exit_reason=ExitReason.UNKNOWN,pnl_result=PnlResult.UNKNOWN,fill_status=fill.status,order_id=fill.order_id,filled_qty=str(fill.filled_quantity),exit_price=str(fill.average_fill_price),execution_receipt=fill.execution_receipt,verification_source=A.AMOUNT_ONLY_MARKER,event_timestamp=0)
    assert A._verified_executed_amount(p)==(D('2'),'USDT','linear')

def test_cross_currency_is_not_a_krw_receipt():
    p,_,_=build();p.execution_receipt['body']['scope']['settlement']='KRW'
    assert not E.payload_fill_valid(p) and A._verified_executed_amount(p) is None

def test_unregistered_estimated_admission_and_frozen_projection(tmp_path):
    from exchange.verified_pnl.delivery_store import NotificationOutboxStore
    p,_,_=build();frozen=NotificationOutboxStore._wire(p)
    p.notification_type='ESTIMATED';store=NotificationOutboxStore(str(tmp_path/'isolated.sqlite3'))
    assert store.enqueue(p) is False and store.enqueue(p) is False
    # A new projection cannot mutate previously frozen bytes or its digest.
    before=hashlib.sha256(frozen).hexdigest();p.exit_price='999'
    assert '확인 중' in A.apply_amount_only(Embed(title='test'),p).data['description']
    assert hashlib.sha256(frozen).hexdigest()==before
    with store._connect_read() as c:assert c.execute('SELECT count(*) FROM vpnl_deliveries').fetchone()[0]==0

@pytest.mark.parametrize('native_cost,accepted',[('999',False),('100',True)])
def test_native_999_vs_trade_100_exact_counterexample(native_cost,accepted):
    def one(o):
        o.update(amount='1',filled='1',average='100',cost=native_cost,fee={'cost':'1','currency':'USDT'})
        o['trades']=o['trades'][:1]
    p,f,_=build(mutate=one)
    assert E.payload_fill_valid(p) is accepted
    assert (A._verified_executed_amount(p)==(D('100'),'USDT','spot')) is accepted

@pytest.mark.parametrize('bad',['NaN','Infinity','-Infinity','1e-10000','9'*129])
def test_verifier_numeric_bounds_without_any_cost_conflict(bad):
    from exchange.verified_pnl.verifier import to_decimal
    assert to_decimal(bad) is None

@pytest.mark.parametrize('outcome',['SENT','UNKNOWN'])
def test_real_outbox_ack_cas_and_restart_no_repost(tmp_path,outcome):
    from exchange.verified_pnl.delivery_store import NotificationOutboxStore
    p,_,_=build();path=str(tmp_path/'wire.sqlite3');store=NotificationOutboxStore(path)
    assert store.enqueue(p)
    claim=store.claim_next();assert claim and store.begin_post(claim)
    wrong=dict(claim,token='wrong-token');assert not store.ack(wrong,True,message_id='123')
    assert store.ack(claim,outcome=='SENT',message_id='123' if outcome=='SENT' else None,error=None if outcome=='SENT' else 'delivery_unknown')
    frozen=claim['wire'];digest=claim['wire_digest']
    assert not store.enqueue(p)
    restarted=NotificationOutboxStore(path);assert restarted.claim_next() is None
    assert not restarted.enqueue(p) and restarted.claim_next() is None
    with restarted._connect_read() as c:
        row=c.execute('SELECT status,wire,wire_digest,message_id FROM vpnl_deliveries').fetchone()
        assert row==(outcome,frozen,digest,'123' if outcome=='SENT' else None)


def test_real_review_hold_remains_unsendable_after_restart(tmp_path):
    from exchange.verified_pnl.delivery_store import NotificationOutboxStore
    p,_,_=build();path=str(tmp_path/'held.sqlite3');store=NotificationOutboxStore(path)
    assert store.enqueue(p)
    with store._connect() as c:
        rid=c.execute('SELECT id FROM vpnl_roots').fetchone()[0]
        store._review_root(c,rid)
    restarted=NotificationOutboxStore(path);assert restarted.claim_next() is None
    assert not restarted.enqueue(p)
    with restarted._connect_read() as c:assert c.execute('SELECT status FROM vpnl_deliveries').fetchone()==('HELD',)

def test_real_partial_fill_only_confirmed_turnover():
    def partial(o):o.update(status='open',amount='5',filled='3',remaining='2')
    p,f,_=build(mutate=partial)
    assert E.payload_fill_valid(p) and f.status.value=='PARTIALLY_FILLED'
    assert A._verified_executed_amount(p)==(D('450'),'USDT','spot')

def test_two_claimers_only_one_has_post_authority(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from exchange.verified_pnl.delivery_store import NotificationOutboxStore
    p,_,_=build();store=NotificationOutboxStore(str(tmp_path/'race.sqlite3'));assert store.enqueue(p)
    with ThreadPoolExecutor(2) as pool:claims=list(pool.map(lambda _:store.claim_next(),range(2)))
    winners=[c for c in claims if c];assert len(winners)==1
    claim=winners[0];assert store.begin_post(claim) and not store.begin_post(claim)
    assert store.ack(claim,True,message_id='123') and store.claim_next() is None
