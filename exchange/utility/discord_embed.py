"""Unified mobile-friendly Discord Embed builder and pure formatters for POA v2."""
import math
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

try:
    from dhooks import Embed
    if not hasattr(Embed, "data"):
        try:
            Embed.data = property(lambda self: self.to_dict())
        except Exception:
            pass
except ImportError:
    class Embed:
        def __init__(self, **kw):
            self.data = dict(kw)
            self.data.setdefault("fields", [])

        def add_field(self, **kw):
            self.data.setdefault("fields", []).append(kw)

from exchange.verified_pnl.models import (
    ExitReason,
    FillStatus,
    NotificationPayload,
    PnlResult,
    VerificationStatus,
)

# 1. Color Palette (Section 3.3)
COLOR_BUY_COMPLETED = 0x16A34A       # #16A34A (Green) - 매수 완료 / 진입 완료
COLOR_SELL_COMPLETED = 0xEF4444      # #EF4444 (Red)   - 매도 완료 / 종료 완료 (익절·손절 무관 항상 빨간색)
COLOR_ORDER_FAILED = 0xF59E0B        # #F59E0B (Orange)- 주문 실패 확정
COLOR_ORDER_AMBIGUOUS = 0x64748B     # #64748B (Gray)  - 주문 결과 미확정 / 수동 확인 / 부분 체결 / 취소 / 레거시 미검증

KST = timezone(timedelta(hours=9))

# 2. ExitReason Korean Mapping (Section 5.4)
EXIT_REASON_KOREAN: Dict[str, str] = {
    ExitReason.TP.value: "익절 조건",
    ExitReason.SL.value: "손절 조건",
    ExitReason.TRAIL.value: "트레일링 스톱",
    ExitReason.BE.value: "본절 조건",
    ExitReason.TIME.value: "시간 청산",
    ExitReason.SIGNAL.value: "전략 종료 신호",
    ExitReason.MANUAL.value: "수동 청산",
    ExitReason.LIQUIDATION.value: "강제 청산",
    ExitReason.UNKNOWN.value: "청산 사유 미확인",
}

_SECRET_PATTERNS = [
    re.compile(r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/[^\s\]\)\"']+", re.IGNORECASE),
    re.compile(r"https?://[^\s\]\)\"']+", re.IGNORECASE),
    re.compile(r"(?i)(password|secret|token|api[_-]?key|passphrase)\s*[:=]\s*[^\s,;\"'\)\}]+"),
]


def sanitize_discord_text(text: Optional[Any], max_len: Optional[int] = None) -> str:
    """Neutralizes Discord mentions, markdown breakage, URLs, and sensitive tokens."""
    if text is None:
        return ""
    s = str(text)
    # Remove secrets and URLs
    s = _SECRET_PATTERNS[0].sub("[WEBHOOK_REDACTED]", s)
    s = _SECRET_PATTERNS[1].sub("[URL_REDACTED]", s)
    s = _SECRET_PATTERNS[2].sub(r"\1=[REDACTED]", s)
    # Neutralize Discord mass mentions & user/role mentions
    s = s.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    s = re.sub(r"<@&?\d+>", "[mention]", s)
    # Escape backticks that could break code/markdown blocks
    s = s.replace("```", "'''").replace("`", "'")
    # Collapse excessive control chars
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if max_len is not None and len(s) > max_len:
        if max_len <= 1:
            return s[:max_len]
        return s[: max_len - 1] + "…"
    return s


def _to_finite_decimal(val: Optional[Any]) -> Optional[Decimal]:
    """Parses numeric value or string (with optional currency/commas) into a finite Decimal."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, Decimal):
        return val if val.is_finite() else None
    if isinstance(val, (int, float)):
        if isinstance(val, float) and not math.isfinite(val):
            return None
        try:
            d = Decimal(str(val))
            return d if d.is_finite() else None
        except Exception:
            return None
    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned or cleaned in ("-", "미확정", "미확인", "None", "null"):
            return None
        # Strip trailing currency or unit tokens like " KRW", " USDT", "원", "%"
        cleaned = re.sub(r"\s*(KRW|USDT|USDC|BTC|ETH|SOL|XRP|원|%)\s*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace(",", "").strip()
        if not cleaned:
            return None
        try:
            d = Decimal(cleaned)
            return d if d.is_finite() else None
        except (InvalidOperation, ValueError):
            return None
    return None


def detect_currency(symbol: Optional[str], pnl_currency: Optional[str] = None, exchange: Optional[str] = None) -> str:
    """Infers quote/settlement currency (e.g., KRW vs USDT)."""
    if pnl_currency:
        c = str(pnl_currency).strip().upper()
        if c and c != "UNKNOWN":
            return c
    sym = str(symbol or "").upper()
    if "/KRW" in sym or sym.endswith("KRW"):
        return "KRW"
    if "/USDT" in sym or "USDT" in sym:
        return "USDT"
    if "/USDC" in sym or "USDC" in sym:
        return "USDC"
    if "/BUSD" in sym or "BUSD" in sym:
        return "BUSD"
    if str(exchange or "").upper() in ("UPBIT", "KRX"):
        return "KRW"
    return "USDT"


def extract_base_asset(symbol: Optional[str]) -> str:
    """Extracts base asset symbol from unified symbol (e.g., 'SOL/KRW' -> 'SOL', 'BTC/USDT:USDT' -> 'BTC')."""
    if not symbol:
        return ""
    sym = str(symbol).strip()
    if "/" in sym:
        return sym.split("/", 1)[0].strip()
    return sym


def normalize_display_symbol(symbol: Optional[str]) -> str:
    """Normalizes symbol for mobile header (e.g., 'BTC/USDT:USDT' -> 'BTC/USDT')."""
    if not symbol:
        return "UNKNOWN"
    sym = sanitize_discord_text(symbol, max_len=40).strip()
    if ":" in sym:
        left, right = sym.split(":", 1)
        if "/" in left:
            quote = left.split("/", 1)[1].replace(".P", "")
            if right == quote:
                return left
    return sym


def normalize_strategy_name(
    order_name: Optional[str],
    strategy_id: Optional[str] = None,
    max_len: int = 40,
) -> Optional[str]:
    """Returns clean strategy name or None if default/empty."""
    candidates = [order_name, strategy_id]
    for cand in candidates:
        if cand is None:
            continue
        cleaned = sanitize_discord_text(cand, max_len=max_len).strip()
        if cleaned and cleaned not in ("주문", "Order", "default", "None", "익절 알림", "손절 알림", "본전 알림"):
            return cleaned
    return None


def format_kst_full(utc_timestamp: Optional[float]) -> str:
    """Formats UTC timestamp into 'YYYY-MM-DD HH:MM:SS KST'."""
    if utc_timestamp is None:
        return "일시 미확인"
    try:
        ts = float(utc_timestamp)
        if not math.isfinite(ts):
            return "일시 미확인"
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(KST)
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} KST"
    except Exception:
        return "일시 미확인"


def format_price_display(
    val: Optional[Any],
    currency: str = "KRW",
    signed: bool = False,
    is_fee: bool = False,
) -> str:
    """Formats price/amount/fee/PnL according to Section 3.5 currency rules."""
    d = _to_finite_decimal(val)
    if d is None:
        return "미확정"

    ccy = (currency or "USDT").upper()
    sign_prefix = ""
    if signed:
        if d > 0:
            sign_prefix = "+"
        elif d < 0:
            sign_prefix = "-"
    elif d < 0:
        sign_prefix = "-"

    abs_d = abs(d)

    if ccy == "KRW":
        if is_fee:
            # Upbit KRW fee often has fractional decimals like 367.56 KRW
            q = abs_d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            s = f"{q:,.2f}"
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return f"{sign_prefix}{s}원"
        # Standard KRW price / notional / PnL: integer won if >= 100 or whole number
        if abs_d >= Decimal("100") or abs_d == abs_d.to_integral_value():
            q = abs_d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            return f"{sign_prefix}{q:,f}원"
        elif abs_d >= Decimal("1"):
            q = abs_d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            s = f"{q:,.2f}".rstrip("0").rstrip(".")
            return f"{sign_prefix}{s}원"
        else:
            q = abs_d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            s = f"{q:,.4f}".rstrip("0").rstrip(".")
            return f"{sign_prefix}{s}원"
    else:
        # USDT / USD / other quote currency
        if abs_d >= Decimal("100"):
            q = abs_d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            s = f"{q:,.2f}".rstrip("0").rstrip(".")
        elif abs_d >= Decimal("1"):
            q = abs_d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            s = f"{q:,.4f}".rstrip("0").rstrip(".")
        else:
            q = abs_d.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
            s = f"{q:,.8f}".rstrip("0").rstrip(".")
        if s == "":
            s = "0"
        return f"{sign_prefix}{s} {ccy}"


def format_quantity_display(val: Optional[Any], base_asset: Optional[str] = None) -> str:
    """Formats asset quantity without trailing zeros, e.g., '5.105 SOL' or '0.00012345 BTC'."""
    d = _to_finite_decimal(val)
    if d is None:
        return "미확정"
    q = d.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    s = f"{q:,f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if not s or s == "-0":
        s = "0"
    asset = sanitize_discord_text(base_asset, max_len=16).strip() if base_asset else ""
    return f"{s} {asset}".strip()


def format_roi_display(val: Optional[Any]) -> Optional[str]:
    """Formats ROI percentage rounded to 2 decimals with explicit sign (+2.68% / -1.42% / 0.00%)."""
    d = _to_finite_decimal(val)
    if d is None:
        return None
    q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q > 0:
        return f"+{q:.2f}%"
    elif q < 0:
        return f"{q:.2f}%"
    return "0.00%"


def format_exit_reason_korean(reason: Optional[Any]) -> str:
    """Maps ExitReason enum/string to Korean label (Section 5.4)."""
    if reason is None:
        return EXIT_REASON_KOREAN[ExitReason.UNKNOWN.value]
    key = reason.value if hasattr(reason, "value") else str(reason).upper()
    return EXIT_REASON_KOREAN.get(key, EXIT_REASON_KOREAN[ExitReason.UNKNOWN.value])


def is_sell_or_close_side(side: Optional[str]) -> bool:
    s = str(side or "").strip()
    return any(tok in s for tok in ("매도", "종료", "청산", "sell", "close"))


def is_buy_or_entry_side(side: Optional[str]) -> bool:
    s = str(side or "").strip()
    if is_sell_or_close_side(s):
        return False
    return any(tok in s for tok in ("매수", "진입", "buy", "entry"))


def get_action_header_label(side: Optional[str], status_suffix: str) -> str:
    """Builds compact header action label (e.g., '매수 완료', '매도 완료', '롱 진입 완료', '롱 종료 완료')."""
    s = sanitize_discord_text(side or "주문", max_len=20).strip()
    if not s:
        s = "주문"
    if not status_suffix:
        return s
    if s.endswith(status_suffix):
        return s
    return f"{s} {status_suffix}"


def enforce_embed_bounds(embed: Embed) -> Embed:
    """Enforces Discord Embed size limits (Title <= 256, Description <= 4096, Fields <= 25, Total <= 6000)."""
    if hasattr(embed, "__dict__") and "data" in embed.__dict__:
        data = embed.data
        if "title" in data and data["title"]:
            data["title"] = str(data["title"])[:256]
        if "description" in data and data["description"]:
            data["description"] = str(data["description"])[:4096]
        fields = data.get("fields", [])
        if len(fields) > 25:
            fields = fields[:25]
            data["fields"] = fields
        for f in fields:
            if "name" in f and f["name"]:
                f["name"] = str(f["name"])[:256]
            if "value" in f and f["value"]:
                f["value"] = str(f["value"])[:1024]
    else:
        # dhooks.Embed object
        if getattr(embed, "title", None):
            embed.title = str(embed.title)[:256]
        if getattr(embed, "description", None):
            embed.description = str(embed.description)[:4096]
        if getattr(embed, "fields", None) is not None:
            if len(embed.fields) > 25:
                embed.fields = embed.fields[:25]
            for f in embed.fields:
                if hasattr(f, "name") and f.name:
                    f.name = str(f.name)[:256]
                if hasattr(f, "value") and f.value:
                    f.value = str(f.value)[:1024]
                elif isinstance(f, dict):
                    if f.get("name"):
                        f["name"] = str(f["name"])[:256]
                    if f.get("value"):
                        f["value"] = str(f["value"])[:1024]

    def _total_len() -> int:
        d = embed.data
        total = len(d.get("title") or "") + len(d.get("description") or "")
        for f in d.get("fields", []):
            total += len(f.get("name") or "") + len(f.get("value") or "")
        return total

    while _total_len() > 5800:
        if hasattr(embed, "__dict__") and "data" in embed.__dict__:
            desc = embed.data.get("description") or ""
            if len(desc) > 250:
                embed.data["description"] = desc[: len(desc) - 250] + "…"
            elif embed.data.get("fields"):
                embed.data["fields"].pop()
            else:
                break
        else:
            desc = getattr(embed, "description", "") or ""
            if len(desc) > 250:
                embed.description = desc[: len(desc) - 250] + "…"
            elif getattr(embed, "fields", None):
                embed.fields.pop()
            else:
                break
    return embed


def _compute_total_fee(payload: NotificationPayload, currency: str) -> Tuple[Optional[Decimal], Optional[str]]:
    """Computes total fee in settlement currency if unambiguous, else returns fee_breakdown."""
    entry_fee_d = _to_finite_decimal(payload.entry_fee_allocated)
    exit_fee_d = _to_finite_decimal(payload.exit_fee)

    # For sell/close: if both entry_fee_allocated and exit_fee exist, sum them
    if is_sell_or_close_side(payload.side):
        if entry_fee_d is not None and exit_fee_d is not None:
            total = entry_fee_d + exit_fee_d
            return total, format_price_display(total, currency=currency, is_fee=True)
        if exit_fee_d is not None and entry_fee_d is None:
            return exit_fee_d, format_price_display(exit_fee_d, currency=currency, is_fee=True)

    # Check fee_breakdown (e.g., "367.56 KRW" or "0.05 USDT")
    if payload.fee_breakdown:
        parts = [p.strip() for p in str(payload.fee_breakdown).split(",") if p.strip()]
        if len(parts) == 1:
            d = _to_finite_decimal(parts[0])
            if d is not None:
                # Extract currency from part if present
                tokens = parts[0].split()
                fee_ccy = tokens[-1].upper() if len(tokens) > 1 else currency
                return d, format_price_display(d, currency=fee_ccy, is_fee=True)
        return None, sanitize_discord_text(payload.fee_breakdown, max_len=120)

    if exit_fee_d is not None:
        return exit_fee_d, format_price_display(exit_fee_d, currency=currency, is_fee=True)
    return None, None


def _compute_notional_display(
    price_val: Optional[Any],
    qty_val: Optional[Any],
    currency: str,
    explicit_notional: Optional[Any] = None,
) -> Optional[str]:
    if explicit_notional is not None:
        d_exp = _to_finite_decimal(explicit_notional)
        if d_exp is not None and d_exp > 0:
            return format_price_display(d_exp, currency=currency)
    p = _to_finite_decimal(price_val)
    q = _to_finite_decimal(qty_val)
    if p is not None and q is not None and p > 0 and q > 0:
        return format_price_display(p * q, currency=currency)
    return None


def build_mobile_verified_embed(payload: NotificationPayload) -> Embed:
    """
    Builds the POA v2 unified mobile-friendly Discord Embed from a NotificationPayload.
    Also populates backward-compatible Embed fields so both mobile scan (title/description)
    and structured field inspection work seamlessly.
    """
    exchange = sanitize_discord_text(payload.exchange or "UNKNOWN", max_len=24).upper()
    display_symbol = normalize_display_symbol(payload.symbol)
    base_asset = extract_base_asset(display_symbol)
    currency = detect_currency(display_symbol, getattr(payload, "pnl_currency", None), exchange)
    strategy_name = normalize_strategy_name(payload.order_name, getattr(payload, "strategy_id", None))

    ts = payload.fill_timestamp if payload.fill_timestamp is not None else payload.event_timestamp
    kst_str = format_kst_full(ts)

    is_manual_review = (
        payload.event_type == "manual_review"
        or "MANUAL_REVIEW" in str(payload.error_message or "")
    )
    status = payload.fill_status

    # Determine category & color (Section 3.3)
    if is_manual_review or payload.event_type == "unverified" or status in (FillStatus.UNVERIFIED, FillStatus.PENDING):
        color = COLOR_ORDER_AMBIGUOUS
        category = "AMBIGUOUS"
    elif status == FillStatus.PARTIALLY_FILLED:
        color = COLOR_ORDER_AMBIGUOUS
        category = "PARTIAL"
    elif status == FillStatus.CANCELED:
        color = COLOR_ORDER_AMBIGUOUS
        category = "CANCELED"
    elif status == FillStatus.FAILED:
        color = COLOR_ORDER_FAILED
        category = "FAILED"
    elif status == FillStatus.FILLED:
        if is_sell_or_close_side(payload.side):
            color = COLOR_SELL_COMPLETED
            category = "SELL_FILLED"
        else:
            color = COLOR_BUY_COMPLETED
            category = "BUY_FILLED"
    else:
        color = COLOR_ORDER_AMBIGUOUS
        category = "AMBIGUOUS"

    lines: List[str] = []

    if category == "BUY_FILLED":
        header_action = get_action_header_label(payload.side, "완료")
        title = f"🟢 {exchange} · {display_symbol} · {header_action}"
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        if payload.leverage is not None and int(payload.leverage) > 1:
            lines.append(f"레버리지: {payload.leverage}x")

        price_str = format_price_display(payload.exit_price or payload.entry_price, currency=currency)
        qty_str = format_quantity_display(payload.filled_qty, base_asset=base_asset)
        notional_str = _compute_notional_display(
            payload.exit_price or payload.entry_price,
            payload.filled_qty,
            currency=currency,
            explicit_notional=getattr(payload, "notional_amount", None),
        )
        _, fee_str = _compute_total_fee(payload, currency=currency)

        lines.append(f"체결가: {price_str}")
        lines.append(f"체결수량: {qty_str}")
        if notional_str:
            lines.append(f"매수금액: {notional_str}")
        if fee_str:
            lines.append(f"수수료: {fee_str}")
        lines.append("")
        if payload.verification_status == VerificationStatus.VERIFIED:
            lines.append("✅ 실체결 검증 완료")
        else:
            lines.append("ℹ️ 체결 내역 확인")
        lines.append(f"🕒 {kst_str}")

    elif category == "SELL_FILLED":
        header_action = get_action_header_label(payload.side, "완료")
        title = f"🔴 {exchange} · {display_symbol} · {header_action}"
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        if payload.leverage is not None and int(payload.leverage) > 1:
            lines.append(f"레버리지: {payload.leverage}x")

        net_pnl_d = _to_finite_decimal(payload.net_pnl)
        roi_d = _to_finite_decimal(payload.roi_pct)
        pnl_verified = (
            payload.verification_status == VerificationStatus.VERIFIED
            and net_pnl_d is not None
        )
        roi_verified = pnl_verified and roi_d is not None

        # Top PnL Banner (Cases A, B, C in Section 5.2)
        if pnl_verified and roi_verified:
            # Case A: Both Net PnL & ROI verified
            pnl_str = format_price_display(net_pnl_d, currency=currency, signed=True)
            roi_str = format_roi_display(roi_d)
            if net_pnl_d > 0:
                lines.append(f"💰 확정손익: {pnl_str} ({roi_str})")
            elif net_pnl_d < 0:
                lines.append(f"📉 확정손익: {pnl_str} ({roi_str})")
            else:
                lines.append(f"➖ 확정손익: {pnl_str} ({roi_str})")
        elif pnl_verified and not roi_verified:
            # Case B: Net PnL verified, ROI unverified
            pnl_str = format_price_display(net_pnl_d, currency=currency, signed=True)
            if net_pnl_d > 0:
                lines.append(f"💰 확정손익: {pnl_str}")
            elif net_pnl_d < 0:
                lines.append(f"📉 확정손익: {pnl_str}")
            else:
                lines.append(f"➖ 확정손익: {pnl_str}")
            lines.append("수익률: 미확정 (투입 원금/증거금 확인 필요)")
        else:
            # Case C: Both unverified
            lines.append("⚪ 확정손익: 미확정")
            lines.append("수익률: 미확정")

        lines.append("")
        entry_d = _to_finite_decimal(payload.entry_price)
        if entry_d is not None:
            lines.append(f"매수가: {format_price_display(entry_d, currency=currency)}")
        else:
            lines.append("매수가: 미확정")

        exit_d = _to_finite_decimal(payload.exit_price)
        if exit_d is not None:
            lines.append(f"매도가: {format_price_display(exit_d, currency=currency)}")
        else:
            lines.append("매도가: 미확정")

        lines.append(f"체결수량: {format_quantity_display(payload.filled_qty, base_asset=base_asset)}")

        notional_str = _compute_notional_display(
            payload.exit_price,
            payload.filled_qty,
            currency=currency,
            explicit_notional=getattr(payload, "notional_amount", None),
        )
        if notional_str:
            lines.append(f"매도금액: {notional_str}")

        _, fee_str = _compute_total_fee(payload, currency=currency)
        if fee_str:
            lines.append(f"총수수료: {fee_str}")

        funding_d = _to_finite_decimal(payload.funding_pnl)
        if funding_d is not None and funding_d != 0:
            lines.append(f"펀딩비: {format_price_display(funding_d, currency=currency, signed=True, is_fee=True)}")

        lines.append(f"청산사유: {format_exit_reason_korean(payload.exit_reason)}")
        lines.append("")

        if pnl_verified and roi_verified:
            lines.append("✅ 손익 검증 완료")
        elif pnl_verified and not roi_verified:
            lines.append("✅ 순실현손익 검증 완료 (수익률 미확정)")
        else:
            lines.append("ℹ️ 매수 원가 또는 수수료 검증 필요")
        lines.append(f"🕒 {kst_str}")

    elif category == "FAILED":
        header_action = get_action_header_label(payload.side, "주문 실패")
        title = f"⚠️ {exchange} · {display_symbol} · {header_action}"
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        req_qty_d = _to_finite_decimal(payload.requested_qty or payload.filled_qty)
        if req_qty_d is not None and req_qty_d > 0:
            lines.append(f"요청수량: {format_quantity_display(req_qty_d, base_asset=base_asset)}")
        reason_str = sanitize_discord_text(payload.error_message or "거래소 주문 거절", max_len=240)
        lines.append(f"사유: {reason_str}")
        lines.append("상태: 주문이 체결되지 않았습니다")
        if payload.signal_id:
            lines.append(f"신호 ID: {sanitize_discord_text(payload.signal_id, max_len=64)}")
        lines.append("")
        lines.append(f"🕒 {kst_str}")

    elif category == "PARTIAL":
        header_action = get_action_header_label(payload.side, "부분 체결")
        title = f"⚪ {exchange} · {display_symbol} · {header_action}"
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        if payload.exit_price:
            lines.append(f"평균체결가: {format_price_display(payload.exit_price, currency=currency)}")
        lines.append(f"체결수량: {format_quantity_display(payload.filled_qty, base_asset=base_asset)}")
        if payload.remaining_qty and payload.remaining_qty != "0":
            lines.append(f"미체결수량: {format_quantity_display(payload.remaining_qty, base_asset=base_asset)}")
        lines.append("상태: 부분 체결 진행 중 (최종 손익 미확정)")
        lines.append("")
        lines.append("ℹ️ 잔여 수량 체결 또는 취소 확인 필요")
        lines.append(f"🕒 {kst_str}")

    elif category == "CANCELED":
        filled_d = _to_finite_decimal(payload.filled_qty)
        has_partial_fill = filled_d is not None and filled_d > 0
        if has_partial_fill:
            header_action = get_action_header_label(payload.side, "부분 체결 후 취소")
        else:
            header_action = get_action_header_label(payload.side, "주문 취소")
        title = f"⚪ {exchange} · {display_symbol} · {header_action}"
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        if has_partial_fill:
            if payload.exit_price:
                lines.append(f"평균체결가: {format_price_display(payload.exit_price, currency=currency)}")
            lines.append(f"체결수량: {format_quantity_display(payload.filled_qty, base_asset=base_asset)}")
            if payload.remaining_qty and payload.remaining_qty != "0":
                lines.append(f"취소수량: {format_quantity_display(payload.remaining_qty, base_asset=base_asset)}")
            lines.append("상태: 일부 체결 후 잔여 주문이 취소되었습니다")
        else:
            if payload.requested_qty:
                lines.append(f"요청수량: {format_quantity_display(payload.requested_qty, base_asset=base_asset)}")
            lines.append("상태: 체결 없이 주문이 취소되었습니다")
        lines.append("")
        lines.append(f"🕒 {kst_str}")

    else:
        # AMBIGUOUS / UNVERIFIED / MANUAL_REVIEW / PENDING
        if is_manual_review:
            header_action = get_action_header_label(payload.side, "주문 결과 확인 필요 (수동 확인 필요)")
        else:
            header_action = get_action_header_label(payload.side, "주문 결과 확인 필요")
        title = f"⚪ {exchange} · {display_symbol} · {header_action}"
        if strategy_name:
            lines.append(f"전략: {strategy_name}")

        if is_manual_review:
            err_raw = str(payload.error_message or "")
            if "missing_order_id" in err_raw:
                reason_text = "주문 ID 누락 (missing_order_id)"
            elif "long_term_recovery_disabled" in err_raw:
                reason_text = "장기 복구 비활성화 (수동 검토 전환)"
            else:
                reason_text = sanitize_discord_text(err_raw or "거래소 주문 조회 불가", max_len=160)
        elif status == FillStatus.PENDING:
            reason_text = "거래소 체결 내역 확인 대기 중"
        else:
            reason_text = "체결 조회 지연 또는 응답 타임아웃"

        lines.append(f"사유: {reason_text}")
        lines.append("상태: 실제 체결 여부 확인 중 (미확정)")
        lines.append("조치: 거래소 체결 내역 확인 필요 (중복 재주문 주의)")
        if payload.signal_id:
            lines.append(f"신호 ID: {sanitize_discord_text(payload.signal_id, max_len=64)}")
        if payload.order_id:
            lines.append(f"주문 ID: {sanitize_discord_text(payload.order_id, max_len=64)}")
        lines.append("")
        lines.append("⚠️ 미확정 주문 — 거래소 실제 체결 여부를 반드시 확인하세요")
        lines.append(f"🕒 {kst_str}")

    description = "\n".join(lines)
    embed = Embed(
        title=title[:256],
        description=description[:4096],
        color=color,
    )

    # Populate structured fields for detailed inspection & backward compatibility with existing tests
    embed.add_field(name="거래소", value=exchange, inline=True)
    embed.add_field(name="심볼", value=sanitize_discord_text(payload.symbol, max_len=40), inline=True)
    embed.add_field(name="체결 상태", value=payload.fill_status.value, inline=True)

    if payload.leverage is not None:
        embed.add_field(name="레버리지 (주문 설정)", value=str(payload.leverage), inline=True)

    if is_sell_or_close_side(payload.side) or payload.exit_reason != ExitReason.UNKNOWN or payload.pnl_result != PnlResult.UNKNOWN:
        if payload.exit_reason != ExitReason.UNKNOWN:
            embed.add_field(name="청산 원인", value=payload.exit_reason.value, inline=True)
        embed.add_field(name="손익 결과", value=payload.pnl_result.value, inline=True)

    if payload.net_pnl is None:
        embed.add_field(name="순실현손익", value="미확정", inline=True)
    else:
        embed.add_field(name="순실현손익", value=sanitize_discord_text(payload.net_pnl, max_len=64), inline=True)

    if payload.exit_price is None:
        embed.add_field(name="평균 체결가격", value="미확인 / 확인 중", inline=True)
    else:
        embed.add_field(name="실제 평균 체결가", value=sanitize_discord_text(payload.exit_price, max_len=64), inline=True)

    if payload.entry_price:
        embed.add_field(name="평균 진입가", value=sanitize_discord_text(payload.entry_price, max_len=64), inline=True)

    if payload.filled_qty:
        embed.add_field(name="실제 체결 수량", value=sanitize_discord_text(payload.filled_qty, max_len=64), inline=True)
    if payload.remaining_qty and payload.remaining_qty != "0":
        embed.add_field(name="미체결 수량", value=sanitize_discord_text(payload.remaining_qty, max_len=64), inline=True)

    if payload.roi_pct is not None:
        roi_fmt = format_roi_display(payload.roi_pct) or f"{payload.roi_pct}%"
        embed.add_field(name=payload.roi_label or "ROI", value=roi_fmt, inline=True)
    elif is_sell_or_close_side(payload.side):
        embed.add_field(name=payload.roi_label or "ROI", value="미확정", inline=True)

    if getattr(payload, "signal_id", None):
        embed.add_field(name="신호 ID", value=sanitize_discord_text(payload.signal_id, max_len=64), inline=True)

    if is_manual_review:
        embed.add_field(name="수동 확인 필요 여부", value="필요 (운영자 확인 요망)", inline=False)

    if payload.order_id:
        embed.add_field(name="주문 ID", value=sanitize_discord_text(payload.order_id, max_len=64), inline=True)

    if payload.error_message:
        embed.add_field(name="오류 상세", value=sanitize_discord_text(payload.error_message, max_len=500), inline=False)

    return enforce_embed_bounds(embed)


def build_legacy_order_embed(
    exchange_name: str,
    symbol: str,
    side: str,
    order_name: Optional[str] = None,
    amount_label: str = "수량",
    amount_value: Optional[str] = None,
    leverage: Optional[int] = None,
    entry_price: Optional[Any] = None,
    close_price: Optional[Any] = None,
    account_label: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> Tuple[str, Embed]:
    """
    Builds a mobile-friendly Discord Embed for the legacy (unverified) path in LogMaker.py.
    Follows Section 4.2 & 8.2:
    - Never presents unverified legacy order response as '실체결 검증 완료' or '확정손익'.
    - Uses Gray (#64748B) and labels order as '접수 (미검증 경로)'.
    """
    exchange = sanitize_discord_text(exchange_name or "UNKNOWN", max_len=24).upper()
    display_symbol = normalize_display_symbol(symbol)
    side_clean = sanitize_discord_text(side or "주문", max_len=24).strip()
    strategy_name = normalize_strategy_name(order_name)
    currency = detect_currency(display_symbol, None, exchange)
    kst_str = format_kst_full(timestamp if timestamp is not None else datetime.now(timezone.utc).timestamp())

    title = f"⚪ {exchange} · {display_symbol} · {side_clean} 접수 (미검증)"
    lines: List[str] = []
    if strategy_name:
        lines.append(f"전략: {strategy_name}")
    if leverage is not None and int(leverage) > 1:
        lines.append(f"레버리지: {leverage}x")
    if account_label:
        lines.append(f"계좌: {sanitize_discord_text(account_label, max_len=40)}")
    if amount_value:
        lines.append(f"요청{amount_label}: {sanitize_discord_text(amount_value, max_len=64)}")
    if entry_price is not None and _to_finite_decimal(entry_price) is not None:
        lines.append(f"참고 진입가(미검증): {format_price_display(entry_price, currency=currency)}")
    if close_price is not None and _to_finite_decimal(close_price) is not None:
        lines.append(f"참고 주문가(미검증): {format_price_display(close_price, currency=currency)}")

    if is_sell_or_close_side(side_clean):
        lines.append("확정손익: 미확정 (레거시 미검증 경로)")
        lines.append("수익률: 미확정")

    lines.append("")
    lines.append("ℹ️ 거래소 실체결·수수료 검증 전 접수 알림입니다")
    lines.append(f"🕒 {kst_str}")

    description = "\n".join(lines)
    embed = Embed(
        title=title[:256],
        description=description[:4096],
        color=COLOR_ORDER_AMBIGUOUS,
    )
    embed.add_field(name="거래소", value=exchange, inline=True)
    embed.add_field(name="심볼", value=display_symbol, inline=True)
    embed.add_field(name="거래유형", value=side_clean, inline=True)
    if amount_value:
        embed.add_field(name=amount_label, value=sanitize_discord_text(amount_value, max_len=64), inline=True)
    if leverage is not None:
        embed.add_field(name="레버리지", value=f"{leverage}배", inline=True)
    embed.add_field(name="검증 상태", value="UNVERIFIED (미검증 접수)", inline=False)

    content = f"[{exchange}] {display_symbol} {side_clean} 접수 (미검증)"
    return content, enforce_embed_bounds(embed)


def classify_error_ambiguity(error_text: Optional[str]) -> bool:
    """
    Returns True if the error indicates an ambiguous / timeout / network state
    where order execution on the exchange cannot be ruled out (Section 6).
    Returns False if it is a confirmed rejection (insufficient balance, min notional, invalid params, etc.).
    """
    if not error_text:
        return False
    lower = str(error_text).lower()
    ambiguous_keywords = (
        "timeout",
        "timed out",
        "requesttimeout",
        "networkerror",
        "connection",
        "disconnected",
        "reset by peer",
        "eof",
        "502",
        "503",
        "504",
        "gateway",
        "service unavailable",
        "delivery_unknown",
        "missing_order_id",
        "manual_review",
        "unverified",
        "응답 지연",
        "타임아웃",
        "네트워크",
        "미확정",
    )
    return any(k in lower for k in ambiguous_keywords)


def build_error_embed(
    error: Any,
    order_info: Optional[Any] = None,
    name: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> Embed:
    """
    Builds mobile-friendly error or ambiguous-order Embed (Section 6).
    - Confirmed failure/rejection -> Orange (#F59E0B), ⚠️
    - Timeout / network error / ambiguous -> Gray (#64748B), ⚪
    """
    raw_err = str(error or "알 수 없는 오류")
    # Extract last meaningful line if it's a multi-line traceback
    err_lines = [ln.strip() for ln in raw_err.splitlines() if ln.strip()]
    concise_err = err_lines[-1] if err_lines else raw_err
    concise_err = sanitize_discord_text(concise_err, max_len=240)

    is_ambiguous = classify_error_ambiguity(raw_err)
    kst_str = format_kst_full(timestamp if timestamp is not None else datetime.now(timezone.utc).timestamp())

    if order_info is not None:
        exchange = sanitize_discord_text(getattr(order_info, "exchange", "UNKNOWN"), max_len=24).upper()
        base = getattr(order_info, "base", "")
        quote = getattr(order_info, "quote", "")
        is_crypto = getattr(order_info, "is_crypto", True)
        is_futures = getattr(order_info, "is_futures", False)
        symbol = getattr(order_info, "unified_symbol", None) or f"{base}/{quote + '.P' if is_crypto and is_futures else quote}"
        display_symbol = normalize_display_symbol(symbol)
        base_asset = extract_base_asset(display_symbol)
        strategy_name = normalize_strategy_name(getattr(order_info, "order_name", None), getattr(order_info, "strategy_id", None))

        side = "주문"
        if getattr(order_info, "is_futures", False):
            if getattr(order_info, "is_entry", False):
                side = "롱 진입" if getattr(order_info, "is_buy", False) else "숏 진입"
            elif getattr(order_info, "is_close", False):
                side = "숏 종료" if getattr(order_info, "is_buy", False) else "롱 종료"
        else:
            if getattr(order_info, "is_buy", False):
                side = "매수"
            elif getattr(order_info, "is_sell", False):
                side = "매도"

        lines: List[str] = []
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        amt = getattr(order_info, "amount", None)
        if amt is not None:
            lines.append(f"요청수량: {format_quantity_display(amt, base_asset=base_asset)}")

        if is_ambiguous:
            title = f"⚪ {exchange} · {display_symbol} · {side} 주문 결과 확인 필요"
            color = COLOR_ORDER_AMBIGUOUS
            lines.append(f"사유: {concise_err}")
            lines.append("상태: 실제 체결 여부 확인 중 (미확정)")
            lines.append("조치: 거래소 체결 내역 확인 필요 (중복 재주문 금지)")
        else:
            title = f"⚠️ {exchange} · {display_symbol} · {side} 주문 실패"
            color = COLOR_ORDER_FAILED
            lines.append(f"사유: {concise_err}")
            lines.append("상태: 주문이 체결되지 않았습니다")

        lines.append("")
        lines.append(f"🕒 {kst_str}")
        embed = Embed(title=title[:256], description="\n".join(lines)[:4096], color=color)
        embed.add_field(name="거래소", value=exchange, inline=True)
        embed.add_field(name="심볼", value=display_symbol, inline=True)
        embed.add_field(name="오류 상세", value=concise_err, inline=False)
        return enforce_embed_bounds(embed)

    # Generic error without order_info
    header_name = sanitize_discord_text(name or "시스템", max_len=40)
    if is_ambiguous:
        title = f"⚪ {header_name} · 응답 확인 필요"
        color = COLOR_ORDER_AMBIGUOUS
        desc = f"사유: {concise_err}\n상태: 응답 지연 또는 네트워크 확인 필요\n\n🕒 {kst_str}"
    else:
        title = f"⚠️ {header_name} · 오류 발생"
        color = COLOR_ORDER_FAILED
        desc = f"사유: {concise_err}\n\n🕒 {kst_str}"

    embed = Embed(title=title[:256], description=desc[:4096], color=color)
    embed.add_field(name="오류 상세", value=concise_err, inline=False)
    return enforce_embed_bounds(embed)
