"""Unified mobile-friendly Discord Embed builder and pure formatters for POA v2."""
import hashlib
import math
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

try:
    from dhooks import Embed
    if hasattr(Embed, "to_dict") and not hasattr(Embed, "data"):
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

# 3. Allowlist-based Error Classification & Safe Korean Descriptions (P1-1, P1-2)
ERROR_CODE_DESCRIPTIONS: Dict[str, str] = {
    # Confirmed Rejections (is_ambiguous = False, COLOR_ORDER_FAILED)
    "INSUFFICIENT_BALANCE": "주문 가능 금액/잔고 부족 (INSUFFICIENT_BALANCE)",
    "MIN_NOTIONAL": "최소 주문금액 또는 수량 미달 (MIN_NOTIONAL)",
    "INVALID_ORDER_PARAMS": "거래소 주문 파라미터 또는 설정 거절 (INVALID_ORDER_PARAMS)",
    "AUTH_PERMISSION_REJECTED": "거래소 인증 또는 권한 거절 (AUTH_PERMISSION_REJECTED)",
    # Ambiguous / Unconfirmed Outcomes (is_ambiguous = True, COLOR_ORDER_AMBIGUOUS)
    "REQUEST_TIMEOUT": "거래소 응답 타임아웃 — 체결 여부 확인 필요 (REQUEST_TIMEOUT)",
    "NETWORK_UNAVAILABLE": "거래소 네트워크 연결 불안정 — 체결 여부 확인 필요 (NETWORK_UNAVAILABLE)",
    "MISSING_ORDER_ID": "주문 ID 누락 (missing_order_id)",
    "LONG_TERM_RECOVERY_DISABLED": "장기 복구 비활성화 (수동 검토 전환)",
    "UNKNOWN_ORDER_RESULT": "주문 처리 결과 미확정 — 거래소 체결 내역 확인 필요 (UNKNOWN_ORDER_RESULT)",
    "REQUEST_PROCESSING_FAILED": "요청 처리 중 오류가 발생했습니다 (REQUEST_PROCESSING_FAILED)",
}

CONFIRMED_REJECTION_CODES = frozenset({
    "INSUFFICIENT_BALANCE",
    "MIN_NOTIONAL",
    "INVALID_ORDER_PARAMS",
    "AUTH_PERMISSION_REJECTED",
})

_SECRET_PATTERNS = [
    re.compile(r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/[^\s\]\)\"']+", re.IGNORECASE),
    re.compile(r"https?://[^\s\]\)\"']+", re.IGNORECASE),
    re.compile(r"(?i)authorization\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;\"'\)\}]+"),
    re.compile(r"(?i)bearer\s+[^\s,;\"'\)\}]+"),
    re.compile(r"(?i)\"(?:apikey|api_key|secret|password|passphrase|access_token|refresh_token|token|account_number|webhook)\"\s*:\s*\"[^\"]*\""),
    re.compile(r"(?i)(?:api%5Fkey|api_key|apikey|secret|token|password|passphrase|access_token|refresh_token|account_number)\s*(?:%3D|[:=])\s*[^\s&;\"'\)\}]+"),
    re.compile(r"(?i)(password|secret|token|api[_-]?key|passphrase|account[_-]?number|access[_-]?token)\s*[:=]\s*[^\s,;\"'\)\}]+"),
]


def _contains_sensitive_marker(text: Optional[Any]) -> bool:
    if not text:
        return False
    s = str(text)
    if any(p.search(s) for p in _SECRET_PATTERNS):
        return True
    lower = s.lower()
    sensitive_tokens = (
        "authorization:",
        "bearer ",
        "apikey",
        "api_key",
        "api%5fkey",
        "access_token",
        "refresh_token",
        "account_number",
        "secret",
        "passphrase",
        "discord.com/api/webhooks",
        "discordapp.com/api/webhooks",
        "response_body=",
    )
    return any(tok in lower for tok in sensitive_tokens)


def sanitize_discord_text(text: Optional[Any], max_len: Optional[int] = None) -> str:
    """Neutralizes Discord mentions, markdown breakage, URLs, and sensitive tokens."""
    if text is None:
        return ""
    s = str(text)
    # Remove secrets and URLs
    s = _SECRET_PATTERNS[0].sub("[WEBHOOK_REDACTED]", s)
    s = _SECRET_PATTERNS[1].sub("[URL_REDACTED]", s)
    for pat in _SECRET_PATTERNS[2:]:
        s = pat.sub("[REDACTED]", s)
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


def _normalize_error_probe(error: Optional[Any]) -> str:
    """Normalizes error input, including vertical single-char joined strings, for safe classification."""
    if error is None:
        return ""
    raw = str(error).strip()
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) > 3 and all(len(ln) == 1 for ln in lines):
        raw = "".join(lines)
    return raw


def classify_error_code(error: Optional[Any], has_order_context: bool = True) -> Tuple[str, bool, str]:
    """
    Classifies an error into an allowlisted (code, is_ambiguous, safe_korean_reason).
    Conservative default: any unknown, generic, or unverified error is treated as AMBIGUOUS (is_ambiguous=True)
    so it is NEVER falsely reported as a confirmed order rejection.
    Raw exception strings are NEVER returned.
    """
    type_name = type(error).__name__.lower() if error is not None else ""
    is_raw_exception = isinstance(error, Exception) and type_name in ("runtimeerror", "exception", "error", "valueerror", "typeerror", "keyerror", "assertionerror")
    probe = _normalize_error_probe(error)
    lower = probe.lower()

    # 1. Explicit Ambiguous / Timeout / Network / Unconfirmed states (checked first!)
    if is_raw_exception and has_order_context:
        return ("UNKNOWN_ORDER_RESULT", True, ERROR_CODE_DESCRIPTIONS["UNKNOWN_ORDER_RESULT"])
    if not probe or probe.upper() == "ORDER_PROCESSING_FAILED" or "order_processing_failed" in lower:
        return (
            "UNKNOWN_ORDER_RESULT",
            True,
            ERROR_CODE_DESCRIPTIONS["UNKNOWN_ORDER_RESULT"],
        )

    if "missing_order_id" in lower or "주문 id 누락" in lower:
        return (
            "MISSING_ORDER_ID",
            True,
            ERROR_CODE_DESCRIPTIONS["MISSING_ORDER_ID"],
        )

    if "long_term_recovery_disabled" in lower or "장기 복구 비활성화" in lower:
        return (
            "LONG_TERM_RECOVERY_DISABLED",
            True,
            ERROR_CODE_DESCRIPTIONS["LONG_TERM_RECOVERY_DISABLED"],
        )

    timeout_markers = (
        "timeout",
        "timed out",
        "requesttimeout",
        "타임아웃",
        "응답 지연",
    )
    if any(k in type_name for k in ("timeout", "requesttimeout")) or any(k in lower for k in timeout_markers):
        return (
            "REQUEST_TIMEOUT",
            True,
            ERROR_CODE_DESCRIPTIONS["REQUEST_TIMEOUT"],
        )

    network_markers = (
        "networkerror",
        "network",
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
        "네트워크",
    )
    if any(k in type_name for k in ("networkerror", "exchangeerror", "ddosprotection", "exchangenotavailable")) or any(
        k in lower for k in network_markers
    ):
        return (
            "NETWORK_UNAVAILABLE",
            True,
            ERROR_CODE_DESCRIPTIONS["NETWORK_UNAVAILABLE"],
        )

    # 1.5 Local / internal errors that must NEVER be classified as confirmed exchange rejections
    local_system_markers = (
        "local cache",
        "reading local",
        "local error",
        "local config",
        "fatal config",
        "during validation",
        "schema validation",
        "validation error",
        "cached wallet",
        "local balance cache",
        "local wallet",
        "while saving",
        "order journal",
        "sqlite commit",
        "commit failed",
        "insufficient funds",
        "minimum amount",
    )
    if any(k in lower for k in local_system_markers):
        if has_order_context:
            return (
                "UNKNOWN_ORDER_RESULT",
                True,
                ERROR_CODE_DESCRIPTIONS["UNKNOWN_ORDER_RESULT"],
            )
        return (
            "REQUEST_PROCESSING_FAILED",
            False,
            ERROR_CODE_DESCRIPTIONS["REQUEST_PROCESSING_FAILED"],
        )

    # 2. If sensitive markers exist in the raw error, never trust substring matches for rejection details
    # and never echo any part of the raw text.
    if _contains_sensitive_marker(probe):
        if has_order_context:
            return (
                "UNKNOWN_ORDER_RESULT",
                True,
                ERROR_CODE_DESCRIPTIONS["UNKNOWN_ORDER_RESULT"],
            )
        return (
            "REQUEST_PROCESSING_FAILED",
            False,
            ERROR_CODE_DESCRIPTIONS["REQUEST_PROCESSING_FAILED"],
        )

    # 3. Explicit Confirmed Rejection categories (only when positive evidence of exchange rejection exists)
    insufficient_markers = (
        "insufficient balance",
        "insufficient_balance",
        "insufficientbalance",
        "insufficient funds",
        "insufficient_funds",
        "insufficientfunds",
        "insufficient margin",
        "account has insufficient balance",
        "주문 가능 금액",
        "잔고 부족",
        "증거금 부족",
    )
    if "insufficientfunds" in type_name or any(k in lower for k in insufficient_markers):
        if "주문 가능 금액(krw) 부족" in lower:
            return (
                "INSUFFICIENT_BALANCE",
                False,
                "주문 가능 금액(KRW) 부족 (INSUFFICIENT_BALANCE)",
            )
        return (
            "INSUFFICIENT_BALANCE",
            False,
            ERROR_CODE_DESCRIPTIONS["INSUFFICIENT_BALANCE"],
        )

    min_notional_markers = (
        "min notional",
        "min_notional",
        "minimum amount",
        "min cost",
        "최소 주문",
    )
    if any(k in lower for k in min_notional_markers):
        return (
            "MIN_NOTIONAL",
            False,
            ERROR_CODE_DESCRIPTIONS["MIN_NOTIONAL"],
        )

    invalid_param_markers = (
        "invalid_order",
        "invalidorder",
        "invalid parameter",
        "invalid order parameter",
        "invalid_order_params",
        "거래소 주문 거절",
        "주문 파라미터 거절",
        "badrequest",
    )
    if "invalidorder" in type_name or "badrequest" in type_name or any(k in lower for k in invalid_param_markers):
        return (
            "INVALID_ORDER_PARAMS",
            False,
            ERROR_CODE_DESCRIPTIONS["INVALID_ORDER_PARAMS"],
        )

    if any(k in type_name for k in ("authenticationerror", "permissiondenied")) or any(
        k in lower for k in ("permission denied", "권한 거절", "invalid api-key", "auth_permission_rejected")
    ):
        return (
            "AUTH_PERMISSION_REJECTED",
            False,
            ERROR_CODE_DESCRIPTIONS["AUTH_PERMISSION_REJECTED"],
        )

    # 4. Conservative Default:
    # For order context: any unclassified error (RuntimeError, TypeError, unknown exception, etc.)
    # MUST be treated as AMBIGUOUS (True) because order submission outcome cannot be ruled out.
    if has_order_context:
        return (
            "UNKNOWN_ORDER_RESULT",
            True,
            ERROR_CODE_DESCRIPTIONS["UNKNOWN_ORDER_RESULT"],
        )

    return (
        "REQUEST_PROCESSING_FAILED",
        False,
        ERROR_CODE_DESCRIPTIONS["REQUEST_PROCESSING_FAILED"],
    )


def classify_error_ambiguity(error_text: Optional[Any]) -> bool:
    """
    Returns True unless the error is positively identified as an explicit confirmed rejection.
    Conservative default (None, ORDER_PROCESSING_FAILED, unknown exception, timeout, network) is True.
    """
    _, is_ambiguous, _ = classify_error_code(error_text, has_order_context=True)
    return is_ambiguous


_CURRENCY_TOKEN_RE = re.compile(
    r"^\s*([+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?)\s*([A-Za-z가-힣%]+)?\s*$"
)


def parse_amount_and_currency(
    val: Optional[Any],
    default_currency: Optional[str] = None,
) -> Tuple[Optional[Decimal], Optional[str]]:
    """
    Parses a numeric value and its explicit currency/unit token without blindly stripping incompatible currencies.
    Examples:
      '0.1 BTC' -> (Decimal('0.1'), 'BTC')
      '367.56 KRW' -> (Decimal('367.56'), 'KRW')
      '367.56원' -> (Decimal('367.56'), 'KRW')
      '100' -> (Decimal('100'), default_currency)
    """
    norm_default = str(default_currency).strip().upper() if default_currency else None
    if val is None or isinstance(val, bool):
        return None, None
    if isinstance(val, Decimal):
        return (val if val.is_finite() else None), norm_default
    if isinstance(val, (int, float)):
        if isinstance(val, float) and not math.isfinite(val):
            return None, None
        try:
            d = Decimal(str(val))
            return (d if d.is_finite() else None), norm_default
        except Exception:
            return None, None
    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned or cleaned in ("-", "미확정", "미확인", "None", "null"):
            return None, None
        m = _CURRENCY_TOKEN_RE.match(cleaned)
        if not m:
            return None, None
        num_str, unit_raw = m.group(1), m.group(2)
        num_clean = num_str.replace(",", "").strip()
        try:
            d = Decimal(num_clean)
            if not d.is_finite():
                return None, None
        except (InvalidOperation, ValueError):
            return None, None

        if unit_raw:
            u = unit_raw.strip()
            if u == "원":
                ccy = "KRW"
            else:
                ccy = u.upper()
        else:
            ccy = norm_default
        return d, ccy
    return None, None


def _to_finite_decimal(val: Optional[Any], expected_currency: Optional[str] = None) -> Optional[Decimal]:
    """
    Parses numeric value or string into a finite Decimal.
    If expected_currency is given, rejects values explicitly tagged with a different currency.
    """
    d, ccy = parse_amount_and_currency(val, default_currency=expected_currency)
    if d is None:
        return None
    if expected_currency is not None and ccy is not None:
        exp_norm = expected_currency.strip().upper()
        if ccy != "%" and ccy != exp_norm:
            return None
    return d


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


def _compute_total_fee_with_consistency(
    payload: NotificationPayload,
    currency: str,
) -> Tuple[Optional[Decimal], Optional[str], bool]:
    """
    Computes total fee in settlement currency ONLY when all fee components share the exact settlement currency.
    Returns (total_fee_decimal, display_string, is_fee_consistent_with_settlement).
    Never sums across different currencies (e.g., BTC + USDT or KRW + BNB), and never estimates missing fees as 0.
    """
    ccy = (currency or "USDT").upper()

    # 1. Inspect fee_breakdown first if provided (e.g. "0.001 BNB, 0.2 USDT" or "367.56 KRW")
    breakdown_has_foreign_ccy = False
    breakdown_single_d: Optional[Decimal] = None
    breakdown_single_ccy: Optional[str] = None
    if payload.fee_breakdown:
        parts = [p.strip() for p in str(payload.fee_breakdown).split(",") if p.strip()]
        if len(parts) > 1:
            breakdown_has_foreign_ccy = True
        elif len(parts) == 1:
            d_part, c_part = parse_amount_and_currency(parts[0], default_currency=ccy)
            if d_part is None or (c_part and c_part != ccy):
                breakdown_has_foreign_ccy = True
                breakdown_single_d = d_part
                breakdown_single_ccy = c_part
            else:
                breakdown_single_d = d_part
                breakdown_single_ccy = c_part or ccy

    # 2. Inspect entry_fee_allocated and exit_fee with strict currency parsing
    entry_d, entry_ccy = parse_amount_and_currency(payload.entry_fee_allocated, default_currency=ccy)
    exit_d, exit_ccy = parse_amount_and_currency(payload.exit_fee, default_currency=ccy)

    raw_entry_present = bool(payload.entry_fee_allocated and str(payload.entry_fee_allocated).strip() not in ("", "-", "None", "null"))
    raw_exit_present = bool(payload.exit_fee and str(payload.exit_fee).strip() not in ("", "-", "None", "null"))

    if (raw_entry_present and entry_d is None) or (raw_exit_present and exit_d is None):
        return None, "미확정 (수수료 형식 확인 필요)", False

    entry_mismatch = entry_d is not None and entry_ccy is not None and entry_ccy != ccy
    exit_mismatch = exit_d is not None and exit_ccy is not None and exit_ccy != ccy

    if entry_mismatch or exit_mismatch:
        fee_parts: List[str] = []
        if entry_d is not None and entry_ccy:
            fee_parts.append(f"진입 {format_price_display(entry_d, currency=entry_ccy, is_fee=True)}")
        if exit_d is not None and exit_ccy:
            fee_parts.append(f"청산 {format_price_display(exit_d, currency=exit_ccy, is_fee=True)}")
        joined = ", ".join(fee_parts) if fee_parts else "미확정"
        return None, f"{joined} (통화 불일치 — 합산 미확정)", False

    if breakdown_has_foreign_ccy:
        if breakdown_single_d is not None and breakdown_single_ccy:
            return None, f"{format_price_display(breakdown_single_d, currency=breakdown_single_ccy, is_fee=True)} (정산통화 외 수수료)", False
        return None, sanitize_discord_text(payload.fee_breakdown, max_len=120), False

    if is_sell_or_close_side(payload.side):
        if entry_d is not None and exit_d is not None:
            total = entry_d + exit_d
            return total, format_price_display(total, currency=ccy, is_fee=True), True
        if exit_d is not None and entry_d is None:
            return exit_d, format_price_display(exit_d, currency=ccy, is_fee=True), True

    if breakdown_single_d is not None:
        return breakdown_single_d, format_price_display(breakdown_single_d, currency=ccy, is_fee=True), True

    if exit_d is not None:
        return exit_d, format_price_display(exit_d, currency=ccy, is_fee=True), True

    return None, None, True


def _compute_total_fee(payload: NotificationPayload, currency: str) -> Tuple[Optional[Decimal], Optional[str]]:
    """Backward-compatible wrapper returning (total_fee_decimal, display_string)."""
    total_d, fee_str, _ = _compute_total_fee_with_consistency(payload, currency)
    return total_d, fee_str


def _compute_notional_display(
    price_val: Optional[Any],
    qty_val: Optional[Any],
    currency: str,
    explicit_notional: Optional[Any] = None,
) -> Optional[str]:
    if explicit_notional is not None:
        d_exp = _to_finite_decimal(explicit_notional, expected_currency=currency)
        if d_exp is not None and d_exp > 0:
            return format_price_display(d_exp, currency=currency)
    p = _to_finite_decimal(price_val, expected_currency=currency)
    q = _to_finite_decimal(qty_val)
    if p is not None and q is not None and p > 0 and q > 0:
        return format_price_display(p * q, currency=currency)
    return None


def _evaluate_verification_states(
    payload: NotificationPayload,
    currency: str,
    fee_consistent: bool,
) -> Tuple[bool, bool, bool, Optional[Decimal], Optional[Decimal]]:
    """
    Independently evaluates (fill_verified, pnl_verified, roi_verified, net_pnl_d, roi_d) (P1-3).
    - Fill verification NEVER automatically verifies Net PnL or ROI.
    - Legacy payloads with 'PnL: UNVERIFIED' or '추정' in roi_label are strictly downgraded.
    - Multi-currency fee mismatch or non-positive entry/exit/quantity invalidates confirmed PnL/ROI.
    """
    fill_status_obj = getattr(payload, "fill_verification_status", None) or payload.verification_status
    filled_d = _to_finite_decimal(payload.filled_qty)
    v_src = str(getattr(payload, "verification_source", "") or "").upper()

    # Fill verification requires explicit VERIFIED flag AND positive finite quantity (or 0 for verified rejection)
    fill_src = v_src.split(" / ")[0].strip() if " / " in v_src else v_src.strip()
    fill_verified = (
        (fill_status_obj == VerificationStatus.VERIFIED or str(getattr(fill_status_obj, "value", fill_status_obj)).upper() == "VERIFIED")
        and filled_d is not None
        and filled_d >= Decimal("0")
    )
    if not fill_src or fill_src in ("LOCAL_DB", "LOCAL_CACHE", "CONFIG_LOADER", "LOCAL_ESTIMATE", "UNVERIFIED"):
        fill_verified = False

    net_pnl_d, net_pnl_ccy = parse_amount_and_currency(payload.net_pnl, default_currency=currency)
    roi_d = _to_finite_decimal(payload.roi_pct)
    entry_d = _to_finite_decimal(payload.entry_price, expected_currency=currency)
    exit_d = _to_finite_decimal(payload.exit_price, expected_currency=currency)

    src_pnl_unverified = (
        not v_src
        or any(
            marker in v_src
            for marker in (
                "PNL: UNVERIFIED", "PNL: ESTIMATED", "PNL: FAILED", "수동 검토",
                "LOCAL_DB", "LOCAL_CACHE", "CONFIG_LOADER", "LOCAL_ESTIMATE", "UNVERIFIED"
            )
        )
    )

    explicit_pnl_status = getattr(payload, "pnl_verification_status", None)
    pnl_status_ok = (
        explicit_pnl_status == VerificationStatus.VERIFIED
        or explicit_pnl_status == "VERIFIED"
    )

    pnl_ccy_ok = (net_pnl_ccy is None or net_pnl_ccy.upper() == currency.upper())
    prices_and_qty_ok = (
        entry_d is not None and entry_d > 0
        and exit_d is not None and exit_d > 0
        and filled_d is not None and filled_d > 0
    )

    pnl_arithmetic_ok = True
    gross_raw = getattr(payload, "gross_pnl", None)
    raw_entry_fee = getattr(payload, "entry_fee_allocated", None)
    raw_exit_fee = getattr(payload, "exit_fee", None)
    raw_funding = getattr(payload, "funding_pnl", None)

    # Check verified net settlement contract
    acc_basis = str(getattr(payload, "accounting_basis", "") or "").lower()
    inc_entry = bool(getattr(payload, "pnl_includes_entry_fee", False))
    inc_exit = bool(getattr(payload, "pnl_includes_exit_fee", False))
    inc_funding = bool(getattr(payload, "pnl_includes_funding", False))
    is_all_inclusive_net = (acc_basis == "net" and inc_entry and inc_exit and inc_funding)

    # 1. Funding currency verification
    funding_d: Optional[Decimal] = Decimal("0")
    if raw_funding is not None:
        parsed_fund_d, funding_ccy = parse_amount_and_currency(raw_funding, default_currency=currency)
        if parsed_fund_d is None or (funding_ccy and funding_ccy.upper() != currency.upper()):
            pnl_arithmetic_ok = False
            funding_d = None
        else:
            funding_d = parsed_fund_d

    # 2. Accounting decomposition consistency
    has_entry_fee = (raw_entry_fee is not None)
    has_exit_fee = (raw_exit_fee is not None)
    has_fee = has_entry_fee or has_exit_fee
    has_gross = (gross_raw is not None)

    entry_fee_d: Optional[Decimal] = None
    exit_fee_d: Optional[Decimal] = None
    gross_d: Optional[Decimal] = None

    if is_all_inclusive_net:
        # Verified net settlement already includes all fees and funding
        entry_fee_d = Decimal("0")
        exit_fee_d = Decimal("0")
        gross_d = net_pnl_d
    elif has_entry_fee != has_exit_fee:
        # If one fee is provided while the other is missing -> incomplete fee evidence
        pnl_arithmetic_ok = False
    elif has_entry_fee and has_exit_fee:
        entry_fee_d, entry_fee_ccy = parse_amount_and_currency(raw_entry_fee, default_currency=currency)
        exit_fee_d, exit_fee_ccy = parse_amount_and_currency(raw_exit_fee, default_currency=currency)

        if entry_fee_d is None or (entry_fee_ccy and entry_fee_ccy.upper() != currency.upper()):
            pnl_arithmetic_ok = False
        if exit_fee_d is None or (exit_fee_ccy and exit_fee_ccy.upper() != currency.upper()):
            pnl_arithmetic_ok = False
    elif has_gross:
        # Gross is present but fee decomposition is missing -> unconfirmed fees (R4-F03 case 3)
        pnl_arithmetic_ok = False
    else:
        # Legacy/simple payload without gross and without fee decomposition (e.g. test_C01)
        entry_fee_d = Decimal("0")
        exit_fee_d = Decimal("0")

    # 3. Gross PnL derivation
    if is_all_inclusive_net:
        pass
    elif has_gross:
        gross_d, gross_ccy = parse_amount_and_currency(gross_raw, default_currency=currency)
        if gross_d is None or (gross_ccy and gross_ccy.upper() != currency.upper()):
            pnl_arithmetic_ok = False
    else:
        # Derive gross from verified entry/exit prices and quantity if available
        if prices_and_qty_ok and entry_d is not None and exit_d is not None and filled_d is not None:
            is_short = ("숏" in str(payload.side or "")) or (str(getattr(payload, "position_side", "") or "").upper() == "SHORT")
            if is_short:
                gross_d = (entry_d - exit_d) * filled_d
            else:
                gross_d = (exit_d - entry_d) * filled_d
        else:
            pnl_arithmetic_ok = False

    if pnl_arithmetic_ok and gross_d is not None and net_pnl_d is not None and not is_all_inclusive_net:
        fee_val = Decimal("0")
        if not inc_entry and entry_fee_d is not None:
            fee_val += entry_fee_d
        if not inc_exit and exit_fee_d is not None:
            fee_val += exit_fee_d

        fund_val = funding_d if (funding_d is not None and not inc_funding) else Decimal("0")
        expected_net = gross_d - fee_val + fund_val

        if expected_net == Decimal("0"):
            if net_pnl_d != Decimal("0"):
                pnl_arithmetic_ok = False
        else:
            # Check sign contradiction and precision (R4-F05)
            if (expected_net > Decimal("0") and net_pnl_d < Decimal("0")) or (
                expected_net < Decimal("0") and net_pnl_d > Decimal("0")
            ):
                pnl_arithmetic_ok = False
            else:
                tol = Decimal("0.01") if currency.upper() == "KRW" else Decimal("0.0001")
                if abs(net_pnl_d - expected_net) > tol:
                    pnl_arithmetic_ok = False

    pnl_verified = bool(
        fill_verified
        and pnl_status_ok
        and not src_pnl_unverified
        and fee_consistent
        and net_pnl_d is not None
        and pnl_ccy_ok
        and prices_and_qty_ok
        and pnl_arithmetic_ok
    )

    roi_label_str = str(getattr(payload, "roi_label", "") or "")
    is_roi_estimated = (
        bool(getattr(payload, "is_roi_estimated", False))
        or ("추정" in roi_label_str)
        or ("ESTIMATED" in roi_label_str.upper())
        or ("PNL: ESTIMATED" in v_src)
    )

    explicit_roi_status = getattr(payload, "roi_verification_status", None)
    roi_status_ok = (
        explicit_roi_status == VerificationStatus.VERIFIED
        or explicit_roi_status == "VERIFIED"
    )

    roi_denominator_ok = True
    has_accounting_contract = (
        getattr(payload, "accounting_basis", None) is not None
        or getattr(payload, "cost_basis_currency", None) is not None
        or getattr(payload, "cost_basis", None) is not None
        or getattr(payload, "gross_pnl", None) is not None
        or getattr(payload, "pnl_currency", None) is not None
    )
    if roi_status_ok and net_pnl_d is not None and roi_d is not None:
        cost_basis_ccy = getattr(payload, "cost_basis_currency", None)
        if cost_basis_ccy is not None and str(cost_basis_ccy).strip().upper() != currency.upper():
            roi_denominator_ok = False

        raw_cost_basis = getattr(payload, "cost_basis", None)
        cost_basis_d = None
        if raw_cost_basis is not None:
            cost_basis_d = _to_finite_decimal(raw_cost_basis, expected_currency=currency)
            if cost_basis_d is None or cost_basis_d <= Decimal("0"):
                roi_denominator_ok = False
        else:
            entry_fee_for_basis = _to_finite_decimal(raw_entry_fee, expected_currency=currency) or Decimal("0")
            if entry_d is not None and filled_d is not None and entry_d > 0 and filled_d > 0:
                cost_basis_d = (entry_d * filled_d) + entry_fee_for_basis

        # Detect forged / altered cost basis in stored payload
        if cost_basis_d is not None and entry_d is not None and filled_d is not None and entry_d > 0 and filled_d > 0:
            basis_no_fee = entry_d * filled_d
            basis_with_fee = basis_no_fee + (_to_finite_decimal(raw_entry_fee, expected_currency=currency) or Decimal("0"))
            if not bool(getattr(payload, "leverage", None)):
                if abs(cost_basis_d - basis_no_fee) > Decimal("0.01") and abs(cost_basis_d - basis_with_fee) > Decimal("0.01"):
                    roi_denominator_ok = False

        # Legacy r3 fixture compatibility only
        is_legacy_r3 = False

        if not is_legacy_r3:
            if cost_basis_d is not None and cost_basis_d > Decimal("0") and roi_denominator_ok:
                expected_roi = (net_pnl_d / cost_basis_d) * Decimal("100")
                if (expected_roi > Decimal("0") and roi_d < Decimal("0")) or (
                    expected_roi < Decimal("0") and roi_d > Decimal("0")
                ):
                    roi_denominator_ok = False
                elif abs(roi_d - expected_roi) > Decimal("0.005"):
                    roi_denominator_ok = False
            else:
                roi_denominator_ok = False

    roi_verified = bool(
        pnl_verified
        and roi_status_ok
        and roi_denominator_ok
        and not is_roi_estimated
        and roi_d is not None
    )

    return fill_verified, pnl_verified, roi_verified, net_pnl_d, roi_d


def build_notification_details(payload: NotificationPayload) -> List[Dict[str, Any]]:
    """
    Returns structured diagnostic details for internal inspection/operator tools (P2-2).
    Never exposes unverified PnL or estimated ROI as confirmed values, and is NOT attached
    to the mobile Discord wire Embed.fields.
    """
    exchange = sanitize_discord_text(payload.exchange or "UNKNOWN", max_len=24).upper()
    display_symbol = normalize_display_symbol(payload.symbol)
    currency = detect_currency(display_symbol, getattr(payload, "pnl_currency", None), exchange)
    _, _, fee_consistent = _compute_total_fee_with_consistency(payload, currency=currency)
    fill_verified, pnl_verified, roi_verified, net_pnl_d, roi_d = _evaluate_verification_states(
        payload, currency, fee_consistent
    )

    is_manual_review = (
        payload.event_type == "manual_review"
        or "MANUAL_REVIEW" in str(payload.error_message or "")
    )

    details: List[Dict[str, Any]] = [
        {"name": "거래소", "value": exchange, "inline": True},
        {"name": "심볼", "value": sanitize_discord_text(payload.symbol, max_len=40), "inline": True},
        {"name": "체결 상태", "value": payload.fill_status.value, "inline": True},
        {"name": "체결 검증", "value": "VERIFIED" if fill_verified else "UNVERIFIED", "inline": True},
    ]

    if payload.leverage is not None:
        details.append({"name": "레버리지 (주문 설정)", "value": str(payload.leverage), "inline": True})

    if is_sell_or_close_side(payload.side) or payload.exit_reason != ExitReason.UNKNOWN or payload.pnl_result != PnlResult.UNKNOWN:
        if payload.exit_reason != ExitReason.UNKNOWN:
            details.append({"name": "청산 원인", "value": payload.exit_reason.value, "inline": True})
        pnl_res_val = payload.pnl_result.value if pnl_verified else PnlResult.UNKNOWN.value
        details.append({"name": "손익 결과", "value": pnl_res_val, "inline": True})

    if pnl_verified and net_pnl_d is not None:
        details.append({"name": "순실현손익", "value": sanitize_discord_text(payload.net_pnl, max_len=64), "inline": True})
    else:
        details.append({"name": "순실현손익", "value": "미확정", "inline": True})

    if payload.exit_price is None:
        details.append({"name": "평균 체결가격", "value": "미확인 / 확인 중", "inline": True})
    else:
        price_name = "실제 평균 체결가" if fill_verified else "보고된 체결가 (미검증)"
        details.append({"name": price_name, "value": sanitize_discord_text(payload.exit_price, max_len=64), "inline": True})

    if payload.entry_price:
        details.append({"name": "평균 진입가", "value": sanitize_discord_text(payload.entry_price, max_len=64), "inline": True})

    if payload.filled_qty:
        qty_name = "실제 체결 수량" if fill_verified else "보고된 체결 수량 (미검증)"
        details.append({"name": qty_name, "value": sanitize_discord_text(payload.filled_qty, max_len=64), "inline": True})
    if payload.remaining_qty and payload.remaining_qty != "0":
        details.append({"name": "미체결 수량", "value": sanitize_discord_text(payload.remaining_qty, max_len=64), "inline": True})

    if roi_verified and roi_d is not None:
        roi_fmt = format_roi_display(roi_d) or "미확정"
        details.append({"name": "ROI", "value": roi_fmt, "inline": True})
    elif is_sell_or_close_side(payload.side):
        details.append({"name": payload.roi_label or "ROI", "value": "미확정", "inline": True})

    if getattr(payload, "signal_id", None):
        safe_sig_ref = hashlib.sha256(str(payload.signal_id).encode("utf-8")).hexdigest()[:8]
        details.append({"name": "신호 ID", "value": safe_sig_ref, "inline": True})

    if is_manual_review:
        details.append({"name": "수동 확인 필요 여부", "value": "필요 (운영자 확인 요망)", "inline": False})

    if payload.order_id:
        details.append({"name": "주문 ID", "value": sanitize_discord_text(payload.order_id, max_len=64), "inline": True})

    if payload.error_message:
        _, _, safe_err = classify_error_code(payload.error_message, has_order_context=True)
        details.append({"name": "오류 상세", "value": safe_err, "inline": False})

    return details


def build_mobile_verified_embed(payload: NotificationPayload) -> Embed:
    """
    Builds the POA v2 unified mobile-friendly Discord Embed from a NotificationPayload.
    Does NOT populate redundant Embed.fields on the Discord wire payload (P2-2) and strictly
    separates fill verification from Net PnL and ROI verification (P1-3, P2-3).
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
    err_text = payload.error_message
    has_err_text = bool(err_text is not None and str(err_text).strip() != "")
    _, is_err_ambig, err_reason = classify_error_code(err_text, has_order_context=True)
    v_src = str(getattr(payload, "verification_source", "") or "").upper()

    filled_d = _to_finite_decimal(payload.filled_qty)
    has_partial_fill = filled_d is not None and filled_d > 0

    fill_v_status = getattr(payload, "fill_verification_status", None)
    if fill_v_status is None:
        fill_v_status = getattr(payload, "verification_status", None)

    fill_src = v_src.split(" / ")[0].strip() if " / " in v_src else v_src.strip()
    fill_is_verified = (
        (fill_v_status == VerificationStatus.VERIFIED or str(getattr(fill_v_status, "value", fill_v_status)).upper() == "VERIFIED")
        and filled_d is not None
        and filled_d >= Decimal("0")
        and fill_src not in ("LOCAL_DB", "LOCAL_CACHE", "CONFIG_LOADER", "LOCAL_ESTIMATE", "UNVERIFIED", "")
    )
    fill_is_failed_verified = (
        fill_v_status == VerificationStatus.FAILED
        or str(getattr(fill_v_status, "value", fill_v_status)).upper() == "FAILED"
    )
    fill_is_unverified = not fill_is_verified

    has_explicit_rejection = has_err_text and not is_err_ambig
    has_verified_partial_fill = fill_is_verified and has_partial_fill

    is_verified_zero_fill = (
        fill_is_verified
        and filled_d is not None
        and filled_d == Decimal("0")
        and fill_src not in ("LOCAL_DB", "LOCAL_CACHE", "CONFIG_LOADER", "LOCAL_ESTIMATE", "UNVERIFIED", "")
    )

    # Determine category & color (Section 3.3)
    if is_manual_review or payload.event_type == "unverified" or status in (FillStatus.UNVERIFIED, FillStatus.PENDING):
        color = COLOR_ORDER_AMBIGUOUS
        category = "AMBIGUOUS"
    elif status == FillStatus.PARTIALLY_FILLED:
        raw_stat = str(getattr(payload, "exchange_status", "") or "").lower()
        if raw_stat in {"canceled", "cancelled", "expired", "cancel"}:
            color = COLOR_ORDER_AMBIGUOUS
            category = "CANCELED"
        else:
            color = COLOR_ORDER_AMBIGUOUS
            category = "PARTIAL"
    elif status == FillStatus.CANCELED:
        color = COLOR_ORDER_AMBIGUOUS
        category = "CANCELED"
    elif status == FillStatus.FAILED:
        if has_explicit_rejection and has_verified_partial_fill:
            color = COLOR_ORDER_FAILED
            category = "FAILED"
        elif has_explicit_rejection and is_verified_zero_fill:
            color = COLOR_ORDER_FAILED
            category = "FAILED"
        else:
            color = COLOR_ORDER_AMBIGUOUS
            category = "AMBIGUOUS"
    elif status == FillStatus.FILLED:
        rem_d = _to_finite_decimal(payload.remaining_qty)
        req_d = _to_finite_decimal(payload.requested_qty)
        has_positive_rem = (rem_d is not None and rem_d > Decimal("0")) or (
            req_d is not None and filled_d is not None and filled_d < req_d
        )
        if not fill_is_verified or (has_positive_rem and not fill_is_verified) or (filled_d is None or filled_d <= Decimal("0")):
            color = COLOR_ORDER_AMBIGUOUS
            category = "AMBIGUOUS"
        elif is_sell_or_close_side(payload.side):
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
        if filled_d is not None and filled_d > 0 and (fill_is_verified or getattr(payload, "fill_verification_status", None) is None):
            header_action = get_action_header_label(payload.side, "완료")
        else:
            header_action = get_action_header_label(payload.side, "체결 확인")
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
        _, fee_str, fee_consistent = _compute_total_fee_with_consistency(payload, currency=currency)
        fill_verified, _, _, _, _ = _evaluate_verification_states(payload, currency, fee_consistent)

        lines.append(f"체결가: {price_str}")
        lines.append(f"체결수량: {qty_str}")
        if notional_str:
            lines.append(f"매수금액: {notional_str}")
        if fee_str:
            lines.append(f"수수료: {fee_str}")
        lines.append("")
        if fill_verified:
            lines.append("✅ 실체결 검증 완료")
        else:
            lines.append("ℹ️ 체결 내역 확인")
        lines.append(f"🕒 {kst_str}")

    elif category == "SELL_FILLED":
        if filled_d is not None and filled_d > 0 and (fill_is_verified or getattr(payload, "fill_verification_status", None) is None):
            header_action = get_action_header_label(payload.side, "완료")
        else:
            header_action = get_action_header_label(payload.side, "체결 확인")
        title = f"🔴 {exchange} · {display_symbol} · {header_action}"
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        if payload.leverage is not None and int(payload.leverage) > 1:
            lines.append(f"레버리지: {payload.leverage}x")

        _, fee_str, fee_consistent = _compute_total_fee_with_consistency(payload, currency=currency)
        _, pnl_verified, roi_verified, net_pnl_d, roi_d = _evaluate_verification_states(
            payload, currency, fee_consistent
        )

        # Top PnL Banner (Cases A, B, C in Section 5.2)
        if pnl_verified and roi_verified and net_pnl_d is not None and roi_d is not None:
            # Case A: Both Net PnL & ROI verified
            pnl_str = format_price_display(net_pnl_d, currency=currency, signed=True)
            roi_str = format_roi_display(roi_d)
            if net_pnl_d > 0:
                lines.append(f"💰 확정손익: {pnl_str} ({roi_str})")
            elif net_pnl_d < 0:
                lines.append(f"📉 확정손익: {pnl_str} ({roi_str})")
            else:
                lines.append(f"➖ 확정손익: {pnl_str} ({roi_str})")
        elif pnl_verified and not roi_verified and net_pnl_d is not None:
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
        entry_d = _to_finite_decimal(payload.entry_price, expected_currency=currency)
        if entry_d is not None and entry_d > 0:
            lines.append(f"매수가: {format_price_display(entry_d, currency=currency)}")
        else:
            lines.append("매수가: 미확정")

        exit_d = _to_finite_decimal(payload.exit_price, expected_currency=currency)
        if exit_d is not None and exit_d > 0:
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

        if fee_str:
            lines.append(f"총수수료: {fee_str}")

        funding_d = _to_finite_decimal(payload.funding_pnl, expected_currency=currency)
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
        req_qty_d = _to_finite_decimal(payload.requested_qty)
        if req_qty_d is not None and req_qty_d > 0:
            lines.append(f"요청수량: {format_quantity_display(req_qty_d, base_asset=base_asset)}")

        if has_verified_partial_fill:
            lines.append(f"체결수량: {format_quantity_display(filled_d, base_asset=base_asset)}")
            rem_d = _to_finite_decimal(payload.remaining_qty)
            if rem_d is not None and rem_d > 0:
                lines.append(f"미체결수량: {format_quantity_display(rem_d, base_asset=base_asset)}")

        lines.append(f"사유: {err_reason}")

        if has_verified_partial_fill:
            lines.append("상태: 부분 체결 완료 후 잔여 주문 실패")
            lines.append("조치: 실제 체결 수량 확인 및 잔여 주문 확인 필요")
        elif is_verified_zero_fill:
            lines.append("상태: 주문이 체결되지 않았습니다")
        else:
            lines.append("상태: 실제 체결 여부 확인 중 (미확정)")
            lines.append("조치: 거래소 체결 내역 확인 필요 (중복 재주문 주의)")

        lines.append("")
        lines.append(f"🕒 {kst_str}")

    elif category == "PARTIAL":
        if fill_is_verified:
            header_action = get_action_header_label(payload.side, "부분 체결")
        else:
            header_action = get_action_header_label(payload.side, "주문 결과 확인 필요")
        title = f"⚪ {exchange} · {display_symbol} · {header_action}"
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        if payload.exit_price:
            lines.append(f"평균체결가: {format_price_display(payload.exit_price, currency=currency)}")
        if fill_is_verified or payload.requested_qty is None:
            lines.append(f"체결수량: {format_quantity_display(payload.filled_qty, base_asset=base_asset)}")
        else:
            lines.append(f"체결수량: {format_quantity_display(payload.filled_qty, base_asset=base_asset)} (미검증)")
        if payload.remaining_qty and payload.remaining_qty != "0":
            lines.append(f"미체결수량: {format_quantity_display(payload.remaining_qty, base_asset=base_asset)}")
        if fill_is_verified:
            lines.append("상태: 부분 체결 진행 중 (최종 손익 미확정)")
            lines.append("")
            lines.append("ℹ️ 잔여 수량 체결 또는 취소 확인 필요")
        else:
            lines.append("상태: 실제 체결 여부 확인 필요 (미확정)")
            lines.append("조치: 거래소 체결 내역 확인 필요 (중복 재주문 금지)")
            lines.append("")
            lines.append("⚠️ 미확정 주문 — 거래소 실제 체결 여부를 반드시 확인하세요")
        lines.append(f"🕒 {kst_str}")

    elif category == "CANCELED":
        filled_d = _to_finite_decimal(payload.filled_qty)
        has_partial_fill = filled_d is not None and filled_d > 0
        if has_partial_fill:
            if fill_is_verified:
                header_action = get_action_header_label(payload.side, "부분 체결 후 취소")
            else:
                header_action = get_action_header_label(payload.side, "주문 결과 확인 필요")
        else:
            header_action = get_action_header_label(payload.side, "주문 취소")
        title = f"⚪ {exchange} · {display_symbol} · {header_action}"
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        if has_partial_fill:
            if payload.exit_price:
                lines.append(f"평균체결가: {format_price_display(payload.exit_price, currency=currency)}")
            if fill_is_verified:
                lines.append(f"체결수량: {format_quantity_display(payload.filled_qty, base_asset=base_asset)}")
                if payload.remaining_qty and payload.remaining_qty != "0":
                    lines.append(f"취소수량: {format_quantity_display(payload.remaining_qty, base_asset=base_asset)}")
                lines.append("상태: 일부 체결 후 잔여 주문이 취소되었습니다")
            else:
                lines.append(f"체결수량: {format_quantity_display(payload.filled_qty, base_asset=base_asset)} (미검증)")
                if payload.remaining_qty and payload.remaining_qty != "0":
                    lines.append(f"미체결수량: {format_quantity_display(payload.remaining_qty, base_asset=base_asset)}")
                lines.append("상태: 실제 체결 여부 확인 필요 (미확정)")
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

        req_qty_d = _to_finite_decimal(payload.requested_qty)
        if req_qty_d is not None and req_qty_d > 0:
            lines.append(f"요청수량: {format_quantity_display(req_qty_d, base_asset=base_asset)}")

        if has_partial_fill:
            if fill_is_verified:
                lines.append(f"체결수량: {format_quantity_display(filled_d, base_asset=base_asset)}")
            else:
                lines.append(f"체결수량: {format_quantity_display(filled_d, base_asset=base_asset)} (미검증)")
            rem_d = _to_finite_decimal(payload.remaining_qty)
            if rem_d is not None and rem_d > 0:
                lines.append(f"미체결수량: {format_quantity_display(rem_d, base_asset=base_asset)}")

        if is_manual_review:
            err_raw = str(payload.error_message or "")
            if "missing_order_id" in err_raw:
                reason_text = "주문 ID 누락 (missing_order_id)"
            elif "long_term_recovery_disabled" in err_raw:
                reason_text = "장기 복구 비활성화 (수동 검토 전환)"
            else:
                _, _, reason_text = classify_error_code(err_raw, has_order_context=True)
        elif status == FillStatus.PENDING:
            reason_text = "거래소 체결 내역 확인 대기 중"
        elif has_err_text:
            reason_text = err_reason
        else:
            reason_text = "주문 처리 결과 미확정 (거래소 확인 필요)"

        lines.append(f"사유: {reason_text}")
        if has_partial_fill:
            if fill_is_verified:
                lines.append("상태: 일부 체결 확인됨 (잔여 주문 상태 미확정)")
            else:
                lines.append("상태: 실제 체결 여부 확인 필요 (미확정)")
            lines.append("조치: 거래소 체결 내역 확인 필요 (중복 재주문 금지)")
        else:
            lines.append("상태: 실제 체결 여부 확인 중 (미확정)")
            lines.append("조치: 거래소 체결 내역 확인 필요 (중복 재주문 주의)")

        if is_manual_review and payload.signal_id:
            safe_sig_ref = hashlib.sha256(str(payload.signal_id).encode("utf-8")).hexdigest()[:8]
            lines.append(f"운영 참조: {safe_sig_ref}")
        lines.append("")
        lines.append("⚠️ 미확정 주문 — 거래소 실제 체결 여부를 반드시 확인하세요")
        lines.append(f"🕒 {kst_str}")

    description = "\n".join(lines)
    embed = Embed(
        title=title[:256],
        description=description[:4096],
        color=color,
    )
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
    - Uses Gray (#64748B) and labels order as '접수 (미검증)'.
    - Avoids duplicate Embed.fields (P2-2).
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
    content = f"[{exchange}] {display_symbol} {side_clean} 접수 (미검증)"
    return content, enforce_embed_bounds(embed)


def build_error_embed(
    error: Any,
    order_info: Optional[Any] = None,
    name: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> Embed:
    """
    Builds mobile-friendly error or ambiguous-order Embed (Section 6, P1-1, P1-2, P2-2).
    - Uses allowlisted error codes and safe Korean descriptions ONLY; never echoes raw exceptions or secrets.
    - Conservative default for order_info: Ambiguous / Unconfirmed (#64748B, ⚪) unless positively classified
      as a confirmed exchange rejection (#F59E0B, ⚠️).
    """
    has_order = order_info is not None
    err_text = error
    has_err_text = bool(err_text is not None and str(err_text).strip() != "")
    _, is_ambiguous, safe_reason = classify_error_code(err_text, has_order_context=has_order)
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
            elif getattr(order_info, "side", None):
                s_raw = str(getattr(order_info, "side", "")).lower()
                if "buy" in s_raw or "매수" in s_raw:
                    side = "매수"
                elif "sell" in s_raw or "매도" in s_raw:
                    side = "매도"

        lines: List[str] = []
        if strategy_name:
            lines.append(f"전략: {strategy_name}")
        amt = getattr(order_info, "amount", None)
        if amt is not None:
            lines.append(f"요청수량: {format_quantity_display(amt, base_asset=base_asset)}")

        filled_val = getattr(order_info, "filled_qty", None)
        if filled_val is None:
            filled_val = getattr(order_info, "filled", None)
        filled_d = _to_finite_decimal(filled_val)
        has_partial_fill = filled_d is not None and filled_d > 0

        has_v_attr = hasattr(order_info, "fill_verification_status") or hasattr(order_info, "verification_status")
        has_qty_attr = hasattr(order_info, "filled_qty") or hasattr(order_info, "filled")

        v_status = getattr(order_info, "fill_verification_status", None)
        if v_status is None:
            v_status = getattr(order_info, "verification_status", None)

        is_verified = (
            v_status is not None
            and (v_status == VerificationStatus.VERIFIED or str(getattr(v_status, "value", v_status)).upper() == "VERIFIED")
        )
        is_verified_zero_fill = (
            is_verified
            and filled_d is not None
            and filled_d == Decimal("0")
        )
        has_verified_partial_fill = (
            is_verified
            and has_partial_fill
        )

        err_lower = str(err_text).lower() if err_text is not None else ""
        # Proven exchange rejection receipt requires specific official exchange response string
        has_official_rejection_receipt = (
            "account has insufficient balance for requested action" in err_lower or
            (str(err_text).strip().upper() == "INSUFFICIENT_BALANCE" and is_verified_zero_fill)
        )

        if is_ambiguous:
            title = f"⚪ {exchange} · {display_symbol} · {side} 주문 결과 확인 필요"
            color = COLOR_ORDER_AMBIGUOUS
            if has_partial_fill:
                if is_verified:
                    lines.append(f"체결수량: {format_quantity_display(filled_d, base_asset=base_asset)}")
                else:
                    lines.append(f"체결수량: {format_quantity_display(filled_d, base_asset=base_asset)} (미검증)")
            lines.append(f"사유: {safe_reason}")
            if has_partial_fill:
                if is_verified:
                    lines.append("상태: 일부 체결 확인됨 (잔여 주문 상태 미확정)")
                else:
                    lines.append("상태: 실제 체결 여부 확인 필요 (미확정)")
            else:
                lines.append("상태: 실제 체결 여부 확인 중 (미확정)")
            lines.append("조치: 거래소 체결 내역 확인 필요 (중복 재주문 금지)")
        else:
            if has_verified_partial_fill:
                title = f"⚠️ {exchange} · {display_symbol} · {side} 주문 실패"
                color = COLOR_ORDER_FAILED
                lines.append(f"체결수량: {format_quantity_display(filled_d, base_asset=base_asset)}")
                lines.append(f"사유: {safe_reason}")
                lines.append("상태: 부분 체결 완료 후 잔여 주문 실패")
                lines.append("조치: 실제 체결 수량 확인 및 잔여 주문 확인 필요")
            elif is_verified_zero_fill or has_official_rejection_receipt:
                title = f"⚠️ {exchange} · {display_symbol} · {side} 주문 실패"
                color = COLOR_ORDER_FAILED
                lines.append(f"사유: {safe_reason}")
                lines.append("상태: 주문이 체결되지 않았습니다")
            else:
                title = f"⚪ {exchange} · {display_symbol} · {side} 주문 결과 확인 필요"
                color = COLOR_ORDER_AMBIGUOUS
                if has_partial_fill:
                    lines.append(f"체결수량: {format_quantity_display(filled_d, base_asset=base_asset)} (미검증)")
                lines.append(f"사유: {safe_reason}")
                lines.append("상태: 실제 체결 여부 확인 중 (미확정)")
                lines.append("조치: 거래소 체결 내역 확인 필요 (중복 재주문 금지)")

        lines.append("")
        lines.append(f"🕒 {kst_str}")
        embed = Embed(title=title[:256], description="\n".join(lines)[:4096], color=color)
        return enforce_embed_bounds(embed)

    # Generic system/webhook error without order_info
    header_name = sanitize_discord_text(name or "시스템", max_len=40)
    if is_ambiguous:
        title = f"⚪ {header_name} · 응답 확인 필요"
        color = COLOR_ORDER_AMBIGUOUS
        desc = f"사유: {safe_reason}\n상태: 응답 지연 또는 네트워크 확인 필요\n\n🕒 {kst_str}"
    else:
        title = f"⚠️ {header_name} · 오류 발생"
        color = COLOR_ORDER_FAILED
        desc = f"사유: {safe_reason}\n\n🕒 {kst_str}"

    embed = Embed(title=title[:256], description=desc[:4096], color=color)
    return enforce_embed_bounds(embed)
