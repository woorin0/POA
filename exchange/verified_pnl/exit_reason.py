from decimal import Decimal
import re
from typing import Optional, Any
from .models import ExitReason, PnlResult, PnlCalculationResult


def classify_exit_reason(order_info: Any, raw_order_data: Optional[dict] = None) -> ExitReason:
    """Classifies exit reason based on:
    1. Verified exchange order type / stop order condition
    2. Explicit TradingView exit_type parameter
    3. order_name string keywords
    4. Default UNKNOWN
    """
    is_close = (getattr(order_info, "is_close", False) is True) or (
        (getattr(order_info, "is_futures", False) is not True) and (getattr(order_info, "is_sell", False) is True)
    )
    if not is_close:
        return ExitReason.SIGNAL

    # 1. Exchange order conditions if available
    if raw_order_data and isinstance(raw_order_data, dict):
        order_type = str(raw_order_data.get("type") or "").upper()
        if "TRAILING" in order_type:
            return ExitReason.TRAIL
        if order_type in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            return ExitReason.TP
        elif order_type in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
            return ExitReason.SL
        elif "TRAILING" in order_type:
            return ExitReason.TRAIL

    # 2. Explicit exit_type from webhook
    exit_type = getattr(order_info, "exit_type", None)
    if exit_type:
        val = str(exit_type).strip().upper()
        if val in ("TP", "TAKE_PROFIT", "TAKE PROFIT", "익절", "PROFIT"):
            return ExitReason.TP
        elif val in ("SL", "STOP_LOSS", "STOP LOSS", "손절", "LOSS"):
            return ExitReason.SL
        elif val in ("TRAIL", "TRAILING", "TRAILING_STOP", "트레일링"):
            return ExitReason.TRAIL
        elif val in ("BE", "BREAKEVEN", "BREAK_EVEN", "본절"):
            return ExitReason.BE
        elif val in ("TIME", "TIME_EXIT", "시간"):
            return ExitReason.TIME
        elif val in ("SIGNAL", "STRATEGY", "전략", "신호"):
            return ExitReason.SIGNAL
        elif val in ("MANUAL", "수동"):
            return ExitReason.MANUAL
        elif val in ("LIQUIDATION", "강제청산"):
            return ExitReason.LIQUIDATION

    # 3. Fallback: order_name string analysis
    order_name = getattr(order_info, "order_name", None)
    if order_name:
        txt = str(order_name).lower()
        tokens = set(re.split(r"[^a-z]+", txt))
        if "tp" in tokens or any(w in txt for w in ("take profit", "익절", "take_profit")):
            return ExitReason.TP
        elif "sl" in tokens or any(w in txt for w in ("stop loss", "손절", "stop_loss")):
            return ExitReason.SL
        elif any(w in txt for w in ("trail", "trailing", "트레일링")):
            return ExitReason.TRAIL
        elif "be" in tokens or any(w in txt for w in ("breakeven", "break even", "본절")):
            return ExitReason.BE
        elif any(w in txt for w in ("time", "시간")):
            return ExitReason.TIME
        elif any(w in txt for w in ("manual", "수동")):
            return ExitReason.MANUAL
        elif any(w in txt for w in ("signal", "전략", "신호")):
            return ExitReason.SIGNAL

    return ExitReason.UNKNOWN


def classify_pnl_result(pnl_data: Optional[PnlCalculationResult], tolerance: Decimal = Decimal("0.0001")) -> PnlResult:
    """Classifies actual PnL outcome into PROFIT, LOSS, BREAKEVEN, or UNKNOWN."""
    if not pnl_data:
        return PnlResult.UNKNOWN

    # Check net realized PnL first
    if pnl_data.net_realized_pnl is not None:
        val = pnl_data.net_realized_pnl
        if val > tolerance:
            return PnlResult.PROFIT
        elif val < -tolerance:
            return PnlResult.LOSS
        else:
            return PnlResult.BREAKEVEN

    return PnlResult.UNKNOWN
