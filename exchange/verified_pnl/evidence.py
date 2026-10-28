"""Canonical signed evidence. Trust is supplied by service bootstrap, never by a DTO/DB row."""
import base64
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_KEYS = {}
_PROVIDER = None
_KRW_PROFILES = {}
KRW_PROFILE = "ORDER_LIFETIME_SINGLE_FILL_DONE_KRW_V1"

def canonical(value):
    def convert(x):
        if isinstance(x, Decimal):
            if not x.is_finite(): raise ValueError('nonfinite_decimal')
            text=format(x,'f')
            return '0' if x==0 else (text.rstrip('0').rstrip('.') if '.' in text else text)
        if isinstance(x, Enum): return x.value
        if isinstance(x, dict): return {str(k):convert(v) for k,v in x.items()}
        if isinstance(x, (list,tuple)): return [convert(v) for v in x]
        if isinstance(x,float) and not __import__('math').isfinite(x): raise ValueError('nonfinite_float')
        return x
    return json.dumps(convert(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()

def strict_object(encoded):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result: raise ValueError('duplicate_json_key')
            result[k]=v
        return result
    value=json.loads(encoded,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
    if not isinstance(value,dict): raise ValueError('object_required')
    return value

def configure_provider(provider, public_keys):
    """Trusted bootstrap only. Keys must be pinned outside the notification database."""
    global _PROVIDER,_KEYS
    _KRW_PROFILES.clear()
    checked={str(k):Ed25519PublicKey.from_public_bytes(v) for k,v in public_keys.items()}
    _KEYS=checked;_PROVIDER=provider

def context_for(exchange, account, info, client):
    if _PROVIDER is None or account_reference(exchange,account)!=account: return None
    context=_PROVIDER(exchange,account,info,client)
    if not isinstance(context,ObservationContext) or context.client is not client: return None
    scope=strict_object(context.scope_json)
    if scope.get('exchange')!=exchange or scope.get('account')!=account: return None
    return context

@dataclass(frozen=True)
class ObservationContext:
    """In-process trusted adapter capability; not a sandbox against malicious Python code."""
    authority: object
    client: object
    scope_json: str
    history_receipt: Optional[dict] = None
    fee_policy: str = 'EXACT_SAME_ORDER_V1'
    rejection_codes: Optional[dict] = None
    execution_reader: object = None
    fee_evidence: Optional[dict] = None

class EvidenceAuthority:
    def __init__(self,key_id,private_key):
        self.key_id=str(key_id);self.__key=private_key
    def _attest(self,kind,body):
        # Called only after verifier validation. No caller supplied trust flags are signed.
        envelope={'version':4,'kind':kind,'key_id':self.key_id,'body':strict_object(canonical(body))}
        if _krw_scope(body.get('scope', {})):
            if (self.key_id,digest(body['scope'])) not in _KRW_PROFILES:raise ValueError('krw_profile_unregistered')
            envelope.update(version=5,profile=KRW_PROFILE)
        signature=self.__key.sign(canonical(envelope).encode())
        return dict(envelope,signature=base64.b64encode(signature).decode('ascii'))

def verify(receipt,kind,expected=None):
    try:
        if not isinstance(receipt,dict) or receipt.get('kind')!=kind:return False
        if receipt.get('version')==4:
            if set(receipt)!={'version','kind','key_id','body','signature'}:return False
            if _krw_scope(receipt.get('body',{}).get('scope',{})) or 'fee_evidence' in receipt.get('body',{}):return False
        elif receipt.get('version')==5:
            if set(receipt)!={'version','profile','kind','key_id','body','signature'} or receipt['profile']!=KRW_PROFILE:return False
            scope=receipt['body'].get('scope',{})
            if not _krw_scope(scope) or (receipt['key_id'],digest(scope)) not in _KRW_PROFILES:return False
            if kind=='EXECUTION' and not krw_execution_body_valid(receipt['body'],receipt['key_id']):return False
        else:return False
        if not isinstance(receipt['signature'],str) or len(receipt['signature'])!=88:return False
        key=_KEYS.get(receipt['key_id'])
        if key is None: return False
        unsigned={k:v for k,v in receipt.items() if k!='signature'}
        key.verify(base64.b64decode(receipt['signature'],validate=True),canonical(unsigned).encode())
        return expected is None or canonical(receipt['body'])==canonical(expected)
    except (ValueError,TypeError,KeyError,AttributeError,InvalidSignature): return False

def fill_facts(fill):
    excluded={'execution_receipt','accounting_receipt','financial_receipt','raw_data','raw_info','source','trades_completeness_reason','outcome_receipt'}
    return {k:v for k,v in fill.dict().items() if k not in excluded}

def execution_valid(fill,scope):
    receipt=getattr(fill,'execution_receipt',None)
    if isinstance(receipt,dict) and receipt.get('version')==5:
        return verify(receipt,'EXECUTION') and canonical(receipt['body']['scope'])==canonical(scope) and canonical(receipt['body']['fill'])==canonical(fill_facts(fill))
    return verify(receipt,'EXECUTION',{'scope':scope,'fill':fill_facts(fill)})

def safe_code(value):
    known={'delivery_unknown','abandoned_send','malformed_json','rate_limited','send_failed','evidence_invalid','schema_invalid','lease_conflict','permanent_http_400','permanent_http_401','permanent_http_403','permanent_http_404'}
    return value if isinstance(value,str) and value in known else 'send_failed'

def payload_valid(payload):
    try:
        receipt=payload.financial_receipt
        if not verify(receipt,'FINANCIAL'):return False
        b=receipt['body'];r=b['result'];f=b['fill'];scope=b['scope']
        if (payload.exchange,payload.symbol,payload.order_id)!=(scope['exchange'],scope['symbol'],b['order_id']):return False
        def number(x):
            if x is None:return None
            n=Decimal(str(x).split()[0])
            if not n.is_finite():raise ValueError('nonfinite_finance')
            if isinstance(x,str) and len(x.split())>1 and x.split()[1]!=scope['settlement']:raise ValueError('currency_binding')
            return n
        mapping={'net_pnl':'net_realized_pnl','gross_pnl':'gross_realized_pnl','entry_fee_allocated':'entry_fee_allocated','exit_fee':'exit_fee','funding_pnl':'funding_pnl_allocated','roi_pct':'net_roi_pct','cost_basis':'cost_basis'}
        if any(number(getattr(payload,p))!=number(r.get(k)) for p,k in mapping.items()):return False
        if number(payload.filled_qty)!=number(f['filled_quantity']) or number(payload.exit_price)!=number(f['average_fill_price']):return False
        if payload.pnl_currency!=scope['settlement'] or payload.cost_basis_currency!=scope['settlement']:return False
        for name in ('accounting_basis','pnl_includes_entry_fee','pnl_includes_exit_fee','pnl_includes_funding'):
            if getattr(payload,name)!=f.get(name):return False
        if payload.is_roi_estimated!=r.get('is_roi_estimated'):return False
        if str(getattr(payload.pnl_verification_status,'value',payload.pnl_verification_status))!='VERIFIED':return False
        return True
    except (KeyError,TypeError,ValueError,ArithmeticError,AttributeError):return False

def payload_acquisition_price(payload):
    """Return the signed, fee-exclusive moving-average acquisition price.

    ACCOUNTING is a local trusted verifier attestation, not an exchange signature.
    Never infer a price from fee-inclusive cost/ROI. Legacy payloads without the
    linked ACCOUNTING proof remain unconfirmed independently of their Net PnL.
    """
    try:
        accounting = getattr(payload, 'accounting_receipt', None)
        financial = getattr(payload, 'financial_receipt', None)
        execution = getattr(payload, 'execution_receipt', None)
        if not (verify(accounting, 'ACCOUNTING') and verify(financial, 'FINANCIAL')
                and verify(execution, 'EXECUTION')):
            return None
        if not (accounting['key_id'] == financial['key_id'] == execution['key_id']):
            return None
        a, f, e = accounting['body'], financial['body'], execution['body']
        scope = f['scope']
        if canonical(a['scope']) != canonical(scope) or canonical(e['scope']) != canonical(scope):
            return None
        krw = _krw_scope(scope) and all(x.get('version')==5 and x.get('profile')==KRW_PROFILE for x in (accounting,financial,execution))
        if scope['market_type'] != 'spot' or (scope['settlement'] != 'USDT' and not krw) or Decimal(str(scope['contract_size'])) != 1:
            return None
        if not scope.get('account') or not scope.get('position_cycle'):
            return None
        if (payload.exchange, payload.symbol, payload.order_id) != (scope['exchange'], scope['symbol'], f['order_id']):
            return None
        if a['order_id'] != f['order_id'] or e['fill']['order_id'] != f['order_id']:
            return None
        if payload.pnl_currency != scope['settlement'] or a['currency'] != scope['settlement']:
            return None
        if not a.get('generation_id') or a['generation_id'] != f['generation_id']:
            return None
        if digest(a) != f['accounting_digest'] or digest(e) != f['execution_digest'] or a['execution_digest'] != f['execution_digest']:
            return None
        price = Decimal(a['entry_price'])
        reported = Decimal(payload.entry_price)
        if not price.is_finite() or price <= 0 or not reported.is_finite() or reported != price:
            return None
        return price
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        return None


def payload_fill_valid(payload):
    try:
        receipt=getattr(payload,'execution_receipt',None)
        if not verify(receipt,'EXECUTION'):return False
        b=receipt['body'];f=b['fill'];scope=b['scope']
        if (payload.exchange,payload.symbol,payload.order_id)!=(scope['exchange'],scope['symbol'],f['order_id']):return False
        expected=('매수' if f['execution_role']=='ENTRY' else '매도') if f['market_type']=='spot' else (('숏 종료' if f['position_side']=='SHORT' else '롱 종료') if f['execution_role']=='CLOSE' else ('숏 진입' if f['position_side']=='SHORT' else '롱 진입'))
        return payload.side==expected and Decimal(payload.filled_qty)==Decimal(f['filled_quantity']) and Decimal(payload.exit_price)==Decimal(f['average_fill_price']) and payload.fill_status.value==f['status']
    except (KeyError,TypeError,ValueError,ArithmeticError,AttributeError):return False

def outcome_rejected(value):
    try:
        receipt=getattr(value,'outcome_receipt',None)
        if not verify(receipt,'OUTCOME'):return False
        b=receipt['body'];symbol=getattr(value,'symbol',None) or getattr(value,'unified_symbol',None)
        return b['outcome']=='REJECTED_FINAL' and b['filled_qty']=='0' and b['scope']['exchange']==value.exchange and b['scope']['symbol']==symbol and b['order_id']==getattr(value,'order_id',None)
    except (KeyError,TypeError,AttributeError):return False

def account_reference(exchange,legacy_reference):
    resolver=getattr(_PROVIDER,'account_reference',None)
    if not callable(resolver):return None
    value=resolver(exchange,legacy_reference)
    return value if isinstance(value,str) and 0<len(value)<=128 else None


def _krw_scope(scope):
    return isinstance(scope,dict) and (scope.get('exchange'),scope.get('market_type'),scope.get('symbol'),scope.get('instrument'),scope.get('settlement'),str(scope.get('contract_size')))==('UPBIT','spot','BTC/KRW','KRW-BTC','KRW','1')


def register_krw_profile(scope,key_id,audit_source_digest):
    """Host bootstrap registry, never deserialized from receipt/DTO/SQLite.

    Local host capability and pinned issuer attest observations; this is NOT an
    exchange signature or a sandbox against malicious code holding signing keys.
    The host must re-register the reviewed scope and independent archive on restart.
    """
    if not _krw_scope(scope) or key_id not in _KEYS or scope.get('read_contract_id')!=KRW_PROFILE:raise ValueError('krw_profile_scope')
    if not scope.get('account') or not scope.get('position_cycle') or not str(scope.get('subaccount','')).startswith('pocket:'):raise ValueError('krw_profile_scope')
    if not isinstance(audit_source_digest,str) or len(audit_source_digest)!=64:raise ValueError('krw_audit_registry')
    _KRW_PROFILES[(key_id,digest(scope))]=audit_source_digest


def krw_execution_body_valid(body,key_id):
    """Validate signed native single-fill attribution and independent cash legs.

    Independence comes from the registered host archive capability, not a JSON
    flag. Exact Decimal reconciliation supplements, and does not replace, origin.
    """
    try:
        from decimal import localcontext
        with localcontext() as ctx:
            ctx.prec=512
            if set(body)!={'scope','fill','fee_evidence'}:return False
            scope=body['scope'];f=body['fill'];e=body['fee_evidence']
            if set(e)!={'contract','scope','orders','audit','native_pair_digest'}:return False
            if e['contract']!=KRW_PROFILE or canonical(e['scope'])!=canonical(scope):return False
            registry=_KRW_PROFILES.get((key_id,digest(scope)))
            audit=e['audit']
            if set(audit)!={'source_digest','record_digest','orders_digest','checkpoints'} or registry!=audit['source_digest']:return False
            orders=e['orders']
            if not isinstance(orders,list) or len(orders)!=2 or [o['side'] for o in orders]!=['buy','sell']:return False
            if audit['orders_digest']!=digest(orders) or e['native_pair_digest']!=digest([o['native_digest'] for o in orders]):return False
            def n(v):
                if not isinstance(v,str):raise ValueError('number_type')
                d=Decimal(v)
                if not d.is_finite() or len(d.as_tuple().digits)>128 or abs(d.as_tuple().exponent)>128:raise ValueError('number_range')
                return d
            import uuid
            for o in orders:
                if set(o)!={'order_id','trade_id','side','quantity','price','paid_fee','currency','created_us','filled_us','native_digest'}:return False
                if any(str(uuid.UUID(o[k]))!=o[k] for k in ('order_id','trade_id')) or o['order_id']==o['trade_id']:return False
                if n(o['quantity'])<=0 or n(o['price'])<=0 or n(o['paid_fee'])<0 or o['currency']!='KRW':return False
                if type(o['created_us']) is not int or type(o['filled_us']) is not int or not 0<=o['created_us']<=o['filled_us']:return False
                if not isinstance(o['native_digest'],str) or len(o['native_digest'])!=64:return False
            buy,sell=orders
            if buy['order_id']==sell['order_id'] or buy['trade_id']==sell['trade_id'] or n(buy['quantity'])!=n(sell['quantity']):return False
            points=audit['checkpoints']
            if not isinstance(points,list) or len(points)!=3:return False
            for c in points:
                if set(c)!={'at_us','KRW','BTC','reference_digest'} or type(c['at_us']) is not int or n(c['KRW'])<0 or n(c['BTC'])<0:return False
                if not isinstance(c['reference_digest'],str) or len(c['reference_digest'])!=64:return False
            c0,c1,c2=points;q=n(buy['quantity'])
            if not c0['at_us']<buy['created_us']<=buy['filled_us']<=c1['at_us']<sell['created_us']<=sell['filled_us']<=c2['at_us']:return False
            if len({c['reference_digest'] for c in points})!=3 or not isinstance(audit['record_digest'],str) or len(audit['record_digest'])!=64:return False
            if n(c0['BTC'])!=0 or n(c1['BTC'])!=q or n(c2['BTC'])!=0:return False
            if n(c0['KRW'])-n(c1['KRW'])!=q*n(buy['price'])+n(buy['paid_fee']):return False
            if n(c2['KRW'])-n(c1['KRW'])!=q*n(sell['price'])-n(sell['paid_fee']):return False
            chosen=[o for o in orders if o['order_id']==f['order_id']]
            if len(chosen)!=1:return False
            o=chosen[0];ts=f['trades']
            if len(ts)!=1 or f['status']!='FILLED' or f['verification_status']!='VERIFIED' or f['trades_complete'] is not True:return False
            if f['pnl_currency']!='KRW' or f['fees_complete'] is not True or f['execution_role']!=('ENTRY' if o['side']=='buy' else 'CLOSE'):return False
            if n(str(f['filled_quantity']))!=n(o['quantity']) or n(str(f['requested_quantity']))!=n(o['quantity']) or n(str(f['remaining_quantity']))!=0 or n(str(f['average_fill_price']))!=n(o['price']):return False
            t=ts[0]
            if t['id']!=o['trade_id'] or t['order_id']!=o['order_id'] or t['order']!=o['order_id']:return False
            if n(str(t['price']))!=n(o['price']) or n(str(t['amount']))!=n(o['quantity']) or n(str(t['fee']))!=n(o['paid_fee']) or t['fee_currency']!='KRW':return False
            if float(t['timestamp'])!=o['filled_us']/1000000 or {k:n(str(v)) for k,v in f['fees'].items()}!={'KRW':n(o['paid_fee'])}:return False
            return True
    except (KeyError,ValueError,TypeError,ArithmeticError,AttributeError):return False
