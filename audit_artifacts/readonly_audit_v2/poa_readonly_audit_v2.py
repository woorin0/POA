#!/usr/bin/env python3
"""POA PR4 historical fill audit: authenticated GET, independent oracle, no DB/order/Discord IO.

Copy this file into the root of the detached PR4 checkout. The only network
calls originate in ReadHost.observe (allowlisted authenticated exchange GETs).
Secrets are prompted interactively; never provide them on command line or save
in the input manifest. This tool does not initialize a POA worker or sender.
"""
from __future__ import annotations

import argparse
from collections import Counter
import http.client
import os
import subprocess
from unittest.mock import patch
from decimal import Decimal, InvalidOperation, localcontext
import getpass
import json
from pathlib import Path
from urllib.parse import urlsplit
import sys

EXPECTED_SHA = "bf6e66e259c7ebbd5099e78b24089caa94936ec9"
VENUES = {"UPBIT", "BYBIT", "BINANCE", "OKX"}
READ_ENDPOINTS = {
    "api.upbit.com": frozenset({"/v1/order"}),
    "api.bybit.com": frozenset({"/v5/order/history", "/v5/order/realtime", "/v5/execution/list"}),
    "api.binance.com": frozenset({"/api/v3/order", "/api/v3/myTrades"}),
    "fapi.binance.com": frozenset({"/fapi/v1/order", "/fapi/v1/userTrades"}),
    "www.okx.com": frozenset({"/api/v5/trade/order", "/api/v5/trade/fills",
                              "/api/v5/trade/fills-history", "/api/v5/public/instruments"}),
}
READ_HOSTS = frozenset(READ_ENDPOINTS)
SUPPORTED = {
    ("UPBIT", "spot"),
    ("BYBIT", "spot"), ("BYBIT", "linear"),
    ("BINANCE", "spot"), ("BINANCE", "linear"),
    ("OKX", "spot"), ("OKX", "linear"),
}


def finite_decimal(x: str) -> Decimal:
    if not isinstance(x, (str, int)) or isinstance(x, bool):
        raise ValueError("reference amount must be a decimal string")
    try:
        y = Decimal(str(x))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid reference decimal") from None
    if (not y.is_finite() or y < 0 or len(y.as_tuple().digits) > 128
            or abs(y.as_tuple().exponent) > 128):
        raise ValueError("reference amount is nonfinite, negative, or unbounded")
    return y


def money(x: str) -> str:
    a = format(Decimal(x), 'f')
    return a.rstrip('0').rstrip('.') if '.' in a else a


def require_order(row: dict) -> None:
    if type(row) is not dict:
        raise ValueError('each order must be an object')
    if row.get('reference_source') not in ('exchange_csv', 'exchange_statement'):
        raise ValueError('reference_source must identify independent exchange CSV or statement')
    client_id = row.get('client_order_id')
    if client_id is not None and (type(client_id) is not str or not 1 <= len(client_id) <= 128 or not client_id.isascii() or not client_id.isprintable()):
        raise ValueError('invalid client_order_id')
    exchange, market = row.get('exchange'), row.get('market')
    if (exchange, market) not in SUPPORTED:
        raise ValueError('unsupported exchange/market')
    if row.get('side') not in ('buy', 'sell'):
        raise ValueError('side must be buy/sell')
    if (not isinstance(row.get('order_id'), str)
            or not 1 <= len(row['order_id']) <= 128
            or not row['order_id'].isprintable()):
        raise ValueError('order_id must be printable and 1..128 chars')
    if row['order_id'].startswith('REPLACE_'):
        raise ValueError('replace the example order_id before preflight')
    symbol = row.get('symbol')
    if not isinstance(symbol, str) or not symbol.count('/') == 1:
        raise ValueError('expected unified symbol')
    expected_quote = 'KRW' if exchange == 'UPBIT' else 'USDT'
    if symbol.split('/')[-1] != (expected_quote+':USDT' if market=='linear' else expected_quote):
        raise ValueError('symbol does not match supported settlement')
    if market=='linear' and type(row.get('is_close')) is not bool:
        raise ValueError('linear position requires original is_close true/false')
    if (exchange, market)==('OKX','linear') and finite_decimal(row.get('contract_size'))<=0:
        raise ValueError('OKX SWAP requires original POA contract_size')
    if not isinstance(row.get('reference_fills'), list) or not 1 <= len(row['reference_fills']) <= 4000:
        raise ValueError('1..4000 independent reference_fills are required')
    if finite_decimal(row.get('reference_total')) <= 0:
        raise ValueError('exchange order summary reference_total must be positive')
    if finite_decimal(row.get('reference_quantity')) <= 0:
        raise ValueError('exchange order summary reference_quantity must be positive')
    for fill in row['reference_fills']:
        if not isinstance(fill, dict) or set(fill)!={'price','quantity_base'}:
            raise ValueError('each reference fill needs price and quantity_base')
        if finite_decimal(fill['price'])<=0 or finite_decimal(fill['quantity_base'])<=0:
            raise ValueError('reference fill must be positive')
    with localcontext() as ctx:
        ctx.prec = 512
        computed_total = sum((finite_decimal(f['price']) * finite_decimal(f['quantity_base'])
                              for f in row['reference_fills']), Decimal(0))
        computed_quantity = sum((finite_decimal(f['quantity_base'])
                                 for f in row['reference_fills']), Decimal(0))
    if computed_total != finite_decimal(row['reference_total']) or computed_quantity != finite_decimal(row['reference_quantity']):
        raise ValueError('independent reference statement does not reconcile with fills')


def verify_checkout(root: Path) -> None:
    """Fail before any secrets or network if SHA or checked-out files differ."""
    if not (root/'exchange/verified_pnl/order_fill_read.py').is_file():
        raise ValueError('execute from the PR4 repository root')
    def git(*args: str) -> str:
        return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL,
                                       text=True, timeout=15).strip()
    if Path(git('rev-parse', '--show-toplevel')).resolve() != root.resolve():
        raise ValueError('incorrect git repository root')
    if git('rev-parse', 'HEAD') != EXPECTED_SHA:
        raise ValueError('wrong POA commit; abort before credential input')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise ValueError('tracked sources modified; use a clean detached checkout')


def limit_network_to_exchange_gets() -> None:
    """Permit only HTTPS GET to enumerated order/fill resources; not an OS sandbox."""
    original = http.client.HTTPSConnection.request
    def guarded(conn, method, url, *args, **kwargs):
        host = str(getattr(conn, 'host', '')).lower()
        target = urlsplit(str(url))
        if (str(method).upper() != 'GET' or host not in READ_ENDPOINTS
                or getattr(conn, 'port', 443) != 443
                or target.scheme or target.netloc or target.fragment
                or target.path not in READ_ENDPOINTS[host]
                or len(str(url)) > 8192 or not str(url).startswith('/')
                or str(url).startswith('//')):
            raise RuntimeError('AUDIT_NETWORK_WRITE_OR_HOST_OR_PATH_BLOCKED')
        return original(conn, method, url, *args, **kwargs)
    http.client.HTTPSConnection.request = guarded
    def deny_http(*args, **kwargs):
        raise RuntimeError('AUDIT_PLAINTEXT_HTTP_BLOCKED')
    http.client.HTTPConnection.request = deny_http
    import requests
    def deny_requests(*args, **kwargs):
        raise RuntimeError('AUDIT_REQUESTS_TRANSPORT_BLOCKED')
    requests.sessions.Session.request = deny_requests


def _confirm_live_read() -> bool:
    """Require interactive approval; operator must provision read-only keys."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    try:
        return input('Type READ_ONLY_KEYS to enable historical authenticated GET (no orders): ').strip() == 'READ_ONLY_KEYS'
    except (EOFError, KeyboardInterrupt):
        return False


def compare_proof_to_reference(item: dict, proof: dict, embed: dict) -> list[str]:
    """Compare real POA validated proof/preview to independent venue source."""
    issues = []
    scope = proof['scope']
    expected = {
        'exchange': item['exchange'], 'order_id': item['order_id'],
        'symbol': item['symbol'], 'side': item['side'],
        'market_type': item['market'],
        'settlement': 'KRW' if item['exchange'] == 'UPBIT' else 'USDT',
    }
    for key, value in expected.items():
        if scope.get(key) != value:
            issues.append('scope mismatch: ' + key)
    actual_fills = Counter((finite_decimal(x['price']), finite_decimal(x['quantity'])) for x in proof['trades'])
    baseline_fills = Counter((finite_decimal(x['price']), finite_decimal(x['quantity_base']))
                             for x in item['reference_fills'])
    with localcontext() as ctx:
        ctx.prec = 512
        total = sum((price * qty for price, qty in baseline_fills.elements()), Decimal(0))
        quantity = sum((qty for _, qty in baseline_fills.elements()), Decimal(0))
        average = total / quantity
    if actual_fills != baseline_fills: issues.append('source fill rows differ')
    if finite_decimal(proof['total']) != total: issues.append('total differs')
    if finite_decimal(proof['filled']) != quantity: issues.append('filled quantity differs')
    if finite_decimal(proof['average']) != average: issues.append('average differs')
    if finite_decimal(item['reference_total']) != total:
        issues.append('exchange order summary total disagrees with independent fill rows')
    if finite_decimal(item['reference_quantity']) != quantity:
        issues.append('exchange order summary quantity disagrees with independent fill rows')
    desc = embed.get('description', '')
    side = item['side']; market = item['market']; venue = item['exchange']; symbol = item['symbol']
    currency = '원' if venue == 'UPBIT' else ' USDT'
    label = '계약 명목가치' if market == 'linear' else '실제 총 매수액' if side == 'buy' else '실제 총 매도액'
    expected_fields = [
        f'실제 평균 체결가: {money(proof["average"])}{currency}',
        f'실제 체결수량: {money(proof["filled"])} {symbol.split("/")[0]}',
        f'{label}: {money(proof["total"])}{currency} (수수료 제외)',
    ]
    for field in expected_fields:
        if field not in desc:
            issues.append('Discord preview field mismatch: ' + field.split(':')[0])
    title = embed.get('title', '')
    if venue not in title or symbol not in title:
        issues.append('Discord preview exchange or symbol mismatch')
    if ('매수' if side == 'buy' else '매도') not in title:
        issues.append('Discord preview buy/sell direction mismatch')
    return issues

def main() -> int:
    ap = argparse.ArgumentParser(description='Read-only historical POA fill and Discord preview audit')
    ap.add_argument('manifest', type=Path, help='JSON of historical order IDs and independently sourced fills')
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preflight', action='store_true', help='validate checkout/manifest without credentials or network')
    mode.add_argument('--live-read', action='store_true', help='explicit opt-in to authenticated GET with read-only keys')
    args = ap.parse_args()
    try:
        verify_checkout(Path.cwd())
        if args.manifest.stat().st_size > 2_000_000:
            raise ValueError('manifest too large; maximum 2 MB')
        document=json.loads(args.manifest.read_text(encoding='utf-8'))
        orders=document['orders']
        if not isinstance(orders,list) or not 1 <= len(orders) <= 100:
            raise ValueError('orders must contain 1..100 entries')
        seen = set()
        for x in orders:
            require_order(x)
            identity_key = (x['exchange'], x['market'], x['symbol'], x['order_id'])
            if identity_key in seen:
                raise ValueError('duplicate historical order reference')
            seen.add(identity_key)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError,
            subprocess.SubprocessError) as e:
        print('PREFLIGHT_FAILED:',type(e).__name__,str(e),file=sys.stderr)
        return 2
    if args.preflight:
        print(f'PREFLIGHT_PASS: exact clean PR4 checkout {EXPECTED_SHA}; {len(orders)} order references valid. No API request performed.')
        return 0

    if not _confirm_live_read():
        print('LIVE_READ_CONFIRMATION_REQUIRED: local interactive TTY and read-only key approval required.', file=sys.stderr)
        return 2
    limit_network_to_exchange_gets()

    # Import only the GET reader, pure proof validator, and pure Embed renderer.
    # Never import main, call tick, start_worker, flush_outbox or run installers.
    from exchange.model.schemas import Settings
    from exchange.verified_pnl.ledger import identity
    from exchange.verified_pnl.order_fill_read import ReadHost, ReadRejected, validate
    from exchange.verified_pnl.order_fill_confirmation import make_payload, render

    exchange_creds={}
    for exchange in dict.fromkeys(x['exchange'] for x in orders):
        print(f'Enter a READ-ONLY {exchange} API key (inputs are not saved):',file=sys.stderr)
        key=getpass.getpass('  API key: ')
        secret=getpass.getpass('  Secret: ')
        passphrase=getpass.getpass('  OKX passphrase: ') if exchange=='OKX' else None
        if not key or not secret or (exchange=='OKX' and not passphrase):
            print('MISSING_READ_ONLY_CREDENTIAL:',exchange,file=sys.stderr)
            return 2
        exchange_creds[exchange]=(key,secret,passphrase)

    configuration={'PASSWORD':'read-only-historical-audit',
                   'VERIFIED_DISCORD_ALERTS':False,
                   'VERIFIED_FILL_PRICE':False,
                   'DISCORD_OUTBOX_ENABLED':False,
                   'POA_EXCHANGE_FILL_CONFIRMATION_ENABLED':False,
                   'DISCORD_WEBHOOK_URL':None}
    for exchange,(key,secret,passphrase) in exchange_creds.items():
        configuration[f'{exchange}_KEY']=key
        configuration[f'{exchange}_SECRET']=secret
        if exchange=='OKX':configuration['OKX_PASSPHRASE']=passphrase
    with patch.dict(os.environ, {}, clear=True):
        st=Settings(_env_file=None,**configuration)
    host=ReadHost(st)
    failed=0
    for i,item in enumerate(orders,1):
        venue=item['exchange']; market=item['market']; side=item['side']; symbol=item['symbol']
        label=f'{i}/{len(orders)} {venue} {market} {symbol} {side} (order ID redacted)'
        try:
            key=exchange_creds[venue][0]
            info={'exchange':venue, 'unified_symbol':symbol,
                  'is_futures':market=='linear','is_coinm':False,
                  'is_buy':side=='buy','is_sell':side=='sell','side':side,
                  'is_close':item.get('is_close',False),
                  'strategy_id':'readonly-audit','order_name':'readonly-audit'}
            if (venue,market)==('OKX','linear'):
                info['contract_size']=str(item['contract_size'])
            result={'id':item['order_id']}
            if item.get('client_order_id'):
                result['clientOrderId']=item['client_order_id']
            request={'info':info,'result':result,'source_account_ref':identity(venue,key)}
            obs=host.observe(request)
            proof=json.loads(obs.encoded)
            if not validate(proof):
                raise ValueError('read proof validation rejected')
            embed=render(make_payload(proof)).data
            issues=compare_proof_to_reference(item,proof,embed)
            print(('PASS' if not issues else 'FAIL')+' '+label)
            print('  qty='+money(proof['filled'])+' avg='+money(proof['average'])+' total='+money(proof['total'])+' '+('KRW' if venue=='UPBIT' else 'USDT'))
            print('  Discord preview (NO POST):')
            print('  '+embed.get('title',''))
            print('  '+' | '.join(s for s in desc.splitlines() if s.startswith(('실제 평균 체결가:','실제 체결수량:','실제 총 매수액:','실제 총 매도액:','계약 명목가치:'))))
            if issues:
                failed+=1
                for issue in issues:print('  ISSUE: '+issue)
        except ReadRejected as exc:
            failed+=1
            print('BLOCKED '+label+' code='+exc.code)
        except Exception as exc:
            failed+=1
            print('BLOCKED '+label+' error_class='+type(exc).__name__)
    print(f'FINAL: {len(orders)-failed}/{len(orders)} locally audited orders PASS. Actual Discord sending and production operations NOT TESTED.')
    return 1 if failed else 0


if __name__=='__main__':
    sys.exit(main())
