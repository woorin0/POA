"""Comprehensive pytest & unittest suite for POA Discord Embed v2 (Mobile Unified Design)."""
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import types

def _load_module(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj

def _setup_isolated(root=ROOT):
    if hasattr(sys, "_poa_isolated_setup"):
        return sys._poa_isolated_setup
    for name in list(sys.modules):
        if name == "exchange" or name.startswith("exchange."):
            del sys.modules[name]
    try:
        from pydantic import class_validators
        if hasattr(class_validators, "_FUNCS"):
            class_validators._FUNCS.clear()
    except Exception:
        pass
    pkg = types.ModuleType("exchange")
    pkg.__path__ = [str(root / "exchange")]
    sys.modules["exchange"] = pkg
    with patch("os.path.isfile", return_value=False):
        schema = _load_module("exchange.model", root / "exchange/model/schemas.py")
    schema.Settings.Config.env_file = None
    os.environ["PASSWORD"] = "test-only"
    settings = schema.Settings(_env_file=None, PASSWORD="test-only", WHITELIST=[])
    util = types.ModuleType("exchange.utility")
    util.settings = settings
    util.__path__ = [str(root / "exchange/utility")]
    sys.modules["exchange.utility"] = util
    for fn_name in [
        "log_message", "log_order_message", "print_alert_message", "log_alert_message",
        "logger_test", "log_order_error_message", "log_validation_error_message",
        "log_hedge_message", "log_error_message",
    ]:
        setattr(util, fn_name, Mock())
    db_mod = types.ModuleType("exchange.database")
    db_mod.db = Mock()
    sys.modules["exchange.database"] = db_mod
    stock_mod = types.ModuleType("exchange.stock")
    stock_mod.KoreaInvestment = type("KoreaInvestment", (), {})
    stock_mod.__path__ = []
    sys.modules["exchange.stock"] = stock_mod
    kis_mod = types.ModuleType("exchange.stock.kis")
    kis_mod.KoreaInvestment = stock_mod.KoreaInvestment
    sys.modules["exchange.stock.kis"] = kis_mod
    sys.modules["exchange.pocket"] = types.ModuleType("exchange.pocket")
    sys.modules["pendulum"] = types.ModuleType("pendulum")
    import ccxt
    import httpx
    pre = types.ModuleType("exchange.pexchange")
    pre.ccxt = ccxt
    pre.ccxt_async = Mock()
    pre.httpx = httpx
    sys.modules["exchange.pexchange"] = pre
    pkg.error = _load_module("exchange.error", root / "exchange/error.py")
    for ex_name in ["binance", "bybit", "okx", "bitget", "upbit"]:
        _load_module("exchange." + ex_name, root / ("exchange/" + ex_name + ".py"))
    pre.__package__ = "exchange"
    exec(compile((root / "exchange/pexchange.py").read_text(encoding="utf-8"), str(root / "exchange/pexchange.py"), "exec"), pre.__dict__)
    for fn_name in ["get_exchange", "get_bot"]:
        setattr(pkg, fn_name, getattr(pre, fn_name))
    pkg.db = db_mod.db
    pkg.settings = settings
    pkg.log_message = util.log_message
    sys._poa_isolated_setup = (schema, pre, util)
    return schema, pre, util

schema, legacy, util = _setup_isolated(ROOT)
MarketOrder = schema.MarketOrder
OrderRequest = schema.OrderRequest
Settings = schema.Settings

from exchange.verified_pnl.models import (
    ExitReason,
    FillStatus,
    NotificationPayload,
    PnlCalculationResult,
    PnlResult,
    VerificationStatus,
    VerifiedFill,
    VerifiedTrade,
)
from exchange.verified_pnl.pnl_calculator import PnlCalculator
from exchange.verified_pnl.exit_reason import classify_exit_reason, classify_pnl_result
from exchange.verified_pnl.ledger import allocate
from exchange.verified_pnl.dispatcher import (
    dispatch_verified_alert,
    flush_outbox,
    make_manual_review_payload,
    make_payload,
    verification_tick,
)
from exchange.verified_pnl.outbox import (
    DiscordSender,
    NotificationOutboxStore,
    build_verified_embed,
)
import importlib.util as _importlib_util
_de_spec = _importlib_util.spec_from_file_location(
    "exchange.utility.discord_embed",
    ROOT / "exchange/utility/discord_embed.py",
)
_de_mod = _importlib_util.module_from_spec(_de_spec)
sys.modules["exchange.utility.discord_embed"] = _de_mod
_de_spec.loader.exec_module(_de_mod)
from exchange.utility.discord_embed import (
    COLOR_BUY_COMPLETED,
    COLOR_ORDER_AMBIGUOUS,
    COLOR_ORDER_FAILED,
    COLOR_SELL_COMPLETED,
    EXIT_REASON_KOREAN,
    build_error_embed,
    build_legacy_order_embed,
    build_mobile_verified_embed,
    classify_error_ambiguity,
    detect_currency,
    enforce_embed_bounds,
    extract_base_asset,
    format_exit_reason_korean,
    format_kst_full,
    format_price_display,
    format_quantity_display,
    format_roi_display,
    get_action_header_label,
    is_buy_or_entry_side,
    is_sell_or_close_side,
    normalize_display_symbol,
    normalize_strategy_name,
    sanitize_discord_text,
)

D = Decimal


class DiscordEmbedV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_v2_notifications.sqlite3")
        self.settings = Settings(
            PASSWORD="test-only",
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            VERIFIED_COST_BASIS_COMPLETE=True,
            NET_REALIZED_PNL_ENABLED=True,
            UPBIT_VERIFIED_ALERTS=True,
            UPBIT_NET_PNL_ENABLED=True,
            UPBIT_PARTIAL_FILL_ALERTS=True,
            EXIT_REASON_CLASSIFICATION_V2=True,
            PARTIAL_FILL_ALERTS=True,
            DISCORD_OUTBOX_ENABLED=True,
            DISCORD_RETRY_ENABLED=True,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
            DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/123456/mock_token",
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # Category A — Embed Rendering Unit Tests (Section 10)
    # =========================================================================
    def test_A01_upbit_spot_buy_completed_embed(self):
        payload = NotificationPayload(
            event_id="buy-sol-1",
            exchange="UPBIT",
            symbol="SOL/KRW",
            side="매수",
            order_name="솔라나_돌파_v1",
            exit_reason=ExitReason.UNKNOWN,
            pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.FILLED,
            filled_qty="5.105",
            exit_price="144,000",
            fee_breakdown="367.56 KRW",
            verification_status=VerificationStatus.VERIFIED,
            event_timestamp=1760947451.0,
            fill_timestamp=1760947451.0,
        )
        embed = build_verified_embed(payload).data
        self.assertEqual(embed["color"], COLOR_BUY_COMPLETED)
        self.assertEqual(embed["color"], 0x16A34A)
        self.assertEqual(embed["title"], "🟢 UPBIT · SOL/KRW · 매수 완료")
        desc = embed["description"]
        self.assertIn("전략: 솔라나_돌파_v1", desc)
        self.assertIn("체결가: 144,000원", desc)
        self.assertIn("체결수량: 5.105 SOL", desc)
        self.assertIn("매수금액: 735,120원", desc)
        self.assertIn("수수료: 367.56원", desc)
        self.assertIn("✅ 실체결 검증 완료", desc)
        self.assertIn("KST", desc)
        # Buy order must not show sell PnL banner
        self.assertNotIn("확정손익", desc)

    def test_A02_binance_futures_long_entry_embed(self):
        payload = NotificationPayload(
            event_id="entry-btc-1",
            exchange="BINANCE",
            symbol="BTC/USDT:USDT",
            side="롱 진입",
            order_name="BTC_추세추종_5M",
            exit_reason=ExitReason.UNKNOWN,
            pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.FILLED,
            filled_qty="0.01",
            exit_price="100,000",
            fee_breakdown="0.5 USDT",
            leverage=5,
            verification_status=VerificationStatus.VERIFIED,
            event_timestamp=1760947451.0,
            fill_timestamp=1760947451.0,
        )
        embed = build_verified_embed(payload).data
        self.assertEqual(embed["color"], 0x16A34A)
        # Symbol deduplication: BTC/USDT:USDT -> BTC/USDT
        self.assertEqual(embed["title"], "🟢 BINANCE · BTC/USDT · 롱 진입 완료")
        desc = embed["description"]
        self.assertIn("전략: BTC_추세추종_5M", desc)
        self.assertIn("레버리지: 5x", desc)
        self.assertIn("체결가: 100,000 USDT", desc)
        self.assertIn("체결수량: 0.01 BTC", desc)
        self.assertIn("매수금액: 1,000 USDT", desc)
        self.assertIn("수수료: 0.5 USDT", desc)
        self.assertIn("✅ 실체결 검증 완료", desc)

    def test_A03_upbit_spot_sell_profit_case_A_embed(self):
        payload = NotificationPayload(
            event_id="sell-sol-profit",
            exchange="UPBIT",
            symbol="SOL/KRW",
            side="매도",
            order_name="솔라나_돌파_v1",
            exit_reason=ExitReason.TP,
            pnl_result=PnlResult.PROFIT,
            fill_status=FillStatus.FILLED,
            filled_qty="5.105",
            entry_price="144,000",
            exit_price="148,000",
            entry_fee_allocated="+367.56 KRW",
            exit_fee="+377.77 KRW",
            net_pnl="+19674.67 KRW",
            roi_pct="2.68",
            verification_status=VerificationStatus.VERIFIED,
            fill_verification_status=VerificationStatus.VERIFIED,
            pnl_verification_status=VerificationStatus.VERIFIED,
            roi_verification_status=VerificationStatus.VERIFIED,
            event_timestamp=1760958129.0,
            fill_timestamp=1760958129.0,
        )
        embed = build_verified_embed(payload).data
        # Sell is ALWAYS Red (#EF4444) even on profit!
        self.assertEqual(embed["color"], COLOR_SELL_COMPLETED)
        self.assertEqual(embed["color"], 0xEF4444)
        self.assertEqual(embed["title"], "🔴 UPBIT · SOL/KRW · 매도 완료")
        desc = embed["description"]
        self.assertIn("💰 확정손익: +19,675원 (+2.68%)", desc)
        self.assertIn("매수가: 144,000원", desc)
        self.assertIn("매도가: 148,000원", desc)
        self.assertIn("체결수량: 5.105 SOL", desc)
        self.assertIn("매도금액: 755,540원", desc)
        self.assertIn("총수수료: 745.33원", desc)
        self.assertIn("청산사유: 익절 조건", desc)
        self.assertIn("✅ 손익 검증 완료", desc)

    def test_A04_upbit_spot_sell_loss_case_A_embed(self):
        payload = NotificationPayload(
            event_id="sell-sol-loss",
            exchange="UPBIT",
            symbol="SOL/KRW",
            side="매도",
            order_name="솔라나_돌파_v1",
            exit_reason=ExitReason.SL,
            pnl_result=PnlResult.LOSS,
            fill_status=FillStatus.FILLED,
            filled_qty="5.105",
            entry_price="144,000",
            exit_price="142,100",
            entry_fee_allocated="367.56 KRW",
            exit_fee="362.71 KRW",
            net_pnl="-10429.77 KRW",
            roi_pct="-1.42",
            verification_status=VerificationStatus.VERIFIED,
            fill_verification_status=VerificationStatus.VERIFIED,
            pnl_verification_status=VerificationStatus.VERIFIED,
            roi_verification_status=VerificationStatus.VERIFIED,
            event_timestamp=1760958129.0,
        )
        embed = build_verified_embed(payload).data
        self.assertEqual(embed["color"], 0xEF4444)
        desc = embed["description"]
        self.assertIn("📉 확정손익: -10,430원 (-1.42%)", desc)
        self.assertIn("청산사유: 손절 조건", desc)
        self.assertIn("✅ 손익 검증 완료", desc)

    def test_A05_upbit_spot_sell_breakeven_embed(self):
        payload = NotificationPayload(
            event_id="sell-sol-be",
            exchange="UPBIT",
            symbol="SOL/KRW",
            side="매도",
            order_name="솔라나_돌파_v1",
            exit_reason=ExitReason.BE,
            pnl_result=PnlResult.BREAKEVEN,
            fill_status=FillStatus.FILLED,
            filled_qty="1",
            entry_price="100,000",
            exit_price="100,000",
            entry_fee_allocated="0 KRW",
            exit_fee="0 KRW",
            net_pnl="0 KRW",
            roi_pct="0.00",
            verification_status=VerificationStatus.VERIFIED,
            fill_verification_status=VerificationStatus.VERIFIED,
            pnl_verification_status=VerificationStatus.VERIFIED,
            roi_verification_status=VerificationStatus.VERIFIED,
            event_timestamp=1760958129.0,
        )
        embed = build_verified_embed(payload).data
        self.assertEqual(embed["color"], 0xEF4444)
        desc = embed["description"]
        self.assertIn("➖ 확정손익: 0원 (0.00%)", desc)
        self.assertIn("청산사유: 본절 조건", desc)

    def test_A06_binance_futures_close_case_B_pnl_verified_roi_unverified(self):
        # Futures close with verified net_pnl (+18.99 USDT), price_return_pct="2.00", but roi_pct=None
        payload = NotificationPayload(
            event_id="close-btc-case-b",
            exchange="BINANCE",
            symbol="BTC/USDT",
            side="롱 종료",
            order_name="BTC_추세추종_5M",
            exit_reason=ExitReason.TP,
            pnl_result=PnlResult.PROFIT,
            fill_status=FillStatus.FILLED,
            filled_qty="0.01",
            entry_price="100,000",
            exit_price="102,000",
            entry_fee_allocated="0.50 USDT",
            exit_fee="0.51 USDT",
            net_pnl="+18.99 USDT",
            price_return_pct="2.00",
            roi_pct=None,  # Margin unverified
            leverage=10,
            verification_status=VerificationStatus.VERIFIED,
            fill_verification_status=VerificationStatus.VERIFIED,
            pnl_verification_status=VerificationStatus.VERIFIED,
            roi_verification_status=VerificationStatus.UNVERIFIED,
            event_timestamp=1760958129.0,
        )
        embed = build_verified_embed(payload).data
        self.assertEqual(embed["color"], 0xEF4444)
        desc = embed["description"]
        self.assertIn("💰 확정손익: +18.99 USDT", desc)
        self.assertIn("수익률: 미확정 (투입 원금/증거금 확인 필요)", desc)
        self.assertIn("✅ 순실현손익 검증 완료 (수익률 미확정)", desc)
        # Crucial check: price_return_pct (2.00%) must NEVER be disguised as confirmed ROI!
        self.assertNotIn("+2.00%", desc)

    def test_A07_sell_case_C_both_pnl_and_roi_unverified(self):
        payload = NotificationPayload(
            event_id="sell-case-c",
            exchange="UPBIT",
            symbol="BTC/KRW",
            side="매도",
            order_name="주문",
            exit_reason=ExitReason.SIGNAL,
            pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.FILLED,
            filled_qty="0.05",
            entry_price=None,
            exit_price="100,000,000",
            net_pnl=None,
            roi_pct=None,
            verification_status=VerificationStatus.UNVERIFIED,
            fill_verification_status=VerificationStatus.VERIFIED,
            pnl_verification_status=VerificationStatus.UNVERIFIED,
            roi_verification_status=VerificationStatus.UNVERIFIED,
            verification_source="ORDER_RESULT",
            event_timestamp=1760958129.0,
        )
        embed = build_verified_embed(payload).data
        self.assertEqual(embed["color"], 0xEF4444)
        desc = embed["description"]
        self.assertIn("⚪ 확정손익: 미확정", desc)
        self.assertIn("수익률: 미확정", desc)
        self.assertIn("매수가: 미확정", desc)
        self.assertIn("매도가: 100,000,000원", desc)
        self.assertIn("ℹ️ 매수 원가 또는 수수료 검증 필요", desc)

    def test_A08_order_failed_confirmed_rejection_embed(self):
        payload = NotificationPayload(
            event_id="fail-1",
            exchange="UPBIT",
            symbol="SOL/KRW",
            side="매수",
            order_name="솔라나_돌파_v1",
            exit_reason=ExitReason.UNKNOWN,
            pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.FAILED,
            requested_qty="5.105",
            filled_qty="0",
            error_message="주문 가능 금액(KRW) 부족",
            verification_status=VerificationStatus.VERIFIED,
            fill_verification_status=VerificationStatus.VERIFIED,
            event_timestamp=1760958129.0,
        )
        embed = build_verified_embed(payload).data
        self.assertEqual(embed["color"], COLOR_ORDER_FAILED)
        self.assertEqual(embed["color"], 0xF59E0B)
        self.assertEqual(embed["title"], "⚠️ UPBIT · SOL/KRW · 매수 주문 실패")
        desc = embed["description"]
        self.assertIn("전략: 솔라나_돌파_v1", desc)
        self.assertIn("요청수량: 5.105 SOL", desc)
        self.assertIn("사유: 주문 가능 금액(KRW) 부족", desc)
        self.assertIn("상태: 주문이 체결되지 않았습니다", desc)

    def test_A09_ambiguous_timeout_and_manual_review_embed(self):
        data = {
            "info": {
                "exchange": "BINANCE",
                "base": "BTC",
                "quote": "USDT",
                "unified_symbol": "BTC/USDT",
                "side": "buy",
                "is_buy": True,
                "is_sell": False,
                "is_futures": True,
                "is_close": False,
                "order_name": "BTC_전략_1",
                "signal_id": "sig-timeout-99",
            },
            "result": {},
            "account": "acct-1",
        }
        payload = make_manual_review_payload("job-key-1", data, "missing_order_id", self.settings)
        embed = build_verified_embed(payload).data
        self.assertEqual(embed["color"], COLOR_ORDER_AMBIGUOUS)
        self.assertEqual(embed["color"], 0x64748B)
        self.assertIn("주문 결과 확인 필요", embed["title"])
        self.assertIn("수동 확인 필요", embed["title"])
        desc = embed["description"]
        self.assertIn("사유: 주문 ID 누락 (missing_order_id)", desc)
        self.assertIn("상태: 실제 체결 여부 확인 중 (미확정)", desc)
        self.assertIn("조치: 거래소 체결 내역 확인 필요", desc)
        expected_sig_ref = hashlib.sha256("sig-timeout-99".encode("utf-8")).hexdigest()[:8]
        self.assertIn(f"운영 참조: {expected_sig_ref}", desc)
        self.assertNotIn("sig-timeout-99", desc)
        # Never claim order was rejected/failed when order_id is missing
        self.assertNotIn("주문이 체결되지 않았습니다", desc)

    def test_A10_partial_fill_and_canceled_embeds(self):
        # 1. Partial fill
        p_partial = NotificationPayload(
            event_id="part-1",
            exchange="BYBIT",
            symbol="ETH/USDT",
            side="롱 진입",
            order_name="ETH_스윙",
            exit_reason=ExitReason.UNKNOWN,
            pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.PARTIALLY_FILLED,
            filled_qty="0.4",
            remaining_qty="0.6",
            exit_price="2,500",
            fill_verification_status=VerificationStatus.VERIFIED,
            verification_source="ORDER_RESULT",
            event_timestamp=1760958129.0,
        )
        e_part = build_verified_embed(p_partial).data
        self.assertEqual(e_part["color"], 0x64748B)
        self.assertEqual(e_part["title"], "⚪ BYBIT · ETH/USDT · 롱 진입 부분 체결")
        self.assertIn("체결수량: 0.4 ETH", e_part["description"])
        self.assertIn("미체결수량: 0.6 ETH", e_part["description"])

        # 2. Canceled without fill
        p_cancel = NotificationPayload(
            event_id="canc-0",
            exchange="OKX",
            symbol="SOL/USDT",
            side="매수",
            order_name="주문",
            exit_reason=ExitReason.UNKNOWN,
            pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.CANCELED,
            requested_qty="10",
            filled_qty="0",
            event_timestamp=1760958129.0,
        )
        e_canc = build_verified_embed(p_cancel).data
        self.assertEqual(e_canc["color"], 0x64748B)
        self.assertEqual(e_canc["title"], "⚪ OKX · SOL/USDT · 매수 주문 취소")
        self.assertIn("상태: 체결 없이 주문이 취소되었습니다", e_canc["description"])

        # 3. Partial fill then Canceled
        p_part_cancel = NotificationPayload(
            event_id="canc-part",
            exchange="UPBIT",
            symbol="XRP/KRW",
            side="매도",
            order_name="XRP_단타",
            exit_reason=ExitReason.UNKNOWN,
            pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.CANCELED,
            filled_qty="100",
            remaining_qty="50",
            exit_price="800",
            fill_verification_status=VerificationStatus.VERIFIED,
            verification_source="ORDER_RESULT",
            event_timestamp=1760958129.0,
        )
        e_pc = build_verified_embed(p_part_cancel).data
        self.assertEqual(e_pc["color"], 0x64748B)
        self.assertEqual(e_pc["title"], "⚪ UPBIT · XRP/KRW · 매도 부분 체결 후 취소")
        self.assertIn("체결수량: 100 XRP", e_pc["description"])
        self.assertIn("취소수량: 50 XRP", e_pc["description"])

    def test_A11_strategy_name_omission_and_truncation_and_sanitization(self):
        # Default "주문" -> strategy line omitted
        p_default = NotificationPayload(
            event_id="def-1",
            exchange="UPBIT",
            symbol="BTC/KRW",
            side="매수",
            order_name="주문",
            exit_reason=ExitReason.UNKNOWN,
            pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.FILLED,
            filled_qty="0.00012345",
            exit_price="100,000,000",
            verification_status=VerificationStatus.VERIFIED,
            fill_verification_status=VerificationStatus.VERIFIED,
            pnl_verification_status=VerificationStatus.VERIFIED,
            roi_verification_status=VerificationStatus.UNVERIFIED,
            event_timestamp=1760958129.0,
        )
        desc_def = build_verified_embed(p_default).data["description"]
        self.assertNotIn("전략:", desc_def)
        self.assertIn("체결수량: 0.00012345 BTC", desc_def)

        # Very long strategy name with @everyone, backticks, and secret webhook URL
        malicious_name = "전략_초장기_" + ("A" * 100) + " @everyone @here <@12345> ```code``` https://discord.com/api/webhooks/999/secret"
        sanitized = normalize_strategy_name(malicious_name, max_len=40)
        self.assertLessEqual(len(sanitized), 40)
        self.assertTrue(sanitized.endswith("…"))
        self.assertNotIn("@everyone", sanitize_discord_text(malicious_name))
        self.assertNotIn("https://discord.com/api/webhooks", sanitize_discord_text(malicious_name))

        # NaN / Infinity / None quantity & price handling
        self.assertEqual(format_quantity_display(float("nan"), "BTC"), "미확정")
        self.assertEqual(format_quantity_display(float("inf"), "BTC"), "미확정")
        self.assertEqual(format_price_display(float("nan"), "KRW"), "미확정")
        self.assertEqual(format_price_display(None, "USDT"), "미확정")
        self.assertIsNone(format_roi_display(float("inf")))

    # =========================================================================
    # Category B — PnL & ROI Financial Calculation Tests (Section 10)
    # =========================================================================
    def test_B01_upbit_spot_buy_then_sell_profit_roi_exact(self):
        store = NotificationOutboxStore(self.db_path)
        buy_info = OrderRequest(exchange="UPBIT", base="SOL", quote="KRW", side="buy", amount=5.105, strategy_id="sol-v1", order_name="솔라나_돌파_v1")
        buy_fill = VerifiedFill(
            exchange="UPBIT",
            symbol="SOL/KRW",
            order_id="b-sol-1",
            market_type="spot",
            position_side="LONG",
            status=FillStatus.FILLED,
            filled_quantity=D("5.105"),
            average_fill_price=D("144000"),
            pnl_currency="KRW",
            fees={"KRW": D("367.56")},
            fees_complete=True,
            verification_status=VerificationStatus.VERIFIED,
            source="ORDER_RESULT",
            trades_complete=True,
            trades=[VerifiedTrade(id="tb1", price=D("144000"), amount=D("5.105"), fee=D("367.56"), fee_currency="KRW", timestamp=100.0)],
        )
        allocate(store, buy_fill, buy_info, "acct-sol", True)

        sell_info = OrderRequest(exchange="UPBIT", base="SOL", quote="KRW", side="sell", amount=5.105, strategy_id="sol-v1", order_name="솔라나_돌파_v1", exit_type="TP")
        sell_fill = VerifiedFill(
            exchange="UPBIT",
            symbol="SOL/KRW",
            order_id="s-sol-1",
            market_type="spot",
            position_side="LONG",
            status=FillStatus.FILLED,
            filled_quantity=D("5.105"),
            average_fill_price=D("148000"),
            pnl_currency="KRW",
            fees={"KRW": D("377.77")},
            fees_complete=True,
            verification_status=VerificationStatus.VERIFIED,
            source="ORDER_RESULT",
            trades_complete=True,
            trades=[VerifiedTrade(id="ts1", price=D("148000"), amount=D("5.105"), fee=D("377.77"), fee_currency="KRW", timestamp=200.0)],
        )
        avg, entry_fee = allocate(store, sell_fill, sell_info, "acct-sol", True)
        self.assertEqual(avg, D("144000"))
        self.assertEqual(entry_fee, D("367.56"))

        pnl = PnlCalculator().calculate_pnl(
            sell_fill, sell_info, entry_price=avg, allocated_entry_fee=entry_fee, allocated_funding=D(0)
        )
        # Gross = (148000 - 144000) * 5.105 = 20420 KRW
        # Net = 20420 - 367.56 - 377.77 = 19674.67 KRW
        # Cost basis with entry fee = 735120 + 367.56 = 735487.56 KRW
        # ROI = 19674.67 / 735487.56 * 100 = 2.675051363... -> formatted +2.68%
        self.assertEqual(pnl.gross_realized_pnl, D("20420.000"))
        self.assertEqual(pnl.net_realized_pnl, D("19674.670"))
        self.assertIsNotNone(pnl.net_roi_pct)
        self.assertEqual(format_roi_display(pnl.net_roi_pct), "+2.68%")

        payload = make_payload("ev-sol-1", sell_fill, SimpleNamespace(**sell_info.dict()), pnl, avg, self.settings)
        embed = build_verified_embed(payload).data
        self.assertIn("💰 확정손익: +19,675원 (+2.68%)", embed["description"])
        self.assertIn("✅ 손익 검증 완료", embed["description"])

    def test_B02_split_entries_and_partial_sells_fee_allocation(self):
        store = NotificationOutboxStore(self.db_path)
        # Entry 1: 1 SOL @ 100,000 KRW (fee 50 KRW)
        # Entry 2: 1 SOL @ 110,000 KRW (fee 55 KRW)
        # Total: 2 SOL @ avg 105,000 KRW, total fee 105 KRW
        for idx, (price, fee) in enumerate([(D("100000"), D("50")), (D("110000"), D("55"))], start=1):
            b_info = OrderRequest(exchange="UPBIT", base="SOL", quote="KRW", side="buy", amount=1, strategy_id="split-s")
            b_fill = VerifiedFill(
                exchange="UPBIT", symbol="SOL/KRW", order_id=f"b-{idx}", market_type="spot", position_side="LONG",
                status=FillStatus.FILLED, filled_quantity=D("1"), average_fill_price=price, pnl_currency="KRW",
                fees={"KRW": fee}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
                source="ORDER_RESULT", trades_complete=True,
                trades=[VerifiedTrade(id=f"tb-{idx}", price=price, amount=D("1"), fee=fee, fee_currency="KRW", timestamp=float(idx))],
            )
            allocate(store, b_fill, b_info, "acct-split", True)

        # Partial Sell 1: 0.5 SOL @ 120,000 KRW (exit fee 30 KRW)
        # Allocated entry fee = 105 * (0.5 / 2.0) = 26.25 KRW
        s1_info = OrderRequest(exchange="UPBIT", base="SOL", quote="KRW", side="sell", amount=0.5, strategy_id="split-s", exit_type="TP")
        s1_fill = VerifiedFill(
            exchange="UPBIT", symbol="SOL/KRW", order_id="s-1", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.5"), average_fill_price=D("120000"), pnl_currency="KRW",
            fees={"KRW": D("30")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="ts-1", price=D("120000"), amount=D("0.5"), fee=D("30"), fee_currency="KRW", timestamp=10.0)],
        )
        avg1, fee1 = allocate(store, s1_fill, s1_info, "acct-split", True)
        self.assertEqual(avg1, D("105000"))
        self.assertEqual(fee1, D("26.25"))
        pnl1 = PnlCalculator().calculate_pnl(s1_fill, s1_info, entry_price=avg1, allocated_entry_fee=fee1, allocated_funding=D(0))
        # Gross = (120000 - 105000) * 0.5 = 7500; Net = 7500 - 26.25 - 30 = 7443.75
        self.assertEqual(pnl1.net_realized_pnl, D("7443.75"))
        self.assertIsNotNone(pnl1.net_roi_pct)

    def test_B03_price_gain_but_net_loss_after_fees(self):
        # Price rose +5 KRW, but fees = 1000.25 KRW -> Net PnL = -995.25 KRW (LOSS)
        s_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="sell", amount=0.01, strategy_id="m1", exit_type="TP")
        s_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="sm1", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.01"), average_fill_price=D("100000500"),
            pnl_currency="KRW", fees={"KRW": D("500.25")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
        )
        pnl = PnlCalculator().calculate_pnl(s_fill, s_info, entry_price=D("100000000"), allocated_entry_fee=D("500"), allocated_funding=D(0))
        self.assertEqual(pnl.gross_realized_pnl, D("5.00"))
        self.assertEqual(pnl.net_realized_pnl, D("-995.25"))
        self.assertEqual(classify_pnl_result(pnl), PnlResult.LOSS)
        payload = make_payload("ev-fee-loss", s_fill, SimpleNamespace(**s_info.dict()), pnl, D("100000000"), self.settings)
        embed = build_verified_embed(payload).data
        self.assertIn("📉 확정손익: -995원 (-0.10%)", embed["description"])
        self.assertIn("청산사유: 익절 조건", embed["description"])

    def test_B04_third_asset_fee_bnb_forces_unverified_net_pnl(self):
        # Fee paid in BNB on a USDT pair -> set(fill.fees) <= {ccy} is False -> net_realized_pnl is None
        s_info = Mock(is_close=True, is_futures=False, is_sell=True, is_buy=False)
        s_fill = VerifiedFill(
            exchange="BINANCE", symbol="BTC/USDT", order_id="bnb-fee-1", market_type="spot",
            status=FillStatus.FILLED, filled_quantity=D("0.01"), average_fill_price=D("102000"),
            pnl_currency="USDT", fees={"BNB": D("0.001")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
        )
        pnl = PnlCalculator().calculate_pnl(s_fill, s_info, entry_price=D("100000"), allocated_entry_fee=D("0.5"))
        self.assertIsNone(pnl.net_realized_pnl)
        self.assertIsNone(pnl.net_roi_pct)
        self.assertEqual(pnl.verification_status, VerificationStatus.UNVERIFIED)

    # =========================================================================
    # Category C — Exit Reason vs Actual PnL Matrix (Section 10)
    # =========================================================================
    def test_C01_exit_reason_and_actual_pnl_full_matrix(self):
        matrix = [
            (ExitReason.TP, D("5000"), "2.50", "💰 확정손익: +5,000원 (+2.50%)", "청산사유: 익절 조건"),
            (ExitReason.TP, D("-500"), "-0.25", "📉 확정손익: -500원 (-0.25%)", "청산사유: 익절 조건"),
            (ExitReason.SL, D("-10000"), "-5.00", "📉 확정손익: -10,000원 (-5.00%)", "청산사유: 손절 조건"),
            (ExitReason.SL, D("2000"), "1.00", "💰 확정손익: +2,000원 (+1.00%)", "청산사유: 손절 조건"),
            (ExitReason.TRAIL, D("8000"), "4.00", "💰 확정손익: +8,000원 (+4.00%)", "청산사유: 트레일링 스톱"),
            (ExitReason.BE, D("-150"), "-0.08", "📉 확정손익: -150원 (-0.08%)", "청산사유: 본절 조건"),
            (ExitReason.TIME, D("1200"), "0.60", "💰 확정손익: +1,200원 (+0.60%)", "청산사유: 시간 청산"),
            (ExitReason.SIGNAL, D("-3000"), "-1.50", "📉 확정손익: -3,000원 (-1.50%)", "청산사유: 전략 종료 신호"),
            (ExitReason.MANUAL, D("0"), "0.00", "➖ 확정손익: 0원 (0.00%)", "청산사유: 수동 청산"),
            (ExitReason.LIQUIDATION, D("-100000"), "-99.50", "📉 확정손익: -100,000원 (-99.50%)", "청산사유: 강제 청산"),
            (ExitReason.UNKNOWN, D("3000"), "1.50", "💰 확정손익: +3,000원 (+1.50%)", "청산사유: 청산 사유 미확인"),
        ]
        for reason, net_val, roi_val, expected_banner, expected_reason_line in matrix:
            res_enum = PnlResult.PROFIT if net_val > 0 else (PnlResult.LOSS if net_val < 0 else PnlResult.BREAKEVEN)
            if net_val == D("-100000"):
                entry_p = "100,500"
                exit_p = "500"
            else:
                entry_p = "200,000"
                exit_p = f"{int(Decimal('200000') + net_val):,}"
            p = NotificationPayload(
                event_id=f"mat-{reason.value}-{net_val}",
                exchange="UPBIT",
                symbol="SOL/KRW",
                side="매도",
                order_name="전략A",
                exit_reason=reason,
                pnl_result=res_enum,
                fill_status=FillStatus.FILLED,
                filled_qty="1",
                entry_price=entry_p,
                exit_price=exit_p,
                entry_fee_allocated="0 KRW",
                exit_fee="0 KRW",
                gross_pnl=f"{net_val:+f} KRW",
                net_pnl=f"{net_val:+f} KRW",
                roi_pct=roi_val,
                verification_status=VerificationStatus.VERIFIED,
                fill_verification_status=VerificationStatus.VERIFIED,
                pnl_verification_status=VerificationStatus.VERIFIED,
                roi_verification_status=VerificationStatus.VERIFIED,
                event_timestamp=1760958129.0,
            )
            embed = build_verified_embed(p).data
            self.assertEqual(embed["color"], 0xEF4444)
            self.assertIn(expected_banner, embed["description"])
            self.assertIn(expected_reason_line, embed["description"])

    # =========================================================================
    # Category D — Confirmed Order Failure vs Ambiguous Timeout (Section 10)
    # =========================================================================
    def test_D01_error_embed_confirmed_failure_vs_ambiguous_timeout(self):
        info = MarketOrder(
            password="test-only",
            exchange="BINANCE",
            base="BTC",
            quote="USDT.P",
            side="entry/buy",
            amount=0.05,
            order_name="BTC_전략",
        )
        # 1. Confirmed rejection (Insufficient balance) -> Orange (#F59E0B), ⚠️
        err_reject = "binance Account has insufficient balance for requested action"
        self.assertFalse(classify_error_ambiguity(err_reject))
        e_reject = build_error_embed(err_reject, order_info=info).data
        self.assertEqual(e_reject["color"], 0xF59E0B)
        self.assertTrue(e_reject["title"].startswith("⚠️ BINANCE · BTC/USDT · 롱 진입 주문 실패"))
        self.assertIn("상태: 주문이 체결되지 않았습니다", e_reject["description"])

        # 2. Ambiguous timeout (RequestTimeout) -> Gray (#64748B), ⚪
        err_timeout = "ccxt.base.errors.RequestTimeout: binance POST https://fapi.binance.com/fapi/v1/order timed out"
        self.assertTrue(classify_error_ambiguity(err_timeout))
        e_timeout = build_error_embed(err_timeout, order_info=info).data
        self.assertEqual(e_timeout["color"], 0x64748B)
        self.assertTrue(e_timeout["title"].startswith("⚪ BINANCE · BTC/USDT · 롱 진입 주문 결과 확인 필요"))
        self.assertIn("상태: 실제 체결 여부 확인 중 (미확정)", e_timeout["description"])
        self.assertIn("중복 재주문 금지", e_timeout["description"])
        self.assertNotIn("주문이 체결되지 않았습니다", e_timeout["description"])

    def test_D02_legacy_log_order_message_never_fakes_verified_fill_or_pnl(self):
        with patch("loguru.logger.remove"), patch("loguru.logger.add"):
            lm = _load_module("exchange.utility.LogMaker", ROOT / "exchange/utility/LogMaker.py")

        info = MarketOrder(
            password="test-only",
            exchange="UPBIT",
            base="SOL",
            quote="KRW",
            side="sell",
            amount=5.105,
            order_name="솔라나_레거시",
            exit_type="TP",
        )
        result = {"id": "leg-1", "amount": 5.105, "entry_price": 140000, "close_price": 150000, "price": 150000}
        captured = []
        mock_hook = Mock()
        mock_hook.send.side_effect = lambda **kw: captured.append(kw["embed"].data)
        with patch.object(lm, "hook", mock_hook, create=True):
            lm.log_order_message("UPBIT", result, info)

        self.assertEqual(len(captured), 1)
        embed = captured[0]
        self.assertEqual(embed["color"], 0x64748B)
        self.assertIn("접수 (미검증)", embed["title"])
        self.assertIn("확정손익: 미확정 (레거시 미검증 경로)", embed["description"])
        self.assertNotIn("실체결 검증 완료", embed["description"])
        self.assertNotIn("손익 검증 완료", embed["description"])

    # =========================================================================
    # Category E — Outbox, Network Exception, Backward Compatibility & Static Checks
    # =========================================================================
    def test_E01_outbox_backward_compatibility_with_old_payload_json(self):
        # Simulate an old v2 payload stored in SQLite before new optional fields were added
        old_json = json.dumps({
            "event_type": "final",
            "event_version": 2,
            "event_id": "old-ev-1",
            "exchange": "UPBIT",
            "symbol": "BTC/KRW",
            "side": "매수",
            "order_name": "레거시호환",
            "exit_reason": "UNKNOWN",
            "pnl_result": "UNKNOWN",
            "fill_status": "FILLED",
            "filled_qty": "0.01",
            "exit_price": "100,000,000",
            "verification_status": "VERIFIED",
            "event_timestamp": 1760958129.0,
        })
        payload = NotificationPayload(**json.loads(old_json))
        embed = build_verified_embed(payload).data
        self.assertEqual(embed["color"], 0x16A34A)
        self.assertIn("🟢 UPBIT · BTC/KRW · 매수 완료", embed["title"])
        self.assertIn("체결가: 100,000,000원", embed["description"])
        self.assertIn("매수금액: 1,000,000원", embed["description"])

    def test_E02_verification_tick_upbit_spot_end_to_end_with_mock_client(self):
        # End-to-end verification_tick for Upbit Buy -> Sell -> Outbox Flush -> Mock Discord Webhook
        buy_info = MarketOrder(
            password="test-only",
            exchange="UPBIT",
            base="SOL",
            quote="KRW",
            side="buy",
            amount=5.105,
            order_name="솔라나_돌파_v1",
            strategy_id="sol-e2e",
        )
        dispatch_verified_alert("UPBIT", {"id": "upbit-buy-1"}, buy_info, self.settings)

        mock_buy_client = Mock()
        mock_buy_client.market.return_value = {"symbol": "SOL/KRW"}
        mock_buy_client.fetch_order.return_value = {
            "id": "upbit-buy-1",
            "status": "closed",
            "amount": 5.105,
            "filled": 5.105,
            "remaining": 0.0,
            "average": 144000.0,
            "cost": 735120.0,
            "fee": {"cost": 367.56, "currency": "KRW"},
            "trades": [
                {"id": "t-buy-1", "price": 144000.0, "amount": 5.105, "cost": 735120.0, "fee": {"cost": 367.56, "currency": "KRW"}, "timestamp": 1760947451000}
            ],
        }
        verification_tick(self.settings, lambda *a: mock_buy_client)

        sell_info = MarketOrder(
            password="test-only",
            exchange="UPBIT",
            base="SOL",
            quote="KRW",
            side="sell",
            amount=5.105,
            order_name="솔라나_돌파_v1",
            strategy_id="sol-e2e",
            exit_type="TP",
        )
        dispatch_verified_alert("UPBIT", {"id": "upbit-sell-1"}, sell_info, self.settings)

        mock_sell_client = Mock()
        mock_sell_client.market.return_value = {"symbol": "SOL/KRW"}
        mock_sell_client.fetch_order.return_value = {
            "id": "upbit-sell-1",
            "status": "closed",
            "amount": 5.105,
            "filled": 5.105,
            "remaining": 0.0,
            "average": 148000.0,
            "cost": 755540.0,
            "fee": {"cost": 377.77, "currency": "KRW"},
            "trades": [
                {"id": "t-sell-1", "price": 148000.0, "amount": 5.105, "cost": 755540.0, "fee": {"cost": 377.77, "currency": "KRW"}, "timestamp": 1760958129000}
            ],
        }
        verification_tick(self.settings, lambda *a: mock_sell_client)

        sent_Payloads = []
        def fake_post(url, params=None, json=None, timeout=None, allow_redirects=None):
            sent_Payloads.append(json)
            resp = Mock(status_code=200)
            resp.json.return_value = {"id": f"msg-{len(sent_Payloads)}"}
            return resp

        with patch("requests.post", side_effect=fake_post):
            flush_outbox(self.settings)

        self.assertEqual(len(sent_Payloads), 2)
        buy_embed = sent_Payloads[0]["embeds"][0]
        sell_embed = sent_Payloads[1]["embeds"][0]

        self.assertEqual(buy_embed["color"], 0x16A34A)
        self.assertEqual(buy_embed["title"], "🟢 UPBIT · SOL/KRW · 매수 완료")
        self.assertIn("매수금액: 735,120원", buy_embed["description"])

        self.assertEqual(sell_embed["color"], 0xEF4444)
        self.assertEqual(sell_embed["title"], "🔴 UPBIT · SOL/KRW · 매도 완료")
        self.assertIn("💰 확정손익: +19,675원 (+2.68%)", sell_embed["description"])
        self.assertIn("총수수료: 745.33원", sell_embed["description"])
        self.assertIn("✅ 손익 검증 완료", sell_embed["description"])

    def test_E03_embed_size_bounds_enforced(self):
        huge_desc = "X" * 6000
        from exchange.utility.discord_embed import Embed
        e = Embed(title="T" * 300, description=huge_desc, color=0x16A34A)
        for i in range(30):
            e.add_field(name=f"F{i}_" + ("N" * 300), value="V" * 1200, inline=False)
        enforce_embed_bounds(e)
        self.assertLessEqual(len(e.data["title"]), 256)
        self.assertLessEqual(len(e.data["description"]), 4096)
        self.assertLessEqual(len(e.data["fields"]), 25)
        for f in e.data["fields"]:
            self.assertLessEqual(len(f["name"]), 256)
            self.assertLessEqual(len(f["value"]), 1024)
        total_chars = len(e.data["title"]) + len(e.data["description"]) + sum(len(f["name"]) + len(f["value"]) for f in e.data["fields"])
        self.assertLessEqual(total_chars, 6000)

    def test_E04_order_execution_files_untouched_via_git_diff(self):
        # Section 11.2 & P2-4: Verify all 26 order execution, safety, and deployment files have ZERO diff against fixed Base SHA
        base_sha = "69e48fc3989f9d957efb6bdef81ba57516cc5dfb"
        protected_files = [
            "main.py",
            "exchange/binance.py",
            "exchange/bybit.py",
            "exchange/okx.py",
            "exchange/bitget.py",
            "exchange/upbit.py",
            "exchange/pexchange.py",
            "exchange/safe_retry.py",
            "exchange/database.py",
            "exchange/pocket.py",
            "exchange/error.py",
            "exchange/model/schemas.py",
            "requirements.txt",
            "deployment/deploy_vultr.py",
            "deployment/fix_alerts.py",
            "deployment/preflight_vultr.py",
            "deployment/remote_deploy.sh",
            "deployment/rollback_vultr.py",
            "exchange/stock/kis.py",
            "exchange/stock/order.py",
            "exchange/stock/schemas.py",
            "exchange/stock/utility.py",
            "scripts/notification_db.py",
            "scripts/smoke_upbit.py",
            "scripts/verify_vultr_runtime.py",
            "tools/safe_order_admin.py",
        ]
        proc = subprocess.run(
            ["git", "diff", base_sha, "--"] + protected_files,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "", f"Protected order execution files were modified:\n{proc.stdout}")


    def test_E05_additional_edge_cases_for_high_coverage(self):
        # Cover remaining branches in discord_embed.py
        self.assertEqual(sanitize_discord_text(None), "")
        self.assertEqual(sanitize_discord_text("abc", max_len=1), "a")
        self.assertEqual(detect_currency("BTC/USDC"), "USDC")
        self.assertEqual(detect_currency("BTC/BUSD"), "BUSD")
        self.assertEqual(detect_currency("005930", None, "KRX"), "KRW")
        self.assertEqual(detect_currency("AAPL", None, "NASDAQ"), "USDT")
        self.assertEqual(extract_base_asset(None), "")
        self.assertEqual(extract_base_asset("005930"), "005930")
        self.assertEqual(normalize_display_symbol(None), "UNKNOWN")
        self.assertEqual(format_kst_full(None), "일시 미확인")
        self.assertEqual(format_kst_full("invalid-ts"), "일시 미확인")
        self.assertEqual(format_kst_full(float("inf")), "일시 미확인")

        # Small KRW prices (< 100 and < 1)
        self.assertEqual(format_price_display("12.34", "KRW"), "12.34원")
        self.assertEqual(format_price_display("0.1234", "KRW"), "0.1234원")
        self.assertEqual(format_price_display("-50", "KRW", signed=False), "-50원")
        # Small USDT prices (< 100 and < 1 and 0)
        self.assertEqual(format_price_display("12.345", "USDT"), "12.345 USDT")
        self.assertEqual(format_price_display("0.00012345", "USDT"), "0.00012345 USDT")
        self.assertEqual(format_price_display("0", "USDT"), "0 USDT")

        self.assertEqual(format_quantity_display("0", "BTC"), "0 BTC")
        self.assertEqual(format_exit_reason_korean(None), "청산 사유 미확인")
        self.assertTrue(is_buy_or_entry_side("매수"))
        self.assertFalse(is_buy_or_entry_side("매도"))
        self.assertEqual(get_action_header_label("", "완료"), "주문 완료")
        self.assertEqual(get_action_header_label("매수 완료", "완료"), "매수 완료")

        # Multi-currency fee breakdown & funding fee display on sell
        p_multi = NotificationPayload(
            event_id="multi-fee",
            exchange="BINANCE",
            symbol="BTC/USDT",
            side="숏 종료",
            order_name="숏전략",
            exit_reason=ExitReason.TP,
            pnl_result=PnlResult.PROFIT,
            fill_status=FillStatus.FILLED,
            filled_qty="0.01",
            entry_price="100,000",
            exit_price="98,000",
            funding_pnl="-0.25 USDT",
            fee_breakdown="0.001 BNB, 0.2 USDT",
            net_pnl="19.55 USDT",
            roi_pct="9.78",
            verification_status=VerificationStatus.VERIFIED,
            fill_verification_status=VerificationStatus.VERIFIED,
            pnl_verification_status=VerificationStatus.VERIFIED,
            roi_verification_status=VerificationStatus.UNVERIFIED,
            event_timestamp=1760958129.0,
        )
        e_multi = build_verified_embed(p_multi).data
        self.assertIn("펀딩비: -0.25 USDT", e_multi["description"])
        self.assertIn("총수수료: 0.001 BNB, 0.2 USDT", e_multi["description"])

        # Generic error without order_info (both confirmed & ambiguous)
        e_gen_err = build_error_embed("fatal config error", name="설정").data
        self.assertEqual(e_gen_err["color"], 0xF59E0B)
        e_gen_amb = build_error_embed("network timeout occurred", name="네트워크").data
        self.assertEqual(e_gen_amb["color"], 0x64748B)

        # Legacy embed with leverage and KIS account
        _, e_leg = build_legacy_order_embed(
            exchange_name="KRX",
            symbol="005930",
            side="매수",
            order_name="삼성전자_전략",
            amount_label="수량",
            amount_value="10",
            leverage=2,
            entry_price=70000,
            close_price=71000,
            account_label="1번째 계좌",
        )
        self.assertEqual(e_leg.data["color"], 0x64748B)
        self.assertIn("계좌: 1번째 계좌", e_leg.data["description"])

        # Fallback dict-based Embed bounds check
        class DictEmbed:
            def __init__(self):
                self.data = {
                    "title": "T" * 300,
                    "description": "D" * 5500,
                    "fields": [{"name": "N" * 300, "value": "V" * 1100} for _ in range(28)],
                }
        de = DictEmbed()
        enforce_embed_bounds(de)
        self.assertLessEqual(len(de.data["title"]), 256)
        self.assertLessEqual(len(de.data["fields"]), 25)

    def test_E06_github_69e48fc_security_masking_and_manual_review_dedup_preserved(self):
        # 1. Verify LogMaker.py preserves 69e48fc security masking and 3 UTC timestamps for deployment/fix_alerts.py
        lm_text = (ROOT / "exchange/utility/LogMaker.py").read_text(encoding="utf-8")
        self.assertEqual(lm_text.count("datetime.now(timezone.utc).timestamp()"), 3)
        self.assertIn("DISCORD_WEBHOOK_CONFIGURATION_INVALID", lm_text)
        self.assertIn("DISCORD_DELIVERY_FAILED", lm_text)
        self.assertIn("ORDER_PROCESSING_FAILED", lm_text)
        self.assertIn("REQUEST_PROCESSING_FAILED", lm_text)
        self.assertIn("WEBHOOK_ORDER_SUCCESS", lm_text)

        # 2. Verify MANUAL_REVIEW signal_id deterministic deduplication across repeated webhook dispatches
        info1 = OrderRequest(
            exchange="BINANCE", base="BTC", quote="USDT", side="buy",
            amount=0.01, signal_id="sig-v2-dedup-01", order_name="전략Dedup"
        )
        dispatch_verified_alert("BINANCE", {}, info1, self.settings)
        dispatch_verified_alert("BINANCE", {}, info1, self.settings)

        mock_client = Mock(market=Mock(return_value={}))
        verification_tick(self.settings, lambda *a: mock_client)
        dispatch_verified_alert("BINANCE", {}, info1, self.settings)
        verification_tick(self.settings, lambda *a: mock_client)

        store = NotificationOutboxStore(self.db_path)
        with store._connect_read() as con:
            alerts = con.execute("SELECT count(*) FROM verified_outbox WHERE event_type='manual_review'").fetchone()[0]
            self.assertEqual(alerts, 1, "Repeated signal_id without order_id must enqueue only 1 manual_review alert!")

    # =========================================================================
    # Category F — Codex Audit Remediation Counterexamples (P1-1..P1-3, P2-1..P2-5)
    # =========================================================================
    def test_F01_conservative_error_classification_and_allowlist(self):
        from exchange.utility.discord_embed import build_notification_details, classify_error_code, parse_amount_and_currency
        info = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT", side="buy", amount=1)
        for err in [None, "", "ORDER_PROCESSING_FAILED", "\n".join("ORDER_PROCESSING_FAILED"), "unknown exception", RuntimeError("lost")]:
            self.assertTrue(classify_error_ambiguity(err))
            e = build_error_embed(err, order_info=info).data
            self.assertEqual(e["color"], COLOR_ORDER_AMBIGUOUS)
            self.assertIn("주문 결과 확인 필요", e["title"])
            self.assertNotIn("주문이 체결되지 않았습니다", e["description"])

        for rej in ["insufficient balance", "min notional", "invalid order parameter", "permission denied"]:
            self.assertFalse(classify_error_ambiguity(rej))
            e = build_error_embed(rej, order_info=info).data
            self.assertEqual(e["color"], COLOR_ORDER_FAILED)
            self.assertNotIn("주문이 체결되지 않았습니다", e["description"])
            self.assertIn("상태: 주문 접수 거절", e["description"])


        # Sensitive marker in error forces safe redaction and never leaks raw text
        code, is_amb, reason = classify_error_code('{"apiKey": "SECRET_CANARY"} insufficient balance', has_order_context=True)
        self.assertTrue(is_amb)
        self.assertNotIn("SECRET_CANARY", reason)
        code2, is_amb2, reason2 = classify_error_code('Authorization: Bearer SECRET_CANARY', has_order_context=False)
        self.assertFalse(is_amb2)
        self.assertNotIn("SECRET_CANARY", reason2)

        # Parse amount and currency edge cases
        self.assertEqual(parse_amount_and_currency("0.1 BTC", "USDT"), (D("0.1"), "BTC"))
        self.assertEqual(parse_amount_and_currency("367.56원", "KRW"), (D("367.56"), "KRW"))
        self.assertEqual(parse_amount_and_currency(True, "USDT"), (None, None))
        self.assertEqual(parse_amount_and_currency("not a number", "USDT"), (None, None))

        # Details helper verification
        p = NotificationPayload(
            event_id="det-1",
            exchange="BINANCE",
            symbol="BTC/USDT",
            side="매도",
            order_name="전략",
            exit_reason=ExitReason.TP,
            pnl_result=PnlResult.PROFIT,
            fill_status=FillStatus.FILLED,
            filled_qty="1",
            remaining_qty="0.5",
            entry_price="100",
            exit_price="110",
            net_pnl="9 USDT",
            roi_pct="9.0",
            leverage=3,
            order_id="oid-1",
            signal_id="sig-1",
            error_message="missing_order_id",
            verification_status=VerificationStatus.UNVERIFIED,
            event_timestamp=1760958129.0,
        )
        details = {d["name"]: d["value"] for d in build_notification_details(p)}
        self.assertEqual(details["순실현손익"], "미확정")
        self.assertEqual(details["ROI"], "미확정")
        self.assertEqual(details["손익 결과"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()

