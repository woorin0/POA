"""Simple original-POA-style order response amount, without another exchange read.

Only an order acknowledgement is available here. Never present this as an
authenticated execution amount, financial PnL, or a confirmed fill.
"""
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta

SUPPORTED = frozenset(("UPBIT", "BINANCE", "BYBIT", "BITGET"))
REJECTED_STATUSES = frozenset(("rejected", "canceled", "cancelled", "expired", "failed"))


def is_rejected_response(result):
    """A terminal rejected/canceled response must not appear as a buy/sell acknowledgement."""
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
    return format(n, "f").rstrip("0").rstrip(".") if "." in format(n, "f") else format(n, "f")


def _first(*values):
    for value in values:
        n = _positive(value)
        if n is not None:
            return n
    return None


def order_amount(exchange, result, order_info, reference_price=None):
    """Return (amount, source) where source is response, estimate, or unavailable."""
    result = result if isinstance(result, dict) else {}
    native = result.get("info")
    native = native if isinstance(native, dict) else {}
    # CCXT cost and native cumulative quote values are *order response* fields.
    # Not all create_order responses contain them; never assume an order is filled.
    quote = _first(result.get("cost"))
    if quote is None and exchange == "BINANCE":
        quote = _first(native.get("cummulativeQuoteQty"), native.get("cumQuote"))
    elif quote is None and exchange == "BYBIT":
        quote = _first(native.get("cumExecValue"))
    elif quote is None and exchange == "BITGET":
        quote = _first(native.get("quoteVolume"), native.get("fillTotalAmount"))
    if quote is not None:
        return quote, "response"

    qty = _first(result.get("filled"), result.get("amount"), getattr(order_info, "amount", None))
    price = _first(result.get("average"), result.get("price"), getattr(order_info, "price", None), reference_price)
    # For quote-quantity market buys, do not accidentally multiply quote units
    # by price a second time. The original POA's order_info.amount is base qty
    # for its four market-buy adapters, but unknown native orderQty is not.
    if qty is not None and price is not None:
        if getattr(order_info, "is_futures", False) and getattr(order_info, "is_contract", False):
            factor = _positive(getattr(order_info, "contract_size", None))
            if factor is None:
                return None, "unavailable"
            qty *= factor
        return qty * price, "estimate"
    return None, "unavailable"


def public_reference_price(exchange, symbol):
    """Optional anonymous ticker GET, only when a response lacks any amount/price.

    This is a *current-market* estimate, not the order's execution price.
    Never supply API credentials or submit an order. Failure is nonfatal.
    """
    if exchange not in SUPPORTED or not isinstance(symbol, str) or "/" not in symbol:
        return None
    try:
        import ccxt
        client = getattr(ccxt, exchange.lower())({"timeout": 3500, "enableRateLimit": True})
        value = client.fetch_ticker(symbol)
        return _positive(value.get("last")) if isinstance(value, dict) else None
    except Exception:
        return None


def build_simple_amount_alert(exchange, result, order_info, reference_price=None):
    """Use the legacy Discord sender only; no order calls, fills reads, or DB writes."""
    from dhooks import Embed
    from exchange.utility.discord_embed import sanitize_discord_text, enforce_embed_bounds
    clean = lambda v, limit=90: sanitize_discord_text(str(v or ""), max_len=limit)
    side = "매수" if getattr(order_info, "is_buy", False) else "매도" if getattr(order_info, "is_sell", False) else "주문"
    if getattr(order_info, "is_futures", False):
        if getattr(order_info, "is_entry", False):
            side = "롱 진입" if side == "매수" else "숏 진입" if side == "매도" else side
        elif getattr(order_info, "is_close", False):
            side = "숏 청산" if side == "매수" else "롱 청산" if side == "매도" else side
    action = "매수" if getattr(order_info, "is_buy", False) else "매도" if getattr(order_info, "is_sell", False) else "주문"
    symbol = clean(getattr(order_info, "unified_symbol", None) or
                   str(getattr(order_info, "base", "")) + "/" + str(getattr(order_info, "quote", "")), 48)
    # Request quotes use USDT.P for swaps, but their monetary unit is USDT.
    quote_currency = "KRW" if exchange == "UPBIT" else clean(symbol.split("/", 1)[-1].split(":", 1)[0], 10)
    if quote_currency.endswith(".P"):
        quote_currency = quote_currency[:-2]
    amount, source = order_amount(exchange, result, order_info, reference_price)
    if source == "response":
        amount_text = _fmt(amount) + " " + quote_currency + " (주문 응답·미검증)"
    elif source == "estimate":
        amount_text = _fmt(amount) + " " + quote_currency + (" (공개 현재가 추정·미검증)" if reference_price is not None and not _first(result.get("average") if isinstance(result, dict) else None, result.get("price") if isinstance(result, dict) else None, getattr(order_info, "price", None)) else " (수량×가격 추정·미검증)")
    else:
        amount_text = "확인 불가 (주문 응답에 금액·가격 정보 없음)"
    result = result if isinstance(result, dict) else {}
    oid = result.get("id") or result.get("uuid")
    if not isinstance(oid, (str, int)) or len(str(oid)) > 128:
        oid = None
    title = clean(exchange, 12) + " · " + symbol + " · " + side + " 주문 응답"
    lines = [
        "전략: " + clean(getattr(order_info, "strategy_id", None) or getattr(order_info, "order_name", None) or "미확인"),
        ("계약 명목가치" if getattr(order_info, "is_futures", False) else action + "금액") + ": " + amount_text,
    ]
    qty = _first(result.get("filled"), result.get("amount"), getattr(order_info, "amount", None))
    if qty is not None:
        lines.append("주문 응답/요청 수량: " + _fmt(qty) + " " + clean(getattr(order_info, "base", ""), 16))
    if oid is not None:
        lines.append("주문번호: " + clean(oid, 128))
    lines.extend([
        "상태: 주문 응답 수신 (실체결 검증 안 함)",
        "수수료·실현손익: 미확정",
        "시각: " + datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST"),
    ])
    embed = enforce_embed_bounds(Embed(title=title, description="\n".join(lines), color=0x64748B))
    return "[" + clean(exchange) + "] " + symbol + " " + side + " 주문 응답", embed
