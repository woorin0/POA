"""Binance BTC/USDT spot GET-only normalization, with no credentials/network client.

The host supplies authenticated GET capability and an INDEPENDENT HistoryReadAdapter
for archived balances, coverage, movements and ownership. No native endpoint alone
proves those facts. DTO completion flags are never authority. REAL_DATA is unverified.
"""
import copy
from dataclasses import replace
from decimal import Decimal, localcontext
from types import SimpleNamespace
from . import evidence as E
from .bootstrap import SpotScope, SpotReadAdapter, AuthenticatedAccount, OrderSnapshot, TradePage
from .history import HistoryReadAdapter, HistoryPage, HistoryOrder, order_fact

class BinanceReadIncomplete(ValueError):
    pass


def number(value):
    if type(value) not in (str,int):raise BinanceReadIncomplete('BINANCE_NUMERIC_TYPE')
    try:
        result=Decimal(value)
        if not result.is_finite():raise ValueError()
        return result
    except Exception:raise BinanceReadIncomplete('BINANCE_NUMERIC_INVALID') from None


def identifier(value):
    if type(value) not in (str,int) or not str(value).isdigit():raise BinanceReadIncomplete('BINANCE_ID_INVALID')
    return str(int(value))


class BinanceSpotReads:
    """Registered host read capability; never accepts a result DTO as evidence."""
    verbose=False
    PATHS=frozenset({'/api/v3/account','/sapi/v1/account/apiRestrictions','/api/v3/exchangeInfo','/api/v3/order','/api/v3/myTrades','/api/v3/allOrders','/sapi/v1/capital/deposit/hisrec','/sapi/v1/capital/withdraw/history','/sapi/v1/asset/transfer'})
    STATUS={'NEW':'open','PENDING_NEW':'open','PARTIALLY_FILLED':'open','FILLED':'closed','CANCELED':'canceled','EXPIRED':'expired','EXPIRED_IN_MATCH':'expired','REJECTED':'rejected'}
    def __init__(self,scope,get,*,inventory_sources=None,page_size=40):
        if not isinstance(scope,SpotScope) or (scope.exchange,scope.symbol,scope.instrument,scope.settlement)!=('BINANCE','BTC/USDT','BTCUSDT','USDT') or not scope.account.isdigit() or scope.subaccount!='uid:'+scope.account:raise BinanceReadIncomplete('BINANCE_SCOPE_UNSUPPORTED')
        if not callable(get) or type(page_size) is not int or not 1<=page_size<=100:raise BinanceReadIncomplete('BINANCE_READ_CAPABILITY_REQUIRED')
        self.scope=scope;self._get=get;self.inventory_sources=inventory_sources;self.page_size=page_size;self._history_orders=();self._bootstrap=None
    def _read(self,path,params):
        if path not in self.PATHS:raise BinanceReadIncomplete('BINANCE_GET_NOT_ALLOWED')
        try:
            result=self._get(path,dict(params))
            if not isinstance(result,(dict,list)) or (isinstance(result,dict) and 'code' in result and int(result['code'])<0):raise ValueError()
            return copy.deepcopy(result)
        except Exception:raise BinanceReadIncomplete('BINANCE_READ_UNAVAILABLE') from None
    def identity(self,client):
        if client is not self:raise BinanceReadIncomplete('BINANCE_CLIENT_MISMATCH')
        account=self._read('/api/v3/account',{});rights=self._read('/sapi/v1/account/apiRestrictions',{})
        required=('enableSpotAndMarginTrading','enableWithdrawals','enableMargin','enableFutures','enableInternalTransfer','permitsUniversalTransfer')
        if str(account.get('uid'))!=self.scope.account or account.get('accountType')!='SPOT' or rights.get('enableReading') is not True or any(rights.get(k) is not False for k in required):raise BinanceReadIncomplete('BINANCE_IDENTITY_OR_RIGHTS')
        if any(v is True for k,v in rights.items() if k.startswith('enable') and k!='enableReading'):raise BinanceReadIncomplete('BINANCE_WRITE_PERMISSION')
        return AuthenticatedAccount('BINANCE',self.scope.account,self.scope.subaccount,frozenset({'read'}))
    def market(self,symbol):
        if symbol!=self.scope.symbol:raise BinanceReadIncomplete('BINANCE_SYMBOL')
        raw=self._read('/api/v3/exchangeInfo',{'symbol':'BTCUSDT'});rows=raw.get('symbols')
        if not isinstance(rows,list) or len(rows)!=1:raise BinanceReadIncomplete('BINANCE_MARKET')
        m=rows[0]
        if (m.get('symbol'),m.get('baseAsset'),m.get('quoteAsset'),m.get('isSpotTradingAllowed'))!=('BTCUSDT','BTC','USDT',True):raise BinanceReadIncomplete('BINANCE_MARKET')
        return dict(id='BTCUSDT',symbol='BTC/USDT',spot=True,contract=False,quote='USDT')
    def _normalize_order(self,raw,oid):
        if raw.get('symbol')!='BTCUSDT' or identifier(raw.get('orderId'))!=oid or raw.get('side') not in ('BUY','SELL') or raw.get('status') not in self.STATUS:raise BinanceReadIncomplete('BINANCE_ORDER_SCOPE')
        if any(raw.get(k) is not None for k in ('fee','fees','paid_fee','commission','fills')):raise BinanceReadIncomplete('BINANCE_ORDER_FEE_SOURCE_UNSUPPORTED')
        qty=number(raw['origQty']);filled=number(raw['executedQty']);quote=number(raw['cummulativeQuoteQty'])
        if qty<=0 or not 0<=filled<=qty or quote<0 or (filled==0 and quote!=0) or (filled>0 and quote<=0):raise BinanceReadIncomplete('BINANCE_ORDER_QUANTITY')
        with localcontext() as arithmetic:
            arithmetic.prec=512;avg=quote/filled if filled else Decimal(0);remaining=qty-filled
        stamp=raw.get('updateTime',raw.get('time'))
        if type(stamp) is not int or stamp<0:raise BinanceReadIncomplete('BINANCE_ORDER_TIME')
        data=dict(id=oid,symbol='BTC/USDT',side=raw['side'].lower(),status=self.STATUS[raw['status']],amount=str(qty),filled=str(filled),remaining=str(remaining),average=str(avg),type=str(raw.get('type','LIMIT')).lower(),timestamp=raw.get('time',stamp),lastTradeTimestamp=stamp)
        if raw['status']=='REJECTED':data['info']={'code':'BINANCE_SPOT_REJECTED_STATUS'}
        return data
    def read_order(self,client,oid,symbol):
        self.identity(client)
        if symbol!=self.scope.symbol:raise BinanceReadIncomplete('BINANCE_SYMBOL')
        oid=identifier(oid);raw=self._read('/api/v3/order',{'symbol':'BTCUSDT','orderId':int(oid)});data=self._normalize_order(raw,oid)
        code='BINANCE_SPOT_REJECTED_STATUS' if raw['status']=='REJECTED' else None
        return OrderSnapshot(data,self.scope.account,self.scope.subaccount,E.digest(data),code)
    def _trade(self,t,oid=None):
        tid=identifier(t.get('id'));order=identifier(t.get('orderId'))
        if t.get('symbol')!='BTCUSDT' or (oid is not None and order!=oid) or type(t.get('isBuyer')) is not bool:raise BinanceReadIncomplete('BINANCE_TRADE_SCOPE')
        price=number(t['price']);qty=number(t['qty']);fee=number(t['commission'])
        if price<=0 or qty<=0 or t.get('commissionAsset')!='USDT':raise BinanceReadIncomplete('BINANCE_TRADE_FEE_OR_QUANTITY')
        with localcontext() as arithmetic:
            arithmetic.prec=512
            if 'quoteQty' in t and number(t['quoteQty'])!=price*qty:raise BinanceReadIncomplete('BINANCE_TRADE_QUOTE')
        if type(t.get('time')) is not int or t['time']<0:raise BinanceReadIncomplete('BINANCE_TRADE_TIME')
        if any(t.get(k) is not None for k in ('fee','fees','paid_fee')):raise BinanceReadIncomplete('BINANCE_TRADE_FEE_DUPLICATE_SOURCE')
        return dict(id=tid,order=order,symbol='BTC/USDT',price=str(price),amount=str(qty),timestamp=t['time'],fee=dict(currency='USDT',cost=str(fee)))
    def read_page(self,client,oid,symbol,cursor,snapshot):
        self.identity(client);oid=identifier(oid)
        if symbol!=self.scope.symbol:raise BinanceReadIncomplete('BINANCE_SYMBOL')
        first=0 if cursor is None else int(identifier(cursor))
        rows=self._read('/api/v3/myTrades',dict(symbol='BTCUSDT',orderId=int(oid),fromId=first,limit=self.page_size))
        if not isinstance(rows,list) or len(rows)>self.page_size:raise BinanceReadIncomplete('BINANCE_TRADE_PAGE')
        trades=[];last=first-1
        order_side=self.read_order(client,oid,symbol).order['side']
        for row in rows:
            trade=self._trade(row,oid);tid=int(trade['id'])
            if tid<=last or tid<first or row['isBuyer']!=(order_side=='buy'):raise BinanceReadIncomplete('BINANCE_TRADE_ORDER_OR_DUPLICATE')
            trades.append(trade);last=tid
        nxt=str(last+1) if len(rows)==self.page_size else None
        return TradePage(trades,self.scope.account,self.scope.subaccount,oid,symbol,snapshot,cursor,nxt)
    def execution_port(self):
        return SpotReadAdapter(self,'binance-spot-raw-get-v1',self.identity,self.read_order,self.read_page,'ORDER_LIFETIME_QUOTE_V1',{'BINANCE_SPOT_REJECTED_STATUS':'ORDER_REJECTED'})
    def _window(self,start,end):
        if start>end or end-start>86400000:raise BinanceReadIncomplete('BINANCE_HISTORY_WINDOW_UNSUPPORTED')
    def _list_window(self,path,start,end,trade=False):
        self._window(start,end);rows=[];after=None;last=-1;last_time=-1
        for _ in range(40):
            params=dict(symbol='BTCUSDT',limit=self.page_size)
            if trade:
                if after is None:params.update(startTime=start,endTime=end)
                else:params['fromId']=after
            else:
                params.update(startTime=start,endTime=end)
                if after is not None:params['orderId']=after
            page=self._read(path,params)
            if not isinstance(page,list) or len(page)>self.page_size:raise BinanceReadIncomplete('BINANCE_HISTORY_PAGE')
            reached=False
            for raw in page:
                n=int(identifier(raw.get('id') if trade else raw.get('orderId')));stamp=raw.get('time')
                if n<=last or (after is not None and n<after) or type(stamp) is not int or stamp<last_time:raise BinanceReadIncomplete('BINANCE_HISTORY_CURSOR')
                last=n;last_time=stamp
                if stamp<start:raise BinanceReadIncomplete('BINANCE_HISTORY_RANGE')
                if stamp>end:
                    if not trade:raise BinanceReadIncomplete('BINANCE_HISTORY_RANGE')
                    reached=True;continue
                if raw.get('symbol')!='BTCUSDT':raise BinanceReadIncomplete('BINANCE_HISTORY_SYMBOL')
                rows.append(raw)
            if reached or len(page)<self.page_size:return rows
            after=last+1
        raise BinanceReadIncomplete('BINANCE_HISTORY_LIMIT')
    def _movement_gate(self,start,end):
        for path in ('/sapi/v1/capital/deposit/hisrec','/sapi/v1/capital/withdraw/history'):
            # Any reported movement requires a richer inventory reconciliation.
            for coin in ('BTC','USDT'):
                rows=self._read(path,dict(coin=coin,startTime=start,endTime=end,offset=0,limit=1000))
                if not isinstance(rows,list) or rows:raise BinanceReadIncomplete('BINANCE_EXTERNAL_MOVEMENT_OR_UNKNOWN')
        rows=self._read('/sapi/v1/asset/transfer',dict(type='MAIN_FUNDING',startTime=start,endTime=end,current=1,size=100))
        if not isinstance(rows,dict) or rows.get('total')!=0 or rows.get('rows')!=[]:raise BinanceReadIncomplete('BINANCE_TRANSFER_OR_UNKNOWN')
        # Empty subsets do NOT certify all transfer types/dividends/manual events.
        # The independent inventory source below must cover the full physical book.
    def history_port(self,bootstrap):
        if not isinstance(self.inventory_sources,HistoryReadAdapter) or bootstrap.adapter.client is not self or bootstrap.scope!=self.scope:raise BinanceReadIncomplete('BINANCE_HISTORY_INDEPENDENT_SOURCE_REQUIRED')
        self._bootstrap=bootstrap
        return HistoryReadAdapter('binance-spot-independent-history-v1',self._coverage,self.inventory_sources.balance,self.inventory_sources.inventory,self._history_page)
    def _coverage(self,client,scope):
        self.identity(client);independent=self.inventory_sources.coverage(client,scope)
        start=int(independent.start*1000);end=int(independent.through*1000);self._window(start,end)
        listed=self._list_window('/api/v3/allOrders',start,end)
        fills=self._list_window('/api/v3/myTrades',start,end,True)
        self._movement_gate(start,end)
        ids={identifier(r['orderId']) for r in listed if number(r['executedQty'])>0}|{identifier(t['orderId']) for t in fills}
        from .verifier import FillVerifier
        scope_obj=self._bootstrap.scope;context=self._bootstrap('BINANCE',scope_obj.account,SimpleNamespace(is_buy=True,is_sell=False,side='buy',is_futures=False,is_coinm=False,unified_symbol='BTC/USDT'),self)
        if context is None or E.canonical(scope)!=context.scope_json:raise BinanceReadIncomplete('BINANCE_HISTORY_SCOPE')
        summaries={};observed={};global_ids={}
        for oid in sorted(ids,key=int):
            summary=self.read_order(self,oid,'BTC/USDT').order;summaries[oid]=summary
            for listed_order in listed:
                if identifier(listed_order['orderId'])==oid and self._normalize_order(listed_order,oid)!=summary:raise BinanceReadIncomplete('BINANCE_ORDER_SUMMARY_CONFLICT')
            info=SimpleNamespace(is_buy=summary['side']=='buy',is_sell=summary['side']=='sell',side=summary['side'],is_close=False,is_futures=False,is_coinm=False,unified_symbol='BTC/USDT')
            fill=FillVerifier().verify_order_result(SimpleNamespace(client=self),'BINANCE','BTC/USDT',{'id':oid},info,context)
            if not E.execution_valid(fill,scope) or not fill.trades:raise BinanceReadIncomplete('BINANCE_HISTORY_EXECUTION')
            for t in fill.trades:
                if t.id in global_ids or t.timestamp is None or not independent.start<=t.timestamp<=independent.through:raise BinanceReadIncomplete('BINANCE_HISTORY_TRADE_COVERAGE')
                global_ids[t.id]=dict(order=oid,price=t.price,amount=t.amount,fee=t.fee,currency=t.fee_currency,timestamp=t.timestamp)
            observed[oid]=fill
        account_fills={}
        for raw in fills:
            t=self._trade(raw);key=t['id']
            if key in account_fills:raise BinanceReadIncomplete('BINANCE_HISTORY_DUPLICATE')
            account_fills[key]=dict(order=t['order'],price=Decimal(t['price']),amount=Decimal(t['amount']),fee=Decimal(t['fee']['cost']),currency=t['fee']['currency'],timestamp=t['timestamp']/1000)
        if account_fills!=global_ids:raise BinanceReadIncomplete('BINANCE_HISTORY_TRADE_RECONCILIATION')
        ordered=sorted(observed,key=lambda oid:(min(t.timestamp for t in observed[oid].trades),int(oid)))
        digest=__import__('hashlib').sha256()
        for i,oid in enumerate(ordered,1):digest.update((E.canonical(order_fact(observed[oid],i))+'\n').encode())
        count=sum(len(f.trades) for f in observed.values())
        if (independent.order_count,independent.trade_count,independent.order_digest)!=(len(ordered),count,digest.hexdigest()):raise BinanceReadIncomplete('BINANCE_INDEPENDENT_COVERAGE_CONFLICT')
        self._history_orders=tuple(HistoryOrder(i,oid,observed[oid].order_side,scope['book']) for i,oid in enumerate(ordered,1))
        return independent
    def _history_page(self,client,scope,after,limit):
        self.identity(client)
        if scope!=E.strict_object(self._bootstrap('BINANCE',self.scope.account,SimpleNamespace(is_buy=True,is_sell=False,side='buy',is_futures=False,is_coinm=False,unified_symbol='BTC/USDT'),self).scope_json) or not 1<=limit<=40:raise BinanceReadIncomplete('BINANCE_HISTORY_SCOPE')
        return HistoryPage(scope,after,self._history_orders[after:after+limit],'binance-enumerated-order-page')
