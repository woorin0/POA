"""Authenticated, bounded order reads for notifications, never financial receipts."""
import copy
import http.client
import json
import socket
import ssl
import time
import uuid
from decimal import Decimal, localcontext
from types import SimpleNamespace
from urllib.parse import urlsplit
from .evidence import canonical, digest, strict_object
from .ledger import identity

CONTRACT = 'POA_EXCHANGE_ORDER_FILL_CONFIRMATION_V1'
VENUES = frozenset({'UPBIT', 'BINANCE', 'BYBIT', 'OKX'})


class ReadRejected(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def number(value, *, positive=False, derived=False):
    if type(value) not in (str, int):
        raise ReadRejected('FILL_NUMBER_INVALID')
    try:
        n = Decimal(value)
        bound=512 if derived else 128
        if not n.is_finite() or len(n.as_tuple().digits) > bound or abs(n.as_tuple().exponent) > bound or (positive and n <= 0):
            raise ValueError()
        return n
    except (ValueError, ArithmeticError):
        raise ReadRejected('FILL_NUMBER_INVALID') from None


def text(value):
    if type(value) not in (str, int) or isinstance(value, bool):
        raise ReadRejected('FILL_REFERENCE_INVALID')
    value = str(value)
    if not 1 <= len(value) <= 128 or not value.isprintable():
        raise ReadRejected('FILL_REFERENCE_INVALID')
    return value


def request_scope(request):
    info = request['info']
    exchange = info['exchange']
    symbol = info['unified_symbol']
    future = info['is_futures']
    if exchange not in VENUES or type(future) is not bool or info.get('is_coinm'):
        raise ReadRejected('FILL_MARKET_UNSUPPORTED')
    if type(info['is_buy']) is not bool or type(info['is_sell']) is not bool or info['is_buy'] == info['is_sell']:
        raise ReadRejected('FILL_SIDE_INVALID')
    side = 'buy' if info['is_buy'] else 'sell'
    if info['side'] != side or not isinstance(symbol, str) or symbol.count('/') != 1:
        raise ReadRejected('FILL_SCOPE_INVALID')
    base, quote = symbol.split('/')
    if not base.isalnum() or not base.isupper() or len(base) > 20:
        raise ReadRejected('FILL_SCOPE_INVALID')
    settlement = quote.split(':')[0]
    if future:
        if exchange == 'UPBIT' or quote != 'USDT:USDT':
            raise ReadRejected('FILL_MARKET_UNSUPPORTED')
    elif ':' in quote or settlement != ('KRW' if exchange == 'UPBIT' else 'USDT'):
        raise ReadRejected('FILL_MARKET_UNSUPPORTED')
    expected_size=None
    if exchange=='OKX' and future:
        value=info.get('contract_size')
        if type(value) not in (str,int,float) or isinstance(value,bool):
            raise ReadRejected('FILL_CONTRACT_UNVERIFIED')
        # This is a consistency pin from POA's original market metadata, never
        # the amount calculation input. Arithmetic uses authenticated ctVal.
        expected_size=str(number(str(value),positive=True))
    return dict(exchange=exchange, account=request['source_account_ref'], subaccount='credential-scoped',
                market_type='linear' if future else 'spot', symbol=symbol, settlement=settlement,
                side=side, order_id=text(request['result']['id']),
                client_order_id=request['result'].get('clientOrderId'),
                strategy=text(info.get('strategy_id') or info.get('order_name') or 'default'),
                order_name=text(info.get('order_name') or 'default'), is_close=bool(info['is_close']),expected_contract_size=expected_size)


def validate(proof):
    """Mathematical/type validation only; durable host authority is checked separately."""
    try:
        if type(proof) is not dict or set(proof) != {'contract', 'scope', 'source_digest', 'observed_at', 'instrument', 'contract_size', 'position', 'filled', 'average', 'total', 'fees', 'trades'} or proof['contract'] != CONTRACT:
            return False
        scope = proof['scope']
        if scope['exchange'] not in VENUES or scope['market_type'] not in ('spot', 'linear') or scope['settlement'] not in ('KRW', 'USDT'):
            return False
        if len(scope['account']) != 64 or any(c not in '0123456789abcdef' for c in scope['account']):
            return False
        if type(proof['observed_at']) not in (int, float) or not 0 < proof['observed_at'] < 4102444800 or len(proof['source_digest']) != 64:
            return False
        factor = number(proof['contract_size'], positive=True)
        if scope['market_type'] == 'spot' and factor != 1:
            return False
        if scope['exchange']!='OKX' and factor!=1:return False
        if scope['exchange']=='OKX' and scope['market_type']=='linear' and factor!=number(scope['expected_contract_size'],positive=True):
            return False
        trades = proof['trades']
        if type(trades) is not list or not 1 <= len(trades) <= 4000:
            return False
        with localcontext() as ctx:
            ctx.prec = 512
            quantity = total = Decimal(0)
            ids = set()
            for t in trades:
                if set(t) != {'id', 'order_id', 'price', 'quantity', 'quote_amount', 'timestamp','native_quantity','quantity_unit','quote_source'} or text(t['id']) in ids or t['order_id'] != scope['order_id']:
                    return False
                ids.add(t['id'])
                p=number(t['price'],positive=True)
                q=number(t['quantity'],positive=True,derived=True)
                v=number(t['quote_amount'],positive=True,derived=True)
                native=number(t['native_quantity'],positive=True)
                contracts=scope['exchange']=='OKX' and scope['market_type']=='linear'
                if t['quantity_unit']!=('CONTRACTS' if contracts else 'BASE') or q!=native*(factor if contracts else 1):return False
                if t['quote_source']!=('PRICE_QUANTITY' if scope['exchange']=='OKX' else 'NATIVE_QUOTE'):return False
                if p * q != v or not 0 <= number(t['timestamp']) / 1000 <= Decimal(str(proof['observed_at'])):
                    return False
                quantity += q
                total += v
            if quantity != number(proof['filled'], positive=True,derived=True) or total != number(proof['total'], positive=True,derived=True) or total / quantity != number(proof['average'], positive=True,derived=True):
                return False
        if proof['fees'] is not None:
            if type(proof['fees']) is not dict or len(proof['fees']) > 16:
                return False
            for currency, fee in proof['fees'].items():
                if not currency.isalnum() or not currency.isupper():
                    return False
                number(fee)
        return True
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        return False


class Observation:
    __slots__ = ('encoded', '_token', '_host')
    def __init__(self, proof, host):
        self.encoded = canonical(proof)
        self._host = host
        self._token = object()


class ReadHost:
    """Existing credentials, separate CCXT instances, GET allowlist, no signing authority.

    The in-process capability is not a sandbox against malicious Python code.
    Tests replace only its authenticated HTTP IO, not validation/authority.
    """
    def __init__(self, settings):
        self.settings = settings
        self.created = time.time()
        self.clients = {}
        self._issued = {}

    def credential(self, exchange):
        key = getattr(self.settings, exchange + '_KEY', None)
        secret = getattr(self.settings, exchange + '_SECRET', None)
        password = getattr(self.settings, exchange + '_PASSPHRASE', None)
        if not key or not secret or (exchange == 'OKX' and not password):
            raise ReadRejected('FILL_CREDENTIAL_UNAVAILABLE')
        return key, secret, password

    def _client(self, scope):
        import ccxt
        credentials = self.credential(scope['exchange'])
        if identity(scope['exchange'], credentials[0]) != scope['account']:
            raise ReadRejected('FILL_ACCOUNT_MISMATCH')
        key = (scope['exchange'], scope['market_type'])
        if key not in self.clients:
            self.clients[key] = getattr(ccxt, scope['exchange'].lower())(dict(apiKey=credentials[0], secret=credentials[1], password=credentials[2], timeout=5000, enableRateLimit=True,
                options={'defaultType': 'swap' if scope['market_type'] == 'linear' else 'spot', 'maxRetriesOnFailure': 0}))
        client = self.clients[key]
        if (client.apiKey, client.secret, client.password) != credentials:
            raise ReadRejected('FILL_CREDENTIAL_CHANGED')
        return client

    def _http(self, exchange, signed):
        """CCXT signs; this transport avoids its raw-header/body debug logging.

        Fixed official HTTPS hosts, certificate validation, no redirects/proxies,
        bounded response, one HTTP attempt. No request/response is logged.
        """
        allowed={'BINANCE':{'api.binance.com','fapi.binance.com'},'BYBIT':{'api.bybit.com'},'OKX':{'www.okx.com'}}
        paths={'api.binance.com':{'/api/v3/order','/api/v3/myTrades'},'fapi.binance.com':{'/fapi/v1/order','/fapi/v1/userTrades'},
               'api.bybit.com':{'/v5/order/history','/v5/order/realtime','/v5/execution/list'},
               'www.okx.com':{'/api/v5/trade/order','/api/v5/trade/fills','/api/v5/trade/fills-history','/api/v5/public/instruments'}}
        parsed=urlsplit(signed['url'])
        if parsed.scheme!='https' or parsed.hostname not in allowed[exchange] or parsed.port not in (None,443) or parsed.username or parsed.password or parsed.fragment or signed.get('method','GET')!='GET' or signed.get('body') is not None:
            raise ReadRejected('FILL_GET_REJECTED')
        if parsed.path not in paths[parsed.hostname]:raise ReadRejected('FILL_GET_REJECTED')
        connection=None
        try:
            context=ssl.create_default_context()
            if context.verify_mode!=ssl.CERT_REQUIRED or not context.check_hostname:
                raise ReadRejected('FILL_TLS_REJECTED')
            connection=http.client.HTTPSConnection(parsed.hostname,443,timeout=5,context=context)
            connection.set_debuglevel(0)
            headers=dict(signed.get('headers') or {})
            headers.update({'Accept':'application/json','Accept-Encoding':'identity'})
            connection.request('GET',parsed.path+('?' + parsed.query if parsed.query else ''),headers=headers)
            response=connection.getresponse()
            if response.status==401:raise ReadRejected('FILL_AUTH_REJECTED')
            if response.status==403:raise ReadRejected('FILL_PERMISSION_REJECTED')
            if response.status in (418,429):raise ReadRejected('FILL_RATE_LIMITED')
            if 300<=response.status<400:raise ReadRejected('FILL_REDIRECT_REJECTED')
            if response.status!=200:
                raise ReadRejected('FILL_TEMPORARY_UNAVAILABLE' if 500<=response.status<600 else 'FILL_READ_FAILED')
            if (response.getheader('Content-Type') or 'application/json').split(';')[0].strip().lower()!='application/json':
                raise ReadRejected('FILL_RESPONSE_INVALID')
            if (response.getheader('Content-Encoding') or 'identity').lower() not in ('','identity'):
                raise ReadRejected('FILL_RESPONSE_INVALID')
            size=response.getheader('Content-Length')
            if size is not None and (not size.isdigit() or int(size)>1048576):
                raise ReadRejected('FILL_RESPONSE_TOO_LARGE')
            chunks=[];length=0;deadline=time.monotonic()+8
            while length<=1048576:
                if time.monotonic()>deadline:raise ReadRejected('FILL_TIMEOUT')
                chunk=response.read1(min(16384,1048577-length))
                if not chunk:break
                chunks.append(chunk);length+=len(chunk)
            if length>1048576:raise ReadRejected('FILL_RESPONSE_TOO_LARGE')
            def pairs(items):
                result={}
                for k,v in items:
                    if k in result:raise ReadRejected('FILL_RESPONSE_INVALID')
                    result[k]=v
                return result
            return json.loads(b''.join(chunks).decode('utf-8'),parse_float=Decimal,object_pairs_hook=pairs,
                parse_constant=lambda value:(_ for _ in ()).throw(ReadRejected('FILL_RESPONSE_INVALID')))
        except (TimeoutError,socket.timeout):raise ReadRejected('FILL_TIMEOUT') from None
        except ssl.SSLError:raise ReadRejected('FILL_TLS_REJECTED') from None
        except ReadRejected:raise
        except OSError:raise ReadRejected('FILL_TEMPORARY_UNAVAILABLE') from None
        except Exception:raise ReadRejected('FILL_READ_FAILED') from None
        finally:
            if connection is not None:
                try:connection.close()
                except Exception:pass

    def observe(self, request):
        scope = request_scope(request)
        try:
            original_credentials = self.credential(scope['exchange'])
            with localcontext() as ctx:
                ctx.prec = 512
                proof = self._observe(scope)
            if not validate(proof):
                raise ReadRejected('FILL_EVIDENCE_INVALID')
            if self.credential(scope['exchange']) != original_credentials:
                raise ReadRejected('FILL_CREDENTIAL_CHANGED')
            observation = Observation(proof, self)
            if len(self._issued) >= 64:
                self._issued.pop(next(iter(self._issued)))
            self._issued[observation._token] = observation.encoded
            return observation
        except ReadRejected:
            raise
        except Exception as exc:
            from .upbit_notification_get import UpbitGETError
            import ccxt
            code = 'FILL_READ_FAILED'
            if isinstance(exc, UpbitGETError):
                code = {'AUTHENTICATION_REJECTED': 'FILL_AUTH_REJECTED', 'IP_REJECTED': 'FILL_IP_REJECTED', 'PERMISSION_REJECTED': 'FILL_PERMISSION_REJECTED', 'RATE_LIMITED': 'FILL_RATE_LIMITED', 'READ_TIMEOUT': 'FILL_TIMEOUT',
                        'UPBIT_SERVER_UNAVAILABLE': 'FILL_TEMPORARY_UNAVAILABLE', 'READ_CONNECTION_FAILED': 'FILL_TEMPORARY_UNAVAILABLE'}.get(exc.code, code)
            elif isinstance(exc, ccxt.PermissionDenied): code = 'FILL_PERMISSION_REJECTED'
            elif isinstance(exc, ccxt.AuthenticationError): code = 'FILL_AUTH_REJECTED'
            elif isinstance(exc, ccxt.RateLimitExceeded): code = 'FILL_RATE_LIMITED'
            elif isinstance(exc, ccxt.RequestTimeout): code = 'FILL_TIMEOUT'
            raise ReadRejected(code) from None

    def consume(self, observation):
        if type(observation) is not Observation or observation._host is not self:
            return False
        return self._issued.pop(observation._token, None) == observation.encoded

    def _observe(self, scope):
        exchange = scope['exchange']
        key, secret, _ = self.credential(exchange)
        if identity(exchange, key) != scope['account']:
            raise ReadRejected('FILL_ACCOUNT_MISMATCH')
        instrument = scope['symbol'].split(':')[0].replace('/', '')
        source = []
        def get(path, api, params):
            # Only these bounded, authenticated GET resources can be requested.
            allowed = {'BINANCE': {'order', 'myTrades', 'userTrades'},
                       'BYBIT': {'v5/order/history', 'v5/order/realtime', 'v5/execution/list'},
                       'OKX': {'trade/order', 'trade/fills', 'trade/fills-history', 'public/instruments'}}
            if path not in allowed[exchange]:
                raise ReadRejected('FILL_GET_REJECTED')
            client = self._client(scope)
            if client.enableRateLimit:client.throttle()
            client.lastRestRequestTimestamp=client.milliseconds()
            result=self._http(exchange,client.sign(path,api,'GET',params))
            if isinstance(result,dict):
                if exchange=='BYBIT' and result.get('retCode',0)!=0:
                    code={10003:'FILL_AUTH_REJECTED',10004:'FILL_AUTH_REJECTED',10005:'FILL_PERMISSION_REJECTED',10006:'FILL_RATE_LIMITED',10007:'FILL_AUTH_REJECTED',10010:'FILL_IP_REJECTED',10000:'FILL_TIMEOUT'}.get(result.get('retCode'),'FILL_VENUE_REJECTED')
                    raise ReadRejected(code)
                if exchange=='OKX' and result.get('code','0')!='0':
                    raise ReadRejected('FILL_RATE_LIMITED' if result.get('code')=='50011' else 'FILL_VENUE_REJECTED')
                if exchange=='BINANCE' and type(result.get('code')) is int and result['code']<0:
                    raise ReadRejected({-2015:'FILL_AUTH_REJECTED',-2014:'FILL_AUTH_REJECTED',-1003:'FILL_RATE_LIMITED'}.get(result['code'],'FILL_VENUE_REJECTED'))
            # Source digest only; raw responses are not persisted or logged.
            source.append(digest(result))
            return result
        fees = {}
        known_fees = True
        rows = []
        factor = Decimal(1)
        position = '확인 중'
        def fee(value, currency, reverse=False):
            nonlocal known_fees
            if value is None or currency is None or currency == '':
                known_fees = False
                return
            currency = text(currency)
            n = number(value)
            fees[currency] = str(number(fees.get(currency, '0')) + (-n if reverse else n))
        if exchange == 'UPBIT':
            from .upbit_notification_get import UpbitNotificationGET, ReadCredentials
            from .upbit_spot_read import UpbitNotificationReads
            from .bootstrap import SpotScope, AuthenticatedAccount
            instrument = 'KRW-' + scope['symbol'].split('/')[0]
            pocket = str(uuid.uuid5(uuid.NAMESPACE_URL, scope['account']))  # internal credential namespace only
            native_scope = SpotScope('UPBIT', 'poa-estimated-upbit:' + scope['account'], 'pocket:' + pocket, scope['account'], scope['symbol'], instrument, 'KRW', 'poa-estimated-internal-stream', scope['strategy'])
            def credentials():
                k, s, _ = self.credential('UPBIT')
                if identity('UPBIT', k) != scope['account']: raise ReadRejected('FILL_ACCOUNT_MISMATCH')
                return ReadCredentials(k, s)
            getter = UpbitNotificationGET(credentials, lambda fp: None, expected_market=instrument)
            reader = UpbitNotificationReads(native_scope, getter, lambda c: AuthenticatedAccount('UPBIT', native_scope.account, native_scope.subaccount, frozenset({'read'})), pocket_uuid=pocket, as_of_ms=int(time.time()*1000))
            snap = reader.read_notification_order(reader, scope['order_id'], scope['symbol'])
            order = snap.order
            if order['side'] != scope['side']:
                raise ReadRejected('FILL_ORDER_SCOPE')
            if order['status'] == 'open':
                raise ReadRejected('FILL_ORDER_PENDING')
            if order['status'] != 'closed' or number(order['remaining']) != 0:
                raise ReadRejected('FILL_ORDER_NOT_FINAL')
            source.append(snap.snapshot_id)
            for t in order['trades']:
                from .upbit_spot_read import utc_us
                rows.append(dict(id=t['id'], order_id=t['order'], price=t['price'], quantity=t['amount'], quote_amount=t['funds'], timestamp=str(utc_us(t['timestamp']) // 1000)))
            filled, average = order['filled'], order['average']
            quote_total = sum((number(t['quote_amount']) for t in rows), Decimal(0))
            fee(order['paid_fee'], 'KRW')
            client_id = order.get('clientOrderId')
        elif exchange == 'BYBIT':
            category = 'linear' if scope['market_type'] == 'linear' else 'spot'
            params = dict(category=category, orderId=scope['order_id'], symbol=instrument)
            envelope = get('v5/order/history', 'private', params)
            if envelope.get('retCode') != 0: raise ReadRejected('FILL_VENUE_REJECTED')
            if not envelope['result']['list']:
                envelope = get('v5/order/realtime', 'private', params)
            result = envelope['result']
            if envelope.get('retCode') != 0 or result.get('category') != category or result.get('nextPageCursor') or len(result['list']) > 1:
                raise ReadRejected('FILL_ORDER_SCOPE')
            if not result['list']:
                raise ReadRejected('FILL_ORDER_PENDING')  # authenticated order history may lag realtime
            order = result['list'][0]
            if (text(order['orderId']), order['symbol'], order['side'].lower()) != (scope['order_id'], instrument, scope['side']):
                raise ReadRejected('FILL_ORDER_SCOPE')
            if order['orderStatus'] in ('Created', 'New', 'PartiallyFilled', 'Untriggered', 'Triggered', 'Active', 'PendingCancel'):
                raise ReadRejected('FILL_ORDER_PENDING')
            if order['orderStatus'] != 'Filled' or number(order.get('leavesQty', '0')) != 0:
                raise ReadRejected('FILL_ORDER_NOT_FINAL')
            filled, average, quote_total = order['cumExecQty'], order['avgPrice'], number(order['cumExecValue'])
            start_ms, end_ms = order['createdTime'], order['updatedTime']
            client_id = order.get('orderLinkId') or None
            cursor = None
            seen = set()
            for _ in range(40):
                query = dict(params, limit=100)
                if cursor: query['cursor'] = cursor
                page = get('v5/execution/list', 'private', query)
                if page.get('retCode') != 0 or page['result'].get('category') != category or len(page['result']['list']) > 100:
                    raise ReadRejected('FILL_PAGE_INVALID')
                for t in page['result']['list']:
                    if (text(t['orderId']), t['symbol'], t['side'].lower(), t['execType']) != (scope['order_id'], instrument, scope['side'], 'Trade'):
                        raise ReadRejected('FILL_TRADE_SCOPE')
                    rows.append(dict(id=text(t['execId']), order_id=scope['order_id'], price=t['execPrice'], quantity=t['execQty'], quote_amount=t['execValue'], timestamp=t['execTime']))
                    fee(t.get('execFee'), t.get('feeCurrency'))
                cursor = page['result'].get('nextPageCursor')
                if not cursor: break
                if not isinstance(cursor, str) or cursor in seen: raise ReadRejected('FILL_CURSOR_REPEAT')
                seen.add(cursor)
            else: raise ReadRejected('FILL_PAGE_LIMIT')
            if category == 'linear':
                idx = order.get('positionIdx')
                if idx in (1, 2): position = ('LONG' if idx == 1 else 'SHORT')
        elif exchange == 'BINANCE':
            api = 'fapiPrivate' if scope['market_type'] == 'linear' else 'private'
            order = get('order', api, dict(symbol=instrument, orderId=scope['order_id']))
            if (text(order['orderId']), order['symbol'], order['side'].lower()) != (scope['order_id'], instrument, scope['side']):
                raise ReadRejected('FILL_ORDER_SCOPE')
            if order['status'] in ('NEW', 'PARTIALLY_FILLED', 'PENDING_NEW'):
                raise ReadRejected('FILL_ORDER_PENDING')
            if order['status'] != 'FILLED':
                raise ReadRejected('FILL_ORDER_NOT_FINAL')
            filled = order['executedQty']
            if number(order['origQty'], positive=True) != number(filled, positive=True):
                raise ReadRejected('FILL_REMAINING_CONFLICT')
            start_ms, end_ms = order['time'], order['updateTime']
            quote_total = number(order['cumQuote'] if api == 'fapiPrivate' else order['cummulativeQuoteQty'])
            average = order.get('avgPrice') if api == 'fapiPrivate' else str(quote_total / number(filled, positive=True))
            client_id = order.get('clientOrderId') or None
            trades = get('userTrades' if api == 'fapiPrivate' else 'myTrades', api, dict(symbol=instrument, orderId=scope['order_id'], limit=1000))
            # A full cap cannot prove a final page; never scan the whole account.
            if type(trades) is not list or len(trades) >= 1000: raise ReadRejected('FILL_PAGE_LIMIT')
            for t in trades:
                trade_side = t.get('side', 'BUY' if t.get('isBuyer') is True else 'SELL' if t.get('isBuyer') is False else '')
                if (text(t['orderId']), t['symbol'], trade_side.lower()) != (scope['order_id'], instrument, scope['side']):
                    raise ReadRejected('FILL_TRADE_SCOPE')
                rows.append(dict(id=text(t['id']), order_id=scope['order_id'], price=t['price'], quantity=t['qty'], quote_amount=t['quoteQty'], timestamp=str(t['time'])))
                fee(t.get('commission'), t.get('commissionAsset'))
                if api == 'fapiPrivate' and t.get('positionSide') != order.get('positionSide'): raise ReadRejected('FILL_POSITION_CONFLICT')
            if order.get('positionSide') in ('LONG', 'SHORT'): position = order['positionSide']
        else:
            kind = 'SWAP' if scope['market_type'] == 'linear' else 'SPOT'
            instrument = scope['symbol'].split(':')[0].replace('/', '-') + ('-SWAP' if kind == 'SWAP' else '')
            envelope = get('trade/order', 'private', dict(instId=instrument, ordId=scope['order_id']))
            if envelope.get('code') != '0' or len(envelope['data']) != 1: raise ReadRejected('FILL_ORDER_SCOPE')
            order = envelope['data'][0]
            if (order['ordId'], order['instId'], order['instType'], order['side']) != (scope['order_id'], instrument, kind, scope['side']):
                raise ReadRejected('FILL_ORDER_SCOPE')
            if order['state'] in ('live', 'partially_filled'):
                raise ReadRejected('FILL_ORDER_PENDING')
            if order['state'] != 'filled':
                raise ReadRejected('FILL_ORDER_NOT_FINAL')
            if kind == 'SPOT' and order.get('tdMode') != 'cash': raise ReadRejected('FILL_MARKET_UNSUPPORTED')
            if kind == 'SWAP':
                metadata = get('public/instruments', 'public', dict(instType='SWAP', instId=instrument))
                if metadata.get('code') != '0' or len(metadata['data']) != 1: raise ReadRejected('FILL_CONTRACT_UNVERIFIED')
                m = metadata['data'][0]
                if (m['instId'], m['instType'], m['ctType'], m['settleCcy'], m['ctValCcy']) != (instrument, 'SWAP', 'linear', 'USDT', scope['symbol'].split('/')[0]):
                    raise ReadRejected('FILL_CONTRACT_UNVERIFIED')
                factor = number(m['ctVal'], positive=True)
                if factor!=number(scope['expected_contract_size'],positive=True):raise ReadRejected('FILL_CONTRACT_UNVERIFIED')
                if m.get('ctMult') not in (None, '', '1'): raise ReadRejected('FILL_CONTRACT_UNVERIFIED')
            filled, average = str(number(order['accFillSz'], positive=True) * factor), order['avgPx']
            start_ms, end_ms = order['cTime'], order['uTime']
            quote_total = None  # OKX has no order quote-value oracle; complete fills provide it.
            client_id = order.get('clOrdId') or None
            after = None
            cursors = set()
            bills = set()
            for _ in range(40):
                params = dict(instType=kind, instId=instrument, ordId=scope['order_id'], limit='100')
                if after: params['after'] = after
                page = get('trade/fills-history', 'private', params)
                if page.get('code') != '0' or len(page['data']) > 100: raise ReadRejected('FILL_PAGE_INVALID')
                items = page['data']
                for t in items:
                    if (t['ordId'], t['instId'], t['instType'], t['side'], t.get('posSide')) != (scope['order_id'], instrument, kind, scope['side'], order.get('posSide')) or text(t['billId']) in bills:
                        raise ReadRejected('FILL_TRADE_SCOPE')
                    bills.add(t['billId'])
                    q = number(t['fillSz'], positive=True) * factor
                    cost = number(t['fillPx'], positive=True) * q
                    # fillNotionalUsd is a USD reference, not USDT settlement.
                    # It must never be substituted for this exact native-pair sum.
                    rows.append(dict(id=text(t['tradeId']), order_id=scope['order_id'], price=t['fillPx'], quantity=str(q), quote_amount=str(cost), timestamp=t['fillTime'],native_quantity=t['fillSz'],quantity_unit='CONTRACTS' if kind=='SWAP' else 'BASE'))
                    fee(t.get('fee'), t.get('feeCcy'), reverse=True)
                    if t.get('rebate') not in (None, ''): fee(t['rebate'], t.get('rebateCcy'), reverse=True)
                if len(items) < 100: break
                after = text(items[-1]['billId'])
                if after in cursors: raise ReadRejected('FILL_CURSOR_REPEAT')
                cursors.add(after)
            else: raise ReadRejected('FILL_PAGE_LIMIT')
            if order.get('posSide') in ('long', 'short'): position = order['posSide'].upper()
        if scope['client_order_id'] is not None and client_id != scope['client_order_id']:
            raise ReadRejected('FILL_CLIENT_ORDER_MISMATCH')
        if exchange != 'UPBIT':
            start_ms, end_ms = number(start_ms), number(end_ms)
            if not 0 < start_ms <= end_ms <= Decimal(str(time.time()))*1000 or any(not start_ms <= number(t['timestamp']) <= end_ms for t in rows):
                raise ReadRejected('FILL_ORDER_TIME_CONFLICT')
        if exchange == 'BYBIT' and order.get('cumFeeDetail') is not None:
            summary = order['cumFeeDetail']
            if type(summary) is not dict or not known_fees or {k:number(v) for k,v in summary.items()} != {k:number(v) for k,v in fees.items()}:
                raise ReadRejected('FILL_FEE_CONFLICT')
        if exchange == 'OKX' and order.get('fee') not in (None, ''):
            summary={order.get('feeCcy'):-number(order['fee'])}
            if order.get('rebate') not in (None,''):
                ccy=order.get('rebateCcy')
                summary[ccy]=summary.get(ccy,Decimal(0))-number(order['rebate'])
            if not known_fees or {k:number(v) for k,v in fees.items()} != summary:
                raise ReadRejected('FILL_FEE_CONFLICT')
        total = sum((number(t['quote_amount'], positive=True) for t in rows), Decimal(0))
        for t in rows:
            t.setdefault('native_quantity',t['quantity'])
            t.setdefault('quantity_unit','BASE')
            t['quote_source']='PRICE_QUANTITY' if exchange=='OKX' else 'NATIVE_QUOTE'
        if quote_total is not None and total != quote_total: raise ReadRejected('FILL_QUOTE_CONFLICT')
        if scope['market_type'] == 'linear' and position in ('LONG', 'SHORT'):
            close = (position == 'LONG' and scope['side'] == 'sell') or (position == 'SHORT' and scope['side'] == 'buy')
            if close != scope['is_close']: raise ReadRejected('FILL_POSITION_CONFLICT')
            position = ('롱' if position == 'LONG' else '숏') + (' 청산' if close else ' 진입')
        return dict(contract=CONTRACT, scope=scope, source_digest=digest(source), observed_at=time.time(), instrument=instrument,
                    contract_size=str(factor), position=position, filled=str(number(filled, positive=True,derived=True)), average=str(number(average, positive=True,derived=exchange=='UPBIT' or (exchange=='BINANCE' and scope['market_type']=='spot'))),
                    total=str(total), fees=fees if known_fees else None, trades=sorted(rows, key=lambda t: (number(t['timestamp']), t['id'])))
