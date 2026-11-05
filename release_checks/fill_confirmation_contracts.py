"""Native HTTP IO fakes; actual POA producer, Intake, read validators and Outbox."""
import ast
import copy
import datetime
import hashlib
import json
import sqlite3
import time
from decimal import Decimal as D, localcontext
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlsplit, parse_qs

import pytest
from exchange.model.schemas import Settings
from exchange.verified_pnl import dispatcher as DP, evidence as E
from exchange.verified_pnl import order_fill_confirmation as C
from exchange.verified_pnl.order_fill_read import CONTRACT, request_scope
from exchange.verified_pnl.bootstrap import configure_execution_bootstrap
from exchange.verified_pnl.upbit_spot_read import configure_upbit_notification_host
from exchange.verified_pnl.upbit_notification_get import HTTPSOrderGETTransport, OrderGETResponse
from exchange.verified_pnl.delivery_store import NotificationOutboxStore
from exchange.verified_pnl.intake import IntakeStore
from exchange.verified_pnl import outbox

ROOT = Path(__file__).resolve().parents[1]
MARKETS = [('UPBIT', False), ('BYBIT', False), ('BYBIT', True), ('BINANCE', False),
           ('BINANCE', True), ('OKX', False), ('OKX', True)]


def original_log(settings):
    tree = ast.parse((ROOT/'main.py').read_text(encoding='utf-8-sig'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('log', 'is_verified_exchange')]
    ns = dict(settings=settings, dispatch_verified_alert=DP.dispatch_verified_alert)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT/'main.py'), 'exec'), ns)
    return ns['log']


def setup(tmp_path, monkeypatch, exchange='UPBIT', future=False, side='sell', count=2, base='SOL', close=None, short=False):
    configure_execution_bootstrap(None)
    configure_upbit_notification_host(None)
    credential_defaults={name:None for name in Settings.__fields__ if any(part in name for part in ('_KEY','_SECRET','PASSPHRASE','WEBHOOK_URL'))}
    credential_defaults.update({exchange+'_KEY':'synthetic-key',exchange+'_SECRET':'synthetic-'+'s'*64})
    if exchange=='OKX':credential_defaults['OKX_PASSPHRASE']='synthetic-pass'
    credential_defaults['DISCORD_WEBHOOK_URL']='https://discord.com/api/webhooks/000/synthetic'
    st = Settings(_env_file=None, PASSWORD='x', VERIFIED_DISCORD_ALERTS=True, VERIFIED_FILL_PRICE=True,
        DISCORD_OUTBOX_ENABLED=True, UPBIT_VERIFIED_ALERTS=True, POA_EXCHANGE_FILL_CONFIRMATION_ENABLED=True,
        VERIFIED_NOTIFICATIONS_DB_PATH=str(tmp_path/'synthetic.sqlite3'), **credential_defaults)
    quote = 'KRW' if exchange=='UPBIT' else 'USDT'
    symbol = base+'/'+quote+(':USDT' if future else '')
    if close is None:close=future and ((not short and side=='sell') or (short and side=='buy'))
    info = NS(exchange=exchange, base=base, quote=quote, unified_symbol=symbol, side=side,
        is_buy=side=='buy', is_sell=side=='sell', is_futures=future, is_coinm=False, is_spot=not future,
        is_close=close, is_entry=not close, strategy_id='strategy-one', order_name='HL', tv_signal=None)
    if exchange=='OKX' and future:info.contract_size=0.1
    oid = '10000000-0000-4000-8000-000000000001' if exchange=='UPBIT' else '10001'
    host = C.prepare(st)
    ref = C.identity(exchange, 'synthetic-key')
    request = dict(info=vars(info), result={'id':oid}, source_account_ref=ref)
    scope = request_scope(request)
    factor = D('0.1') if exchange=='OKX' and future else D(1)
    contract_qty = D(2) if exchange=='OKX' and future else D(1)
    qty = factor*contract_qty
    prices = [D(100+i*10) for i in range(count)]
    total = sum((p*qty for p in prices), D(0))
    amount = qty*count
    average = total/amount
    instrument = base+'USDT'
    now = int(time.time()*1000)-2000
    calls, posts = [], []
    position = 'SHORT' if short else 'LONG'
    if exchange=='UPBIT':
        instrument='KRW-'+base
        stamp=lambda ms: datetime.datetime.fromtimestamp(ms/1000, datetime.timezone.utc).isoformat()
        native=dict(uuid=oid,market=instrument,side='bid' if side=='buy' else 'ask',ord_type='limit',state='done',
            created_at=stamp(now),price=str(max(prices) if side=='buy' else min(prices)),volume=str(amount),remaining_volume='0',executed_volume=str(amount),
            reserved_fee='0',remaining_fee='0',paid_fee='1',locked='0',trades_count=count,
            trades=[dict(uuid='20000000-0000-4000-8000-'+str(i+1).zfill(12),market=instrument,side='bid' if side=='buy' else 'ask',
                         price=str(p),volume=str(qty),funds=str(p*qty),created_at=stamp(now+1000)) for i,p in enumerate(prices)])
        state=dict(order=native)
        def upbit_http(self, req):
            import jwt
            token=jwt.decode(req.headers['Authorization'].removeprefix('Bearer '),'synthetic-'+'s'*64,algorithms=['HS512'])
            assert token['query_hash']==hashlib.sha512(('uuid='+oid).encode()).hexdigest()
            assert req.method=='GET' and req.verify and not req.allow_redirects
            calls.append(req.url)
            return OrderGETResponse(200,json.dumps(state['order']).encode(),{})
        monkeypatch.setattr(HTTPSOrderGETTransport,'__call__',upbit_http)
    elif exchange=='BYBIT':
        category='linear' if future else 'spot'
        native=dict(orderId=oid,symbol=instrument,side=side.title(),orderStatus='Filled',leavesQty='0',cumExecQty=str(amount),cumExecValue=str(total),avgPrice=str(average),positionIdx=2 if short else 1,createdTime=str(now),updatedTime=str(now+1000))
        trades=[dict(orderId=oid,symbol=instrument,side=side.title(),execType='Trade',execId=str(i+1),execPrice=str(p),execQty=str(qty),execValue=str(p*qty),execTime=str(now+1000),execFee='0.1',feeCurrency='USDT') for i,p in enumerate(prices)]
        state=dict(order=native,trades=trades,cursor='')
    elif exchange=='BINANCE':
        native=dict(orderId=int(oid),symbol=instrument,side=side.upper(),status='FILLED',executedQty=str(amount),origQty=str(amount),time=now,updateTime=now+1000,cumQuote=str(total),cummulativeQuoteQty=str(total),avgPrice=str(average),positionSide=position)
        trades=[dict(id=i+1,orderId=int(oid),symbol=instrument,side=side.upper(),isBuyer=side=='buy',price=str(p),qty=str(qty),quoteQty=str(p*qty),time=now+1000,commission='0.1',commissionAsset='USDT',positionSide=position) for i,p in enumerate(prices)]
        state=dict(order=native,trades=trades)
    else:
        instrument=base+'-USDT'+('-SWAP' if future else '')
        native=dict(ordId=oid,instId=instrument,instType='SWAP' if future else 'SPOT',side=side,state='filled',tdMode='cross' if future else 'cash',accFillSz=str(contract_qty*count),avgPx=str(average),posSide=position.lower() if future else 'net',cTime=str(now),uTime=str(now+1000))
        trades=[dict(ordId=oid,instId=instrument,instType=native['instType'],side=side,posSide=native['posSide'],tradeId=str(i+1),billId=str(i+100),fillPx=str(p),fillSz=str(contract_qty),fillNotionalUsd=str(p*qty),fillTime=str(now+1000),fee='-0.1',feeCcy='USDT') for i,p in enumerate(prices)]
        state=dict(order=native,trades=trades,metadata=dict(instId=instrument,instType='SWAP',ctType='linear',settleCcy='USDT',ctValCcy=base,ctVal=str(factor),ctMult='1'))
    if exchange!='UPBIT':
        client=host._client(scope)
        def fetch(url, method='GET', headers=None, body=None):
            assert method=='GET' and body is None
            path=urlsplit(url).path
            params=parse_qs(urlsplit(url).query)
            calls.append((path,params))
            if exchange=='BYBIT':
                assert headers.get('X-BAPI-SIGN')
                assert params['category']==['linear' if future else 'spot']
                if path.endswith('/execution/list'):
                    return dict(retCode=0,result=dict(category='linear' if future else 'spot',list=copy.deepcopy(state['trades']),nextPageCursor=state['cursor']))
                return dict(retCode=0,result=dict(category='linear' if future else 'spot',list=[copy.deepcopy(state['order'])],nextPageCursor=''))
            if exchange=='BINANCE':
                assert params.get('signature') and headers.get('X-MBX-APIKEY')=='synthetic-key'
                return copy.deepcopy(state['trades'] if path.endswith(('myTrades','userTrades')) else state['order'])
            if path.endswith('/public/instruments'):
                return dict(code='0',data=[copy.deepcopy(state['metadata'])])
            assert headers.get('OK-ACCESS-SIGN')
            return dict(code='0',data=copy.deepcopy(state['trades']) if path.endswith(('fills','fills-history')) else [copy.deepcopy(state['order'])])
        monkeypatch.setattr(host,'_http',lambda exchange,signed:fetch(signed['url'],'GET',signed.get('headers'),signed.get('body')))
        client.enableRateLimit=False  # mock HTTP only: no API rate budget is consumed
    def discord_post(url, **kwargs):
        assert url==st.DISCORD_WEBHOOK_URL and kwargs['params']=={'wait':'true'}
        posts.append(json.loads(kwargs['data']))
        return NS(status_code=200,headers={},json=lambda:dict(id=str(10000+len(posts))))
    monkeypatch.setattr(outbox.requests,'post',discord_post)
    return NS(st=st,info=info,oid=oid,host=host,scope=scope,state=state,calls=calls,posts=posts,total=total,amount=amount,average=average,
              store=lambda:NotificationOutboxStore(st.VERIFIED_NOTIFICATIONS_DB_PATH),log=original_log(st))


def feed(r):
    return r.log(r.info.exchange,{'id':r.oid},r.info)


def tick(r):
    DP.verification_tick(r.st,lambda *args:(_ for _ in ()).throw(AssertionError('FINANCIAL_CLIENT_NOT_CONFIGURED')))


def confirmation_posts(r):
    return [p for p in r.posts if '확인 방식: 거래소 인증 체결 조회' in p['embeds'][0]['description']]


@pytest.mark.parametrize('exchange,future',MARKETS)
@pytest.mark.parametrize('side', ['buy','sell'])
@pytest.mark.parametrize('count',[1,2])
@pytest.mark.parametrize('base',['BTC','ETH','SOL'])
def test_real_entry_native_signed_get_outbox_all_markets(tmp_path,monkeypatch,exchange,future,side,count,base):
    # LONG metadata: BUY entry, SELL close. Tests below cover SHORT too.
    r=setup(tmp_path,monkeypatch,exchange,future,side,count,base,close=future and side=='sell')
    feed(r)
    assert r.calls==[] and confirmation_posts(r)==[]
    tick(r)
    DP.flush_outbox(r.st)
    tick(r)
    DP.flush_outbox(r.st)
    posts=confirmation_posts(r)
    assert len(posts)==1
    body=posts[0]['embeds'][0]
    lines=body['description'].splitlines()
    with localcontext() as ctx:
        ctx.prec=512
        native_prices=[D(100+i*10) for i in range(count)]
        q=D('0.2') if exchange=='OKX' and future else D(1)
        oracle=sum((p*q for p in native_prices),D(0))
        qty=q*count
        avg=oracle/qty
    def field(prefix):return next(x.split(': ',1)[1].split()[0].removesuffix('원') for x in lines if x.startswith(prefix+': '))
    assert D(field('실제 평균 체결가'))==avg
    assert D(field('실제 체결수량'))==qty
    assert D(field('계약 명목가치' if future else '실제 총 매수액' if side=='buy' else '실제 총 매도액'))==oracle
    assert body['color']==0x2563EB and 'KST' in body['description']
    assert not any(x in body['description'] for x in ('ROI','수익률','확정손익'))
    before=len(r.calls)
    feed(r);tick(r);DP.flush_outbox(r.st)
    C._HOSTS.pop(r.store().db_path,None)
    C.prepare(r.st);tick(r);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1 and len(r.calls)==before
    with r.store()._connect_read() as c:
        assert c.execute('SELECT count(*) FROM vpnl_fill_submissions').fetchone()[0]==1
        raw=c.execute("SELECT payload FROM vpnl_revisions WHERE json_extract(payload,'$.notification_type')=?",(CONTRACT,)).fetchone()[0]
        p=json.loads(raw)
        assert all(p.get(k) is None for k in ('execution_receipt','financial_receipt','accounting_receipt','net_pnl','roi_pct'))
        assert not E.payload_fill_valid(NS(**p))
        if future:
            assert c.execute('SELECT count(*) FROM vpnl_jobs').fetchone()[0]==0
    with IntakeStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:
        row=c.execute('SELECT request,request_digest,status FROM notification_intake').fetchone()
        assert E.digest(json.loads(row[0]))==row[1]
        if future:assert row[2]=='WAIT_PROVIDER'


@pytest.mark.parametrize('exchange,future',MARKETS)
@pytest.mark.parametrize('fault',['order','symbol','side','not_final','quantity','missing','duplicate','quote','number'])
def test_native_conflicts_never_authorize(tmp_path,monkeypatch,exchange,future,fault):
    r=setup(tmp_path,monkeypatch,exchange,future)
    o=r.state['order'];trades=o['trades'] if exchange=='UPBIT' else r.state['trades']
    if fault=='order':o[{'UPBIT':'uuid','BYBIT':'orderId','BINANCE':'orderId','OKX':'ordId'}[exchange]]='30000000-0000-4000-8000-000000000003' if exchange=='UPBIT' else '99999'
    elif fault=='symbol':o[{'UPBIT':'market','BYBIT':'symbol','BINANCE':'symbol','OKX':'instId'}[exchange]]='KRW-BTC' if exchange=='UPBIT' else 'WRONG'
    elif fault=='side':o['side']='bid' if exchange=='UPBIT' else 'Buy' if exchange=='BYBIT' else 'BUY' if exchange=='BINANCE' else 'buy'
    elif fault=='not_final':o[{'UPBIT':'state','BYBIT':'orderStatus','BINANCE':'status','OKX':'state'}[exchange]]={'UPBIT':'cancel','BYBIT':'PartiallyFilledCanceled','BINANCE':'CANCELED','OKX':'canceled'}[exchange]
    elif fault=='quantity':o[{'UPBIT':'executed_volume','BYBIT':'cumExecQty','BINANCE':'executedQty','OKX':'accFillSz'}[exchange]]='999'
    elif fault=='missing':trades.pop()
    elif fault=='duplicate':trades.append(copy.deepcopy(trades[0]))
    elif fault=='quote':trades[0][{'UPBIT':'funds','BYBIT':'execValue','BINANCE':'quoteQty','OKX':'fillPx'}[exchange]]='999'
    elif fault=='number':trades[0][{'UPBIT':'price','BYBIT':'execPrice','BINANCE':'price','OKX':'fillPx'}[exchange]]='NaN'
    feed(r);C.tick(r.st,r.store());DP.flush_outbox(r.st)
    assert confirmation_posts(r)==[]
    with r.store()._connect_read() as c:
        assert c.execute('SELECT count(*) FROM vpnl_fill_submissions').fetchone()[0]==0
        assert c.execute("SELECT state FROM vpnl_fill_requests").fetchone()==('REVIEW',)


@pytest.mark.parametrize('state',['PENDING','CLAIMED','POSTING','UNKNOWN','HELD','SENT'])
def test_acceptance_state_controls_followup_and_original_bytes(tmp_path,monkeypatch,state):
    r=setup(tmp_path,monkeypatch,side='buy')
    feed(r);tick(r)
    store=r.store()
    with store._connect_read() as c:
        old=c.execute('SELECT id,wire,wire_digest FROM vpnl_deliveries').fetchone()
        assert old is not None
    claim=None
    if state!='PENDING':
        claim=store.claim_next();assert claim
    if state in ('POSTING','UNKNOWN','HELD','SENT'):
        assert store.begin_post(claim)
    if state=='SENT':assert store.ack(claim,True,message_id='123456')
    if state in ('UNKNOWN','HELD'):
        assert store.ack(claim,False,error='delivery_unknown')
        if state=='HELD':
            # Synthetic operator gate; no production or old immutable bytes changed.
            with store._connect() as c:c.execute("UPDATE vpnl_deliveries SET status='HELD' WHERE id=?",(old[0],))
    C.tick(r.st,store)
    with store._connect_read() as c:
        assert c.execute('SELECT wire,wire_digest FROM vpnl_deliveries WHERE id=?',(old[0],)).fetchone()==old[1:]
        count=c.execute("SELECT count(*) FROM vpnl_revisions WHERE json_extract(payload,'$.notification_type')=?",(CONTRACT,)).fetchone()[0]
        assert count==(1 if state=='SENT' else 0)
    if state=='SENT':
        DP.flush_outbox(r.st);C.tick(r.st,store);DP.flush_outbox(r.st)
        assert len(confirmation_posts(r))==1


@pytest.mark.parametrize('exchange,future',MARKETS)
def test_credential_mismatch_and_missing_key_are_not_authority(tmp_path,monkeypatch,exchange,future):
    r=setup(tmp_path,monkeypatch,exchange,future)
    feed(r)
    setattr(r.st,exchange+'_KEY','other-synthetic-key')
    C.tick(r.st,r.store())
    assert r.calls==[] and confirmation_posts(r)==[]
    assert E._PROVIDER is None and E._KEYS=={}


@pytest.mark.parametrize('exchange',['BYBIT','BINANCE','OKX'])
@pytest.mark.parametrize('short,close',[(True,False),(True,True),(False,False),(False,True)])
def test_linear_direction_is_derived_from_native_position_metadata(tmp_path,monkeypatch,exchange,short,close):
    side='buy' if short==close else 'sell'
    r=setup(tmp_path,monkeypatch,exchange,True,side=side,short=short,close=close)
    feed(r);tick(r);DP.flush_outbox(r.st)
    posts=confirmation_posts(r);assert len(posts)==1
    assert ('숏' if short else '롱')+(' 청산' if close else ' 진입') in posts[0]['embeds'][0]['title']


def test_opaque_read_capability_and_immutable_proof_not_dto_flags(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch)
    feed(r);tick(r)
    store=r.store()
    with store._connect_read() as c:
        job,raw=c.execute('SELECT id,proof FROM vpnl_fill_submissions').fetchone()
        proof=json.loads(raw);p=C.make_payload(proof)
        assert C.validate_stored(store,c,p)
        p.fill_confirmation['total']='999'
        assert not C.validate_stored(store,c,p)
        p.fill_confirmation=proof;p.net_pnl='999'
        assert not C.validate_stored(store,c,p)
    assert not store.enqueue(p)
    from exchange.verified_pnl.order_fill_read import Observation
    forged=Observation(proof,r.host)
    assert not r.host.consume(forged)
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as c:c.execute('UPDATE vpnl_fill_submissions SET proof=?',(E.canonical(proof),))


def test_duplicate_strategies_and_two_workers_cannot_double_publish(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    r=setup(tmp_path,monkeypatch)
    feed(r)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:C.tick(r.st,r.store()),range(2)))
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:DP.flush_outbox(r.st),range(2)))
    assert len(confirmation_posts(r))==1 and len(r.calls)==1
    r.info.strategy_id='another-strategy'
    feed(r);C.tick(r.st,r.store());DP.flush_outbox(r.st)
    with r.store()._connect_read() as c:assert c.execute('SELECT reason FROM vpnl_fill_reviews').fetchone()==('FILL_ORIGIN_CONFLICT',)
    assert len(confirmation_posts(r))==1


@pytest.mark.parametrize('fault',['cursor','cap','time','average','multiplier','currency'])
def test_pagination_time_and_metadata_fail_closed(tmp_path,monkeypatch,fault):
    exchange='BYBIT' if fault=='cursor' else 'BINANCE' if fault=='cap' else 'OKX'
    r=setup(tmp_path,monkeypatch,exchange,True,side='buy')
    if fault=='cursor':r.state['cursor']='same-cursor'
    elif fault=='cap':r.state['trades']*=500
    elif fault=='time':r.state['trades'][0]['fillTime']='1'
    elif fault=='average':r.state['order']['avgPx']='999'
    elif fault=='multiplier':r.state['metadata']['ctVal']='10'
    elif fault=='currency':r.state['metadata']['ctValCcy']='USDT'
    feed(r);C.tick(r.st,r.store());DP.flush_outbox(r.st)
    assert confirmation_posts(r)==[]


def test_default_disabled_and_old_intake_not_retroactively_enrolled(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch)
    r.st.POA_EXCHANGE_FILL_CONFIRMATION_ENABLED=False
    feed(r);tick(r)
    with r.store()._connect_read() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='vpnl_fill_requests'").fetchone()
    r.st.POA_EXCHANGE_FILL_CONFIRMATION_ENABLED=True
    C._HOSTS.pop(r.store().db_path,None);C.prepare(r.st)
    tick(r)
    assert r.calls==[]
    feed(r) # even a duplicate old Intake cannot be silently enrolled
    with r.store()._connect_read() as c:
        table=c.execute("SELECT 1 FROM sqlite_master WHERE name='vpnl_fill_requests'").fetchone()
        assert not table or c.execute('SELECT count(*) FROM vpnl_fill_requests').fetchone()[0]==0


@pytest.mark.parametrize('exchange,future',MARKETS)
def test_negative_fixtures_have_a_valid_unmodified_control(tmp_path,monkeypatch,exchange,future):
    r=setup(tmp_path,monkeypatch,exchange,future)
    request=dict(info=vars(r.info),result={'id':r.oid},source_account_ref=r.scope['account'])
    observation=r.host.observe(request)
    from exchange.verified_pnl.order_fill_read import validate
    assert validate(json.loads(observation.encoded))
    assert r.host.consume(observation) and not r.host.consume(observation)


@pytest.mark.parametrize('exchange,future',MARKETS)
@pytest.mark.parametrize('failure',['401','403','429','timeout'])
def test_authenticated_read_failure_no_confirmation_or_secret_output(tmp_path,monkeypatch,capsys,exchange,future,failure):
    r=setup(tmp_path,monkeypatch,exchange,future)
    calls=[]
    if exchange=='UPBIT':
        def failure_http(self,request):
            calls.append(1)
            if failure=='timeout':raise TimeoutError('SYNTHETIC_SECRET_RESPONSE')
            return OrderGETResponse(int(failure),b'{"error":{"name":"SYNTHETIC_SECRET_RESPONSE"}}',{'retry-after':'60'})
        monkeypatch.setattr(HTTPSOrderGETTransport,'__call__',failure_http)
    else:
        import ccxt
        client=r.host._client(r.scope)
        def failure_fetch(*args,**kwargs):
            calls.append(1)
            error={'401':ccxt.AuthenticationError,'403':ccxt.PermissionDenied,'429':ccxt.RateLimitExceeded,'timeout':ccxt.RequestTimeout}[failure]
            raise error('SYNTHETIC_SECRET_RESPONSE')
        monkeypatch.setattr(r.host,'_http',failure_fetch)
    feed(r);C.tick(r.st,r.store());DP.flush_outbox(r.st)
    assert confirmation_posts(r)==[] and 1<=len(calls)<=2
    if failure in ('401','403'):assert len(calls)==1
    with r.store()._connect_read() as c:
        assert c.execute('SELECT count(*) FROM vpnl_fill_submissions').fetchone()[0]==0
        code=c.execute('SELECT error FROM vpnl_fill_requests').fetchone()[0]
        assert code in {'FILL_AUTH_REJECTED','FILL_PERMISSION_REJECTED','FILL_RATE_LIMITED','FILL_TIMEOUT'}
    captured=capsys.readouterr()
    assert 'SYNTHETIC_SECRET_RESPONSE' not in captured.out+captured.err


@pytest.mark.parametrize('exchange,future',MARKETS)
def test_missing_fee_is_unknown_not_zero(tmp_path,monkeypatch,exchange,future):
    r=setup(tmp_path,monkeypatch,exchange,future)
    if exchange=='UPBIT':r.state['order'].pop('paid_fee')
    else:
        for t in r.state['trades']:t.pop({'BYBIT':'execFee','BINANCE':'commission','OKX':'fee'}[exchange])
    feed(r);tick(r);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1
    assert '수수료/리베이트: 확인 중' in confirmation_posts(r)[0]['embeds'][0]['description']


def test_proof_rechecked_before_post_without_altering_ack_cas(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch);feed(r);tick(r)
    store=r.store();claim=store.claim_next();assert claim
    # A changed original credential identity invalidates only the new claim.
    r.st.UPBIT_KEY='other-synthetic-key'
    assert not store.begin_post(claim)
    with store._connect_read() as c:
        assert c.execute('SELECT status FROM vpnl_deliveries WHERE id=?',(claim['delivery_id'],)).fetchone()==('HELD',)
        assert c.execute('SELECT phase FROM vpnl_delivery_attempts WHERE id=?',(claim['attempt_id'],)).fetchone()==('CLAIMED',)
    r.st.UPBIT_KEY='synthetic-key'
    DP.flush_outbox(r.st)
    assert confirmation_posts(r)==[]


def test_new_tables_are_additive_backup_restore_preserves_pending_proof(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch);feed(r);tick(r)
    original=r.st.VERIFIED_NOTIFICATIONS_DB_PATH
    destination=str(tmp_path/'restored.sqlite3')
    for left,right in [(original,destination),(original+'.intake.sqlite3',destination+'.intake.sqlite3')]:
        src=sqlite3.connect('file:'+Path(left).as_posix()+'?mode=ro',uri=True);dst=sqlite3.connect(right)
        try:
            src.backup(dst)
            assert dst.execute('PRAGMA integrity_check').fetchone()==('ok',)
        finally:src.close();dst.close()
    with r.store()._connect_read() as c:
        before=c.execute('SELECT wire,wire_digest,status FROM vpnl_deliveries').fetchall()
    r.st.VERIFIED_NOTIFICATIONS_DB_PATH=destination
    C.prepare(r.st)
    with r.store()._connect_read() as c:
        assert c.execute('SELECT wire,wire_digest,status FROM vpnl_deliveries').fetchall()==before
    DP.flush_outbox(r.st);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1


def test_distinct_process_restores_confirmation_and_does_not_requery(tmp_path,monkeypatch):
    import os,subprocess,sys
    r=setup(tmp_path,monkeypatch);feed(r);tick(r)
    program='''
import sys,json
from pathlib import Path
import release_checks.isolation
from types import SimpleNamespace as NS
from exchange.model.schemas import Settings
from exchange.verified_pnl import dispatcher as DP,order_fill_confirmation as C,outbox
st=Settings(_env_file=None,**json.loads(sys.stdin.read()));C.prepare(st)
posts=[]
def post(url,**kw):
 posts.append(json.loads(kw['data']));return NS(status_code=200,headers={},json=lambda:{'id':'123456'})
outbox.requests.post=post
DP.flush_outbox(st);DP.flush_outbox(st)
print(json.dumps({'posts':posts}))
'''
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
    result=subprocess.run([sys.executable,'-B','-c',program],cwd=ROOT,input=r.st.json(),capture_output=True,text=True,env=env,timeout=45)
    assert result.returncode==0,result.stderr[-400:]
    value=json.loads(result.stdout)
    assert len(value['posts'])==1 and '실제 총 매도액' in value['posts'][0]['embeds'][0]['description']
    with r.store()._connect_read() as c:assert c.execute('SELECT status FROM vpnl_deliveries').fetchone()==('SENT',)
    before=len(r.calls);C.tick(r.st,r.store());DP.flush_outbox(r.st)
    assert len(r.calls)==before and confirmation_posts(r)==[]


def test_unregistered_producer_cannot_enroll(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch);feed(r)
    with IntakeStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:iid=c.execute('SELECT id FROM notification_intake').fetchone()[0]
    with pytest.raises(ValueError,match='FILL_PRODUCER_REJECTED'):C.enroll(r.st,iid,object())


@pytest.mark.parametrize('status,expected',[(200,None),(401,'FILL_AUTH_REJECTED'),(403,'FILL_PERMISSION_REJECTED'),(429,'FILL_RATE_LIMITED'),(500,'FILL_READ_FAILED'),(302,'FILL_REDIRECT_REJECTED')])
def test_real_tls_transport_preserves_status_and_never_follows_redirect(tmp_path,monkeypatch,status,expected):
    from exchange.verified_pnl.order_fill_read import ReadHost,ReadRejected
    import ssl
    import exchange.verified_pnl.order_fill_read as R
    r=setup(tmp_path,monkeypatch,'BYBIT')
    calls=[]
    class Connection:
        def __init__(self,host,port,timeout,context):
            assert (host,port,timeout)==('api.bybit.com',443,5)
            assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
            self.sent=False
        def set_debuglevel(self,value):assert value==0
        def request(self,method,target,headers):
            assert method=='GET' and target.startswith('/v5/order/history?')
            assert headers.get('X-BAPI-SIGN');calls.append((method,target.split('?')[0]))
        def getresponse(self):
            connection=self
            class Response:
                def getheader(self,name):return 'application/json' if name=='Content-Type' else None
                def read1(self,limit):
                    if connection.sent:return b''
                    connection.sent=True;return b'{"retCode":0}'
            response=Response();response.status=status;return response
        def close(self):pass
    monkeypatch.setattr(R.http.client,'HTTPSConnection',Connection)
    signed=r.host._client(r.scope).sign('v5/order/history','private','GET',dict(category='spot',orderId=r.oid))
    if expected:
        with pytest.raises(ReadRejected,match=expected):ReadHost._http(r.host,'BYBIT',signed)
    else:assert ReadHost._http(r.host,'BYBIT',signed)=={'retCode':0}
    assert len(calls)==1


@pytest.mark.parametrize('mutation',['method','host','path','http','body'])
def test_read_transport_allowlist_before_any_connection(tmp_path,monkeypatch,mutation):
    import exchange.verified_pnl.order_fill_read as R
    r=setup(tmp_path,monkeypatch,'BYBIT')
    signed=r.host._client(r.scope).sign('v5/order/history','private','GET',dict(category='spot',orderId=r.oid))
    if mutation=='method':signed['method']='POST'
    elif mutation=='host':signed['url']=signed['url'].replace('api.bybit.com','invalid.example')
    elif mutation=='path':signed['url']=signed['url'].replace('/order/history','/order/create')
    elif mutation=='http':signed['url']=signed['url'].replace('https:','http:')
    elif mutation=='body':signed['body']='{}'
    def forbidden(*args,**kwargs):raise AssertionError('connection before policy check')
    monkeypatch.setattr(R.http.client,'HTTPSConnection',forbidden)
    with pytest.raises(R.ReadRejected,match='FILL_GET_REJECTED'):R.ReadHost._http(r.host,'BYBIT',signed)


@pytest.mark.parametrize('code,expected',[(10003,'FILL_AUTH_REJECTED'),(10005,'FILL_PERMISSION_REJECTED'),(10010,'FILL_IP_REJECTED'),(10006,'FILL_RATE_LIMITED')])
def test_native_error_allowlist_not_error_message(tmp_path,monkeypatch,capsys,code,expected):
    r=setup(tmp_path,monkeypatch,'BYBIT')
    monkeypatch.setattr(r.host,'_http',lambda *args:dict(retCode=code,retMsg='SYNTHETIC_SECRET_MESSAGE'))
    feed(r);C.tick(r.st,r.store());DP.flush_outbox(r.st)
    with r.store()._connect_read() as c:assert c.execute('SELECT error FROM vpnl_fill_requests').fetchone()==(expected,)
    assert confirmation_posts(r)==[]
    output=capsys.readouterr();assert 'SYNTHETIC_SECRET_MESSAGE' not in output.out+output.err


def test_exact_new_source_pins_reject_admission_or_quantity_tamper():
    from release_checks.fill_source_contract import restore_notification_source,verify
    verify()
    for name,original,changed in [('exchange/verified_pnl/delivery_store.py',b'if not self._attempt(c,claim,\'CLAIMED\'):return False',b'if False:return False'),
                                  ('exchange/model/schemas.py',b'POA_EXCHANGE_FILL_CONFIRMATION_ENABLED: bool = False',b'POA_EXCHANGE_FILL_CONFIRMATION_ENABLED: bool = True')]:
        raw=(ROOT/name).read_bytes();bad=raw.replace(original,changed);assert bad!=raw
        with pytest.raises(AssertionError):restore_notification_source(name,bad)


def test_same_binance_order_id_on_two_symbols_is_not_one_physical_order(tmp_path,monkeypatch):
    first=setup(tmp_path,monkeypatch,'BINANCE',base='BTC')
    feed(first);tick(first);DP.flush_outbox(first.st)
    second=setup(tmp_path,monkeypatch,'BINANCE',base='ETH')
    feed(second);tick(second);DP.flush_outbox(second.st)
    assert len(confirmation_posts(first))==len(confirmation_posts(second))==1
    with second.store()._connect_read() as c:
        assert c.execute('SELECT count(*) FROM vpnl_fill_submissions').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM vpnl_fill_reviews').fetchone()[0]==0


@pytest.mark.parametrize('timeout',[False,True])
def test_uncertain_confirmation_post_is_never_automatically_repeated(tmp_path,monkeypatch,timeout):
    import requests
    r=setup(tmp_path,monkeypatch);feed(r);tick(r)
    calls=[]
    def post(*args,**kwargs):
        calls.append(1)
        if timeout:raise requests.exceptions.Timeout('SYNTHETIC_ERROR')
        return NS(status_code=500,headers={},json=lambda:{})
    monkeypatch.setattr(outbox.requests,'post',post)
    DP.flush_outbox(r.st);tick(r);DP.flush_outbox(r.st)
    assert calls==[1]
    with r.store()._connect_read() as c:assert c.execute('SELECT status FROM vpnl_deliveries').fetchone()==('UNKNOWN',)


def test_fees_and_rebates_separate_from_okx_notional(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch,'OKX',True,side='buy')
    r.state['trades'][0].update(fee='-0.1000',rebate='0.02',rebateCcy='USDT')
    r.state['order'].update(fee='-0.2000',feeCcy='USDT',rebate='0.0200',rebateCcy='USDT')
    feed(r);tick(r);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1
    description=confirmation_posts(r)[0]['embeds'][0]['description']
    assert '계약 명목가치: 42 USDT' in description and '수수료/리베이트: 0.18 USDT' in description


def test_a_sent_status_flag_without_original_ack_is_not_acceptance(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch,side='buy');feed(r);tick(r)
    with r.store()._connect() as c:c.execute("UPDATE vpnl_deliveries SET status='SENT'")
    C.tick(r.st,r.store());DP.flush_outbox(r.st)
    assert confirmation_posts(r)==[]
    with r.store()._connect_read() as c:
        assert c.execute('SELECT error FROM vpnl_fill_requests').fetchone()==('FILL_PREDECESSOR_REVIEW',)


def test_source_none_key_disables_only_that_venue(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch);r.st.UPBIT_KEY=None
    feed(r);tick(r);DP.flush_outbox(r.st)
    assert r.calls==[] and confirmation_posts(r)==[]


def test_corrupt_sidecar_cannot_authorize_a_previously_proved_message(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch);feed(r);tick(r)
    with IntakeStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:
        raw=c.execute('SELECT request FROM notification_intake').fetchone()[0]
        changed=json.loads(raw);changed['result']['id']='30000000-0000-4000-8000-000000000003'
        c.execute('UPDATE notification_intake SET request=?',(E.canonical(changed),))
    DP.flush_outbox(r.st)
    assert confirmation_posts(r)==[]


def test_expired_read_lease_has_a_durable_two_attempt_bound(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch);feed(r)
    with r.store()._connect() as c:
        c.execute("UPDATE vpnl_fill_requests SET state='READING',attempts=2,lease='synthetic-expired',expires=0")
    C.tick(r.st,r.store());C.tick(r.st,r.store())
    assert r.calls==[]
    with r.store()._connect_read() as c:assert c.execute('SELECT state,error FROM vpnl_fill_requests').fetchone()==('REVIEW','FILL_READ_ATTEMPTS_EXHAUSTED')


@pytest.mark.parametrize('exchange,future',MARKETS)
@pytest.mark.parametrize('bad',['0','-1','Infinity','1e-129','9'*129])
def test_native_number_bounds_not_relaxed_by_derived_arithmetic(tmp_path,monkeypatch,exchange,future,bad):
    r=setup(tmp_path,monkeypatch,exchange,future)
    trades=r.state['order']['trades'] if exchange=='UPBIT' else r.state['trades']
    trades[0][{'UPBIT':'price','BYBIT':'execPrice','BINANCE':'price','OKX':'fillPx'}[exchange]]=bad
    feed(r);C.tick(r.st,r.store());DP.flush_outbox(r.st)
    assert confirmation_posts(r)==[]


@pytest.mark.parametrize('factor',['1','0.01','10'])
def test_okx_native_contract_count_converted_exactly_once(tmp_path,monkeypatch,factor):
    r=setup(tmp_path,monkeypatch,'OKX',True,side='buy')
    r.state['metadata']['ctVal']=factor
    r.info.contract_size=float(factor)
    for trade in r.state['trades']:
        trade['fillNotionalUsd']=str(D(trade['fillPx'])*D(trade['fillSz'])*D(factor))
    feed(r);tick(r);DP.flush_outbox(r.st)
    post=confirmation_posts(r);assert len(post)==1
    body=post[0]['embeds'][0]['description']
    amount=D('4')*D(factor);total=D('420')*D(factor)
    assert '실제 체결수량: '+format(amount.normalize(),'f')+' SOL' in body
    assert '계약 명목가치: '+format(total.normalize(),'f')+' USDT' in body


def test_real_read_proof_cannot_be_relabelled_financial_verified(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch);feed(r);tick(r)
    with r.store()._connect_read() as c:
        proof=json.loads(c.execute('SELECT proof FROM vpnl_fill_submissions').fetchone()[0])
        for flag in ('verification_status','fill_verification_status','pnl_verification_status','roi_verification_status'):
            payload=C.make_payload(proof);setattr(payload,flag,'VERIFIED')
            assert not C.validate_stored(r.store(),c,payload)


def test_previous_store_opens_additive_schema_and_fails_closed_on_new_pending_kind(tmp_path,monkeypatch):
    import subprocess,types
    r=setup(tmp_path,monkeypatch);feed(r);tick(r)
    with r.store()._connect_read() as c:before=c.execute('SELECT wire,wire_digest FROM vpnl_deliveries').fetchone()
    source=subprocess.check_output(['git','show','77ec5dabd00639e8e6cb4f1bb81f6c7bcca8e140:exchange/verified_pnl/delivery_store.py'],cwd=ROOT)
    legacy=types.ModuleType('exchange.verified_pnl._baseline_fill_store');legacy.__file__=str(ROOT/'exchange/verified_pnl/delivery_store.py')
    exec(compile(source,legacy.__file__,'exec'),legacy.__dict__)
    old=legacy.NotificationOutboxStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH)
    assert old.claim_next() is None
    with r.store()._connect_read() as c:
        assert c.execute('SELECT wire,wire_digest FROM vpnl_deliveries').fetchone()==before
        assert c.execute('SELECT status FROM vpnl_deliveries').fetchone()==('QUARANTINED',)
        assert c.execute('SELECT count(*) FROM vpnl_fill_submissions').fetchone()[0]==1
        assert c.execute('SELECT version FROM vpnl_schema').fetchone()==(4,)


# Genuine open -> done and partial -> done are not financial assertions.
# Native GET responses change; real Intake, durable retry state and Discord
# Outbox execute without mocking the confirmation validator or its ACK/CAS.

def _set_native_pending(r, *, partial=False):
    order=r.state['order']
    first=r.state['trades'][0] if r.info.exchange!='UPBIT' else order['trades'][0]
    q=r.amount/D(2)
    if r.info.exchange=='UPBIT':
        order['state']='wait'
        order['executed_volume']=str(q if partial else D(0))
        order['remaining_volume']=str(r.amount-(q if partial else D(0)))
        order['trades']=order['trades'][:1] if partial else []
        order['trades_count']=len(order['trades'])
        order['paid_fee']='0'
    elif r.info.exchange=='BYBIT':
        order['orderStatus']='PartiallyFilled' if partial else 'New'
        order['cumExecQty']=str(q if partial else D(0))
        order['cumExecValue']=str(D(first['execPrice'])*q if partial else D(0))
        order['avgPrice']=first['execPrice'] if partial else ''
        order['leavesQty']=str(r.amount-(q if partial else D(0)))
        r.state['trades']=r.state['trades'][:1] if partial else []
    elif r.info.exchange=='BINANCE':
        order['status']='PARTIALLY_FILLED' if partial else 'NEW'
        order['executedQty']=str(q if partial else D(0))
        order['cumQuote']=str(D(first['price'])*q if partial else D(0))
        order['cummulativeQuoteQty']=order['cumQuote']
        order['avgPrice']=first['price'] if partial else '0'
        r.state['trades']=r.state['trades'][:1] if partial else []
    else:
        order['state']='partially_filled' if partial else 'live'
        order['accFillSz']=first['fillSz'] if partial else '0'
        order['avgPx']=first['fillPx'] if partial else ''
        r.state['trades']=r.state['trades'][:1] if partial else []


@pytest.mark.parametrize('exchange,future',MARKETS)
@pytest.mark.parametrize('side',['buy','sell'])
@pytest.mark.parametrize('delay',[10,60,300,1800])
def test_limit_order_fills_after_delay(tmp_path,monkeypatch,exchange,future,side,delay):
    r=setup(tmp_path,monkeypatch,exchange,future,side,count=2)
    final=copy.deepcopy(r.state)
    _set_native_pending(r)
    clock=[time.time()]
    monkeypatch.setattr(C.time,'time',lambda:clock[0])
    feed(r)
    tick(r);DP.flush_outbox(r.st)
    with r.store()._connect_read() as c:
        assert c.execute('SELECT state,error,attempts FROM vpnl_fill_requests').fetchone()==('WAIT_READ','FILL_ORDER_PENDING',1)
    assert confirmation_posts(r)==[]
    clock[0]+=delay
    r.state.clear();r.state.update(final)
    tick(r);DP.flush_outbox(r.st)
    posts=confirmation_posts(r)
    assert len(posts)==1
    fields=posts[0]['embeds'][0]['description']
    label='계약 명목가치' if future else '실제 총 매수액' if side=='buy' else '실제 총 매도액'
    assert '실제 평균 체결가: '+str(r.average) in fields
    assert '실제 체결수량: '+str(r.amount) in fields
    assert label+': '+format(r.total.normalize(),'f') in fields
    tick(r);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1


@pytest.mark.parametrize('exchange,future',MARKETS)
@pytest.mark.parametrize('side',['buy','sell'])
def test_partial_order_becomes_complete(tmp_path,monkeypatch,exchange,future,side):
    r=setup(tmp_path,monkeypatch,exchange,future,side,count=2)
    final=copy.deepcopy(r.state)
    _set_native_pending(r,partial=True)
    clock=[time.time()]
    monkeypatch.setattr(C.time,'time',lambda:clock[0])
    feed(r);tick(r);DP.flush_outbox(r.st)
    assert confirmation_posts(r)==[]
    with r.store()._connect_read() as c:
        assert c.execute('SELECT state,error FROM vpnl_fill_requests').fetchone()==('WAIT_READ','FILL_ORDER_PENDING')
    clock[0]+=60
    r.state.clear();r.state.update(final)
    tick(r);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1


@pytest.mark.parametrize('exchange,future',MARKETS)
@pytest.mark.parametrize('failure',['429','timeout'])
def test_transient_api_failure_recovers_to_confirmed_fill(tmp_path,monkeypatch,exchange,future,failure):
    r=setup(tmp_path,monkeypatch,exchange,future)
    clock=[time.time()]
    monkeypatch.setattr(C.time,'time',lambda:clock[0])
    calls=[0]
    if exchange=='UPBIT':
        original=HTTPSOrderGETTransport.__call__
        def flaky(self,request):
            calls[0]+=1
            if calls[0]==1:
                if failure=='429':return OrderGETResponse(429,b'',{'retry-after':'60'})
                raise TimeoutError('SYNTHETIC_SECRET_NOT_TO_LOG')
            return original(self,request)
        monkeypatch.setattr(HTTPSOrderGETTransport,'__call__',flaky)
    else:
        import ccxt
        original=r.host._http
        def flaky(venue,signed):
            calls[0]+=1
            if calls[0]==1:
                cls=ccxt.RateLimitExceeded if failure=='429' else ccxt.RequestTimeout
                raise cls('SYNTHETIC_SECRET_NOT_TO_LOG')
            return original(venue,signed)
        monkeypatch.setattr(r.host,'_http',flaky)
    feed(r);tick(r);DP.flush_outbox(r.st)
    with r.store()._connect_read() as c:
        row=c.execute('SELECT state,error,attempts FROM vpnl_fill_requests').fetchone()
        assert row==('WAIT_READ','FILL_RATE_LIMITED' if failure=='429' else 'FILL_TIMEOUT',1)
    assert confirmation_posts(r)==[]
    clock[0]+=60
    tick(r);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1 and calls[0]>=2


@pytest.mark.parametrize('exchange,future',MARKETS)
def test_repeated_open_polls_do_not_exhaust_read_budget(tmp_path,monkeypatch,exchange,future):
    r=setup(tmp_path,monkeypatch,exchange,future)
    final=copy.deepcopy(r.state)
    _set_native_pending(r)
    clock=[time.time()]
    monkeypatch.setattr(C.time,'time',lambda:clock[0])
    feed(r)
    for _ in range(9):
        tick(r)
        clock[0]+=240
        with r.store()._connect_read() as c:
            state,error=c.execute('SELECT state,error FROM vpnl_fill_requests').fetchone()
            assert (state,error)==('WAIT_READ','FILL_ORDER_PENDING')
        assert confirmation_posts(r)==[]
    r.state.clear();r.state.update(final)
    tick(r);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1
    with r.store()._connect_read() as c:
        assert c.execute('SELECT attempts FROM vpnl_fill_requests').fetchone()[0]==10


def test_pending_then_one_abandoned_lease_still_recovers(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch)
    final=copy.deepcopy(r.state)
    _set_native_pending(r)
    clock=[time.time()]
    monkeypatch.setattr(C.time,'time',lambda:clock[0])
    feed(r)
    for _ in range(4):
        tick(r);clock[0]+=240
    with r.store()._connect() as c:
        c.execute("UPDATE vpnl_fill_requests SET state='READING',lease='synthetic-crash',expires=0,error='FILL_ORDER_PENDING'")
    tick(r)
    with r.store()._connect_read() as c:
        assert c.execute('SELECT state,error FROM vpnl_fill_requests').fetchone()==('WAIT_READ','FILL_READ_LEASE_EXPIRED')
    clock[0]+=10
    r.state.clear();r.state.update(final)
    tick(r);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1
