"""Original-POA-style Discord order receipt: quantity, price, buy/sell amount.

Only the create_order response and an optional anonymous public ticker are used.
All monetary values remain unverified. No fill lookup, order placement or DB I/O.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

SUPPORTED = frozenset(("UPBIT", "BINANCE", "BYBIT", "BITGET", "OKX"))
REJECTED_STATUSES = frozenset(("rejected", "canceled", "cancelled", "expired", "failed"))


def is_rejected_response(result):
    return isinstance(result, dict) and str(result.get("status") or "").lower() in REJECTED_STATUSES


def _positive(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n <= 0 or len(n.as_tuple().digits) > 40 or abs(n.as_tuple().exponent) > 40:
            return None
        return n
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def _fmt(n):
    value = format(n, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _first(*values):
    for value in values:
        number = _positive(value)
        if number is not None:
            return number
    return None


def _quote_currency(exchange, info):
    if exchange == "UPBIT":
        return "KRW"
    symbol = str(getattr(info, "unified_symbol", "") or "")
    quote = symbol.split("/", 1)[-1].split(":", 1)[0] if "/" in symbol else str(getattr(info, "quote", "") or "")
    return quote.removesuffix(".P").upper() if quote else "USDT"


def _response_cost(exchange, result):
    result = result if isinstance(result, dict) else {}
    native = result.get("info")
    native = native if isinstance(native, dict) else {}
    amount = _positive(result.get("cost"))
    if amount is not None:
        return amount
    if exchange == "BINANCE":
        return _first(native.get("cummulativeQuoteQty"), native.get("cumQuote"))
    if exchange == "BYBIT":
        return _first(native.get("cumExecValue"))
    if exchange == "BITGET":
        return _first(native.get("quoteVolume"), native.get("fillTotalAmount"))
    return None


def _quantity(result, info):
    """Return (display quantity, base-asset quantity, source, contract count).

    For OKX linear contracts, a CCXT response amount is a contract count.
    A request amount, however, is the original user base quantity.
    """
    result = result if isinstance(result, dict) else {}
    filled = _positive(result.get("filled"))
    ordered = _positive(result.get("amount"))
    requested = _positive(getattr(info, "amount", None))
    raw = filled if filled is not None else ordered if ordered is not None else requested
    source = "응답" if filled is not None or ordered is not None else "요청"
    if raw is None:
        return None, None, source, None
    contract = bool(getattr(info, "is_futures", False) and getattr(info, "is_contract", False))
    if contract and source == "응답":
        factor = _positive(getattr(info, "contract_size", None))
        if factor is None:
            return raw, None, "계약(환산 불가)", raw
        if getattr(info, "is_coinm", False):
            # Inverse COIN-M contractSize generally denotes quote value/contract.
            return raw, None, "계약", raw
        return raw * factor, raw * factor, "응답 계약 환산", raw
    if contract and source == "요청" and getattr(info, "is_coinm", False):
        return raw, raw, "요청", None
    return raw, raw, source, None


def order_price(exchange, result, info, reference_price=None):
    """Price with provenance: response, derived response, request, public, unavailable."""
    result = result if isinstance(result, dict) else {}
    p = _positive(result.get("average"))
    if p is not None:
        return p, "응답 평균가"
    p = _positive(result.get("price"))
    if p is not None:
        return p, "응답 가격"
    qty, base_qty, origin, _ = _quantity(result, info)
    cost = _response_cost(exchange, result)
    if cost is not None and base_qty is not None and origin.startswith("응답"):
        try:
            derived = cost / base_qty
            if derived.is_finite() and derived > 0:
                return derived, "응답 금액÷수량 추정"
        except (InvalidOperation, ZeroDivisionError):
            pass
    p = _positive(getattr(info, "price", None))
    if p is not None:
        return p, "요청 참고가"
    p = _positive(reference_price)
    if p is not None:
        return p, "공개 현재가 추정"
    return None, "확인 불가"


def order_amount(exchange, result, order_info, reference_price=None):
    """Return quote notional and 'response', 'estimate', or 'unavailable'."""
    if exchange not in SUPPORTED:
        return None, "unavailable"
    result = result if isinstance(result, dict) else {}
    cost = _response_cost(exchange, result)
    if cost is not None:
        return cost, "response"
    qty, base_qty, origin, contracts = _quantity(result, order_info)
    if contracts is not None and getattr(order_info, "is_coinm", False):
        factor = _positive(getattr(order_info, "contract_size", None))
        if factor is not None:
            return contracts * factor, "estimate"
        return None, "unavailable"
    price, _ = order_price(exchange, result, order_info, reference_price)
    if base_qty is not None and price is not None:
        return base_qty * price, "estimate"
    return None, "unavailable"


def public_reference_price(exchange, symbol):
    """Anonymous ticker read only; failure must not block the order alert."""
    if exchange not in SUPPORTED or not isinstance(symbol, str) or "/" not in symbol:
        return None
    try:
        import ccxt
        client = getattr(ccxt, exchange.lower())({"timeout": 3500, "enableRateLimit": True})
        ticker = client.fetch_ticker(symbol)
        return _positive(ticker.get("last")) if isinstance(ticker, dict) else None
    except Exception:
        return None


def build_simple_amount_alert(exchange, result, order_info, reference_price=None):
    from dhooks import Embed
    from exchange.utility.discord_embed import sanitize_discord_text, enforce_embed_bounds
    clean = lambda value, limit=90: sanitize_discord_text(str(value or ""), max_len=limit)
    if exchange not in SUPPORTED:
        raise ValueError("UNSUPPORTED_SIMPLE_AMOUNT_EXCHANGE")
    if not isinstance(result, dict) or is_rejected_response(result):
        raise ValueError("NO_SUCCESSFUL_ORDER_RESPONSE")
    action = "매수" if getattr(order_info, "is_buy", False) else "매도" if getattr(order_info, "is_sell", False) else "주문"
    direction = action
    if getattr(order_info, "is_futures", False):
        if getattr(order_info, "is_entry", False):
            direction = "롱 진입" if action == "매수" else "숏 진입" if action == "매도" else action
        elif getattr(order_info, "is_close", False):
            direction = "숏 청산" if action == "매수" else "롱 청산" if action == "매도" else action
    symbol = clean(getattr(order_info, "unified_symbol", None) or
                   str(getattr(order_info, "base", "")) + "/" + str(getattr(order_info, "quote", "")), 48)
    base = clean(getattr(order_info, "base", ""), 16)
    currency = clean(_quote_currency(exchange, order_info), 12)
    qty, base_qty, qty_source, contracts = _quantity(result, order_info)
    price, price_source = order_price(exchange, result, order_info, reference_price)
    amount, source = order_amount(exchange, result, order_info, reference_price)
    if qty is None:
        qty_line = "확인 불가"
    elif contracts is not None and getattr(order_info, "is_coinm", False):
        qty_line = _fmt(qty) + " 계약 (주문 응답·미검증)"
    else:
        tag = ("응답 계약 환산" if qty_source == "응답 계약 환산"
               else "주문 응답" if qty_source == "응답" else "요청")
        qty_line = _fmt(qty) + " " + base + " (" + tag + "·미검증)"
    price_line = (_fmt(price) + " " + currency + " (" + price_source + "·미검증)"
                  if price is not None else "확인 불가")
    if amount is None:
        amount_line = "확인 불가"
    elif source == "response":
        amount_line = _fmt(amount) + " " + currency + " (주문 응답·미검증)"
    else:
        amount_line = _fmt(amount) + " " + currency + " (추정·미검증)"
    amount_label = action + "액"
    if getattr(order_info, "is_futures", False):
        amount_label += " (계약 명목가치)"
    oid = result.get("id") or result.get("uuid")
    if not isinstance(oid, (str, int)) or isinstance(oid, bool) or len(str(oid)) > 128:
        oid = None
    lines = [
        "전략: " + clean(getattr(order_info, "strategy_id", None) or getattr(order_info, "order_name", None) or "미확인"),
        "수량: " + qty_line,
        "가격: " + price_line,
        amount_label + ": " + amount_line,
    ]
    if oid is not None:
        lines.append("주문번호: " + clean(oid, 128))
    lines += [
        "상태: 주문 응답 수신 (실체결 미검증)",
        "시각: " + datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST"),
    ]
    title = clean(exchange, 12) + " · " + symbol + " · " + direction + " 주문 응답"
    embed = enforce_embed_bounds(Embed(title=title, description="\n".join(lines), color=0x64748B))
    return "[" + clean(exchange) + "] " + symbol + " " + direction + " 주문 응답", embed
