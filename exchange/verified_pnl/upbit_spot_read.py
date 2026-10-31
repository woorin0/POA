"""Upbit native read observations and opt-in single-fill KRW fee attribution.

No HTTP/JWT/credential client or finance signer. Financial certification requires
registered independent historical cash legs plus the existing trusted engine.
Default observational ports remain fail-closed. No live archive is supplied.
"""
import copy,datetime,calendar,uuid,re,threading
from decimal import Decimal,localcontext
from . import evidence as E
from .bootstrap import SpotScope,AuthenticatedAccount,SpotReadAdapter,OrderSnapshot
from dataclasses import dataclass,field

class UpbitReadIncomplete(ValueError):pass

def decimal(v):
    try:
        if type(v) not in (str,int):raise ValueError()
        n=Decimal(v)
        if not n.is_finite() or len(n.as_tuple().digits)>128 or abs(n.as_tuple().exponent)>128:raise ValueError()
        return n
    except Exception:raise UpbitReadIncomplete('UPBIT_NUMBER_INVALID') from None

def canonical_uuid(v):
    try:
        if type(v) is not str or str(uuid.UUID(v))!=v:raise ValueError()
        return v
    except Exception:raise UpbitReadIncomplete('UPBIT_UUID_INVALID') from None

def utc_us(value):
    try:
        if type(value) is not str:raise ValueError()
        t=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
        if t.tzinfo is None or t.utcoffset() not in (datetime.timedelta(0),datetime.timedelta(hours=9)):raise ValueError()
        t=t.astimezone(datetime.timezone.utc)
        return calendar.timegm(t.utctimetuple())*1000000+t.microsecond
    except Exception:raise UpbitReadIncomplete('UPBIT_TIME_INVALID') from None

def iso_utc(value):
    s,us=divmod(value,1000000)
    return datetime.datetime.fromtimestamp(s,datetime.timezone.utc).replace(microsecond=us).isoformat()

def summary_facts(value):
    numeric={'requested_volume','requested_quote','limit_price','executed_volume','remaining_volume','paid_fee'}
    return E.canonical({k:(decimal(v) if k in numeric and v is not None else v) for k,v in value.items()})

@dataclass(frozen=True)
class KrwCashCheckpoint:
    at_us:int
    krw:str
    btc:str
    reference:str

@dataclass(frozen=True)
class KrwCashAudit:
    scope:SpotScope
    orders_digest:str
    checkpoints:tuple
    external_events:tuple
    owners:tuple
    archive_reference:str

@dataclass(frozen=True)
class KrwCashAuditCapability:
    """Host-registered independent historic archive, not /accounts snapshots.

    The host is responsible for authentic account binding, archive continuity,
    all movements and physical-book ownership. No live collector is supplied.
    A DTO, copied paid_fee or source flag cannot install this capability.
    """
    scope:SpotScope
    source:str
    read:object



class UpbitSpotReads:
    verbose=False
    PATHS=frozenset({'/v1/order','/v1/orders/closed','/v1/accounts'})
    KRW_STATUS='BLOCKED_KRW_ACQUISITION_AND_FEE_CONTRACT'
    def __init__(self,scope,get,read_identity,*,pocket_uuid,as_of_ms,page_size=1000):
        pocket_uuid=canonical_uuid(pocket_uuid)
        if not isinstance(scope,SpotScope) or (scope.exchange,scope.symbol,scope.instrument,scope.settlement)!=('UPBIT','BTC/KRW','KRW-BTC','KRW') or scope.subaccount!='pocket:'+pocket_uuid:raise UpbitReadIncomplete('UPBIT_SCOPE_UNSUPPORTED')
        if not callable(get) or not callable(read_identity) or type(as_of_ms) is not int or as_of_ms<=0 or type(page_size) is not int or not 1<=page_size<=1000:raise UpbitReadIncomplete('UPBIT_REGISTERED_CAPABILITY_REQUIRED')
        self.scope=scope;self.pocket_uuid=pocket_uuid;self._get=get;self._identity=read_identity;self.as_of_ms=as_of_ms;self.page_size=page_size;self._owners={};self.last_scan_requests=0
    def identity(self,client):
        if client is not self:raise UpbitReadIncomplete('UPBIT_CLIENT_SCOPE')
        try:a=self._identity(self)
        except Exception:raise UpbitReadIncomplete('UPBIT_IDENTITY_UNAVAILABLE') from None
        if not isinstance(a,AuthenticatedAccount) or (a.exchange,a.account,a.subaccount,a.permissions)!=('UPBIT',self.scope.account,self.scope.subaccount,frozenset({'read'})):raise UpbitReadIncomplete('UPBIT_ACCOUNT_POCKET_UNVERIFIED')
        return a
    def _read(self,path,params):
        if path not in self.PATHS:raise UpbitReadIncomplete('UPBIT_GET_DENIED')
        self.identity(self)
        try:
            result=self._get(path,dict(params))
            if type(result) not in (dict,list) or (type(result) is dict and 'error' in result):raise ValueError()
            result=copy.deepcopy(result)
        except Exception as exc:
            from .upbit_notification_get import UpbitGETError
            if type(exc) is UpbitGETError:raise
            raise UpbitReadIncomplete('UPBIT_READ_UNAVAILABLE') from None
        self.identity(self)
        return result
    def market(self,symbol):
        if symbol!=self.scope.symbol:raise UpbitReadIncomplete('UPBIT_SYMBOL')
        return dict(id='KRW-BTC',symbol='BTC/KRW',spot=True,contract=False,quote='KRW')
    def read_accounts(self):
        rows=self._read('/v1/accounts',{})
        if type(rows) is not list or len(rows)>1000:raise UpbitReadIncomplete('UPBIT_ACCOUNTS_SCHEMA')
        seen=set();out={}
        for r in rows:
            c=r.get('currency')
            if type(c) is not str or c in seen:raise UpbitReadIncomplete('UPBIT_ACCOUNT_CURRENCY')
            seen.add(c)
            if c not in ('KRW','BTC'):continue
            if r.get('unit_currency')!='KRW' or decimal(r.get('balance'))<0 or decimal(r.get('locked'))<0:raise UpbitReadIncomplete('UPBIT_BALANCE_UNIT')
            out[c]=dict(balance=str(decimal(r['balance'])),locked=str(decimal(r['locked'])))
        # Missing asset is unknown, never manufactured as a flat opening.
        return out
    def _summary(self,r,*,allow_missing_paid_fee=False):
        if type(r) is not dict or r.get('market')!=self.scope.instrument or r.get('side') not in ('bid','ask') or r.get('ord_type') not in ('limit','price','market','best') or r.get('state') not in ('wait','watch','done','cancel'):raise UpbitReadIncomplete('UPBIT_ORDER_SCHEMA')
        oid=canonical_uuid(r.get('uuid'));stamp=utc_us(r.get('created_at'))
        if not 0<=stamp<=self.as_of_ms*1000:raise UpbitReadIncomplete('UPBIT_ORDER_TIME')
        count=r.get('trades_count')
        if type(count) is not int or not 0<=count<=1000:raise UpbitReadIncomplete('UPBIT_TRADES_COUNT')
        if r.get('identifier') is not None and (type(r['identifier']) is not str or not 1<=len(r['identifier'])<=128):raise UpbitReadIncomplete('UPBIT_IDENTIFIER')
        for k in ('remaining_volume','executed_volume','reserved_fee','remaining_fee','paid_fee','locked'):
            if k=='paid_fee' and allow_missing_paid_fee and r.get(k) is None:continue
            if decimal(r.get(k))<0:raise UpbitReadIncomplete('UPBIT_NEGATIVE_SUMMARY')
        for k in ('prevented_volume','prevented_locked'):
            if r.get(k) is not None and decimal(r[k])!=0:raise UpbitReadIncomplete('UPBIT_SMP_UNSUPPORTED')
        kind=r['ord_type'];side=r['side'];q=None;total=None;limit=None
        if kind=='limit':q=decimal(r.get('volume'));limit=decimal(r.get('price'))
        elif kind in ('price','best') and side=='bid':
            if r.get('volume') is not None:raise UpbitReadIncomplete('UPBIT_QUOTE_REQUEST_VOLUME_AMBIGUOUS')
            total=decimal(r.get('price'))
        elif kind in ('market','best') and side=='ask':q=decimal(r.get('volume'))
        else:raise UpbitReadIncomplete('UPBIT_TYPE_SIDE_CONFLICT')
        if (q is not None and q<=0) or (limit is not None and limit<=0) or (total is not None and total<=0):raise UpbitReadIncomplete('UPBIT_REQUEST_INVALID')
        executed=decimal(r['executed_volume']);remaining=decimal(r['remaining_volume'])
        if r['state']=='done' and (executed<=0 or remaining!=0):raise UpbitReadIncomplete('UPBIT_DONE_QUANTITY_CONFLICT')
        with localcontext() as arithmetic:
            arithmetic.prec=512
            if q is not None and q!=executed+remaining:raise UpbitReadIncomplete('UPBIT_QUANTITY_SUMMARY')
        return dict(uuid=oid,market=self.scope.instrument,side=side,ord_type=kind,state=r['state'],created_us=stamp,requested_volume=None if q is None else str(q),requested_quote=None if total is None else str(total),limit_price=None if limit is None else str(limit),executed_volume=str(executed),remaining_volume=str(remaining),paid_fee=None if allow_missing_paid_fee and r.get('paid_fee') is None else str(decimal(r['paid_fee'])),trades_count=count,identifier=r.get('identifier'))
    def read_order(self,client,oid,symbol):
        return self._read_order_normalized(client,oid,symbol)
    def read_notification_order(self,client,oid,symbol):
        # Only notification observation tolerates missing order fee. Finance stays strict.
        return self._read_order_normalized(client,oid,symbol,allow_missing_paid_fee=True)
    def _read_order_normalized(self,client,oid,symbol,*,allow_missing_paid_fee=False):
        self.identity(client);oid=canonical_uuid(oid)
        if symbol!=self.scope.symbol:raise UpbitReadIncomplete('UPBIT_SYMBOL')
        raw=self._read('/v1/order',{'uuid':oid});s=self._summary(raw,allow_missing_paid_fee=allow_missing_paid_fee)
        if s['uuid']!=oid:raise UpbitReadIncomplete('UPBIT_ORDER_UUID_MISMATCH')
        rows=raw.get('trades')
        if type(rows) is not list or len(rows)!=s['trades_count']:raise UpbitReadIncomplete('UPBIT_TRADE_LIST_INCOMPLETE')
        trades=[];seen=set()
        with localcontext() as arithmetic:
            arithmetic.prec=512;volume=Decimal(0);funds=Decimal(0)
            for r in rows:
                tid=canonical_uuid(r.get('uuid'));stamp=utc_us(r.get('created_at'));p=decimal(r.get('price'));q=decimal(r.get('volume'));v=decimal(r.get('funds'))
                if tid==oid or tid in seen or self._owners.get(tid,oid)!=oid or r.get('market')!=self.scope.instrument or r.get('side')!=s['side'] or r.get('order_uuid',oid)!=oid or not s['created_us']<=stamp<=self.as_of_ms*1000:raise UpbitReadIncomplete('UPBIT_TRADE_SCOPE_OR_UUID')
                if any(r.get(k) is not None for k in ('fee','paid_fee','ask_fee','bid_fee','trade_fee')):raise UpbitReadIncomplete('UPBIT_UNREVIEWED_TRADE_FEE_SOURCE')
                if p<=0 or q<=0 or v!=p*q:raise UpbitReadIncomplete('UPBIT_TRADE_FUNDS_CONFLICT')
                if s['limit_price'] is not None and ((s['side']=='bid' and p>Decimal(s['limit_price'])) or (s['side']=='ask' and p<Decimal(s['limit_price']))):raise UpbitReadIncomplete('UPBIT_LIMIT_FILL_CONFLICT')
                seen.add(tid);volume+=q;funds+=v
                trades.append(dict(id=tid,order=oid,symbol=self.scope.symbol,price=str(p),amount=str(q),timestamp=iso_utc(stamp),funds=str(v)))
            if volume!=Decimal(s['executed_volume']):raise UpbitReadIncomplete('UPBIT_EXECUTED_VOLUME_CONFLICT')
            if s['requested_quote'] is not None and funds>Decimal(s['requested_quote']):raise UpbitReadIncomplete('UPBIT_QUOTE_FUNDS_CONFLICT')
            avg=funds/volume if volume else None
        if len(self._owners)+len(seen-set(self._owners))>4096:raise UpbitReadIncomplete('UPBIT_READ_CACHE_BOUND')
        for t in seen:self._owners[t]=oid
        trades.sort(key=lambda t:(utc_us(t['timestamp']),t['id']))
        data=dict(id=oid,clientOrderId=s['identifier'],symbol=self.scope.symbol,side='buy' if s['side']=='bid' else 'sell',status={'done':'closed','cancel':'canceled','wait':'open','watch':'open'}[s['state']],type=s['ord_type'],amount=s['requested_volume'],filled=s['executed_volume'],remaining=s['remaining_volume'],average=None if avg is None else str(avg),price=s['limit_price'],requested_quote=s['requested_quote'],timestamp=iso_utc(s['created_us']),lastTradeTimestamp=trades[-1]['timestamp'] if trades else None,fee=dict(cost=s['paid_fee'],currency='KRW'),paid_fee=s['paid_fee'],trades=trades,observation_summary=s)
        if s['paid_fee'] is None:data['fee']=None
        return OrderSnapshot(data,self.scope.account,self.scope.subaccount,E.digest(data))
    def closed_orders(self,start_ms,end_ms):
        if type(start_ms) is not int or type(end_ms) is not int or not 0<=start_ms<=end_ms<=self.as_of_ms or end_ms-start_ms>7*86400000:raise UpbitReadIncomplete('UPBIT_SEVEN_DAY_WINDOW')
        stack=[(start_ms,end_ms)];leaves={};observed=set();calls=0
        while stack:
            if calls>=40:raise UpbitReadIncomplete('UPBIT_WINDOW_BUDGET_EXHAUSTED')
            lo,hi=stack.pop();calls+=1
            rows=self._read('/v1/orders/closed',dict(market='KRW-BTC',start_time=str(lo),end_time=str(hi),order_by='desc',limit=self.page_size))
            if type(rows) is not list or len(rows)>self.page_size:raise UpbitReadIncomplete('UPBIT_CLOSED_PAGE_SCHEMA')
            page={};last=None
            for r in rows:
                s=self._summary(r);t=s['created_us'];oid=s['uuid']
                if s['state'] not in ('done','cancel') or oid in page or not lo*1000<=t<=hi*1000 or (last is not None and t>last):raise UpbitReadIncomplete('UPBIT_CLOSED_PAGE_SCOPE')
                page[oid]=s;observed.add(oid);last=t
            if len(rows)==self.page_size:
                if hi-lo<=1:raise UpbitReadIncomplete('UPBIT_DENSE_BOUNDARY_UNPROVEN')
                mid=(lo+hi)//2;stack.extend(((lo,mid),(mid,hi)));continue
            for oid,s in page.items():
                if oid in leaves and summary_facts(leaves[oid])!=summary_facts(s):raise UpbitReadIncomplete('UPBIT_DUPLICATE_ORDER_CONFLICT')
                leaves[oid]=s
            if len(leaves)>4096:raise UpbitReadIncomplete('UPBIT_READ_CACHE_BOUND')
        self.last_scan_requests=calls
        if not observed<=set(leaves):raise UpbitReadIncomplete('UPBIT_SPLIT_PAGE_MISSING')
        out=[]
        for s in sorted(leaves.values(),key=lambda s:(s['created_us'],s['uuid'])):
            snap=self.read_order(self,s['uuid'],self.scope.symbol)
            detail=snap.order['observation_summary'];expected=dict(s)
            if expected.get('identifier') is None:expected['identifier']=detail['identifier']
            if summary_facts(expected)!=summary_facts(detail):raise UpbitReadIncomplete('UPBIT_CLOSED_DETAIL_CONFLICT')
            out.append(snap)
        return tuple(out)
    def read_page(self,*args,**kwargs):raise UpbitReadIncomplete('UPBIT_PER_TRADE_FEES_NOT_PROVEN')
    def execution_port(self):
        # An observational port only; existing bootstrap MUST reject unspecified fee provenance.
        return SpotReadAdapter(self,'upbit-krw-observation-only-v1',self.identity,self.read_order,self.read_page,'UNSPECIFIED',{})
    def history_port(self,*args,**kwargs):raise UpbitReadIncomplete(self.KRW_STATUS)
    def readiness(self):return dict(observation='READ_ONLY_HOST_CAPABILITY_REQUIRED',execution='PER_TRADE_FEE_EVIDENCE_MISSING',history='HISTORY_NOT_READY',financial=self.KRW_STATUS,acquisition='USDT_ONLY_EXISTING_GUARD')


    def krw_contract_ready(self,scope):
        cap=getattr(self,'_krw_audit_capability',None)
        return scope==self.scope and isinstance(cap,KrwCashAuditCapability) and cap.scope==scope and isinstance(cap.source,str) and 0<len(cap.source)<=128 and callable(cap.read) and cap.read is not self._get and getattr(cap.read,'__self__',None) is not self


    def enable_krw_contract(self,capability):
        """Explicit trusted-host injection. Never called from raw REST/DTO flags."""
        self._krw_audit_capability=capability
        if not self.krw_contract_ready(self.scope):
            self._krw_audit_capability=None
            raise UpbitReadIncomplete('UPBIT_INDEPENDENT_ARCHIVE_REQUIRED')
        return SpotReadAdapter(self,E.KRW_PROFILE,self.identity,self.read_order,self.read_page,E.KRW_PROFILE,{})


    def observe_krw_execution(self,oid,scope):
        self.identity(self)
        if not self.krw_contract_ready(self.scope):raise UpbitReadIncomplete('UPBIT_INDEPENDENT_ARCHIVE_REQUIRED')
        with localcontext() as arithmetic:
            arithmetic.prec=512
            return self._observe_krw_precise(oid,scope)


    def _observe_krw_precise(self,oid,scope):
        # The archive independently enumerates exactly one closed flat cycle; it must
        # not be derived from the order endpoint. Bounded pair only, no invented fees.
        cap=self._krw_audit_capability
        try:a=cap.read(self.scope)
        except Exception:raise UpbitReadIncomplete('UPBIT_ARCHIVE_UNAVAILABLE') from None
        if not isinstance(a,KrwCashAudit) or a.scope!=self.scope or type(a.checkpoints) is not tuple or len(a.checkpoints)!=3 or a.external_events!=() or a.owners!=(self.scope.book,) or not isinstance(a.archive_reference,str) or not a.archive_reference:raise UpbitReadIncomplete('UPBIT_ARCHIVE_SCOPE')
        # UUID enumeration is supplied independently by the archive, not producer.
        pair=getattr(cap.read,'order_ids',None)
        if type(pair) is not tuple or len(pair)!=2 or oid not in pair or pair[0]==pair[1]:raise UpbitReadIncomplete('UPBIT_ARCHIVE_ORDER_COVERAGE')
        snapshots=[];orders=[]
        for order_id in pair:
            snap=self.read_order(self,order_id,self.scope.symbol);d=snap.order;s=d['observation_summary']
            if s['ord_type']!='limit' or s['state']!='done' or s['trades_count']!=1 or len(d['trades'])!=1 or decimal(s['requested_volume'])!=decimal(s['executed_volume']) or decimal(s['remaining_volume'])!=0:raise UpbitReadIncomplete('UPBIT_SINGLE_FILL_DONE_REQUIRED')
            t=d['trades'][0]
            orders.append(dict(order_id=d['id'],trade_id=t['id'],side=d['side'],quantity=str(decimal(t['amount'])),price=str(decimal(t['price'])),paid_fee=str(decimal(s['paid_fee'])),currency='KRW',created_us=s['created_us'],filled_us=utc_us(t['timestamp']),native_digest=snap.snapshot_id))
            snapshots.append(snap)
        if a.orders_digest!=E.digest(orders):raise UpbitReadIncomplete('UPBIT_ARCHIVE_ORDER_CONFLICT')
        points=[]
        for c in a.checkpoints:
            if not isinstance(c,KrwCashCheckpoint) or not isinstance(c.reference,str) or not c.reference:raise UpbitReadIncomplete('UPBIT_CHECKPOINT_REQUIRED')
            points.append(dict(at_us=c.at_us,KRW=str(decimal(c.krw)),BTC=str(decimal(c.btc)),reference_digest=E.digest(c.reference)))
        proof=dict(contract=E.KRW_PROFILE,scope=scope,orders=orders,audit=dict(source_digest=E.digest(cap.source),record_digest=E.digest(dict(reference=a.archive_reference,orders_digest=a.orders_digest,checkpoints=points,owners=a.owners,external_events=a.external_events,scope=vars(a.scope))),orders_digest=a.orders_digest,checkpoints=points),native_pair_digest=E.digest([o['native_digest'] for o in orders]))
        for snap in snapshots:
            if snap!=self.read_order(self,snap.order['id'],self.scope.symbol):raise UpbitReadIncomplete('UPBIT_UNSTABLE_ORDER')
        d=copy.deepcopy(next(s.order for s in snapshots if s.order['id']==oid))
        # Sole proven event attribution. Raw read_order remains unchanged and has no
        # per-trade fee; signed proof explicitly records the order-level origin.
        d['trades'][0]['fee']=copy.deepcopy(d['fee'])
        return d,proof


    def audit_source_digest(self):
        return E.digest(self._krw_audit_capability.source)


# Notification host capability is separate from the financial provider/issuer.
_UPBIT_NOTIFICATION_HOST=None

class UpbitNotificationReads:
    """Internal KRW order observation only. No financial adapter/history port.

    Shares Decimal/native order validation with the BTC financial reader, whose
    constructor, market, history and signed-evidence scope remain BTC-only.
    """
    PATHS=frozenset({'/v1/order'})
    def __init__(self,scope,get,read_identity,*,pocket_uuid,as_of_ms):
        pocket_uuid=canonical_uuid(pocket_uuid)
        if type(scope) is not SpotScope or not re.fullmatch(r'[A-Z0-9]{1,20}/KRW',scope.symbol) or scope.exchange!='UPBIT' or scope.instrument!='KRW-'+scope.symbol.split('/')[0] or scope.settlement!='KRW' or scope.subaccount!='pocket:'+pocket_uuid:raise UpbitReadIncomplete('UPBIT_NOTIFICATION_MARKET')
        if not scope.account.startswith('poa-estimated-upbit:') or scope.position_cycle!='poa-estimated-internal-stream':raise UpbitReadIncomplete('UPBIT_NOTIFICATION_INTERNAL_ONLY')
        if not callable(get) or not callable(read_identity) or type(as_of_ms) is not int or as_of_ms<=0:raise UpbitReadIncomplete('UPBIT_REGISTERED_CAPABILITY_REQUIRED')
        self.scope=scope;self.pocket_uuid=pocket_uuid;self._get=get;self._identity=read_identity;self.as_of_ms=as_of_ms;self._owners={}
    identity=UpbitSpotReads.identity
    _read=UpbitSpotReads._read
    _summary=UpbitSpotReads._summary
    _read_order_normalized=UpbitSpotReads._read_order_normalized
    read_notification_order=UpbitSpotReads.read_notification_order


class UpbitNotificationWait(ValueError):pass

@dataclass(frozen=True)
class UpbitNotificationObservation:
    intake_id:str
    _facts_json:str=field(repr=False)
    source_digest:str
    binding_digest:str
    _permit:object=field(repr=False,compare=False)
    @property
    def facts(self):return E.strict_object(self._facts_json)

class UpbitNotificationHost:
    """Explicit trusted host registration, not an exchange signature/Python sandbox.

    The host owns authenticated GET/identity provenance. This port cannot issue
    EXECUTION/HISTORY/ACCOUNTING/FINANCIAL or authorize a delivery attempt.
    """
    CONTRACT='UPBIT_ESTIMATED_ORDER_READ_V1'
    def __init__(self,reader,*,source_account_ref,strategy,allow_unattributed=False):
        from .estimated_pnl import text
        if type(reader) not in (UpbitSpotReads,UpbitNotificationReads) or source_account_ref!=reader.scope.legacy_account_reference or not re.fullmatch('[0-9a-f]{64}',source_account_ref):raise UpbitReadIncomplete('UPBIT_NOTIFICATION_BINDING')
        if any(type(v) is not str or not 1<=len(v)<=128 or not v.isprintable() for v in vars(reader.scope).values()):raise UpbitReadIncomplete('UPBIT_NOTIFICATION_ACCOUNT_SCOPE')
        text(strategy)
        internal=allow_unattributed is True and reader.scope.account.startswith('poa-estimated-upbit:') and reader.scope.position_cycle=='poa-estimated-internal-stream'
        if strategy in ('default','주문') and not internal:raise UpbitReadIncomplete('UPBIT_STRATEGY_BINDING_REQUIRED')
        self.reader=reader;self.source_account_ref=source_account_ref;self.strategy=strategy;self._issued={};self._issued_lock=threading.Lock()
        self._scope_json=E.canonical(vars(reader.scope))
        self.binding=E.digest([self.CONTRACT,self._scope_json,source_account_ref,strategy])
    def pinned(self):return self._scope_json==E.canonical(vars(self.reader.scope)) and self.reader.pocket_uuid==self.reader.scope.subaccount.removeprefix('pocket:')
    def accepts(self,info,ref):
        return self.pinned() and ref==self.source_account_ref and getattr(info,'exchange',None)=='UPBIT' and getattr(info,'unified_symbol',None)==self.reader.scope.symbol and getattr(info,'is_futures',None) is False and not getattr(info,'is_coinm',False) and (getattr(info,'strategy_id',None) or getattr(info,'order_name',None) or 'default')==self.strategy
    def scope_facts(self):
        s=self.reader.scope
        return dict(exchange='UPBIT',account=s.account,subaccount=s.subaccount,market_type='spot',symbol=s.symbol,instrument=s.instrument,settlement='KRW',contract_size='1',position_side='LONG',position_cycle=s.position_cycle,book=s.book,read_contract_id=self.CONTRACT)
    def snapshot(self,facts):
        from .estimated_pnl import capture
        required={'id','symbol','side','status','filled','remaining','average','type','paid_fee','trades'}
        if type(facts) is not dict or set(facts)!=required or canonical_uuid(facts['id'])!=facts['id'] or facts['symbol']!=self.reader.scope.symbol or facts['side'] not in ('buy','sell') or facts['status']!='closed' or facts['type'] not in ('limit','price','market','best'):raise UpbitReadIncomplete('UPBIT_NOTIFICATION_FACTS')
        if decimal(facts['remaining'])!=0 or decimal(facts['filled'])<=0:raise UpbitReadIncomplete('UPBIT_NOTIFICATION_QUANTITY')
        if facts['paid_fee'] is not None and decimal(facts['paid_fee'])<0:raise UpbitReadIncomplete('UPBIT_NOTIFICATION_FEE')
        rows=facts['trades']
        if type(rows) is not list or not 1<=len(rows)<=40:raise UpbitReadIncomplete('UPBIT_NOTIFICATION_TRADE_BOUND')
        for t in rows:
            if type(t) is not dict or set(t)!={'id','order','symbol','price','amount','funds','timestamp'} or canonical_uuid(t['id'])!=t['id'] or t['id']==facts['id'] or t['order']!=facts['id'] or t['symbol']!=self.reader.scope.symbol:raise UpbitReadIncomplete('UPBIT_NOTIFICATION_TRADE_SCOPE')
            utc_us(t['timestamp'])
        snapshot=capture('UPBIT',facts,dict(unified_symbol=self.reader.scope.symbol,strategy_id=self.strategy,is_buy=facts['side']=='buy'),self.source_account_ref,True)
        if not snapshot['confirmed'] or Decimal(str(facts['average']))!=Decimal(snapshot['average']):raise UpbitReadIncomplete('UPBIT_NOTIFICATION_VWAP')
        return snapshot
    def observe(self,intake_id,info,ref,oid):
        if not self.accepts(info,ref):raise UpbitReadIncomplete('UPBIT_NOTIFICATION_SCOPE')
        oid=canonical_uuid(oid)
        snap=self.reader.read_notification_order(self.reader,oid,self.reader.scope.symbol);data=snap.order
        if not self.pinned() or (snap.account,snap.subaccount)!=(self.reader.scope.account,self.reader.scope.subaccount):raise UpbitReadIncomplete('UPBIT_NOTIFICATION_IDENTITY_CHANGED')
        if data.get('status')=='open':raise UpbitNotificationWait('UPBIT_ORDER_NOT_TERMINAL')
        if data.get('status')!='closed':raise UpbitReadIncomplete('UPBIT_NOTIFICATION_TERMINAL_REQUIRED')
        if data.get('id')!=oid or data.get('side')!=('buy' if getattr(info,'is_buy',False) else 'sell'):raise UpbitReadIncomplete('UPBIT_NOTIFICATION_ORDER_SIDE')
        facts={k:copy.deepcopy(data[k]) for k in ('id','symbol','side','status','filled','remaining','average','type','paid_fee')}
        facts['trades']=[{k:copy.deepcopy(t[k]) for k in ('id','order','symbol','price','amount','funds','timestamp')} for t in data['trades']]
        self.snapshot(facts)
        observation=UpbitNotificationObservation(intake_id,E.canonical(facts),snap.snapshot_id,self.binding,object())
        with self._issued_lock:
            if len(self._issued)>=40:self._issued.pop(next(iter(self._issued)))
            self._issued[observation._permit]=self._witness(observation)
        return observation
    @staticmethod
    def _witness(observation):return E.digest([observation.intake_id,observation._facts_json,observation.source_digest,observation.binding_digest])
    def owns(self,observation):
        if type(observation) is not UpbitNotificationObservation or observation.binding_digest!=self.binding or not self.pinned():return False
        with self._issued_lock:return self._issued.get(observation._permit)==self._witness(observation)


def configure_upbit_notification_host(host):
    global _UPBIT_NOTIFICATION_HOST
    _UPBIT_NOTIFICATION_HOST=None
    if host is None:return True
    try:
        from .upbit_notification_simple import SimpleUpbitNotificationHosts
        if type(host) is SimpleUpbitNotificationHosts:
            if not host.pinned():return False
        else:
            if type(host) is not UpbitNotificationHost or not host.pinned():return False
            host.reader.identity(host.reader)
        _UPBIT_NOTIFICATION_HOST=host;return True
    except Exception:return False


def current_upbit_notification_host(*,strategy=None,binding=None,symbol='BTC/KRW'):
    host=_UPBIT_NOTIFICATION_HOST
    if host is None or type(host) is UpbitNotificationHost:return host
    from .upbit_notification_simple import SimpleUpbitNotificationHosts
    if type(host) is not SimpleUpbitNotificationHosts:return None
    if binding is not None:return host.by_binding(binding)
    if strategy is not None:return host.for_strategy(strategy,symbol=symbol)
    return host
