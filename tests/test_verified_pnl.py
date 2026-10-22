import os
import shutil
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness import setup
schema, legacy, util = setup(ROOT)
MarketOrder = schema.MarketOrder
Settings = schema.Settings
from exchange.verified_pnl.models import (
    ExitReason,
    PnlResult,
    FillStatus,
    VerificationStatus,
    VerifiedFill,
    VerifiedTrade,
    NotificationPayload,
)
from exchange.verified_pnl.verifier import FillVerifier
from exchange.verified_pnl.pnl_calculator import PnlCalculator
from exchange.verified_pnl.exit_reason import classify_exit_reason, classify_pnl_result
from exchange.verified_pnl.outbox import (
    NotificationOutboxStore,
    DiscordSender,
    build_verified_embed,
    format_kst,
)
from exchange.verified_pnl.dispatcher import dispatch_verified_alert, dispatch_verified_error_alert, flush_outbox


class VerifiedPnlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_notifications.sqlite3")
        self.settings = Settings(
            PASSWORD="test-password",
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            NET_REALIZED_PNL_ENABLED=True,
            EXIT_REASON_CLASSIFICATION_V2=True,
            PARTIAL_FILL_ALERTS=True,
            DISCORD_OUTBOX_ENABLED=True,
            DISCORD_RETRY_ENABLED=True,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
            DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/test/mock",
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # 12-2. 신규 체결 검증 테스트
    # =========================================================================
    def test_full_fill_verification(self):
        verifier = FillVerifier()
        order_result = {
            "id": "ord-1",
            "status": "closed",
            "amount": 0.01,
            "filled": 0.01,
            "remaining": 0.0,
            "average": 100000.0,
            "price": 100000.0,
        }
        order_info = Mock(is_futures=True, is_buy=True, is_close=False, side="buy")
        fill = verifier.verify_order_result(None, "BINANCE", "BTC/USDT", order_result, order_info)

        self.assertEqual(fill.status, FillStatus.FILLED)
        self.assertEqual(fill.verification_status, VerificationStatus.VERIFIED)
        self.assertEqual(fill.average_fill_price, Decimal("100000"))
        self.assertEqual(fill.filled_quantity, Decimal("0.01"))

    def test_partial_fill_verification(self):
        verifier = FillVerifier()
        order_result = {
            "id": "ord-2",
            "status": "open",
            "amount": 0.01,
            "filled": 0.004,
            "remaining": 0.006,
            "average": 101000.0,
        }
        order_info = Mock(is_futures=True, is_buy=True, is_close=False, side="buy")
        fill = verifier.verify_order_result(None, "BYBIT", "BTC/USDT", order_result, order_info)

        self.assertEqual(fill.status, FillStatus.PARTIALLY_FILLED)
        self.assertEqual(fill.verification_status, VerificationStatus.VERIFIED)
        self.assertEqual(fill.average_fill_price, Decimal("101000"))
        self.assertEqual(fill.filled_quantity, Decimal("0.004"))
        self.assertEqual(fill.remaining_quantity, Decimal("0.006"))

    def test_order_accepted_pending_fill(self):
        verifier = FillVerifier()
        order_result = {
            "id": "ord-3",
            "status": "open",
            "amount": 0.01,
            "filled": 0.0,
            "remaining": 0.01,
            "average": None,
            "price": None,
        }
        order_info = Mock(is_futures=True, is_buy=True, is_close=False, side="buy")
        fill = verifier.verify_order_result(None, "BINANCE", "BTC/USDT", order_result, order_info)

        self.assertEqual(fill.status, FillStatus.PENDING)
        self.assertEqual(fill.verification_status, VerificationStatus.UNVERIFIED)
        self.assertIsNone(fill.average_fill_price)

    def test_ticker_market_price_never_substituted_as_fill_price(self):
        verifier = FillVerifier()
        # Market order where exchange result does not have price/average
        order_result = {
            "id": "ord-4",
            "status": "open",
            "amount": 0.01,
            "filled": 0.0,
            "average": None,
            "price": None,
        }
        order_info = Mock(is_futures=True, is_buy=False, is_close=True, side="sell")
        bot = Mock()
        bot.client.fetch_order.side_effect = Exception("Order not found yet")
        bot.get_price.return_value = 99999.0  # Simulated current market price

        fill = verifier.verify_order_result(bot, "BINANCE", "BTC/USDT", order_result, order_info)

        # Ensure market price 99999.0 was NEVER substituted into average_fill_price
        self.assertIsNone(fill.average_fill_price)
        self.assertEqual(fill.verification_status, VerificationStatus.UNVERIFIED)

    def test_okx_contract_size_normalization(self):
        verifier = FillVerifier()
        order_result = {
            "id": "ord-okx-1",
            "status": "closed",
            "amount": 2,  # 2 contracts
            "filled": 2,
            "average": 50000.0,
            "trades": [
                {"id": "t1", "price": 50000.0, "amount": 2, "cost": 1000.0, "commission": 0.5, "commissionAsset": "USDT"}
            ],
        }
        order_info = Mock(is_futures=True, is_buy=True, is_close=False, side="buy", contract_size=0.01)
        fill = verifier.verify_order_result(None, "OKX", "BTC/USDT:USDT", order_result, order_info)

        # 2 contracts * 0.01 contract_size = 0.02 BTC
        self.assertEqual(fill.filled_quantity, Decimal("0.02"))
        self.assertEqual(fill.average_fill_price, Decimal("50000"))

    def test_exchange_query_failure_handling(self):
        verifier = FillVerifier()
        order_result = {"id": "ord-fail", "status": "open", "filled": 0.0}
        bot = Mock()
        bot.client.fetch_order.side_effect = RuntimeError("503 Service Unavailable")
        bot.client.fetch_my_trades.side_effect = RuntimeError("Network timeout")

        order_info = Mock(is_futures=True, is_buy=True, is_close=False, side="buy")
        fill = verifier.verify_order_result(bot, "BYBIT", "BTC/USDT", order_result, order_info)

        self.assertEqual(fill.status, FillStatus.PENDING)
        self.assertEqual(fill.verification_status, VerificationStatus.UNVERIFIED)

    # =========================================================================
    # 12-3. 손익 계산 테스트
    # =========================================================================
    def test_normal_take_profit_pnl(self):
        calculator = PnlCalculator()
        fill = VerifiedFill(
            verification_status=VerificationStatus.VERIFIED,
            exchange="BINANCE",
            symbol="BTC/USDT",
            status=FillStatus.FILLED,
            filled_quantity=Decimal("0.01"),
            average_fill_price=Decimal("102000"),
            fees={"USDT": Decimal("0.51")},
            fees_complete=True,
        )
        order_info = Mock(is_close=True, is_buy=False, is_sell=True, leverage=10)
        # Entry at 100,000, Exit at 102,000. Qty 0.01
        # Gross = (102000 - 100000) * 0.01 = 20.00
        # Entry fee = 0.50, Exit fee = 0.51
        # Net = 20.00 - 0.50 - 0.51 = 18.99
        res = calculator.calculate_pnl(
            fill=fill,
            order_info=order_info,
            entry_price=Decimal("100000"),
            allocated_entry_fee=Decimal("0.50"),
            allocated_funding=Decimal("0"),  # confirmed zero in this fixture
        )
        self.assertEqual(res.gross_realized_pnl, Decimal("20.0000"))
        self.assertEqual(res.net_realized_pnl, Decimal("18.9900"))
        self.assertEqual(res.price_return_pct, Decimal("2.00"))
        self.assertIsNone(res.net_roi_pct)  # No actual allocated margin: do not infer ROI.
        self.assertFalse(res.is_roi_estimated)

    def test_fee_loss_after_price_gain(self):
        calculator = PnlCalculator()
        fill = VerifiedFill(
            verification_status=VerificationStatus.VERIFIED,
            exchange="BINANCE",
            symbol="BTC/USDT",
            status=FillStatus.FILLED,
            filled_quantity=Decimal("0.01"),
            average_fill_price=Decimal("100050"),
            fees={"USDT": Decimal("0.80")},
            fees_complete=True,
        )
        order_info = Mock(is_close=True, is_buy=False, is_sell=True, leverage=5)
        # Entry at 100,000, Exit at 100,050. Qty 0.01
        # Gross = (100050 - 100000) * 0.01 = +0.50 USDT
        # Fees = Entry(0.50) + Exit(0.80) = 1.30 USDT
        # Net = 0.50 - 1.30 = -0.80 USDT (Fee-induced Net Loss!)
        res = calculator.calculate_pnl(
            fill=fill,
            order_info=order_info,
            entry_price=Decimal("100000"),
            allocated_entry_fee=Decimal("0.50"),
            allocated_funding=Decimal("0"),
        )
        self.assertEqual(res.gross_realized_pnl, Decimal("0.5000"))
        self.assertEqual(res.net_realized_pnl, Decimal("-0.8000"))
        pnl_result = classify_pnl_result(res)
        # Even though price rose, actual net result is LOSS!
        self.assertEqual(pnl_result, PnlResult.LOSS)

    def test_funding_fee_adjustment(self):
        calculator = PnlCalculator()
        fill = VerifiedFill(
            verification_status=VerificationStatus.VERIFIED,
            exchange="BYBIT",
            symbol="BTC/USDT",
            status=FillStatus.FILLED,
            filled_quantity=Decimal("0.01"),
            average_fill_price=Decimal("100000"),
            fees={"USDT": Decimal("0.10")},
            fees_complete=True,
        )
        order_info = Mock(is_close=True, is_buy=True, is_sell=False)
        # Entry at 100,000, Exit at 100,000 (Gross = 0). Funding received = +1.50 USDT
        # Net = 0 - 0.10 + 1.50 = +1.40 USDT
        res = calculator.calculate_pnl(
            fill=fill,
            order_info=order_info,
            entry_price=Decimal("100000"),
            allocated_funding=Decimal("1.50"),
            allocated_entry_fee=Decimal("0"),
        )
        self.assertEqual(res.gross_realized_pnl, Decimal("0.0000"))
        self.assertEqual(res.net_realized_pnl, Decimal("1.4000"))
        self.assertEqual(classify_pnl_result(res), PnlResult.PROFIT)

    def test_partial_exit_allocated_entry_fee(self):
        calculator = PnlCalculator()
        # 1st partial close: 0.004 BTC out of 0.010 BTC
        total_entry_fee = Decimal("1.00")
        total_entry_qty = Decimal("0.010")
        close_qty = Decimal("0.004")
        allocated_entry_fee = total_entry_fee * (close_qty / total_entry_qty)  # 0.40 USDT

        fill = VerifiedFill(
            verification_status=VerificationStatus.VERIFIED,
            exchange="OKX",
            symbol="BTC/USDT",
            status=FillStatus.FILLED,
            filled_quantity=close_qty,
            average_fill_price=Decimal("102000"),
            fees={"USDT": Decimal("0.20")},
            fees_complete=True,
        )
        order_info = Mock(is_close=True, is_buy=False, is_sell=True)
        # Gross = (102000 - 100000) * 0.004 = 8.00 USDT
        # Net = 8.00 - 0.40 - 0.20 = 7.40 USDT
        res = calculator.calculate_pnl(
            fill=fill,
            order_info=order_info,
            entry_price=Decimal("100000"),
            allocated_entry_fee=allocated_entry_fee,
            allocated_funding=Decimal("0"),
        )
        self.assertEqual(res.entry_fee_allocated, Decimal("0.4000"))
        self.assertEqual(res.net_realized_pnl, Decimal("7.4000"))

    def test_actual_margin_true_roi(self):
        calculator = PnlCalculator()
        fill = VerifiedFill(
            verification_status=VerificationStatus.VERIFIED,
            exchange="BINANCE",
            symbol="BTC/USDT",
            status=FillStatus.FILLED,
            filled_quantity=Decimal("0.01"),
            average_fill_price=Decimal("105000"),
            fees={"USDT": Decimal("1.00")},
            fees_complete=True,
        )
        order_info = Mock(is_close=True, is_buy=False, is_sell=True)
        # Gross = 50.00, Net = 49.00
        # Allocated margin = 100.00 USDT
        # True ROI = 49.00 / 100.00 * 100 = 49.00%
        res = calculator.calculate_pnl(
            fill=fill,
            order_info=order_info,
            entry_price=Decimal("100000"),
            allocated_margin=Decimal("100.00"),
            allocated_entry_fee=Decimal("0"),
            allocated_funding=Decimal("0"),
        )
        self.assertEqual(res.net_roi_pct, Decimal("49.00"))
        self.assertFalse(res.is_roi_estimated)

    # =========================================================================
    # 12-4. 청산 이유 및 실제 손익 결과 분리 판별 테스트
    # =========================================================================
    def test_exit_reason_and_pnl_matrix(self):
        # 1. TP signal + profit -> TP / PROFIT
        pnl_profit = Mock(net_realized_pnl=Decimal("10.0"), gross_realized_pnl=Decimal("10.0"), price_return_pct=Decimal("1.0"))
        order_tp = Mock(is_close=True, exit_type="TP", order_name="주문")
        self.assertEqual(classify_exit_reason(order_tp), ExitReason.TP)
        self.assertEqual(classify_pnl_result(pnl_profit), PnlResult.PROFIT)

        # 2. TP signal + net loss -> TP / LOSS
        pnl_loss = Mock(net_realized_pnl=Decimal("-2.0"), gross_realized_pnl=Decimal("1.0"), price_return_pct=Decimal("0.1"))
        self.assertEqual(classify_pnl_result(pnl_loss), PnlResult.LOSS)

        # 3. SL signal + net loss -> SL / LOSS
        order_sl = Mock(is_close=True, exit_type="SL", order_name="주문")
        self.assertEqual(classify_exit_reason(order_sl), ExitReason.SL)
        self.assertEqual(classify_pnl_result(pnl_loss), PnlResult.LOSS)

        # 4. SL signal + net profit -> SL / PROFIT
        self.assertEqual(classify_exit_reason(order_sl), ExitReason.SL)
        self.assertEqual(classify_pnl_result(pnl_profit), PnlResult.PROFIT)

        # 5. TRAIL signal + profit -> TRAIL / PROFIT
        order_trail = Mock(is_close=True, exit_type="TRAIL", order_name="주문")
        self.assertEqual(classify_exit_reason(order_trail), ExitReason.TRAIL)
        self.assertEqual(classify_pnl_result(pnl_profit), PnlResult.PROFIT)

        # 6. BE signal + loss -> BE / LOSS
        order_be = Mock(is_close=True, exit_type="BE", order_name="주문")
        self.assertEqual(classify_exit_reason(order_be), ExitReason.BE)
        self.assertEqual(classify_pnl_result(pnl_loss), PnlResult.LOSS)

        # 7. TIME signal -> TIME / PROFIT
        order_time = Mock(is_close=True, exit_type="TIME", order_name="주문")
        self.assertEqual(classify_exit_reason(order_time), ExitReason.TIME)

        # 8. Missing exit_type, parsed from order_name
        order_fallback = Mock(is_close=True, exit_type=None, order_name="BTC_Strategy_TP_3")
        self.assertEqual(classify_exit_reason(order_fallback), ExitReason.TP)

        # 9. Completely unknown signal
        order_unknown = Mock(is_close=True, exit_type=None, order_name="CustomOrder")
        self.assertEqual(classify_exit_reason(order_unknown), ExitReason.UNKNOWN)

    # =========================================================================
    # 12-5. Discord Outbox & 재전송 복구 테스트
    # =========================================================================
    def test_outbox_duplicate_prevention(self):
        store = NotificationOutboxStore(self.db_path)
        payload = NotificationPayload(
            event_id="BINANCE_BTC-USDT_ORD-1_TP_v1",
            exchange="BINANCE",
            symbol="BTC/USDT",
            side="롱 종료",
            order_name="TP 청산",
            exit_reason=ExitReason.TP,
            pnl_result=PnlResult.PROFIT,
            fill_status=FillStatus.FILLED,
            event_timestamp=time.time(),
        )
        first_insert = store.enqueue(payload)
        self.assertTrue(first_insert)

        # Second insert with identical event_id must be ignored
        second_insert = store.enqueue(payload)
        self.assertFalse(second_insert)

    def test_discord_rate_limit_429_backoff(self):
        store = NotificationOutboxStore(self.db_path)
        sender = DiscordSender(self.settings.DISCORD_WEBHOOK_URL)
        payload = NotificationPayload(
            event_id="BYBIT_ETH-USDT_ORD-2_SL_v1",
            exchange="BYBIT",
            symbol="ETH/USDT",
            side="숏 종료",
            order_name="SL 청산",
            exit_reason=ExitReason.SL,
            pnl_result=PnlResult.LOSS,
            fill_status=FillStatus.FILLED,
            event_timestamp=time.time(),
        )
        store.enqueue(payload)

        # Mock 429 response with Retry-After: 3.5
        mock_resp = Mock(status_code=429)
        mock_resp.json.return_value = {"retry_after": 3.5}
        with patch("requests.post", return_value=mock_resp):
            success, err, delay = sender.send_payload(payload)
            self.assertFalse(success)
            self.assertEqual(delay, 3.5)
            store.mark_failed(payload.event_id, err, delay=delay)

        # Immediately query pending; should not be ready until delay passes
        pending = store.get_pending()
        self.assertEqual(len(pending), 0)

    def test_permanent_404_webhook_error_no_infinite_retry(self):
        store = NotificationOutboxStore(self.db_path)
        sender = DiscordSender(self.settings.DISCORD_WEBHOOK_URL)
        payload = NotificationPayload(
            event_id="OKX_SOL-USDT_ORD-3_TRAIL_v1",
            exchange="OKX",
            symbol="SOL/USDT",
            side="롱 종료",
            order_name="트레일링 청산",
            exit_reason=ExitReason.TRAIL,
            pnl_result=PnlResult.PROFIT,
            fill_status=FillStatus.FILLED,
            event_timestamp=time.time(),
        )
        store.enqueue(payload)

        # Mock 404 Not Found (deleted webhook)
        mock_resp = Mock(status_code=404, text="Unknown Webhook")
        with patch("requests.post", return_value=mock_resp):
            success, err, delay = sender.send_payload(payload)
            self.assertFalse(success)
            self.assertIsNone(delay)  # Permanent error, no delay retry
            store.mark_failed(payload.event_id, err, terminal=True)

        with store._connect() as con:
            row = con.execute("SELECT status FROM verified_outbox WHERE event_id = ?", (payload.event_id,)).fetchone()
            self.assertEqual(row[0], "FAILED")

    def test_discord_outage_does_not_break_order_dispatch(self):
        # Even if Discord throws connection exception, dispatch must not crash
        with patch("requests.post", side_effect=ConnectionError("Discord DNS failed")):
            dispatch_verified_alert(
                exchange_name="BINANCE",
                order_result={"id": "ord-outage", "status": "closed", "filled": 0.01, "average": 100000.0},
                order_info=Mock(unified_symbol="BTC/USDT", base="BTC", quote="USDT", is_close=True, is_sell=True, is_buy=False, is_futures=True, exit_type="TP"),
                settings=self.settings,
                bot=None,
            )

        store = NotificationOutboxStore(self.db_path)
        # Stored in outbox for background retry
        with store._connect() as con:
            count = con.execute("SELECT count(*) FROM verified_jobs WHERE state='PENDING'").fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(con.execute("SELECT count(*) FROM verified_outbox").fetchone()[0], 0)

    def test_embed_color_mapping(self):
        # Buy / Entry FILLED -> Green (0x16A34A)
        p0 = NotificationPayload(
            event_id="e0", exchange="UPBIT", symbol="SOL/KRW", side="매수", order_name="주문",
            exit_reason=ExitReason.UNKNOWN, pnl_result=PnlResult.UNKNOWN, fill_status=FillStatus.FILLED,
            filled_qty="1.0", fill_verification_status=VerificationStatus.VERIFIED,
            verification_source="ORDER_RESULT", event_timestamp=time.time()
        )
        self.assertEqual(build_verified_embed(p0).data["color"], 0x16A34A)

        # Sell / Close FILLED (PROFIT) -> Red (0xEF4444)
        p1 = NotificationPayload(
            event_id="e1", exchange="BINANCE", symbol="BTC/USDT", side="롱 종료", order_name="주문",
            exit_reason=ExitReason.TP, pnl_result=PnlResult.PROFIT, fill_status=FillStatus.FILLED,
            filled_qty="1.0", fill_verification_status=VerificationStatus.VERIFIED,
            verification_source="ORDER_RESULT", event_timestamp=time.time()
        )
        self.assertEqual(build_verified_embed(p1).data["color"], 0xEF4444)

        # Sell / Close FILLED (LOSS) -> Red (0xEF4444)
        p2 = NotificationPayload(
            event_id="e2", exchange="BINANCE", symbol="BTC/USDT", side="롱 종료", order_name="주문",
            exit_reason=ExitReason.SL, pnl_result=PnlResult.LOSS, fill_status=FillStatus.FILLED,
            filled_qty="1.0", fill_verification_status=VerificationStatus.VERIFIED,
            verification_source="ORDER_RESULT", event_timestamp=time.time()
        )
        self.assertEqual(build_verified_embed(p2).data["color"], 0xEF4444)

        # Sell / Close FILLED (BREAKEVEN) -> Red (0xEF4444)
        p3 = NotificationPayload(
            event_id="e3", exchange="BINANCE", symbol="BTC/USDT", side="롱 종료", order_name="주문",
            exit_reason=ExitReason.BE, pnl_result=PnlResult.BREAKEVEN, fill_status=FillStatus.FILLED,
            filled_qty="1.0", fill_verification_status=VerificationStatus.VERIFIED,
            verification_source="ORDER_RESULT", event_timestamp=time.time()
        )
        self.assertEqual(build_verified_embed(p3).data["color"], 0xEF4444)

        # PENDING/UNKNOWN -> Gray (0x64748B)
        p4 = NotificationPayload(
            event_id="e4", exchange="BINANCE", symbol="BTC/USDT", side="롱 종료", order_name="주문",
            exit_reason=ExitReason.SIGNAL, pnl_result=PnlResult.UNKNOWN, fill_status=FillStatus.PENDING,
            event_timestamp=time.time()
        )
        self.assertEqual(build_verified_embed(p4).data["color"], 0x64748B)

        # FAILED -> Orange (0xF59E0B) (confirmed rejection with verified zero fill)
        p5 = NotificationPayload(
            event_id="e5", exchange="BINANCE", symbol="BTC/USDT", side="매수", order_name="주문",
            exit_reason=ExitReason.UNKNOWN, pnl_result=PnlResult.UNKNOWN, fill_status=FillStatus.FAILED,
            error_message="INSUFFICIENT_BALANCE", filled_qty="0",
            verification_status=VerificationStatus.VERIFIED,
            fill_verification_status=VerificationStatus.VERIFIED,
            event_timestamp=time.time()
        )
        self.assertEqual(build_verified_embed(p5).data["color"], 0xF59E0B)

    def test_weighted_average_fill_precision(self):
        verifier = FillVerifier()
        order_result = {
            "id": "ord-weighted",
            "status": "closed",
            "trades": [
                {"id": "t1", "price": 100.0, "amount": 1.0},
                {"id": "t2", "price": 103.0, "amount": 2.0},
            ],
        }
        order_info = Mock(is_futures=True, is_buy=True, is_close=False, side="buy")
        fill = verifier.verify_order_result(None, "BINANCE", "BTC/USDT", order_result, order_info)
        # (100*1 + 103*2) / 3 = 306 / 3 = 102.0
        self.assertEqual(fill.average_fill_price, Decimal("102.0"))
        self.assertEqual(fill.filled_quantity, Decimal("3.0"))

    def test_missing_pnl_data_unverified(self):
        calculator = PnlCalculator()
        fill = VerifiedFill(
            verification_status=VerificationStatus.VERIFIED,
            exchange="BINANCE",
            symbol="BTC/USDT",
            status=FillStatus.FILLED,
            filled_quantity=Decimal("0.01"),
            average_fill_price=Decimal("100000"),
        )
        # Close order without entry_price
        order_info = Mock(is_close=True, is_buy=False, is_sell=True)
        res = calculator.calculate_pnl(fill=fill, order_info=order_info, entry_price=None)
        self.assertEqual(res.verification_status, VerificationStatus.UNVERIFIED)
        self.assertIsNone(res.gross_realized_pnl)
        self.assertIsNone(res.net_realized_pnl)
        self.assertEqual(classify_pnl_result(res), PnlResult.UNKNOWN)

    def test_no_leverage_no_roi(self):
        calculator = PnlCalculator()
        fill = VerifiedFill(
            verification_status=VerificationStatus.VERIFIED,
            exchange="BINANCE",
            symbol="BTC/USDT",
            status=FillStatus.FILLED,
            filled_quantity=Decimal("0.01"),
            average_fill_price=Decimal("102000"),
        )
        # leverage is None and pos_leverage is None, no margin
        order_info = Mock(is_close=True, is_buy=False, is_sell=True, leverage=None)
        res = calculator.calculate_pnl(fill=fill, order_info=order_info, entry_price=Decimal("100000"), pos_leverage=None)
        self.assertIsNone(res.net_roi_pct)
        self.assertFalse(res.is_roi_estimated)

    def test_outbox_flush_worker(self):
        store = NotificationOutboxStore(self.db_path)
        payload = NotificationPayload(
            event_id="FLUSH_TEST_1",
            exchange="BINANCE",
            symbol="BTC/USDT",
            side="롱 종료",
            order_name="테스트",
            exit_reason=ExitReason.TP,
            pnl_result=PnlResult.PROFIT,
            fill_status=FillStatus.FILLED,
            event_timestamp=time.time(),
        )
        store.enqueue(payload)

        # Mock successful dispatch during flush
        mock_resp = Mock(status_code=204)
        with patch("requests.post", return_value=mock_resp):
            flush_outbox(self.settings)

        with store._connect() as con:
            row = con.execute("SELECT status FROM verified_outbox WHERE event_id = ?", ("FLUSH_TEST_1",)).fetchone()
            self.assertEqual(row[0], "SENT")

    def test_outbox_max_retry_terminal_failure(self):
        store = NotificationOutboxStore(self.db_path)
        payload = NotificationPayload(
            event_id="MAX_RETRY_TEST",
            exchange="BINANCE",
            symbol="BTC/USDT",
            side="롱 종료",
            order_name="테스트",
            exit_reason=ExitReason.TP,
            pnl_result=PnlResult.PROFIT,
            fill_status=FillStatus.FILLED,
            event_timestamp=time.time(),
        )
        store.enqueue(payload, max_attempts=2)

        # Fail twice
        store.mark_failed("MAX_RETRY_TEST", "Error 1", delay=0)
        store.mark_failed("MAX_RETRY_TEST", "Error 2", delay=0)

        with store._connect() as con:
            row = con.execute("SELECT status, attempts FROM verified_outbox WHERE event_id = ?", ("MAX_RETRY_TEST",)).fetchone()
            self.assertEqual(row[0], "FAILED")
            self.assertEqual(row[1], 2)

    def test_feature_flag_off_preserves_legacy_log(self):
        from main import log
        legacy_mock = Mock()
        print_mock = Mock()
        dispatch_mock = Mock()

        # Turn off VERIFIED_DISCORD_ALERTS
        off_settings = Settings(
            PASSWORD="test-password",
            VERIFIED_DISCORD_ALERTS=False,
        )

        with patch("main.settings", off_settings), \
             patch("main.log_order_message", legacy_mock), \
             patch("main.print_alert_message", print_mock), \
             patch("exchange.verified_pnl.dispatcher.dispatch_verified_alert", dispatch_mock):
            order_info = Mock(exchange="BINANCE")
            log("BINANCE", {"id": "legacy_res"}, order_info)

            # Legacy functions must be called
            legacy_mock.assert_called_once_with("BINANCE", {"id": "legacy_res"}, order_info)
            print_mock.assert_called_once_with(order_info)
            # Verified dispatcher must NOT be called
            dispatch_mock.assert_not_called()

    def test_time_formatting_kst(self):
        # 2026-10-10 12:00:00 UTC = 2026-10-10 21:00:00 KST
        import datetime
        dt_utc = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
        ts = dt_utc.timestamp()
        formatted = format_kst(ts)
        self.assertEqual(formatted, "26-10-10 21:00:00")


if __name__ == "__main__":
    unittest.main()
