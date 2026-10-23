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

class EvidenceAuthority:
    def __init__(self,key_id,private_key):
        self.key_id=str(key_id);self.__key=private_key
    def _attest(self,kind,body):
        # Called only after verifier validation. No caller supplied trust flags are signed.
        envelope={'version':4,'kind':kind,'key_id':self.key_id,'body':strict_object(canonical(body))}
        signature=self.__key.sign(canonical(envelope).encode())
        return dict(envelope,signature=base64.b64encode(signature).decode('ascii'))

def verify(receipt,kind,expected=None):
    try:
        if not isinstance(receipt,dict) or set(receipt)!={'version','kind','key_id','body','signature'}: return False
        if receipt['version']!=4 or receipt['kind']!=kind: return False
        if not isinstance(receipt['signature'],str) or len(receipt['signature'])!=88:return False
        key=_KEYS.get(receipt['key_id'])
        if key is None: return False
        unsigned={k:v for k,v in receipt.items() if k!='signature'}
        key.verify(base64.b64decode(receipt['signature'],validate=True),canonical(unsigned).encode())
        return expected is None or canonical(receipt['body'])==canonical(expected)
    except (ValueError,TypeError,KeyError,InvalidSignature): return False

def fill_facts(fill):
    excluded={'execution_receipt','accounting_receipt','financial_receipt','raw_data','raw_info','source','trades_completeness_reason','outcome_receipt'}
    return {k:v for k,v in fill.dict().items() if k not in excluded}

def execution_valid(fill,scope):
    return verify(getattr(fill,'execution_receipt',None),'EXECUTION',{'scope':scope,'fill':fill_facts(fill)})

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
