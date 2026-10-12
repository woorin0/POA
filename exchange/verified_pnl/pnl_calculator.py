from decimal import Decimal
from .models import PnlCalculationResult, VerificationStatus


class PnlCalculator:
    def calculate_pnl(self, fill, order_info, entry_price=None, pos_leverage=None,
                      allocated_entry_fee=None, allocated_funding=None, allocated_margin=None):
        is_close = (getattr(order_info, "is_close", False) is True) or (
            (getattr(order_info, "is_futures", False) is not True) and (getattr(order_info, "is_sell", False) is True)
        )
        if not is_close:
            return PnlCalculationResult(source="ENTRY_ORDER")
        if fill.verification_status != VerificationStatus.VERIFIED:
            return PnlCalculationResult(source="UNVERIFIED_FILL")
        ccy = fill.pnl_currency or "USDT"
        gross = fill.realized_pnl if fill.accounting_basis == "gross" else None
        close = fill.average_fill_price
        price_return = None
        if entry_price is not None and entry_price > 0 and close is not None and fill.filled_quantity > 0:
            delta = close - entry_price if getattr(order_info, "is_sell", False) else entry_price - close
            if gross is None and fill.accounting_basis != "net": gross = delta * fill.filled_quantity
            price_return = delta / entry_price * 100
        exit_fee = fill.fees.get(ccy) if set(fill.fees) <= {ccy} else None
        net = None
        base = fill.realized_pnl if fill.accounting_basis == "net" else gross
        if base is not None:
            is_spot = getattr(fill, "market_type", None) == "spot" or not getattr(order_info, "is_futures", False)
            entry_ok = fill.pnl_includes_entry_fee or allocated_entry_fee is not None
            exit_ok = fill.pnl_includes_exit_fee or (fill.fees_complete and exit_fee is not None)
            funding_ok = is_spot or fill.pnl_includes_funding or allocated_funding is not None
            if entry_ok and exit_ok and funding_ok:
                net = base
                if not fill.pnl_includes_entry_fee and allocated_entry_fee is not None: net -= allocated_entry_fee
                if not fill.pnl_includes_exit_fee and exit_fee is not None: net -= exit_fee
                if not fill.pnl_includes_funding and allocated_funding is not None: net += allocated_funding
        roi = net / allocated_margin * 100 if net is not None and allocated_margin is not None and allocated_margin > 0 else None
        return PnlCalculationResult(gross_realized_pnl=gross, entry_fee_allocated=allocated_entry_fee,
            exit_fee=exit_fee, funding_pnl_allocated=allocated_funding, net_realized_pnl=net,
            price_return_pct=price_return, net_roi_pct=roi, is_roi_estimated=False, pnl_currency=ccy,
            verification_status=VerificationStatus.VERIFIED if net is not None else VerificationStatus.UNVERIFIED,
            source="EXCHANGE_ACCOUNTING" if fill.realized_pnl is not None else "VERIFIED_COST_BASIS",
            notes=[] if net is not None else ["수수료·펀딩·체결 귀속 미확정"])
