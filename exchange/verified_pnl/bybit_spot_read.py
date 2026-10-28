"""Bybit V5 cash Spot, one pinned UTA2 account, GET capability only.

No HTTP/signing/finance engine. Independent archives are mandatory for HISTORY.
Read capability and account registry must be supplied by a trusted host.
"""
import copy,re,hashlib
from decimal import Decimal,localcontext
from types import SimpleNamespace as NS
from . import evidence as E
from .bootstrap import SpotScope,AuthenticatedAccount,SpotReadAdapter,OrderSnapshot,TradePage
from .history import HistoryReadAdapter,HistoryOrder,HistoryPage,order_fact

class BybitReadIncomplete(ValueError):pass

def decimal(value):
    try:
        if type(value) not in (str,int):raise ValueError()
        n=Decimal(value)
        if not n.is_finite() or len(n.as_tuple().digits)>128 or abs(n.as_tuple().exponent)>128:raise ValueError()
        return n
    except Exception:raise BybitReadIncomplete('BYBIT_NUMBER_INVALID') from None

def token(value,empty=False):
    if value=='' and empty:return ''
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):raise BybitReadIncomplete('BYBIT_ID_INVALID')
    return value

class BybitSpotReads:
    verbose=False
    PATHS=frozenset({'/v5/user/query-api','/v5/account/info','/v5/account/wallet-balance','/v5/market/instruments-info','/v5/order/realtime','/v5/order/history','/v5/execution/list'})
    STATUS={'New':'open','PartiallyFilled':'open','Filled':'closed','Cancelled':'canceled','PartiallyFilledCanceled':'canceled','Rejected':'rejected'}
    def __init__(self,scope,get,*,parent_uid,as_of_ms,inventory_sources=None,page_size=40):
        if not isinstance(scope,SpotScope) or (scope.exchange,scope.symbol,scope.instrument,scope.settlement)!=('BYBIT','BTC/USDT','BTCUSDT','USDT') or not scope.account.isdigit() or not str(parent_uid).isdigit() or scope.subaccount!='uid:'+scope.account+':parent:'+str(parent_uid)+':UTA2':raise BybitReadIncomplete('BYBIT_SCOPE_UNSUPPORTED')
        if not callable(get) or type(as_of_ms) is not int or as_of_ms<=0 or type(page_size) is not int or not 1<=page_size<=50:raise BybitReadIncomplete('BYBIT_CAPABILITY_REQUIRED')
        self.scope=scope;self.parent_uid=str(parent_uid);self._get=get;self.as_of_ms=as_of_ms;self.page_size=page_size;self.inventory_sources=inventory_sources;self._cache={};self._history_orders=();self._boot=None
    def _read(self,path,params):
        if path not in self.PATHS:raise BybitReadIncomplete('BYBIT_GET_DENIED')
        try:
            raw=self._get(path,dict(params))
            if type(raw) is not dict or type(raw.get('retCode')) is not int or raw['retCode']!=0 or type(raw.get('result')) is not dict:raise ValueError()
            result=raw['result']
            if 'category' in params and result.get('category')!='spot':raise ValueError()
            return copy.deepcopy(result)
        except Exception:raise BybitReadIncomplete('BYBIT_READ_UNAVAILABLE') from None
    def identity(self,client):
        if client is not self:raise BybitReadIncomplete('BYBIT_CLIENT_SCOPE')
        r=self._read('/v5/user/query-api',{})
        if str(r.get('userID'))!=self.scope.account or str(r.get('parentUid'))!=self.parent_uid or type(r.get('readOnly')) is not int or r['readOnly']!=1 or type(r.get('uta')) is not int or r['uta']!=1 or type(r.get('isMaster')) is not bool or r['isMaster']!=(self.parent_uid=='0'):raise BybitReadIncomplete('BYBIT_ACCOUNT_OR_PERMISSION')
        p=r.get('permissions')
        if type(p) is not dict or p.get('Spot')!=['SpotTrade'] or any(type(v) is not list or (k!='Spot' and v!=[]) for k,v in p.items()) or not {'Spot','Wallet','ContractTrade','Options','Derivatives'}<=set(p):raise BybitReadIncomplete('BYBIT_PERMISSION_SCOPE')
        a=self._read('/v5/account/info',{})
        if type(a.get('unifiedMarginStatus')) is not int or a['unifiedMarginStatus']!=5 or a.get('marginMode')!='REGULAR_MARGIN' or a.get('spotHedgingStatus')!='OFF' or a.get('isMasterTrader') is not False:raise BybitReadIncomplete('BYBIT_ACCOUNT_MODE_UNSUPPORTED')
        w=self._read('/v5/account/wallet-balance',{'accountType':'UNIFIED','coin':'BTC,USDT'})
        rows=w.get('list')
        if type(rows) is not list or len(rows)!=1 or rows[0].get('accountType')!='UNIFIED':raise BybitReadIncomplete('BYBIT_WALLET_SCOPE')
        coins=rows[0].get('coin')
        if type(coins) is not list or {c.get('coin') for c in coins}!={'BTC','USDT'} or len(coins)!=2:raise BybitReadIncomplete('BYBIT_WALLET_COVERAGE')
        for c in coins:
            if any(decimal(c.get(k))!=0 for k in ('borrowAmount','spotBorrow','accruedInterest','totalPositionIM','unrealisedPnl')):raise BybitReadIncomplete('BYBIT_BORROW_OR_POSITION')
        return AuthenticatedAccount('BYBIT',self.scope.account,self.scope.subaccount,frozenset({'read'}))
    def market(self,symbol):
        if symbol!=self.scope.symbol:raise BybitReadIncomplete('BYBIT_SYMBOL')
        r=self._read('/v5/market/instruments-info',{'category':'spot','symbol':'BTCUSDT'});rows=r.get('list')
        if type(rows) is not list or len(rows)!=1 or (rows[0].get('symbol'),rows[0].get('baseCoin'),rows[0].get('quoteCoin'),rows[0].get('status'))!=('BTCUSDT','BTC','USDT','Trading'):raise BybitReadIncomplete('BYBIT_INSTRUMENT')
        return dict(id='BTCUSDT',symbol='BTC/USDT',quote='USDT',spot=True,contract=False)
    def _pages(self,path,params,key,stamp):
        cursor=None;seen_cursor=set();seen_ids=set();out=[];last=None
        for _ in range(40):
            p=dict(params,category='spot',symbol='BTCUSDT',limit=self.page_size)
            if cursor is not None:p['cursor']=cursor
            r=self._read(path,p);rows=r.get('list');nxt=r.get('nextPageCursor')
            if type(rows) is not list or len(rows)>self.page_size or type(nxt) is not str or len(nxt)>2048 or (nxt and (not rows or nxt in seen_cursor)):raise BybitReadIncomplete('BYBIT_PAGE_INCOMPLETE')
            for row in rows:
                ident=token(row.get(key));t=decimal(row.get(stamp))
                if t!=int(t) or t<0 or t>self.as_of_ms or ident in seen_ids or row.get('symbol')!='BTCUSDT':raise BybitReadIncomplete('BYBIT_PAGE_ID_OR_TIME')
                if last is not None and t>last:raise BybitReadIncomplete('BYBIT_PAGE_ORDER')
                if 'startTime' in params and not params['startTime']<=t<=params['endTime']:raise BybitReadIncomplete('BYBIT_PAGE_RANGE')
                seen_ids.add(ident);out.append(row);last=t
            if not nxt:return out
            seen_cursor.add(nxt);cursor=nxt
        raise BybitReadIncomplete('BYBIT_PAGE_LIMIT')
    def _window(self,start,end):
        if not 0<=start<=end<=self.as_of_ms or end-start>7*86400000:raise BybitReadIncomplete('BYBIT_SEVEN_DAY_COVERAGE_REQUIRED')
    def _order(self,r,oid):
        if token(r.get('orderId'))!=oid or r.get('symbol')!='BTCUSDT' or r.get('side') not in ('Buy','Sell') or r.get('orderStatus') not in self.STATUS or r.get('isLeverage')!='0' or r.get('orderFilter')!='Order' or r.get('extraFees') not in ('',None):raise BybitReadIncomplete('BYBIT_ORDER_SCOPE')
        token(r.get('orderLinkId'),True)
        if any(r.get(k) is not None for k in ('fee','fees','paid_fee')):raise BybitReadIncomplete('BYBIT_DUPLICATE_FEE_SOURCE')
        fees=r.get('cumFeeDetail')
        if type(fees) is not dict or set(fees)!={'USDT'}:raise BybitReadIncomplete('BYBIT_FEE_SOURCE_OR_CURRENCY')
        fee=decimal(fees['USDT']);q=decimal(r['qty']);f=decimal(r['cumExecQty']);v=decimal(r['cumExecValue'])
        with localcontext() as ctx:
            ctx.prec=512;avg=v/f if f else Decimal(0);remaining=q-f
        if not q>0 or not 0<=f<=q or v<0 or (f==0 and v!=0) or (f>0 and decimal(r['avgPrice'])!=avg):raise BybitReadIncomplete('BYBIT_SUMMARY_CONFLICT')
        if decimal(r['leavesQty'])!=remaining:raise BybitReadIncomplete('BYBIT_REMAINING_CONFLICT')
        for k in ('createdTime','updatedTime'):
            t=decimal(r[k])
            if t!=int(t) or not 0<=t<=self.as_of_ms:raise BybitReadIncomplete('BYBIT_ORDER_TIME')
        return dict(id=oid,clientOrderId=r['orderLinkId'],symbol='BTC/USDT',side=r['side'].lower(),status=self.STATUS[r['orderStatus']],amount=str(q),filled=str(f),remaining=str(remaining),average=str(avg),type=str(r.get('orderType','Limit')).lower(),timestamp=int(r['createdTime']),lastTradeTimestamp=int(r['updatedTime']),fee=dict(cost=str(fee),currency='USDT'))
    def read_order(self,client,oid,symbol):
        self.identity(client);oid=token(oid)
        if symbol!=self.scope.symbol:raise BybitReadIncomplete('BYBIT_SYMBOL')
        recent=self._pages('/v5/order/realtime',{'orderId':oid},'orderId','createdTime')
        historic=self._pages('/v5/order/history',{'orderId':oid},'orderId','createdTime')
        if any(r.get('orderId')!=oid for r in recent+historic) or len(recent)>1 or len(historic)>1 or not recent+historic:raise BybitReadIncomplete('BYBIT_ORDER_MISSING_OR_AMBIGUOUS')
        values=[self._order(r,oid) for r in recent+historic]
        if any(v!=values[0] for v in values):raise BybitReadIncomplete('BYBIT_LATEST_HISTORY_CONFLICT')
        return OrderSnapshot(values[0],self.scope.account,self.scope.subaccount,E.digest(values[0]))
    def _trade(self,r,order=None):
        oid=token(r.get('orderId'));tid=token(r.get('execId'));link=token(r.get('orderLinkId'),True)
        if r.get('symbol')!='BTCUSDT' or r.get('execType')!='Trade' or r.get('side') not in ('Buy','Sell') or r.get('feeCurrency')!='USDT' or r.get('extraFees') not in ('',None) or r.get('execFeeV2') not in ('',None,'0'):raise BybitReadIncomplete('BYBIT_EXECUTION_SOURCE')
        if order and (oid!=order['id'] or link!=order['clientOrderId'] or r['side'].lower()!=order['side']):raise BybitReadIncomplete('BYBIT_EXECUTION_ORDER_LINK')
        if any(r.get(k) is not None for k in ('fee','fees','paid_fee')):raise BybitReadIncomplete('BYBIT_DUPLICATE_FEE_SOURCE')
        p=decimal(r['execPrice']);q=decimal(r['execQty']);f=decimal(r['execFee']);t=decimal(r['execTime'])
        with localcontext() as ctx:
            ctx.prec=512
            if p<=0 or q<=0 or decimal(r['execValue'])!=p*q or t!=int(t) or not 0<=t<=self.as_of_ms:raise BybitReadIncomplete('BYBIT_EXECUTION_VALUE')
        return dict(id=tid,order=oid,symbol='BTC/USDT',price=str(p),amount=str(q),timestamp=int(t),fee=dict(cost=str(f),currency='USDT'))
    def read_page(self,client,oid,symbol,cursor,snapshot):
        self.identity(client)
        if symbol!=self.scope.symbol:raise BybitReadIncomplete('BYBIT_SYMBOL')
        order=self.read_order(client,oid,symbol).order
        if E.digest(order)!=snapshot:raise BybitReadIncomplete('BYBIT_UNSTABLE_ORDER')
        if cursor is None:
            self._window(order['timestamp'],self.as_of_ms)
            raw=self._pages('/v5/execution/list',dict(orderId=oid,startTime=order['timestamp'],endTime=self.as_of_ms),'execId','execTime')
            trades=[self._trade(t,order) for t in raw];trades.sort(key=lambda t:(t['timestamp'],t['id']))
            for key in list(self._cache):
                if key[0]==oid:self._cache.pop(key)
            while len(self._cache)>=8:self._cache.pop(next(iter(self._cache)))
            self._cache[(oid,snapshot)]=trades
            offset=0
        else:
            if not re.fullmatch(r'offset:[0-9]+',cursor):raise BybitReadIncomplete('BYBIT_CURSOR')
            offset=int(cursor.split(':')[1])
        rows=self._cache.get((oid,snapshot))
        if rows is None or offset>len(rows):raise BybitReadIncomplete('BYBIT_CURSOR')
        selected=copy.deepcopy(rows[offset:offset+100]);nxt='offset:'+str(offset+100) if offset+100<len(rows) else None
        return TradePage(selected,self.scope.account,self.scope.subaccount,oid,symbol,snapshot,cursor,nxt)
    def execution_port(self):return SpotReadAdapter(self,'bybit-v5-cash-spot-usdt-v1',self.identity,self.read_order,self.read_page,'ORDER_LIFETIME_QUOTE_V1',{})
    def history_port(self,boot):
        if not isinstance(self.inventory_sources,HistoryReadAdapter) or boot.scope!=self.scope or boot.adapter.client is not self:raise BybitReadIncomplete('BYBIT_INDEPENDENT_HISTORY_REQUIRED')
        self._boot=boot
        return HistoryReadAdapter('bybit-v5-independent-archive-v1',self._coverage,self.inventory_sources.balance,self.inventory_sources.inventory,self._history_page)
    def _context(self):
        info=NS(is_buy=True,is_sell=False,side='buy',is_futures=False,is_coinm=False,unified_symbol='BTC/USDT')
        c=self._boot('BYBIT',self.scope.account,info,self)
        if c is None:raise BybitReadIncomplete('BYBIT_CONTEXT_UNAVAILABLE')
        return c
    def _coverage(self,client,scope):
        self.identity(client);c=self._context()
        if E.canonical(scope)!=c.scope_json:raise BybitReadIncomplete('BYBIT_HISTORY_SCOPE')
        audit=self.inventory_sources.coverage(client,scope);start=int(audit.start*1000);end=int(audit.through*1000);self._window(start,end)
        listed=self._pages('/v5/order/history',dict(startTime=start,endTime=end),'orderId','createdTime')
        raw=self._pages('/v5/execution/list',dict(startTime=start,endTime=end),'execId','execTime')
        ids={token(r['orderId']) for r in listed if decimal(r['cumExecQty'])>0}|{token(r['orderId']) for r in raw}
        from .verifier import FillVerifier
        fills={};all_trades={}
        for oid in sorted(ids):
            summary=self.read_order(self,oid,'BTC/USDT').order
            for r in listed:
                if r['orderId']==oid and self._order(r,oid)!=summary:raise BybitReadIncomplete('BYBIT_HISTORY_SUMMARY_CONFLICT')
            info=NS(is_buy=summary['side']=='buy',is_sell=summary['side']=='sell',side=summary['side'],is_close=False,is_futures=False,is_coinm=False,unified_symbol='BTC/USDT')
            f=FillVerifier().verify_order_result(NS(client=self),'BYBIT','BTC/USDT',{'id':oid},info,c)
            if not E.execution_valid(f,scope) or not f.trades:raise BybitReadIncomplete('BYBIT_HISTORY_EXECUTION')
            for t in f.trades:
                if t.id in all_trades or t.timestamp is None or not audit.start<=t.timestamp<=audit.through:raise BybitReadIncomplete('BYBIT_HISTORY_TRADE_SCOPE')
                all_trades[t.id]=dict(order=oid,price=t.price,amount=t.amount,fee=t.fee,currency=t.fee_currency,timestamp=t.timestamp)
            fills[oid]=f
        observed={}
        for r in raw:
            t=self._trade(r)
            observed[t['id']]=dict(order=t['order'],price=Decimal(t['price']),amount=Decimal(t['amount']),fee=Decimal(t['fee']['cost']),currency=t['fee']['currency'],timestamp=t['timestamp']/1000)
        if observed!=all_trades:raise BybitReadIncomplete('BYBIT_ACCOUNT_TRADE_CONFLICT')
        order_ids=sorted(fills,key=lambda oid:(min(t.timestamp for t in fills[oid].trades),oid));digest=hashlib.sha256()
        for i,oid in enumerate(order_ids,1):digest.update((E.canonical(order_fact(fills[oid],i))+'\n').encode())
        if (audit.order_count,audit.trade_count,audit.order_digest)!=(len(fills),len(all_trades),digest.hexdigest()):raise BybitReadIncomplete('BYBIT_ARCHIVE_CONFLICT')
        self._history_orders=tuple(HistoryOrder(i,oid,fills[oid].order_side,scope['book']) for i,oid in enumerate(order_ids,1))
        return audit
    def _history_page(self,client,scope,after,limit):
        self.identity(client)
        if E.canonical(scope)!=self._context().scope_json or type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=40:raise BybitReadIncomplete('BYBIT_HISTORY_PAGE')
        return HistoryPage(scope,after,self._history_orders[after:after+limit],'bybit-v5-enumerated-order-page')
