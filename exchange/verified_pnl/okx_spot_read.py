"""OKX V5 GET-only cash Spot capability; no transport, keys or finance engine.

A trusted host pins UID/mainUID/book and supplies independent archived HISTORY.
API return code, cursor end and account bills alone never certify acquisition.
"""
import copy,calendar,datetime,hashlib,re
from decimal import Decimal,localcontext
from types import SimpleNamespace as NS
from . import evidence as E
from .bootstrap import SpotScope,SpotReadAdapter,AuthenticatedAccount,OrderSnapshot,TradePage
from .history import HistoryReadAdapter,HistoryOrder,HistoryPage,order_fact

class OKXReadIncomplete(ValueError):pass

def number(v):
    try:
        if type(v) not in (str,int):raise ValueError()
        n=Decimal(v)
        if not n.is_finite() or len(n.as_tuple().digits)>128 or abs(n.as_tuple().exponent)>128:raise ValueError()
        return n
    except Exception:raise OKXReadIncomplete('OKX_NUMBER_INVALID') from None

def ident(v):
    if type(v) is not str or not re.fullmatch(r'[0-9]{1,40}',v):raise OKXReadIncomplete('OKX_ID_INVALID')
    return v

def client_id(v):
    if type(v) is not str or not re.fullmatch(r'[A-Za-z0-9]{0,32}',v):raise OKXReadIncomplete('OKX_CLIENT_ID_INVALID')
    return v

class OKXSpotReads:
    verbose=False
    PATHS=frozenset({'/api/v5/account/config','/api/v5/account/balance','/api/v5/public/instruments','/api/v5/trade/order','/api/v5/trade/orders-history','/api/v5/trade/orders-history-archive','/api/v5/trade/fills-history','/api/v5/account/bills-archive'})
    STATUS={'live':'open','partially_filled':'open','filled':'closed','canceled':'canceled'}
    def __init__(self,scope,get,*,main_uid,as_of_ms,inventory_sources=None,page_size=40):
        if not isinstance(scope,SpotScope) or (scope.exchange,scope.symbol,scope.instrument,scope.settlement)!=('OKX','BTC/USDT','BTC-USDT','USDT') or not scope.account.isdigit() or not str(main_uid).isdigit() or scope.subaccount!='uid:'+scope.account+':main:'+str(main_uid)+':spot':raise OKXReadIncomplete('OKX_SCOPE_UNSUPPORTED')
        if not callable(get) or type(as_of_ms) is not int or as_of_ms<=0 or type(page_size) is not int or not 1<=page_size<=100:raise OKXReadIncomplete('OKX_CAPABILITY_REQUIRED')
        self.scope=scope;self.main_uid=str(main_uid);self._get=get;self.as_of_ms=as_of_ms;self.inventory_sources=inventory_sources;self.page_size=page_size;self._cache={};self._boot=None;self._orders=()
    def _read(self,path,params):
        if path not in self.PATHS:raise OKXReadIncomplete('OKX_GET_DENIED')
        try:
            r=self._get(path,dict(params))
            if type(r) is not dict or r.get('code')!='0' or type(r.get('data')) is not list:raise ValueError()
            return copy.deepcopy(r['data'])
        except Exception:raise OKXReadIncomplete('OKX_READ_UNAVAILABLE') from None
    def identity(self,client):
        if client is not self:raise OKXReadIncomplete('OKX_CLIENT_SCOPE')
        rows=self._read('/api/v5/account/config',{})
        if len(rows)!=1:raise OKXReadIncomplete('OKX_ACCOUNT_MISSING')
        r=rows[0]
        if (r.get('uid'),r.get('mainUid'),r.get('acctLv'),r.get('perm'))!=(self.scope.account,self.main_uid,'1','read_only') or r.get('autoLoan') is not False or r.get('enableSpotBorrow') is not False or r.get('feeType') not in ('0','1'):raise OKXReadIncomplete('OKX_ACCOUNT_OR_PERMISSION')
        balance=self._read('/api/v5/account/balance',{'ccy':'BTC,USDT'})
        if len(balance)!=1 or type(balance[0].get('details')) is not list:raise OKXReadIncomplete('OKX_BALANCE_INCOMPLETE')
        coins=balance[0]['details']
        if len(coins)!=2 or {c.get('ccy') for c in coins}!={'BTC','USDT'}:raise OKXReadIncomplete('OKX_BALANCE_SCOPE')
        for c in coins:
            if number(c.get('cashBal'))<0 or number(c.get('liab'))!=0:raise OKXReadIncomplete('OKX_LIABILITY_OR_BALANCE')
        return AuthenticatedAccount('OKX',self.scope.account,self.scope.subaccount,frozenset({'read'}))
    def market(self,symbol):
        if symbol!=self.scope.symbol:raise OKXReadIncomplete('OKX_SYMBOL')
        rows=self._read('/api/v5/public/instruments',{'instType':'SPOT','instId':'BTC-USDT'})
        if len(rows)!=1 or (rows[0].get('instType'),rows[0].get('instId'),rows[0].get('baseCcy'),rows[0].get('quoteCcy'),rows[0].get('state'))!=('SPOT','BTC-USDT','BTC','USDT','live'):raise OKXReadIncomplete('OKX_INSTRUMENT')
        return dict(id='BTC-USDT',symbol='BTC/USDT',quote='USDT',spot=True,contract=False)
    def _floor(self):
        d=datetime.datetime.fromtimestamp(self.as_of_ms//1000,datetime.timezone.utc).replace(microsecond=(self.as_of_ms%1000)*1000)
        y,m=d.year,d.month-3
        if m<=0:y-=1;m+=12
        return int(d.replace(year=y,month=m,day=min(d.day,calendar.monthrange(y,m)[1])).timestamp())*1000+d.microsecond//1000
    def _window(self,start,end):
        if not self._floor()<=start<=end<=self.as_of_ms:raise OKXReadIncomplete('OKX_THREE_MONTH_COVERAGE_UNAVAILABLE')
    def _pages(self,path,params,key):
        out=[];last=None;seen=set()
        for _ in range(40):
            p=dict(params,limit=str(self.page_size))
            if last is not None:p['after']=str(last)
            rows=self._read(path,p)
            if len(rows)>self.page_size:raise OKXReadIncomplete('OKX_PAGE_LIMIT')
            for r in rows:
                val=ident(r.get(key));n=int(val)
                if val in seen or (last is not None and n>=last):raise OKXReadIncomplete('OKX_CURSOR_DUPLICATE_OR_ORDER')
                if 'begin' in params:
                    stamp=number(r['ts'] if key=='billId' else r['cTime'])
                    if stamp!=int(stamp) or not int(params['begin'])<=stamp<=int(params['end']):raise OKXReadIncomplete('OKX_PAGE_RANGE')
                seen.add(val);out.append(r);last=n
            if len(rows)<self.page_size:return out
        raise OKXReadIncomplete('OKX_PAGE_COMPLETENESS_UNPROVEN')
    def _order(self,r,oid):
        if ident(r.get('ordId'))!=oid or (r.get('instType'),r.get('instId'),r.get('tdMode'))!=('SPOT','BTC-USDT','cash') or r.get('side') not in ('buy','sell') or r.get('state') not in self.STATUS or r.get('tradeQuoteCcy') not in (None,'','USDT') or r.get('posSide') not in ('','net'):raise OKXReadIncomplete('OKX_ORDER_SCOPE')
        client_id(r.get('clOrdId'))
        if r.get('ordType') not in ('limit','post_only','fok','ioc','market') or (r['ordType']=='market' and r.get('tgtCcy')!='base_ccy'):raise OKXReadIncomplete('OKX_QUOTE_SIZE_UNSUPPORTED')
        if r.get('feeCcy')!='USDT' or any(r.get(k) is not None for k in ('fees','paid_fee')):raise OKXReadIncomplete('OKX_FEE_CURRENCY_OR_SCOPE')
        fee=number(r.get('fee'));rebate=Decimal(0) if r.get('rebate')=='' else number(r.get('rebate'))
        if rebate<0 or (rebate!=0 and r.get('rebateCcy')!='USDT') or (r['side']=='buy' and fee>0) or (r['side']=='sell' and rebate!=0):raise OKXReadIncomplete('OKX_SPLIT_REBATE_UNSUPPORTED')
        q=number(r['sz']);f=number(r['accFillSz']);avg=number(r['avgPx']) if f else Decimal(0)
        if q<=0 or not 0<=f<=q or (f>0 and avg<=0):raise OKXReadIncomplete('OKX_ORDER_QUANTITY')
        created=number(r['cTime']);updated=number(r['uTime']);filled=number(r['fillTime']) if f else created
        if any(v!=int(v) for v in (created,updated,filled)) or not 0<=created<=filled<=updated<=self.as_of_ms:raise OKXReadIncomplete('OKX_ORDER_TIME')
        with localcontext() as arithmetic:
            arithmetic.prec=512;remaining=q-f;cost=-(fee+rebate)
        data=dict(id=oid,clientOrderId=r['clOrdId'],symbol='BTC/USDT',side=r['side'],status=self.STATUS[r['state']],amount=str(q),filled=str(f),remaining=str(remaining),average=str(avg),timestamp=int(created),lastTradeTimestamp=int(filled),type=r['ordType'],fee=dict(cost=str(cost),currency='USDT'),lastFillQty=str(number(r['fillSz'])) if f else '0',lastTradeId=ident(r['tradeId']) if f else '',nativeFee=str(fee),nativeRebate=str(rebate))
        return data
    def read_order(self,client,oid,symbol):
        self.identity(client);oid=ident(oid)
        if symbol!=self.scope.symbol:raise OKXReadIncomplete('OKX_SYMBOL')
        rows=self._read('/api/v5/trade/order',{'instId':'BTC-USDT','ordId':oid})
        if len(rows)!=1:raise OKXReadIncomplete('OKX_ORDER_MISSING_OR_AMBIGUOUS')
        data=self._order(rows[0],oid);self._window(data['timestamp'],self.as_of_ms)
        return OrderSnapshot(data,self.scope.account,self.scope.subaccount,E.digest(data))
    def _trade(self,r,order=None):
        oid=ident(r.get('ordId'));tid=ident(r.get('tradeId'));bid=ident(r.get('billId'));client_id(r.get('clOrdId'))
        if (r.get('instType'),r.get('instId'),r.get('feeCcy'))!=('SPOT','BTC-USDT','USDT') or r.get('side') not in ('buy','sell') or r.get('execType') not in ('M','T') or r.get('tradeQuoteCcy') not in (None,'','USDT') or r.get('tdMode') not in (None,'cash') or any(r.get(k) is not None for k in ('fillFee','fillFeeCcy','fees','paid_fee')):raise OKXReadIncomplete('OKX_FILL_SCOPE_OR_FEE')
        if order and (oid!=order['id'] or r['clOrdId']!=order['clientOrderId'] or r['side']!=order['side']):raise OKXReadIncomplete('OKX_FILL_ORDER_BINDING')
        p=number(r['fillPx']);q=number(r['fillSz']);fee=number(r['fee']);t=number(r['fillTime']);generated=number(r['ts'])
        if p<=0 or q<=0 or t!=int(t) or generated!=int(generated) or not 0<=t<=generated<=self.as_of_ms:raise OKXReadIncomplete('OKX_FILL_VALUE_OR_TIME')
        if 'accFillSz' in r:raise OKXReadIncomplete('OKX_ORDER_FILL_SCHEMA_MIX')
        # Composite economic execution identity; billId is a pagination/accounting ID.
        return dict(id=oid+':'+tid,order=oid,symbol='BTC/USDT',price=str(p),amount=str(q),timestamp=int(t),fee=dict(cost=str(fee.copy_negate()),currency='USDT'))
    def read_page(self,client,oid,symbol,cursor,snapshot):
        self.identity(client);order=self.read_order(client,oid,symbol).order
        if E.digest(order)!=snapshot:raise OKXReadIncomplete('OKX_UNSTABLE_ORDER')
        if cursor is None:
            raw=self._pages('/api/v5/trade/fills-history',dict(instType='SPOT',instId='BTC-USDT',ordId=oid,begin=str(order['timestamp']),end=str(self.as_of_ms)),'billId')
            rows=[self._trade(r,order) for r in raw]
            if len({t['id'] for t in rows})!=len(rows):raise OKXReadIncomplete('OKX_DUPLICATE_EXECUTION')
            rows.sort(key=lambda t:(t['timestamp'],int(t['id'].split(':')[1])))
            if rows:
                last=[t for t in rows if t['id']==oid+':'+order['lastTradeId']]
                if len(last)!=1 or last[0]['timestamp']!=order['lastTradeTimestamp'] or max(t['timestamp'] for t in rows)!=order['lastTradeTimestamp'] or Decimal(last[0]['amount'])!=Decimal(order['lastFillQty']):raise OKXReadIncomplete('OKX_LAST_FILL_CONFLICT')
            with localcontext() as arithmetic:
                arithmetic.prec=512
                debit=sum((number(r['fee']) for r in raw if number(r['fee'])<0),Decimal(0));credit=sum((number(r['fee']) for r in raw if number(r['fee'])>0),Decimal(0))
                if order['side']=='buy' and (debit!=Decimal(order['nativeFee']) or credit!=Decimal(order['nativeRebate'])):raise OKXReadIncomplete('OKX_SPLIT_FEE_CONFLICT')
            for key in list(self._cache):
                if key[0]==oid:self._cache.pop(key)
            while len(self._cache)>=8:self._cache.pop(next(iter(self._cache)))
            self._cache[(oid,snapshot)]=rows;offset=0
        else:
            if type(cursor) is not str or not re.fullmatch(r'offset:[0-9]+',cursor):raise OKXReadIncomplete('OKX_CURSOR')
            offset=int(cursor.split(':')[1])
        rows=self._cache.get((oid,snapshot))
        if rows is None or not 0<=offset<=len(rows):raise OKXReadIncomplete('OKX_CURSOR')
        return TradePage(copy.deepcopy(rows[offset:offset+100]),self.scope.account,self.scope.subaccount,oid,symbol,snapshot,cursor,'offset:'+str(offset+100) if offset+100<len(rows) else None)
    def execution_port(self):return SpotReadAdapter(self,'okx-v5-cash-spot-usdt-v1',self.identity,self.read_order,self.read_page,'ORDER_LIFETIME_QUOTE_V1',{})
    def history_port(self,boot):
        if not isinstance(self.inventory_sources,HistoryReadAdapter) or boot.scope!=self.scope or boot.adapter.client is not self:raise OKXReadIncomplete('OKX_INDEPENDENT_HISTORY_REQUIRED')
        self._boot=boot
        return HistoryReadAdapter('okx-v5-independent-archive-v1',self._coverage,self.inventory_sources.balance,self.inventory_sources.inventory,self._history_page)
    def _context(self):
        c=self._boot('OKX',self.scope.account,NS(is_buy=True,is_sell=False,side='buy',is_futures=False,is_coinm=False,unified_symbol='BTC/USDT'),self)
        if c is None:raise OKXReadIncomplete('OKX_CONTEXT_UNAVAILABLE')
        return c
    def _bills(self,start,end,raw_fills):
        expected={}
        with localcontext() as arithmetic:
            arithmetic.prec=512
            for r in raw_fills:expected[r['ordId']]=expected.get(r['ordId'],Decimal(0))+number(r['fee'])
            found={}
            for coin in ('BTC','USDT'):
                rows=self._pages('/api/v5/account/bills-archive',dict(instType='SPOT',ccy=coin,begin=str(start),end=str(end)),'billId')
                for r in rows:
                    if (r.get('instType'),r.get('instId'),r.get('mgnMode'),r.get('type'),r.get('ccy'))!=('SPOT','BTC-USDT','cash','2',coin) or r.get('ordId') not in expected:raise OKXReadIncomplete('OKX_BILLS_UNEXPLAINED_MOVEMENT')
                    fee=number(r['fee'])
                    if coin=='BTC' and fee!=0:raise OKXReadIncomplete('OKX_BILLS_NONQUOTE_FEE')
                    if coin=='USDT':found[r['ordId']]=found.get(r['ordId'],Decimal(0))+fee
            if found!=expected:raise OKXReadIncomplete('OKX_BILLS_FEE_CONFLICT')
    def _coverage(self,client,scope):
        self.identity(client);c=self._context()
        if E.canonical(scope)!=c.scope_json:raise OKXReadIncomplete('OKX_HISTORY_SCOPE')
        audit=self.inventory_sources.coverage(client,scope)
        closing=self.inventory_sources.balance(client,scope,'closing')
        now=self._read('/api/v5/account/balance',{'ccy':'BTC,USDT'})
        if closing is None or len(now)!=1 or type(now[0].get('details')) is not list:raise OKXReadIncomplete('OKX_CLOSING_BALANCE_UNPROVEN')
        btc=[r for r in now[0]['details'] if r.get('ccy')=='BTC']
        if len(btc)!=1 or number(btc[0].get('cashBal'))!=number(closing.quantity):raise OKXReadIncomplete('OKX_CLOSING_BALANCE_CONFLICT')
        start=int(audit.start*1000);end=int(audit.through*1000);self._window(start,end)
        raw=self._pages('/api/v5/trade/fills-history',dict(instType='SPOT',instId='BTC-USDT',begin=str(start),end=str(end)),'billId')
        self._bills(start,end,raw)
        path='/api/v5/trade/orders-history' if self.as_of_ms-start<=7*86400000 else '/api/v5/trade/orders-history-archive'
        listed=self._pages(path,dict(instType='SPOT',instId='BTC-USDT',begin=str(start),end=str(end)),'ordId')
        ids={ident(r['ordId']) for r in listed if number(r['accFillSz'])>0}|{ident(r['ordId']) for r in raw}
        from .verifier import FillVerifier
        fills={};trades={}
        for oid in sorted(ids,key=int):
            summary=self.read_order(self,oid,self.scope.symbol).order
            for r in listed:
                if r['ordId']==oid and self._order(r,oid)!=summary:raise OKXReadIncomplete('OKX_HISTORY_ORDER_CONFLICT')
            info=NS(is_buy=summary['side']=='buy',is_sell=summary['side']=='sell',side=summary['side'],is_close=False,is_futures=False,is_coinm=False,unified_symbol='BTC/USDT')
            f=FillVerifier().verify_order_result(NS(client=self),'OKX',self.scope.symbol,{'id':oid},info,c)
            if not E.execution_valid(f,scope) or not f.trades:raise OKXReadIncomplete('OKX_HISTORY_EXECUTION')
            for t in f.trades:
                if t.id in trades or t.timestamp is None or not audit.start<=t.timestamp<=audit.through:raise OKXReadIncomplete('OKX_HISTORY_FILL_COVERAGE')
                trades[t.id]=dict(order=oid,price=t.price,amount=t.amount,fee=t.fee,currency=t.fee_currency,timestamp=t.timestamp)
            fills[oid]=f
        account={}
        for r in raw:
            t=self._trade(r)
            if t['id'] in account:raise OKXReadIncomplete('OKX_DUPLICATE_EXECUTION')
            account[t['id']]=dict(order=t['order'],price=Decimal(t['price']),amount=Decimal(t['amount']),fee=Decimal(t['fee']['cost']),currency=t['fee']['currency'],timestamp=t['timestamp']/1000)
        if trades!=account:raise OKXReadIncomplete('OKX_ACCOUNT_FILL_CONFLICT')
        ids=sorted(fills,key=lambda oid:(min(t.timestamp for t in fills[oid].trades),int(oid)));digest=hashlib.sha256()
        for i,oid in enumerate(ids,1):digest.update((E.canonical(order_fact(fills[oid],i))+'\n').encode())
        if (audit.order_count,audit.trade_count,audit.order_digest)!=(len(fills),len(trades),digest.hexdigest()):raise OKXReadIncomplete('OKX_ARCHIVED_COVERAGE_CONFLICT')
        self._orders=tuple(HistoryOrder(i,oid,fills[oid].order_side,scope['book']) for i,oid in enumerate(ids,1))
        return audit
    def _history_page(self,client,scope,after,limit):
        self.identity(client)
        if E.canonical(scope)!=self._context().scope_json or type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=40:raise OKXReadIncomplete('OKX_HISTORY_PAGE')
        return HistoryPage(scope,after,self._orders[after:after+limit],'okx-verified-order-enumeration')
