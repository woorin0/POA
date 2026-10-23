from decimal import Decimal,localcontext
from .models import PnlCalculationResult, VerificationStatus


class PnlCalculator:
    def calculate_pnl(self, fill, order_info, entry_price=None, pos_leverage=None,
                      allocated_entry_fee=None, allocated_funding=None, allocated_margin=None):
        with localcontext() as arithmetic:
            arithmetic.prec=512
            return self._calculate_pnl(fill,order_info,entry_price,pos_leverage,allocated_entry_fee,allocated_funding,allocated_margin)

    def _calculate_pnl(self,fill,order_info,entry_price=None,pos_leverage=None,allocated_entry_fee=None,allocated_funding=None,allocated_margin=None):
        is_close = (getattr(order_info, "is_close", False) is True) or (
            (getattr(order_info, "is_futures", False) is not True) and (getattr(order_info, "is_sell", False) is True)
        )
        if not is_close:
            return PnlCalculationResult(source="ENTRY_ORDER")
        if fill.verification_status != VerificationStatus.VERIFIED:
            return PnlCalculationResult(source="UNVERIFIED_FILL")
        from .evidence import execution_valid,verify
        if not execution_valid(fill,fill.scope_data) or not verify(fill.accounting_receipt,"ACCOUNTING"):
            return PnlCalculationResult(source="UNVERIFIED_ACCOUNTING_EVIDENCE")
        basis=fill.accounting_receipt["body"]
        if basis.get("scope")!=fill.scope_data or basis.get("order_id")!=fill.order_id:
            return PnlCalculationResult(source="UNVERIFIED_ACCOUNTING_EVIDENCE")
        if entry_price!=Decimal(basis["entry_price"]) or allocated_entry_fee!=Decimal(basis["entry_fee"]):
            return PnlCalculationResult(source="UNVERIFIED_ACCOUNTING_EVIDENCE")
        if allocated_margin is not None and allocated_margin!=Decimal(basis["cost_basis"]):
            return PnlCalculationResult(source="UNVERIFIED_ACCOUNTING_EVIDENCE")
        if fill.market_type=="spot" and allocated_funding not in (None,Decimal(0)):
            return PnlCalculationResult(source="UNVERIFIED_FUNDING")
        ccy = fill.pnl_currency or "USDT"
        gross = fill.realized_pnl if fill.accounting_basis == "gross" else (fill.realized_pnl if fill.accounting_basis == "net" else None)
        close = fill.average_fill_price
        price_return = None
        if entry_price is not None and entry_price > 0 and close is not None and fill.filled_quantity > 0:
            delta = close - entry_price if getattr(order_info, "is_sell", False) else entry_price - close
            if gross is None: gross = delta * fill.filled_quantity
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
        if (
            allocated_margin is None
            and getattr(fill, "market_type", None) == "spot"
            and entry_price is not None
            and entry_price > 0
            and fill.filled_quantity > 0
            and allocated_entry_fee is not None
        ):
            allocated_margin = (entry_price * fill.filled_quantity) + allocated_entry_fee
        roi = net / allocated_margin * 100 if net is not None and allocated_margin is not None and allocated_margin > 0 else None
        return PnlCalculationResult(gross_realized_pnl=gross, entry_fee_allocated=allocated_entry_fee,
            exit_fee=exit_fee, funding_pnl_allocated=allocated_funding, net_realized_pnl=net,
            price_return_pct=price_return, net_roi_pct=roi, is_roi_estimated=False, pnl_currency=ccy,
            verification_status=VerificationStatus.VERIFIED if net is not None else VerificationStatus.UNVERIFIED,
            source="EXCHANGE_ACCOUNTING" if fill.realized_pnl is not None else "VERIFIED_COST_BASIS",
            notes=[] if net is not None else ["수수료·펀딩·체결 귀속 미확정"],
            cost_basis=allocated_margin,
            accounting_basis=fill.accounting_basis,
            pnl_includes_entry_fee=fill.pnl_includes_entry_fee,
            pnl_includes_exit_fee=fill.pnl_includes_exit_fee,
            pnl_includes_funding=fill.pnl_includes_funding)
