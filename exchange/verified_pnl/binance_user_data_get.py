"""Offline-first HMAC USER_DATA GET capability. No default/network transport.

The host must supply trusted transport and externally supplied credentials. This
module never installs providers, generates keys or certifies HISTORY completeness.
"""
from dataclasses import dataclass,field
from decimal import Decimal
import hashlib,hmac,json,threading,time
from urllib.parse import urlencode
from .binance_spot_read import BinanceSpotReads

class ReadGateError(RuntimeError):
    def __init__(self,code,retry_after=None):
        super().__init__(code)
        self.code=code;self.retry_after=retry_after

@dataclass(frozen=True)
class ReadCredentials:
    api_key:str=field(repr=False)
    secret:bytes=field(repr=False)

@dataclass(frozen=True)
class GetRequest:
    path:str
    query:str=field(repr=False)
    headers:dict=field(repr=False)
    method:str=field(default='GET',init=False)
    origin:str=field(default='https://api.binance.com',init=False)
    timeout_seconds:int=field(default=10,init=False)
    allow_redirects:bool=field(default=False,init=False)

@dataclass(frozen=True)
class ReadResponse:
    status:int
    body:bytes=field(repr=False)
    headers:dict=field(default_factory=dict,repr=False)

class BinanceUserDataGET:
    """Callable accepted by BinanceSpotReads. All IO requires injected transport.

    transport(GetRequest) -> ReadResponse must enforce TLS/origin/no redirects,
    timeout/size bounds and must not log signed queries or authentication headers.
    No retry/sleep loop: caller receives only safe category and cooldown seconds.
    This GET signer and financial Ed25519 issuer are separate trust contracts.
    """
    ALLOWED={
        '/api/v3/time':frozenset(),
        '/api/v3/exchangeInfo':frozenset({'symbol'}),
        '/api/v3/account':frozenset(),
        '/sapi/v1/account/apiRestrictions':frozenset(),
        '/api/v3/order':frozenset({'symbol','orderId'}),
        '/api/v3/myTrades':frozenset({'symbol','orderId','fromId','startTime','endTime','limit'}),
        '/api/v3/allOrders':frozenset({'symbol','orderId','startTime','endTime','limit'}),
        '/sapi/v1/capital/deposit/hisrec':frozenset({'coin','startTime','endTime','offset','limit'}),
        '/sapi/v1/capital/withdraw/history':frozenset({'coin','startTime','endTime','offset','limit'}),
        '/sapi/v1/asset/transfer':frozenset({'type','startTime','endTime','current','size'}),
    }
    WRITE_RIGHTS=('enableSpotAndMarginTrading','enableWithdrawals','enableMargin','enableFutures','enableInternalTransfer','permitsUniversalTransfer')
    def __init__(self,transport,credential_provider,account_uid,*,recv_window=5000,wall_ms=None,monotonic_ms=None):
        if not callable(transport) or not callable(credential_provider) or type(account_uid) is not str or not account_uid.isdigit():raise ReadGateError('READ_CONFIGURATION_REQUIRED')
        if type(recv_window) is not int or not 1<=recv_window<=5000:raise ReadGateError('READ_WINDOW_INVALID')
        self._transport=transport;self._credentials=credential_provider;self._uid=account_uid;self._window=recv_window
        self._wall=wall_ms or (lambda:int(time.time()*1000));self._mono=monotonic_ms or (lambda:int(time.monotonic()*1000))
        self._clock=None;self._cooldown=0;self._lock=threading.RLock()
    def _parameters(self,path,params):
        if path not in self.ALLOWED or type(params) is not dict or not set(params)<=self.ALLOWED[path]:raise ReadGateError('READ_PATH_OR_PARAMETERS_DENIED')
        for k,v in params.items():
            if k=='symbol':valid=v=='BTCUSDT'
            elif k=='coin':valid=v in ('BTC','USDT')
            elif k=='type':valid=v=='MAIN_FUNDING'
            else:valid=type(v) is int and 0<=v<=2**63-1
            if not valid:raise ReadGateError('READ_SCOPE_OR_TYPE_DENIED')
        if 'symbol' in self.ALLOWED[path] and params.get('symbol')!='BTCUSDT':raise ReadGateError('READ_SYMBOL_REQUIRED')
        if path=='/api/v3/order' and 'orderId' not in params:raise ReadGateError('READ_ORDER_REQUIRED')
        if '/capital/' in path and params.get('coin') not in ('BTC','USDT'):raise ReadGateError('READ_COIN_REQUIRED')
        if path=='/sapi/v1/asset/transfer' and params.get('type')!='MAIN_FUNDING':raise ReadGateError('READ_TRANSFER_SCOPE_REQUIRED')
        if path in ('/api/v3/allOrders','/sapi/v1/capital/deposit/hisrec','/sapi/v1/capital/withdraw/history','/sapi/v1/asset/transfer') and not {'startTime','endTime'}<=set(params):raise ReadGateError('READ_WINDOW_REQUIRED')
        if path=='/api/v3/myTrades' and not ('orderId' in params or 'fromId' in params or {'startTime','endTime'}<=set(params)):raise ReadGateError('READ_TRADE_RANGE_REQUIRED')
        if 'limit' in params and not 1<=params['limit']<=1000:raise ReadGateError('READ_PAGE_LIMIT')
        if 'size' in params and not 1<=params['size']<=100:raise ReadGateError('READ_PAGE_LIMIT')
        if 'current' in params and params['current']<1:raise ReadGateError('READ_PAGE_LIMIT')
        if 'fromId' in params and ('startTime' in params or 'endTime' in params):raise ReadGateError('READ_CURSOR_WINDOW_CONFLICT')
        if 'startTime' in params and 'endTime' in params:
            if not 0<=params['endTime']-params['startTime']<=86400000:raise ReadGateError('READ_CYCLE_OVER_24H_UNSUPPORTED')
        return dict(params)
    def _decode(self,response):
        if not isinstance(response,ReadResponse) or type(response.status) is not int or type(response.body) is not bytes:raise ReadGateError('READ_TRANSPORT_CONTRACT')
        if type(response.headers) is not dict:raise ReadGateError('READ_TRANSPORT_CONTRACT')
        if len(response.body)>8*1024*1024:raise ReadGateError('READ_RESPONSE_LIMIT')
        status=response.status
        if status!=200:
            retry=None
            if status in (418,429):
                raw=response.headers.get('Retry-After',response.headers.get('retry-after'))
                retry=int(raw) if isinstance(raw,(str,int)) and str(raw).isdigit() else 60
                retry=max(1,min(retry,86400));self._cooldown=self._mono()+retry*1000
            code={401:'READ_AUTH_DENIED',403:'READ_PERMISSION_DENIED',418:'READ_IP_BANNED',429:'READ_RATE_LIMITED'}.get(status,'READ_SERVER_UNCERTAIN' if status>=500 else 'READ_HTTP_REJECTED')
            if status==400:
                try:
                    if json.loads(response.body).get('code')==-1021:self._clock=None;code='READ_TIMESTAMP_REJECTED'
                except Exception:pass
            raise ReadGateError(code,retry)
        if len(response.body)>8*1024*1024:raise ReadGateError('READ_RESPONSE_LIMIT')
        def unique(pairs):
            out={}
            for k,v in pairs:
                if k in out:raise ValueError()
                out[k]=v
            return out
        try:
            value=json.loads(response.body,parse_float=lambda s:s,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()),object_pairs_hook=unique)
        except Exception:raise ReadGateError('READ_JSON_INVALID') from None
        if not isinstance(value,(dict,list)):raise ReadGateError('READ_JSON_INVALID')
        if isinstance(value,dict) and 'code' in value and (type(value['code']) is not int or value['code']<0):
            if value['code']==-1021:self._clock=None;raise ReadGateError('READ_TIMESTAMP_REJECTED')
            raise ReadGateError('READ_API_ERROR')
        return value
    def _send(self,request):
        if self._mono()<self._cooldown:raise ReadGateError('READ_COOLDOWN_ACTIVE')
        try:response=self._transport(request)
        except TimeoutError:raise ReadGateError('READ_TIMEOUT_UNCERTAIN') from None
        except Exception:raise ReadGateError('READ_TRANSPORT_UNAVAILABLE') from None
        return self._decode(response)
    def _sync(self):
        before=self._mono();raw=self._send(GetRequest('/api/v3/time','',{}));after=self._mono()
        if not isinstance(raw,dict) or type(raw.get('serverTime')) is not int or raw['serverTime']<=0 or not 0<=after-before<=1000:raise ReadGateError('READ_SERVER_TIME_UNVERIFIED')
        self._clock=(raw['serverTime'],(before+after)//2,self._wall())
    def _timestamp(self):
        if self._clock is None or not 0<=self._mono()-self._clock[1]<=60000:self._sync()
        server,at,wall=self._clock;elapsed=self._mono()-at
        if abs((self._wall()-wall)-elapsed)>1000:self._clock=None;raise ReadGateError('READ_LOCAL_CLOCK_JUMP')
        return server+elapsed
    def _credential(self):
        try:c=self._credentials()
        except Exception:raise ReadGateError('READ_CREDENTIAL_UNAVAILABLE') from None
        if not isinstance(c,ReadCredentials) or type(c.api_key) is not str or not c.api_key.isascii() or not c.api_key.isalnum() or type(c.secret) is not bytes or not c.secret:raise ReadGateError('READ_CREDENTIAL_INVALID')
        return c
    def _signed(self,path,params,credential):
        p=dict(params);p.update(timestamp=self._timestamp(),recvWindow=self._window)
        query=urlencode(sorted(p.items()));signature=hmac.new(credential.secret,query.encode('ascii'),hashlib.sha256).hexdigest()
        return self._send(GetRequest(path,query+'&signature='+signature,{'X-MBX-APIKEY':credential.api_key,'Accept':'application/json'}))
    def _rights(self,r):
        if type(r) is not dict or r.get('enableReading') is not True or any(r.get(k) is not False for k in self.WRITE_RIGHTS):raise ReadGateError('READ_ONLY_PERMISSION_REQUIRED')
        if any(type(v) is not bool for k,v in r.items() if k.startswith(('enable','permits'))):raise ReadGateError('READ_ONLY_PERMISSION_REQUIRED')
        if any(v is True for k,v in r.items() if (k.startswith('enable') and k!='enableReading') or (k.startswith('permits') and k!='permitsUniversalTransfer')):raise ReadGateError('READ_ONLY_PERMISSION_REQUIRED')
    def _account(self,a):
        if type(a) is not dict or str(a.get('uid'))!=self._uid or a.get('accountType')!='SPOT':raise ReadGateError('READ_ACCOUNT_SCOPE_DENIED')
    def __call__(self,path,params):
        p=self._parameters(path,params)
        with self._lock:
            if path in ('/api/v3/time','/api/v3/exchangeInfo'):return self._send(GetRequest(path,urlencode(sorted(p.items())),{}))
            c=self._credential()
            rights=self._signed('/sapi/v1/account/apiRestrictions',{},c);self._rights(rights)
            account=self._signed('/api/v3/account',{},c);self._account(account)
            if path=='/sapi/v1/account/apiRestrictions':return rights
            if path=='/api/v3/account':return account
            return self._signed(path,p,c)
