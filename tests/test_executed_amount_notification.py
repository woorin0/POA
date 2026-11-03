"""Regression: confirmed totals come only from verified exchange execution fills."""
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from harness import setup
setup(ROOT)

from exchange.verified_pnl.alert_presentation import (
    AMOUNT_ONLY_MARKER, _verified_executed_amount, apply_amount_only,
)
from exchange.utility.discord_embed import Embed


def payload(*, side="매수", trades=None, filled="3", average="150",
            complete=True, market="spot", valid=True):
    if trades is None:
        trades = [
            {"id":"a","price":"100","amount":"1"},
            {"id":"b","price":"175","amount":"2"},
        ]
    fill = dict(
        trades_complete=complete, trades=trades, market_type=market,
        filled_quantity=filled, average_fill_price=average, pnl_currency="USDT",
    )
    scope = dict(settlement="USDT", market_type=market)
    return SimpleNamespace(
        side=side, verification_source=AMOUNT_ONLY_MARKER,
        execution_receipt={"body":{"fill":fill,"scope":scope}},
        _test_valid=valid,
    )


def embed(description="전략: test\n체결수량: 3 BTC\n확정손익: +99 USDT\n수익률: +1%\n매도금액: 999 USDT"):
    return Embed(title="test", description=description, color=0x64748B)


class AmountNotificationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "exchange.verified_pnl.evidence.payload_fill_valid",
            side_effect=lambda p: p._test_valid,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_buy_signed_multi_fill_total(self):
        p = payload()
        out = apply_amount_only(embed(),p).data["description"]
        self.assertIn("실제 총 매수액: 450",out)
        self.assertIn("USDT",out)
        self.assertNotIn("확정손익",out)
        self.assertNotIn("수익률",out)
        self.assertNotIn("매도금액: 999",out)

    def test_sell_futures_is_nominal_not_cash_flow(self):
        p=payload(side="롱 종료",market="linear")
        out=apply_amount_only(embed(),p).data["description"]
        self.assertIn("실제 총 매도액 (계약 명목가치)",out)

    def test_no_proof_no_actual_amount(self):
        p=payload(valid=False)
        out=apply_amount_only(embed(),p).data["description"]
        self.assertIn("확인 중",out)
        self.assertNotIn("실제 총 매수액: 450",out)

    def test_partial_incomplete_trades_rejected(self):
        p=payload(complete=False)
        self.assertIsNone(_verified_executed_amount(p))
        self.assertIn("확인 중",apply_amount_only(embed(),p).data["description"])

    def test_conflicting_quantity_or_vwap_rejected(self):
        self.assertIsNone(_verified_executed_amount(payload(filled="4")))
        self.assertIsNone(_verified_executed_amount(payload(average="140")))

    def test_duplicate_trade_id_rejected(self):
        p=payload(trades=[{"id":"a","price":"100","amount":"1"},{"id":"a","price":"175","amount":"2"}])
        self.assertIsNone(_verified_executed_amount(p))

    def test_non_finite_and_nonpositive_rejected(self):
        for bad in ("NaN","Infinity","0","-1"):
            with self.subTest(value=bad):
                p=payload(trades=[{"id":"a","price":bad,"amount":"3"}])
                self.assertIsNone(_verified_executed_amount(p))

    def test_old_notification_untouched_without_marker(self):
        p=payload()
        p.verification_source="old version"
        original=embed()
        self.assertIs(apply_amount_only(original,p),original)
        self.assertEqual(original.data["description"],"전략: test\n체결수량: 3 BTC\n확정손익: +99 USDT\n수익률: +1%\n매도금액: 999 USDT")

    def test_unknown_sell_amount_never_uses_request_or_pnl(self):
        p=payload(side="매도",valid=False)
        out=apply_amount_only(embed("전략: a\n요청수량: 10 BTC\n예상 손익: +10\n예상 ROI: +10%"),p).data["description"]
        self.assertIn("실제 총 매도액: 확인 중",out)
        self.assertIn("요청수량: 10 BTC",out)
        self.assertNotIn("예상 손익",out)
        self.assertNotIn("예상 ROI",out)


if __name__ == "__main__":
    unittest.main()
