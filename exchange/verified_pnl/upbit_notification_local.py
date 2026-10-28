"""Local Windows-vault bridge; no secret discovery, registry writer or API at import.

Trust root: operator-controlled service-user Windows Credential Manager + pinned
external registration record. An operator attestation is not exchange identity proof.
"""
import argparse,ctypes,hashlib,ipaddress,json,os,re,sys,time
# Direct-file diagnostic avoids exchange.__init__, which initializes POA settings/DB.
# Production import through main keeps its ordinary package initialization.
if __name__=='__main__' and not __package__:
    from pathlib import Path
    from types import ModuleType
    from importlib.machinery import ModuleSpec
    root=Path(__file__).resolve().parents[2]
    for name,path in [('exchange',root/'exchange'),('exchange.verified_pnl',root/'exchange/verified_pnl')]:
        pkg=ModuleType(name);pkg.__path__=[str(path)];pkg.__spec__=ModuleSpec(name,loader=None,is_package=True);sys.modules[name]=pkg
    __package__='exchange.verified_pnl'
from dataclasses import replace
from . import upbit_notification_bootstrap as B
from .upbit_notification_get import ReadCredentials,UpbitGETError,HTTPSOrderGETTransport,OrderGETResponse,order_uuid

BINDING_TARGET='POA/Upbit/OrderQuery/BindingV1'
CREDENTIAL_TARGET='POA/Upbit/OrderQuery/CredentialV1'

class PreflightError(ValueError):
    def __init__(self):super().__init__('UPBIT_LOCAL_SUPPLY_UNAVAILABLE')


def _vault_read(target):
    """Read only fixed generic credentials from the current service user's vault.

    No CredWrite/Enumerate; never invoked by this task outside patched tests.
    """
    if os.name!='nt' or target not in (BINDING_TARGET,CREDENTIAL_TARGET):raise PreflightError()
    from ctypes import wintypes as W
    class Credential(ctypes.Structure):
        _fields_=[('Flags',W.DWORD),('Type',W.DWORD),('TargetName',W.LPWSTR),('Comment',W.LPWSTR),('LastWritten',W.FILETIME),('CredentialBlobSize',W.DWORD),('CredentialBlob',ctypes.POINTER(ctypes.c_ubyte)),('Persist',W.DWORD),('AttributeCount',W.DWORD),('Attributes',ctypes.c_void_p),('TargetAlias',W.LPWSTR),('UserName',W.LPWSTR)]
    pointer=ctypes.POINTER(Credential)();api=ctypes.WinDLL('Advapi32.dll',use_last_error=True)
    api.CredReadW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,ctypes.POINTER(ctypes.POINTER(Credential))];api.CredReadW.restype=W.BOOL
    api.CredFree.argtypes=[ctypes.c_void_p];api.CredFree.restype=None
    try:
        if not api.CredReadW(target,1,0,ctypes.byref(pointer)):raise PreflightError()
        c=pointer.contents
        if c.Type!=1 or c.TargetName!=target or not 1<=c.CredentialBlobSize<=2560:raise PreflightError()
        return ctypes.string_at(c.CredentialBlob,c.CredentialBlobSize)
    except Exception:raise PreflightError() from None
    finally:
        if pointer:
            if 0<pointer.contents.CredentialBlobSize<=2560:ctypes.memset(pointer.contents.CredentialBlob,0,pointer.contents.CredentialBlobSize)
            api.CredFree(pointer)


def _object(raw):
    def pairs(values):
        result={}
        for k,v in values:
            if k in result:raise PreflightError()
            result[k]=v
        return result
    try:
        if type(raw) is not bytes or not 1<=len(raw)<=2560:raise PreflightError()
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(PreflightError()))
        if type(value) is not dict:raise PreflightError()
        return value
    except Exception:raise PreflightError() from None


def _record(env):
    try:
        pin=env.get('POA_UPBIT_READ_BINDING_SHA256','')
        if not re.fullmatch('[0-9a-f]{64}',pin):raise PreflightError()
        raw=_vault_read(BINDING_TARGET)
        if hashlib.sha256(raw).hexdigest()!=pin:raise PreflightError()
        r=_object(raw)
        if set(r)!={'format','config','evidence','operator','permissions','diagnostic_order'} or r['format']!='POA_UPBIT_OPERATOR_BINDING_V1':raise PreflightError()
        cfg=B.UpbitReadConfig(**r['config']);cfg.validate()
        ev=r['evidence'];op=r['operator'];diag=r['diagnostic_order']
        if set(ev)!={'source','reference','digest','expires_at'} or set(op)!={'actor_digest','approved_at','allowed_ips','permission_review_digest'}:raise PreflightError()
        if r['permissions']!=['order_query']:raise PreflightError()
        for v in (ev['digest'],op['actor_digest'],op['permission_review_digest']):
            if type(v) is not str or not re.fullmatch('[0-9a-f]{64}',v):raise PreflightError()
        if type(op['approved_at']) not in (int,float) or not 0<op['approved_at']<=time.time():raise PreflightError()
        if not isinstance(op['allowed_ips'],list) or not 1<=len(op['allowed_ips'])<=8:raise PreflightError()
        for ip in op['allowed_ips']:ipaddress.ip_address(ip)
        if set(diag)!={'uuid','market','side'} or diag['market']!='KRW-BTC' or diag['side'] not in ('buy','sell'):raise PreflightError()
        order_uuid(diag['uuid'])
        proof=B.ExternalAccountBinding(cfg.account_uuid,cfg.pocket_uuid,cfg.source_account_ref,cfg.strategy,cfg.read_key_fingerprint,cfg.producer,frozenset({'order_query'}),ev['source'],ev['reference'],ev['digest'],ev['expires_at'])
        return cfg,proof,diag,pin
    except Exception:raise PreflightError() from None


def _sources(env):
    cfg,proof,diag,pin=_record(env)
    def resolve(current):
        fresh,p,_,digest=_record(env)
        if fresh!=cfg or current!=cfg or digest!=pin:raise PreflightError()
        return p
    def credentials():
        try:
            # Revalidate registry before loading any key, including expiry.
            temporary=B.UpbitReadSources(cfg,lambda:None,resolve)
            B._binding(temporary)
            r=_object(_vault_read(CREDENTIAL_TARGET))
            if set(r)!={'access_key','secret_key'}:raise PreflightError()
            value=ReadCredentials(r['access_key'],r['secret_key'])
            if value.fingerprint()!=cfg.read_key_fingerprint:raise PreflightError()
            return value
        except Exception:raise PreflightError() from None
    result=B.UpbitReadSources(cfg,credentials,resolve)
    B._binding(result)
    return result,diag


def configure_local_upbit_sources(*,environment=None):
    """Service startup hook. Disabled path does not read vault, keys or network."""
    env=os.environ if environment is None else environment
    flag=env.get('POA_UPBIT_ESTIMATED_READ_ENABLED','false')
    if type(flag) is str and flag.strip().lower()=='false':return 'DISABLED'
    try:
        if type(flag) is not str or flag.strip().lower()!='true':raise PreflightError()
        sources,_=_sources(env)
        if not B.configure_upbit_read_sources(sources):raise PreflightError()
        return 'LOCAL_SOURCES_CONFIGURED_EXTERNAL_IDENTITY_UNVERIFIED'
    except Exception:
        B.configure_upbit_read_sources(None)
        return 'LOCAL_SOURCES_UNAVAILABLE'


class _OneRequest:
    """Single physical HTTP attempt, including timeout/429/5xx. Never retry."""
    def __init__(self,oid):self.oid=oid;self.used=False
    def __call__(self,request):
        if self.used or request.url!='https://api.upbit.com/v1/order?uuid='+self.oid:raise UpbitGETError('GET_TARGET_REJECTED')
        self.used=True
        try:r=HTTPSOrderGETTransport()(request)
        except Exception:raise UpbitGETError('ORDER_QUERY_REJECTED') from None
        if type(r) is not OrderGETResponse:raise UpbitGETError('RESPONSE_CONTRACT_REJECTED')
        if r.status!=200:
            code={401:'AUTHENTICATION_REJECTED',403:'PERMISSION_REJECTED',429:'RATE_LIMITED'}.get(r.status,'ORDER_QUERY_REJECTED')
            raise UpbitGETError(code)
        return r


def one_shot_summary(oid,*,approved=False,environment=None):
    """Explicit diagnostic only. No worker, DB, receipt or Discord calls.

    Both flags and exact operator-registered UUID are mandatory. A local CLI flag
    is a technical opt-in, not proof of human authorization or external delivery.
    """
    env=os.environ if environment is None else environment
    if approved is not True or env.get('POA_UPBIT_ONE_SHOT_GET_ENABLED')!='true' or env.get('POA_UPBIT_ESTIMATED_READ_ENABLED')!='true':raise PreflightError()
    try:
        oid=order_uuid(oid);sources,diag=_sources(env)
        if oid!=diag['uuid']:raise PreflightError()
        sources=replace(sources,transport=_OneRequest(oid))
        if not B.configure_upbit_read_sources(sources):raise PreflightError()
        host=B.prepare_upbit_notification_host(environment=env).host
        if host is None:raise PreflightError()
        data=host.reader.read_notification_order(host.reader,oid,'BTC/KRW').order
        if data['id']!=oid or data['symbol']!='BTC/KRW' or data['side']!=diag['side'] or data['status']!='closed':raise PreflightError()
        facts={k:data[k] for k in ('id','symbol','side','status','filled','remaining','average','type','paid_fee','trades')}
        host.snapshot(facts) # Existing native terminal/full-trade rules; no issuance.
        return dict(order_ref=hashlib.sha256(oid.encode()).hexdigest()[:16],market='KRW-BTC',side=data['side'],state='done',filled=data['filled'],vwap=data['average'],paid_fee=data['paid_fee'],identity='OPERATOR_REGISTERED_NOT_EXCHANGE_VERIFIED')
    except Exception:raise PreflightError() from None
    finally:B.configure_upbit_read_sources(None)


def cli():
    class SafeParser(argparse.ArgumentParser):
        def error(self,message):
            print('UPBIT_LOCAL_PREFLIGHT_ARGUMENT_REJECTED',file=sys.stderr);raise SystemExit(2)
    parser=SafeParser(prog='upbit-order-once',description='Explicit one-order read-only diagnostic; external approval required')
    parser.add_argument('--order-uuid',required=True);parser.add_argument('--approved-once',action='store_true')
    args=parser.parse_args()
    try:result=one_shot_summary(args.order_uuid,approved=args.approved_once)
    except Exception:
        print('UPBIT_LOCAL_PREFLIGHT_FAILED',file=sys.stderr);return 2
    print(json.dumps(result,separators=(',',':')));return 0

if __name__=='__main__':raise SystemExit(cli())
