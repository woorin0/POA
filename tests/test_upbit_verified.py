import json
import sys
import tempfile
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness import setup
schema, legacy, util = setup(ROOT)
Settings = schema.Settings
OrderRequest = schema.OrderRequest
from exchange.verified_pnl.models import (
    FillStatus,
    VerificationStatus,
    ExitReason,
    PnlResult,
    VerifiedFill,
    VerifiedTrade,
    PnlCalculationResult,
)
from exchange.verified_pnl.verifier import FillVerifier
from exchange.verified_pnl.ledger import allocate, identity
from exchange.verified_pnl.pnl_calculator import PnlCalculator
from exchange.verified_pnl.exit_reason import classify_exit_reason, classify_pnl_result
from exchange.verified_pnl.outbox import NotificationOutboxStore, build_verified_embed
from exchange.verified_pnl.dispatcher import (
    dispatch_verified_alert,
    verification_tick,
    flush_outbox,
    make_payload,
)


class UpbitVerifiedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "upbit_alerts.db")
        self.settings = Settings(
            _env_file=None,
            PASSWORD="test-only",
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            DISCORD_OUTBOX_ENABLED=True,
            DISCORD_RETRY_ENABLED=True,
            UPBIT_VERIFIED_ALERTS=True,
            UPBIT_NET_PNL_ENABLED=True,
            UPBIT_PARTIAL_FILL_ALERTS=False,
            UPBIT_MAX_VERIFY_ATTEMPTS=5,
            UPBIT_VERIFY_BACKOFF_ENABLED=True,
            VERIFIED_COST_BASIS_COMPLETE=True,
            EXIT_REASON_CLASSIFICATION_V2=True,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
            DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/test/upbit",
        )
        self.store = NotificationOutboxStore(self.db_path)

    def test_upbit_order_parsing_done_with_trades(self):
        verifier = FillVerifier()
        order_info = OrderRequest(
            exchange="UPBIT",
            base="BTC",
            quote="KRW",
            side="buy",
            amount=0.01,
        )
        upbit_data = {
            "uuid": "upbit-order-1",
            "side": "bid",
            "ord_type": "limit",
            "price": "100000000",
            "state": "done",
            "market": "KRW-BTC",
            "created_at": "2026-03-01T12:00:00+09:00",
            "volume": "0.01",
            "remaining_volume": "0.0",
            "executed_volume": "0.01",
            "paid_fee": "500.0",
            "trades_count": 2,
            "trades": [
                {
                    "uuid": "trade-1",
                    "price": "99000000",
                    "volume": "0.005",
                    "funds": "495000",
                    "bid_fee": "247.5",
                    "created_at": "2026-03-01T12:00:01+09:00",
                },
                {
                    "uuid": "trade-2",
                    "price": "101000000",
                    "volume": "0.005",
                    "funds": "505000",
                    "bid_fee": "252.5",
                    "created_at": "2026-03-01T12:00:02+09:00",
                },
            ],
        }
        fill = verifier.verify_order_result(None, "UPBIT", "BTC/KRW", upbit_data, order_info)
        self.assertEqual(fill.status, FillStatus.FILLED)
        self.assertEqual(fill.verification_status, VerificationStatus.VERIFIED)
        self.assertEqual(fill.order_id, "upbit-order-1")
        self.assertEqual(fill.filled_quantity, D("0.01"))
        # Weighted average: (99M * 0.005 + 101M * 0.005) / 0.01 = 100,000,000
        self.assertEqual(fill.average_fill_price, D("100000000"))
        self.assertEqual(fill.market_type, "spot")
        self.assertEqual(fill.position_side, "LONG")
        self.assertEqual(fill.pnl_currency, "KRW")
        self.assertEqual(fill.fees.get("KRW"), D("500.0"))
        self.assertTrue(fill.fees_complete)
        self.assertEqual(len(fill.trades), 2)

    def test_upbit_order_parsing_no_trades_fallback_funds(self):
        verifier = FillVerifier()
        order_info = OrderRequest(
            exchange="UPBIT",
            base="ETH",
            quote="KRW",
            side="sell",
            amount=2.0,
        )
        upbit_data = {
            "uuid": "upbit-order-2",
            "side": "ask",
            "ord_type": "market",
            "state": "done",
            "market": "KRW-ETH",
            "volume": "2.0",
            "remaining_volume": "0.0",
            "executed_volume": "2.0",
            "executed_funds": "6000000",  # 3,000,000 KRW per ETH
            "paid_fee": "3000.0",
            "trades": [],
        }
        fill = verifier.verify_order_result(None, "UPBIT", "ETH/KRW", upbit_data, order_info)
        self.assertEqual(fill.status, FillStatus.FILLED)
        self.assertEqual(fill.verification_status, VerificationStatus.VERIFIED)
        self.assertEqual(fill.filled_quantity, D("2.0"))
        self.assertEqual(fill.average_fill_price, D("3000000"))
        self.assertEqual(fill.fees.get("KRW"), D("3000.0"))

    def test_upbit_order_parsing_status_states(self):
        verifier = FillVerifier()
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=1.0)
        # Cancel with partial fill
        data_partial = {
            "uuid": "ord-part",
            "state": "cancel",
            "volume": "1.0",
            "executed_volume": "0.4",
            "remaining_volume": "0.6",
            "average": "100000000",
        }
        fill = verifier.verify_order_result(None, "UPBIT", "BTC/KRW", data_partial, order_info)
        self.assertEqual(fill.status, FillStatus.PARTIALLY_FILLED)

        # Cancel with zero fill
        data_cancel = {
            "uuid": "ord-cancel",
            "state": "cancel",
            "volume": "1.0",
            "executed_volume": "0.0",
            "remaining_volume": "1.0",
        }
        fill = verifier.verify_order_result(None, "UPBIT", "BTC/KRW", data_cancel, order_info)
        self.assertEqual(fill.status, FillStatus.CANCELED)

        # Wait state (open)
        data_wait = {
            "uuid": "ord-wait",
            "state": "wait",
            "volume": "1.0",
            "executed_volume": "0.0",
            "remaining_volume": "1.0",
        }
        fill = verifier.verify_order_result(None, "UPBIT", "BTC/KRW", data_wait, order_info)
        self.assertEqual(fill.status, FillStatus.PENDING)

    def test_upbit_spot_single_buy_and_sell_realized_pnl(self):
        # 1. Buy 0.01 BTC @ 100,000,000 KRW, fee 500 KRW
        buy_info = OrderRequest(
            exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01, strategy_id="strat-1"
        )
        buy_fill = VerifiedFill(
            exchange="UPBIT",
            symbol="BTC/KRW",
            order_id="buy-1",
            market_type="spot",
            position_side="LONG",
            status=FillStatus.FILLED,
            filled_quantity=D("0.01"),
            average_fill_price=D("100000000"),
            pnl_currency="KRW",
            fees={"KRW": D("500")},
            fees_complete=True,
            verification_status=VerificationStatus.VERIFIED,
            trades=[
                VerifiedTrade(
                    id="t-b1",
                    price=D("100000000"),
                    amount=D("0.01"),
                    fee=D("500"),
                    fee_currency="KRW",
                    timestamp=1000.0,
                )
            ],
        )
        entry, entry_fee = allocate(self.store, buy_fill, buy_info, "acct-1", True)
        self.assertIsNone(entry)  # Buy is entry, does not produce cost basis for itself

        # Buy order calculator check: source is ENTRY_ORDER
        calc = PnlCalculator()
        pnl_buy = calc.calculate_pnl(buy_fill, buy_info)
        self.assertEqual(pnl_buy.source, "ENTRY_ORDER")
        self.assertIsNone(pnl_buy.net_realized_pnl)

        # 2. Sell 0.01 BTC @ 105,000,000 KRW, fee 525 KRW
        sell_info = OrderRequest(
            exchange="UPBIT",
            base="BTC",
            quote="KRW",
            side="sell",
            amount=0.01,
            exit_type="TP",
            order_name="TP_익절",
            strategy_id="strat-1",
        )
        sell_fill = VerifiedFill(
            exchange="UPBIT",
            symbol="BTC/KRW",
            order_id="sell-1",
            market_type="spot",
            position_side="LONG",
            status=FillStatus.FILLED,
            filled_quantity=D("0.01"),
            average_fill_price=D("105000000"),
            pnl_currency="KRW",
            fees={"KRW": D("525")},
            fees_complete=True,
            verification_status=VerificationStatus.VERIFIED,
            trades=[
                VerifiedTrade(
                    id="t-s1",
                    price=D("105000000"),
                    amount=D("0.01"),
                    fee=D("525"),
                    fee_currency="KRW",
                    timestamp=1050.0,
                )
            ],
        )
        avg_cost, alloc_fee = allocate(self.store, sell_fill, sell_info, "acct-1", True)
        self.assertEqual(avg_cost, D("100000000"))
        self.assertEqual(alloc_fee, D("500"))

        pnl_sell = calc.calculate_pnl(
            sell_fill,
            sell_info,
            entry_price=avg_cost,
            allocated_entry_fee=alloc_fee,
            allocated_funding=D(0),
        )
        # Gross = (105,000,000 - 100,000,000) * 0.01 = 50,000 KRW
        self.assertEqual(pnl_sell.gross_realized_pnl, D("50000"))
        self.assertEqual(pnl_sell.entry_fee_allocated, D("500"))
        self.assertEqual(pnl_sell.exit_fee, D("525"))
        # Net = 50,000 - 500 - 525 = 48,975 KRW
        self.assertEqual(pnl_sell.net_realized_pnl, D("48975"))
        self.assertEqual(pnl_sell.verification_status, VerificationStatus.VERIFIED)
        self.assertEqual(classify_pnl_result(pnl_sell), PnlResult.PROFIT)
        self.assertEqual(classify_exit_reason(sell_info), ExitReason.TP)

    def test_upbit_split_buys_and_partial_sells(self):
        calc = PnlCalculator()
        # Buy 1: 0.005 BTC @ 100,000,000 KRW, fee 250 KRW
        b1_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.005, strategy_id="s1")
        b1_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="b1", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.005"), average_fill_price=D("100000000"),
            pnl_currency="KRW", fees={"KRW": D("250")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="tb1", price=D("100000000"), amount=D("0.005"), fee=D("250"), fee_currency="KRW", timestamp=10.0)],
        )
        allocate(self.store, b1_fill, b1_info, "acct-split", True)

        # Buy 2: 0.005 BTC @ 110,000,000 KRW, fee 275 KRW
        b2_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.005, strategy_id="s1")
        b2_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="b2", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.005"), average_fill_price=D("110000000"),
            pnl_currency="KRW", fees={"KRW": D("275")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="tb2", price=D("110000000"), amount=D("0.005"), fee=D("275"), fee_currency="KRW", timestamp=20.0)],
        )
        allocate(self.store, b2_fill, b2_info, "acct-split", True)
        # Total: 0.010 BTC, total cost = 1,050,000 KRW, avg cost = 105,000,000 KRW, total buy fee = 525 KRW

        # Partial Sell 1: 0.004 BTC @ 120,000,000 KRW, fee 240 KRW
        s1_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="sell", amount=0.004, strategy_id="s1", exit_type="TP")
        s1_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="s1", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.004"), average_fill_price=D("120000000"),
            pnl_currency="KRW", fees={"KRW": D("240")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="ts1", price=D("120000000"), amount=D("0.004"), fee=D("240"), fee_currency="KRW", timestamp=30.0)],
        )
        avg1, fee1 = allocate(self.store, s1_fill, s1_info, "acct-split", True)
        self.assertEqual(avg1, D("105000000"))
        # Allocated buy fee: 525 * (0.004 / 0.010) = 210
        self.assertEqual(fee1, D("210"))
        pnl1 = calc.calculate_pnl(s1_fill, s1_info, entry_price=avg1, allocated_entry_fee=fee1, allocated_funding=D(0))
        # Gross = (120M - 105M) * 0.004 = 60,000 KRW
        self.assertEqual(pnl1.gross_realized_pnl, D("60000"))
        # Net = 60,000 - 210 - 240 = 59,550 KRW
        self.assertEqual(pnl1.net_realized_pnl, D("59550"))
        self.assertEqual(classify_pnl_result(pnl1), PnlResult.PROFIT)

        # Remaining Sell 2: 0.006 BTC @ 115,000,000 KRW, fee 345 KRW
        s2_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="sell", amount=0.006, strategy_id="s1", exit_type="TRAIL")
        s2_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="s2", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.006"), average_fill_price=D("115000000"),
            pnl_currency="KRW", fees={"KRW": D("345")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="ts2", price=D("115000000"), amount=D("0.006"), fee=D("345"), fee_currency="KRW", timestamp=40.0)],
        )
        avg2, fee2 = allocate(self.store, s2_fill, s2_info, "acct-split", True)
        self.assertEqual(avg2, D("105000000"))
        # Remaining allocated buy fee: 525 - 210 = 315
        self.assertEqual(fee2, D("315"))
        pnl2 = calc.calculate_pnl(s2_fill, s2_info, entry_price=avg2, allocated_entry_fee=fee2, allocated_funding=D(0))
        # Gross = (115M - 105M) * 0.006 = 60,000 KRW
        self.assertEqual(pnl2.gross_realized_pnl, D("60000"))
        # Net = 60,000 - 315 - 345 = 59,340 KRW
        self.assertEqual(pnl2.net_realized_pnl, D("59340"))
        self.assertEqual(classify_pnl_result(pnl2), PnlResult.PROFIT)
        self.assertEqual(classify_exit_reason(s2_info), ExitReason.TRAIL)

    def test_upbit_unconfirmed_cost_basis_when_preexisting_coin(self):
        # Account holds pre-existing coins with no prior buy recorded in bot
        sell_info = OrderRequest(
            exchange="UPBIT", base="BTC", quote="KRW", side="sell", amount=0.05, strategy_id="strat-pre"
        )
        sell_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="s-pre", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.05"), average_fill_price=D("100000000"),
            pnl_currency="KRW", fees={"KRW": D("2500")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="t-pre", price=D("100000000"), amount=D("0.05"), fee=D("2500"), fee_currency="KRW", timestamp=50.0)],
        )
        avg, fee = allocate(self.store, sell_fill, sell_info, "acct-pre", True)
        self.assertIsNone(avg)
        self.assertIsNone(fee)

        calc = PnlCalculator()
        pnl = calc.calculate_pnl(sell_fill, sell_info, entry_price=avg, allocated_entry_fee=fee, allocated_funding=D(0))
        self.assertIsNone(pnl.net_realized_pnl)
        self.assertEqual(pnl.verification_status, VerificationStatus.UNVERIFIED)
        self.assertEqual(classify_pnl_result(pnl), PnlResult.UNKNOWN)

        # Discord payload formatting check
        payload = make_payload("event-pre", sell_fill, NS(**sell_info.dict()), pnl, avg, self.settings)
        self.assertIsNone(payload.net_pnl)
        self.assertEqual(payload.exit_price, "100,000,000")
        self.assertEqual(payload.filled_qty, "0.05")
        embed = build_verified_embed(payload)
        self.assertEqual(embed.data["fields"], [])
        self.assertIn("⚪ 확정손익: 미확정", embed.data["description"])
        from exchange.utility.discord_embed import build_notification_details
        fields = {f["name"]: f["value"] for f in build_notification_details(payload)}
        self.assertEqual(fields["순실현손익"], "미확정")
        self.assertEqual(fields["실제 평균 체결가"], "100,000,000")
        self.assertEqual(fields["손익 결과"], "UNKNOWN")

    def test_upbit_fee_loss_after_price_gain_matrix(self):
        # Buy: 0.01 BTC @ 100,000,000 KRW, fee 500 KRW
        b_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01, strategy_id="matrix-1")
        b_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="bm1", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.01"), average_fill_price=D("100000000"),
            pnl_currency="KRW", fees={"KRW": D("500")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="tbm1", price=D("100000000"), amount=D("0.01"), fee=D("500"), fee_currency="KRW", timestamp=10.0)],
        )
        allocate(self.store, b_fill, b_info, "acct-m", True)

        # Sell with TP exit_type @ 100,000,500 KRW (gain 5 KRW), exit fee 500.25 KRW
        s_info = OrderRequest(
            exchange="UPBIT", base="BTC", quote="KRW", side="sell", amount=0.01, strategy_id="matrix-1", exit_type="TP", order_name="TP 익절"
        )
        s_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="sm1", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.01"), average_fill_price=D("100000500"),
            pnl_currency="KRW", fees={"KRW": D("500.25")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="tsm1", price=D("100000500"), amount=D("0.01"), fee=D("500.25"), fee_currency="KRW", timestamp=20.0)],
        )
        avg, fee = allocate(self.store, s_fill, s_info, "acct-m", True)
        pnl = PnlCalculator().calculate_pnl(s_fill, s_info, entry_price=avg, allocated_entry_fee=fee, allocated_funding=D(0))
        # Gross = 5 KRW. Net = 5 - 500 - 500.25 = -995.25 KRW
        self.assertEqual(pnl.net_realized_pnl, D("-995.25"))
        # Independent exit reason vs outcome:
        self.assertEqual(classify_exit_reason(s_info), ExitReason.TP)
        self.assertEqual(classify_pnl_result(pnl), PnlResult.LOSS)

    def test_upbit_sl_with_slippage_profit_matrix(self):
        # Buy: 0.01 BTC @ 100,000,000 KRW, fee 500 KRW
        b_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01, strategy_id="matrix-2")
        b_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="bm2", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.01"), average_fill_price=D("100000000"),
            pnl_currency="KRW", fees={"KRW": D("500")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="tbm2", price=D("100000000"), amount=D("0.01"), fee=D("500"), fee_currency="KRW", timestamp=10.0)],
        )
        allocate(self.store, b_fill, b_info, "acct-m2", True)

        # Sell with SL exit_type but market bounced to 101,000,000 KRW, fee 505 KRW
        s_info = OrderRequest(
            exchange="UPBIT", base="BTC", quote="KRW", side="sell", amount=0.01, strategy_id="matrix-2", exit_type="SL", order_name="SL 손절"
        )
        s_fill = VerifiedFill(
            exchange="UPBIT", symbol="BTC/KRW", order_id="sm2", market_type="spot", position_side="LONG",
            status=FillStatus.FILLED, filled_quantity=D("0.01"), average_fill_price=D("101000000"),
            pnl_currency="KRW", fees={"KRW": D("505")}, fees_complete=True, verification_status=VerificationStatus.VERIFIED,
            trades=[VerifiedTrade(id="tsm2", price=D("101000000"), amount=D("0.01"), fee=D("505"), fee_currency="KRW", timestamp=20.0)],
        )
        avg, fee = allocate(self.store, s_fill, s_info, "acct-m2", True)
        pnl = PnlCalculator().calculate_pnl(s_fill, s_info, entry_price=avg, allocated_entry_fee=fee, allocated_funding=D(0))
        # Gross = 10,000 KRW. Net = 10,000 - 500 - 505 = 8,995 KRW
        self.assertEqual(pnl.net_realized_pnl, D("8995"))
        self.assertEqual(classify_exit_reason(s_info), ExitReason.SL)
        self.assertEqual(classify_pnl_result(pnl), PnlResult.PROFIT)

    def test_upbit_feature_flag_off_blocks_dispatch(self):
        # When UPBIT_VERIFIED_ALERTS is False, dispatch must be a no-op
        settings_off = Settings(
            _env_file=None,
            PASSWORD="test-only",
            VERIFIED_DISCORD_ALERTS=True,
            VERIFIED_FILL_PRICE=True,
            DISCORD_OUTBOX_ENABLED=True,
            UPBIT_VERIFIED_ALERTS=False,
            VERIFIED_NOTIFICATIONS_DB_PATH=self.db_path,
        )
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        dispatch_verified_alert("UPBIT", {"uuid": "ord-off"}, order_info, settings_off)
        with self.store._connect_read() as con:
            rows = con.execute("SELECT * FROM verified_jobs").fetchall()
            self.assertEqual(len(rows), 0)

    def test_upbit_max_verify_attempts_and_backoff(self):
        # Test that an uncompleted Upbit order caps out at UPBIT_MAX_VERIFY_ATTEMPTS
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        dispatch_verified_alert("UPBIT", {"uuid": "ord-pending-upbit"}, order_info, self.settings)

        client = Mock()
        client.market.return_value = {}
        # Returns order in 'wait' (pending) status with 0 fill
        client.fetch_order.return_value = {
            "uuid": "ord-pending-upbit",
            "state": "wait",
            "volume": "0.01",
            "executed_volume": "0.0",
        }

        # Tick 5 times up to max attempts, fast-forwarding due
        for i in range(5):
            with self.store._connect() as con:
                con.execute("UPDATE verified_jobs SET due=0")
            verification_tick(self.settings, lambda *a: client)

        with self.store._connect_read() as con:
            job = con.execute("SELECT state FROM verified_jobs WHERE data LIKE '%ord-pending-upbit%'").fetchone()
            self.assertIsNotNone(job)
            # After max attempts (5), job transitions to recovery PENDING (never marked DONE prematurely)
            self.assertEqual(job[0], "PENDING")
            # Unverified event must be enqueued in outbox
            outbox_rows = con.execute("SELECT payload FROM verified_outbox").fetchall()
            self.assertEqual(len(outbox_rows), 1)
            payload = json.loads(outbox_rows[0][0])
            self.assertEqual(payload["event_type"], "unverified")

    def test_upbit_private_get_order_fallback(self):
        verifier = FillVerifier()
        order_info = OrderRequest(exchange="UPBIT", base="BTC", quote="KRW", side="buy", amount=0.01)
        bot = Mock()
        # CCXT fetch_order throws error
        bot.client.fetch_order.side_effect = RuntimeError("CCXT Upbit parse_trade crash")
        # privateGetOrder returns raw Upbit dict
        bot.client.privateGetOrder.return_value = {
            "uuid": "uuid-private-1",
            "state": "done",
            "market": "KRW-BTC",
            "volume": "0.01",
            "executed_volume": "0.01",
            "average": "95000000",
            "paid_fee": "475.0",
        }
        fill = verifier.verify_order_result(bot, "UPBIT", "BTC/KRW", {"uuid": "uuid-private-1"}, order_info)
        self.assertIsNotNone(fill)
        self.assertEqual(fill.status, FillStatus.FILLED)
        self.assertEqual(fill.average_fill_price, D("95000000"))
        self.assertEqual(fill.fees.get("KRW"), D("475.0"))

    def test_upbit_13_concurrent_strategies_simulation(self):
        # Simulate 13 independent strategies trading simultaneously on Upbit
        clients = {}
        for i in range(1, 14):
            strat_id = f"upbit-strategy-{i}"
            order_info = OrderRequest(
                exchange="UPBIT",
                base=f"COIN{i}",
                quote="KRW",
                side="buy",
                amount=float(i),
                strategy_id=strat_id,
            )
            dispatch_verified_alert("UPBIT", {"uuid": f"uuid-strat-{i}"}, order_info, self.settings)

            mock_client = Mock()
            mock_client.market.return_value = {}
            mock_client.fetch_order.return_value = {
                "uuid": f"uuid-strat-{i}",
                "state": "done",
                "volume": str(i),
                "executed_volume": str(i),
                "average": str(1000 * i),
                "paid_fee": str(5 * i),
            }
            clients[f"COIN{i}/KRW"] = mock_client

        def client_factory(exch, info):
            return clients.get(info.unified_symbol, Mock())

        # Process all 13 strategy jobs
        verification_tick(self.settings, client_factory)

        with self.store._connect_read() as con:
            jobs = con.execute("SELECT state FROM verified_jobs").fetchall()
            self.assertEqual(len(jobs), 13)
            self.assertTrue(all(j[0] == "DONE" for j in jobs))

            outbox = con.execute("SELECT event_id FROM verified_outbox").fetchall()
            self.assertEqual(len(outbox), 13)


if __name__ == "__main__":
    unittest.main()
