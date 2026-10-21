from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel


class ExitReason(str, Enum):
    TP = "TP"
    SL = "SL"
    TRAIL = "TRAIL"
    BE = "BE"
    TIME = "TIME"
    SIGNAL = "SIGNAL"
    MANUAL = "MANUAL"
    LIQUIDATION = "LIQUIDATION"
    UNKNOWN = "UNKNOWN"


class PnlResult(str, Enum):
    PROFIT = "PROFIT"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"
    UNKNOWN = "UNKNOWN"


class FillStatus(str, Enum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING = "PENDING"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    ESTIMATED = "ESTIMATED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    HELD = "HELD"


class VerifiedTrade(BaseModel):
    id: str
    price: Decimal
    amount: Decimal
    cost: Optional[Decimal] = None
    fee: Optional[Decimal] = None
    fee_currency: Optional[str] = None
    timestamp: Optional[float] = None  # seconds or ms

    class Config:
        arbitrary_types_allowed = True


class VerifiedFill(BaseModel):
    account_id: str = "default"
    market_type: str = "unknown"
    fees_complete: bool = False
    accounting_basis: str = "unknown"
    pnl_includes_entry_fee: bool = False
    pnl_includes_exit_fee: bool = False
    pnl_includes_funding: bool = False
    verified_order_type: Optional[str] = None
    exchange: str
    symbol: str
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: FillStatus = FillStatus.UNVERIFIED
    order_side: Optional[str] = None  # buy, sell
    position_side: Optional[str] = None  # long, short, net
    requested_quantity: Optional[Decimal] = None
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Optional[Decimal] = None
    average_fill_price: Optional[Decimal] = None
    contract_size: Optional[Decimal] = None
    trades: List[VerifiedTrade] = []
    fees: Dict[str, Decimal] = {}  # currency -> total fee
    realized_pnl: Optional[Decimal] = None
    pnl_currency: Optional[str] = None
    fill_timestamp: Optional[float] = None
    source: str = "REST_FETCH"
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    raw_info: Optional[Dict[str, Any]] = None

    class Config:
        arbitrary_types_allowed = True


class PnlCalculationResult(BaseModel):
    gross_realized_pnl: Optional[Decimal] = None
    entry_fee_allocated: Optional[Decimal] = None
    exit_fee: Optional[Decimal] = None
    funding_pnl_allocated: Optional[Decimal] = None
    net_realized_pnl: Optional[Decimal] = None
    price_return_pct: Optional[Decimal] = None
    net_roi_pct: Optional[Decimal] = None
    is_roi_estimated: bool = False
    pnl_currency: str = "USDT"
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    source: str = "UNVERIFIED"
    notes: List[str] = []
    cost_basis: Optional[Decimal] = None
    accounting_basis: Optional[str] = None
    pnl_includes_entry_fee: Optional[bool] = None
    pnl_includes_exit_fee: Optional[bool] = None
    pnl_includes_funding: Optional[bool] = None

    class Config:
        arbitrary_types_allowed = True


class NotificationPayload(BaseModel):
    event_type: str = "final"
    event_version: int = 2
    fee_breakdown: Optional[str] = None
    event_id: str
    exchange: str
    symbol: str
    side: str  # e.g. "롱 종료", "매도", "롱 진입"
    order_name: str
    exit_reason: ExitReason
    pnl_result: PnlResult
    fill_status: FillStatus
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    requested_qty: Optional[str] = None
    filled_qty: Optional[str] = None
    remaining_qty: Optional[str] = None
    entry_price: Optional[str] = None
    exit_price: Optional[str] = None
    gross_pnl: Optional[str] = None
    entry_fee_allocated: Optional[str] = None
    exit_fee: Optional[str] = None
    funding_pnl: Optional[str] = None
    net_pnl: Optional[str] = None
    price_return_pct: Optional[str] = None
    roi_pct: Optional[str] = None
    roi_label: str = "ROI"
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    fill_verification_status: Optional[VerificationStatus] = None
    pnl_verification_status: Optional[VerificationStatus] = None
    roi_verification_status: Optional[VerificationStatus] = None
    is_roi_estimated: bool = False
    verification_source: str = "거래소 실제 체결 내역"
    event_timestamp: float  # UTC timestamp
    fill_timestamp: Optional[float] = None  # UTC timestamp
    leverage: Optional[int] = None
    amount_field_name: str = "수량"
    error_message: Optional[str] = None
    signal_id: Optional[str] = None
    strategy_id: Optional[str] = None
    pnl_currency: Optional[str] = None
    notional_amount: Optional[str] = None
    accounting_basis: Optional[str] = None
    pnl_includes_entry_fee: Optional[bool] = None
    pnl_includes_exit_fee: Optional[bool] = None
    pnl_includes_funding: Optional[bool] = None
    cost_basis: Optional[str] = None
    cost_basis_currency: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True
