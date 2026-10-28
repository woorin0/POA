"""Explicit host-injected spot read/signing port; no key or exchange client creation.

The host owns pin provenance and adapter authentication/completeness semantics.
This is a local verifier attestation, not an exchange signature or a Python sandbox.
Normalized CCXT dictionaries alone cannot satisfy this adapter contract.
"""
from dataclasses import dataclass,field
from decimal import Decimal,localcontext
from typing import Optional
import copy,hashlib
from . import evidence as E

_BOOTSTRAP=None

@dataclass(frozen=True)
class SpotScope:
    exchange:str
    account:str
    subaccount:str
    legacy_account_reference:str
    symbol:str
    instrument:str
    settlement:str
    position_cycle:str
    book:str

@dataclass(frozen=True)
class AuthenticatedAccount:
    exchange:str
    account:str
    subaccount:str
    permissions:frozenset

@dataclass(frozen=True)
class OrderSnapshot:
    order:dict
    account:str
    subaccount:str
    snapshot_id:str
    rejection_code:Optional[str]=None

@dataclass(frozen=True)
class TradePage:
    trades:list
    account:str
    subaccount:str
    order_id:str
    symbol:str
    snapshot_id:str
    cursor:Optional[str]
    next_cursor:Optional[str]

@dataclass(frozen=True)
class SpotReadAdapter:
    client:object
    contract_id:str
    read_identity:object
    read_order:object
    read_page:object
    # Registered venue-specific contract: every represented fee covers this order's
    # entire filled lifetime, in quote settlement currency (including paid_fee).
    fee_contract:str='UNSPECIFIED'
    rejection_codes:dict=field(default_factory=dict)

@dataclass(frozen=True)
class ObservedExecution:
    data:dict
    rejection_codes:dict
    fee_evidence:Optional[dict]=None

class SpotExecutionBootstrap:
    def __init__(self,scope,adapter,authority,public_keys,pin_fingerprints,*,revoked_keys=()):
        self.scope=scope;self.adapter=adapter;self.authority=authority
        self.public_keys=dict(public_keys);self.pin_fingerprints=dict(pin_fingerprints)
        self.revoked_keys=frozenset(revoked_keys);self.state='PROVIDER_NOT_CONFIGURED'
    def _key_ready(self):
        try:
            kid=self.authority.key_id
            if kid in self.revoked_keys or kid not in self.public_keys:return False
            if not self.public_keys or set(self.public_keys)!=set(self.pin_fingerprints):return False
            return all(type(v) is bytes and len(v)==32 and k not in self.revoked_keys and hashlib.sha256(v).hexdigest()==self.pin_fingerprints[k] for k,v in self.public_keys.items())
        except Exception:return False
    def _scope_ready(self):
        s=self.scope
        return isinstance(s,SpotScope) and s.exchange in {'BINANCE','BYBIT','OKX','UPBIT'} and all(isinstance(x,str) and 0<len(x)<=128 for x in vars(s).values()) and s.symbol.endswith('/'+s.settlement) and ':' not in s.symbol
    def _identity_ready(self):
        a=self.adapter;s=self.scope
        if not isinstance(a,SpotReadAdapter) or not a.contract_id:return False
        if a.fee_contract==E.KRW_PROFILE:
            from .upbit_spot_read import UpbitSpotReads
            if not isinstance(a.client,UpbitSpotReads) or not a.client.krw_contract_ready(s) or a.contract_id!=E.KRW_PROFILE:return False
        elif a.fee_contract!='ORDER_LIFETIME_QUOTE_V1':return False
        if getattr(a.client,'verbose',False) is True:return False
        if not all(callable(f) for f in (a.read_identity,a.read_order,a.read_page)):return False
        actual=a.read_identity(a.client)
        return isinstance(actual,AuthenticatedAccount) and (actual.exchange,actual.account,actual.subaccount)==(s.exchange,s.account,s.subaccount) and actual.permissions==frozenset({'read'})
    def activate(self):
        if not self._key_ready():self.state='KEY_UNAVAILABLE';return False
        if not self._scope_ready():self.state='ACCOUNT_SCOPE_UNVERIFIED';return False
        try:
            if not self._identity_ready():self.state='READ_CLIENT_UNVERIFIED';return False
        except Exception:
            self.state='READ_CLIENT_UNVERIFIED';return False
        try:
            E.configure_provider(self,self.public_keys)
            if self.adapter.fee_contract==E.KRW_PROFILE:
                E.register_krw_profile(self.scope_facts(),self.authority.key_id,self.adapter.client.audit_source_digest())
            challenge=self.authority._attest('BOOTSTRAP',{'contract':self.adapter.contract_id,'account':self.scope.account})
            if not E.verify(challenge,'BOOTSTRAP'):raise ValueError('key_unavailable')
            self.state='EXECUTION_EVIDENCE_INCOMPLETE';return True
        except Exception:
            E.configure_provider(None,{});self.state='KEY_UNAVAILABLE';return False
    def account_reference(self,exchange,reference):
        if exchange==self.scope.exchange and reference in (self.scope.legacy_account_reference,self.scope.account):return self.scope.account
        return None
    def client_for(self,exchange,info):
        if not self._matches(exchange,self.scope.account,info,self.adapter.client):raise ValueError('ACCOUNT_SCOPE_UNVERIFIED')
        return self.adapter.client
    def _matches(self,exchange,account,info,client):
        buy=getattr(info,'is_buy',None);sell=getattr(info,'is_sell',None)
        if type(buy) is not bool or type(sell) is not bool or buy==sell or getattr(info,'side',None)!=('buy' if buy else 'sell'):return False
        return exchange==self.scope.exchange and account==self.scope.account and client is self.adapter.client and getattr(info,'is_futures',None) is False and getattr(info,'is_coinm',False) is not True and getattr(info,'unified_symbol',None)==self.scope.symbol
    def __call__(self,exchange,account,info,client):
        if not self._matches(exchange,account,info,client):self.state='ACCOUNT_SCOPE_UNVERIFIED';return None
        try:
            if not self._key_ready() or not E.verify(self.authority._attest('BOOTSTRAP',{'contract':self.adapter.contract_id}),'BOOTSTRAP'):
                self.state='KEY_UNAVAILABLE';return None
        except Exception:self.state='KEY_UNAVAILABLE';return None
        try:
            if not self._identity_ready():self.state='READ_CLIENT_UNVERIFIED';return None
        except Exception:self.state='READ_CLIENT_UNVERIFIED';return None
        return E.ObservationContext(self.authority,client,E.canonical(self.scope_facts()),execution_reader=self)
    def scope_facts(self):
        s=self.scope
        return dict(exchange=s.exchange,account=s.account,subaccount=s.subaccount,market_type='spot',symbol=s.symbol,instrument=s.instrument,settlement=s.settlement,contract_size='1',position_side='LONG',position_cycle=s.position_cycle,book=s.book,read_contract_id=self.adapter.contract_id)
    def _snapshot(self,oid):
        snap=self.adapter.read_order(self.adapter.client,oid,self.scope.symbol)
        if not isinstance(snap,OrderSnapshot) or (snap.account,snap.subaccount)!=(self.scope.account,self.scope.subaccount) or not isinstance(snap.snapshot_id,str) or not snap.snapshot_id:raise ValueError('observation_scope')
        data=snap.order
        if not isinstance(data,dict) or str(data.get('id') or '')!=oid or data.get('symbol')!=self.scope.symbol:raise ValueError('order_scope')
        return snap
    def _fee(self,row,required):
        reps=[];currency=self.scope.settlement
        def value(f):
            if not isinstance(f,dict) or f.get('currency')!=currency or f.get('cost') is None:raise ValueError('fee_currency')
            n=_decimal(f['cost'])
            if not n.is_finite():raise ValueError('fee_value')
            return n
        if row.get('fee') is not None:reps.append(value(row['fee']))
        if row.get('fees') is not None:
            fs=row['fees']
            if not isinstance(fs,list) or not fs:raise ValueError('fee_missing')
            reps.append(sum((value(f) for f in fs),Decimal(0)))
        if row.get('paid_fee') is not None:
            n=_decimal(row['paid_fee'])
            if not n.is_finite():raise ValueError('fee_value')
            reps.append(n)
        if not reps:
            if required:raise ValueError('fee_missing')
            return None
        if any(n!=reps[0] for n in reps):raise ValueError('fee_conflict')
        return reps[0]
    def observe(self,oid,client):
        with localcontext() as arithmetic:
            arithmetic.prec=512
            return self._observe(oid,client)
    def _observe(self,oid,client):
        """Only verifier calls this registered read capability; DTO flags are unused."""
        try:
            if client is not self.adapter.client or not oid or not self._identity_ready():raise ValueError('read_identity')
            market=client.market(self.scope.symbol)
            if not isinstance(market,dict) or market.get('id')!=self.scope.instrument or market.get('symbol')!=self.scope.symbol or market.get('spot') is not True or market.get('contract') is True or market.get('quote')!=self.scope.settlement:raise ValueError('market_scope')
            if self.adapter.fee_contract==E.KRW_PROFILE:
                data,proof=client.observe_krw_execution(oid,self.scope_facts())
                return ObservedExecution(data,{},proof)
            first=self._snapshot(oid);cursor=None;seen=set();trades=[];ids=set()
            for _ in range(40):
                page=self.adapter.read_page(client,oid,self.scope.symbol,cursor,first.snapshot_id)
                if not isinstance(page,TradePage) or (page.account,page.subaccount,page.order_id,page.symbol,page.snapshot_id,page.cursor)!=(self.scope.account,self.scope.subaccount,oid,self.scope.symbol,first.snapshot_id,cursor):raise ValueError('page_scope')
                if not isinstance(page.trades,list) or len(page.trades)>100 or len(trades)+len(page.trades)>4000:raise ValueError('page_limit')
                for t in page.trades:
                    if not isinstance(t,dict) or not t.get('id') or str(t['id']) in ids or str(t.get('order') or '')!=oid or t.get('symbol')!=self.scope.symbol:raise ValueError('trade_scope')
                    ids.add(str(t['id']));fee=self._fee(t,True)
                    clean={k:copy.deepcopy(t[k]) for k in ('id','order','symbol','price','amount','timestamp') if k in t}
                    clean['fee']={'currency':self.scope.settlement,'cost':str(fee)};trades.append(clean)
                nxt=page.next_cursor
                if nxt is None:break
                if not isinstance(nxt,str) or not nxt or nxt in seen:raise ValueError('page_cycle')
                seen.add(nxt);cursor=nxt
            else:raise ValueError('page_limit')
            last=self._snapshot(oid)
            if first!=last:raise ValueError('unstable_snapshot')
            data={k:copy.deepcopy(first.order[k]) for k in ('id','symbol','status','side','amount','filled','remaining','average','fee','fees','paid_fee','type','timestamp','lastTradeTimestamp') if k in first.order}
            self._fee(data,False);data['trades']=trades
            filled=_decimal(data['filled']);requested=_decimal(data['amount'])
            qty=sum((_decimal(t['amount']) for t in trades),Decimal(0))
            if not filled.is_finite() or not requested.is_finite() or filled<0 or requested<=0 or filled>requested or qty!=filled:raise ValueError('quantity_conflict')
            if filled>0:
                if any(not _decimal(t['price']).is_finite() or _decimal(t['price'])<=0 or _decimal(t['amount'])<=0 for t in trades):raise ValueError('trade_value')
                vwap=sum((_decimal(t['price'])*_decimal(t['amount']) for t in trades),Decimal(0))/filled
                if vwap!=_decimal(data['average']):raise ValueError('vwap_conflict')
            codes={}
            if first.rejection_code is not None:
                code=first.rejection_code
                if filled!=0 or str(data.get('status')).lower()!='rejected' or code not in self.adapter.rejection_codes:raise ValueError('rejection_evidence')
                if str((first.order.get('info') or {}).get('code'))!=code:raise ValueError('rejection_evidence')
                codes={code:self.adapter.rejection_codes[code]};data['info']={'code':code}
            return ObservedExecution(data,codes)
        except Exception:
            self.state='EXECUTION_EVIDENCE_INCOMPLETE';return None
    def observed_result(self,verified):
        try:
            if not E.verify(self.authority._attest('BOOTSTRAP',{'contract':self.adapter.contract_id}),'BOOTSTRAP'):
                self.state='KEY_UNAVAILABLE';return
        except Exception:
            self.state='KEY_UNAVAILABLE';return
        self.state='EXECUTION_READY_TESTED' if verified else 'EXECUTION_EVIDENCE_INCOMPLETE'


def configure_execution_bootstrap(bootstrap):
    global _BOOTSTRAP
    E.configure_provider(None,{});_BOOTSTRAP=bootstrap
    if bootstrap is None:return False
    if not isinstance(bootstrap,SpotExecutionBootstrap):_BOOTSTRAP=None;return False
    return bootstrap.activate()

def current_bootstrap():return _BOOTSTRAP

def execution_readiness():
    return {'execution':_BOOTSTRAP.state if _BOOTSTRAP is not None else 'PROVIDER_NOT_CONFIGURED','history':'HISTORY_NOT_READY','live_account_connected':False}


def _decimal(value):
    n=Decimal(str(value))
    if not n.is_finite() or len(n.as_tuple().digits)>128 or abs(n.as_tuple().exponent)>128:raise ValueError('decimal_range')
    return n
