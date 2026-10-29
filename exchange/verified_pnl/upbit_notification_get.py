"""Upbit notification-only authenticated GET. No credential discovery or order API.
Authentication contract: https://docs.upbit.com/kr/reference/auth
"""
import base64,hashlib,hmac,http.client,json,math,socket,ssl,threading,time,uuid,re
from dataclasses import dataclass,field
from decimal import Decimal
from urllib.parse import urlencode

ORIGIN='https://api.upbit.com'
PATH='/v1/order'

_CODES=frozenset('CREDENTIAL_UNAVAILABLE ORDER_UUID_REQUIRED GET_POLICY_REJECTED GET_TARGET_REJECTED GET_BOUNDS_REJECTED TLS_POLICY_REJECTED RESPONSE_ENCODING_REJECTED RESPONSE_TOO_LARGE READ_TIMEOUT TLS_FAILURE READ_CONNECTION_FAILED GET_CONFIGURATION_REQUIRED GET_TRANSPORT_REQUIRED AUTHENTICATION_DISABLED RATE_LIMITED CREDENTIAL_OR_BINDING_UNAVAILABLE RESPONSE_CONTRACT_REJECTED AUTHENTICATION_REJECTED PERMISSION_REJECTED REDIRECT_REJECTED READ_BLOCKED UPBIT_SERVER_UNAVAILABLE ORDER_QUERY_REJECTED RESPONSE_TYPE_REJECTED ORDER_RESPONSE_INVALID READ_ATTEMPTS_EXHAUSTED ACCOUNT_BINDING_UNAVAILABLE CREDENTIAL_BINDING_MISMATCH READ_SOURCES_NOT_CONFIGURED READ_FAILURE'.split())

class UpbitGETError(ValueError):
    def __init__(self,code,retry_after=0):
        self.code=code if code in _CODES else 'READ_FAILURE'
        self.retry_after=min(60,max(0,retry_after)) if type(retry_after) in (int,float) and math.isfinite(retry_after) else 0
        super().__init__(self.code)

@dataclass(frozen=True,repr=False)
class ReadCredentials:
    access_key:str
    secret_key:str
    def fingerprint(self):
        if any(type(s) is not str or not 1<=len(s)<=4096 or not s.isascii() or not s.isprintable() for s in (self.access_key,self.secret_key)):raise UpbitGETError('CREDENTIAL_UNAVAILABLE')
        return hashlib.sha256(self.access_key.encode()).hexdigest()

@dataclass(frozen=True,repr=False)
class OrderGETRequest:
    method:str
    url:str
    headers:dict=field(repr=False)
    timeout:tuple=(3.05,5.0)
    max_bytes:int=1048576
    verify:bool=True
    allow_redirects:bool=False

@dataclass(frozen=True,repr=False)
class OrderGETResponse:
    status:int
    body:bytes=field(repr=False)
    headers:dict=field(default_factory=dict,repr=False)


def order_uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError()
        return value
    except Exception:raise UpbitGETError('ORDER_UUID_REQUIRED') from None


def validate_request(request):
    if type(request) is not OrderGETRequest or request.method!='GET' or request.verify is not True or request.allow_redirects is not False:raise UpbitGETError('GET_POLICY_REJECTED')
    prefix=ORIGIN+PATH+'?uuid='
    if not request.url.startswith(prefix):raise UpbitGETError('GET_TARGET_REJECTED')
    oid=order_uuid(request.url[len(prefix):])
    if request.url!=prefix+oid or set(request.headers)!={'Authorization','Accept','Accept-Encoding'} or not request.headers['Authorization'].startswith('Bearer '):raise UpbitGETError('GET_POLICY_REJECTED')
    if request.headers['Accept']!='application/json' or request.headers['Accept-Encoding']!='identity':raise UpbitGETError('GET_POLICY_REJECTED')
    if request.timeout!=(3.05,5.0) or type(request.max_bytes) is not int or not 1<=request.max_bytes<=1048576:raise UpbitGETError('GET_BOUNDS_REJECTED')
    return oid


class HTTPSOrderGETTransport:
    """Fixed-origin TLS, no proxy/redirect support and no automatic HTTP retries.

    Constructors perform no IO. Only __call__ opens an HTTPS connection.
    Per-connection debug is disabled; response bodies never enter errors/logs.
    """
    def __call__(self,request):
        validate_request(request);connection=None
        try:
            context=ssl.create_default_context()
            if context.verify_mode!=ssl.CERT_REQUIRED or context.check_hostname is not True:raise UpbitGETError('TLS_POLICY_REJECTED')
            connection=http.client.HTTPSConnection('api.upbit.com',443,timeout=request.timeout[0],context=context)
            connection.set_debuglevel(0);connection.connect()
            connection.sock.settimeout(request.timeout[1])
            target=request.url[len(ORIGIN):]
            connection.request('GET',target,headers=dict(request.headers))
            response=connection.getresponse();headers={k.lower():v for k,v in response.getheaders()}
            if response.status!=200:return OrderGETResponse(response.status,b'',headers)
            if headers.get('content-encoding','identity').lower() not in ('','identity'):raise UpbitGETError('RESPONSE_ENCODING_REJECTED')
            length=headers.get('content-length')
            if length is not None and (not length.isdigit() or int(length)>request.max_bytes):raise UpbitGETError('RESPONSE_TOO_LARGE')
            chunks=[];size=0;deadline=time.monotonic()+8
            while size<=request.max_bytes:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise UpbitGETError('READ_TIMEOUT')
                connection.sock.settimeout(min(request.timeout[1],remaining))
                part=response.read1(min(16384,request.max_bytes+1-size))
                if not part:break
                size+=len(part)
                if size>request.max_bytes:raise UpbitGETError('RESPONSE_TOO_LARGE')
                chunks.append(part)
            return OrderGETResponse(200,b''.join(chunks),headers)
        except UpbitGETError:raise
        except ssl.SSLError:raise UpbitGETError('TLS_FAILURE') from None
        except (TimeoutError,socket.timeout):raise UpbitGETError('READ_TIMEOUT') from None
        except Exception:raise UpbitGETError('READ_CONNECTION_FAILED') from None
        finally:
            if connection is not None:
                try:connection.close()
                except Exception:pass


def _json(body):
    def pairs(values):
        result={}
        for k,v in values:
            if k in result:raise ValueError()
            result[k]=v
        return result
    return json.loads(body.decode('utf-8'),parse_float=Decimal,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()),object_pairs_hook=pairs)


def _b64(value):return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


class UpbitNotificationGET:
    """Exactly one order-query key, supplied externally and checked before use.

    key_guard(fingerprint) must validate local external-registry evidence. It must
    not perform an Upbit request. Total HTTP attempts per call <=2; no auth retry.
    """
    def __init__(self,credential_provider,key_guard,*,transport=None,monotonic=None,sleep=None,expected_market='KRW-BTC'):
        if not callable(credential_provider) or not callable(key_guard):raise UpbitGETError('GET_CONFIGURATION_REQUIRED')
        self._expected_market=expected_market
        self._credentials=credential_provider;self._guard=key_guard;self._transport=transport if transport is not None else HTTPSOrderGETTransport()
        if not callable(self._transport):raise UpbitGETError('GET_TRANSPORT_REQUIRED')
        self._clock=monotonic or time.monotonic;self._sleep=sleep or time.sleep;self._lock=threading.RLock();self._next=0.;self._cooldown=0.;self._auth_failed=False
    def __call__(self,path,params):return self.get(path,params)
    def get(self,path,params,*,method='GET',origin=ORIGIN):
        if method!='GET' or origin!=ORIGIN or path!=PATH or type(params) is not dict or set(params)!={'uuid'}:raise UpbitGETError('GET_TARGET_REJECTED')
        oid=order_uuid(params['uuid'])
        market=self._expected_market() if callable(self._expected_market) else self._expected_market
        if not isinstance(market,str) or not re.fullmatch(r'KRW-[A-Z0-9]{1,20}',market):raise UpbitGETError('GET_POLICY_REJECTED')
        with self._lock:
            if self._auth_failed:raise UpbitGETError('AUTHENTICATION_DISABLED')
            if self._clock()<self._cooldown:raise UpbitGETError('RATE_LIMITED',min(60,max(0,self._cooldown-self._clock())))
            for attempt in range(2):
                wait=self._next-self._clock()
                if wait>0:self._sleep(min(wait,2))
                try:
                    credentials=self._credentials()
                    if type(credentials) is not ReadCredentials:raise UpbitGETError('CREDENTIAL_UNAVAILABLE')
                    fingerprint=credentials.fingerprint();self._guard(fingerprint)
                except UpbitGETError:raise
                except Exception:raise UpbitGETError('CREDENTIAL_OR_BINDING_UNAVAILABLE') from None
                query=urlencode({'uuid':oid}) # UUID uses only unreserved ASCII: signed bytes == sent bytes.
                header=_b64(b'{"alg":"HS512","typ":"JWT"}')
                payload=_b64(json.dumps(dict(access_key=credentials.access_key,nonce=str(uuid.uuid4()),query_hash=hashlib.sha512(query.encode()).hexdigest(),query_hash_alg='SHA512'),separators=(',',':')).encode())
                signing=header+'.'+payload;signature=_b64(hmac.digest(credentials.secret_key.encode(),signing.encode(),'sha512'))
                request=OrderGETRequest('GET',ORIGIN+PATH+'?'+query,{'Authorization':'Bearer '+signing+'.'+signature,'Accept':'application/json','Accept-Encoding':'identity'})
                self._next=self._clock()+0.1
                try:response=self._transport(request)
                except UpbitGETError as exc:
                    if exc.code in ('READ_TIMEOUT','READ_CONNECTION_FAILED') and attempt==0:self._sleep(.25);continue
                    raise UpbitGETError(exc.code) from None
                except (TimeoutError,socket.timeout):
                    if attempt==0:self._sleep(.25);continue
                    raise UpbitGETError('READ_TIMEOUT') from None
                except Exception:
                    if attempt==0:self._sleep(.25);continue
                    raise UpbitGETError('READ_CONNECTION_FAILED') from None
                if type(response) is not OrderGETResponse or type(response.status) is not int or type(response.body) is not bytes or type(response.headers) is not dict:raise UpbitGETError('RESPONSE_CONTRACT_REJECTED')
                headers={str(k).lower():v for k,v in response.headers.items()};status=response.status
                if status in (401,403):
                    self._auth_failed=True;raise UpbitGETError('AUTHENTICATION_REJECTED' if status==401 else 'PERMISSION_REJECTED')
                if 300<=status<400:raise UpbitGETError('REDIRECT_REJECTED')
                if status in (429,418):
                    try:delay=float(headers.get('retry-after',1 if status==429 else 60));assert math.isfinite(delay) and delay>=0
                    except Exception:delay=60
                    delay=min(60,max(1,delay));self._cooldown=self._clock()+delay
                    if status==429 and attempt==0 and delay<=2:self._sleep(delay);continue
                    raise UpbitGETError('RATE_LIMITED' if status==429 else 'READ_BLOCKED',delay)
                if 500<=status<600:
                    if attempt==0:self._sleep(.25);continue
                    raise UpbitGETError('UPBIT_SERVER_UNAVAILABLE')
                if status!=200:raise UpbitGETError('ORDER_QUERY_REJECTED')
                if len(response.body)>1048576:raise UpbitGETError('RESPONSE_TOO_LARGE')
                if str(headers.get('content-type','application/json')).split(';')[0].strip().lower()!='application/json':raise UpbitGETError('RESPONSE_TYPE_REJECTED')
                try:
                    data=_json(response.body)
                    if type(data) is not dict or 'error' in data or data.get('uuid')!=oid or data.get('market')!=market or data.get('side') not in ('bid','ask'):raise ValueError()
                except Exception:raise UpbitGETError('ORDER_RESPONSE_INVALID') from None
                remaining=str(headers.get('remaining-req',''))
                if any(p.strip()=='sec=0' for p in remaining.split(';')):self._cooldown=max(self._cooldown,self._clock()+1)
                return data
            raise UpbitGETError('READ_ATTEMPTS_EXHAUSTED')
