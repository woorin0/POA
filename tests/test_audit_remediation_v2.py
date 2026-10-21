"""
Independent remediation tests for Codex 2nd audit findings on POA Discord Embed v2.
"""
import hashlib
import json
import logging
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import importlib.util

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('audit_pr_bootstrap', ROOT / 'tests/test_discord_embed_v2.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

from exchange.verified_pnl.models import (
    ExitReason,
    FillStatus,
    NotificationPayload,
    PnlResult,
    VerificationStatus,
    VerifiedFill,
    VerifiedTrade,
)
from exchange.verified_pnl.outbox import NotificationOutboxStore, OutboxStatus
from exchange.utility import discord_embed as de


def make_payload(**kw):
    base_data = dict(
        event_id="test-remediation-evt",
        exchange="BINANCE",
        symbol="BTC/USDT",
        side="매도",
        order_name="audit_test",
        exit_reason=ExitReason.TP,
        pnl_result=PnlResult.UNKNOWN,
        fill_status=FillStatus.FILLED,
        event_timestamp=1700000000.0,
        fill_timestamp=1700000000.0,
        requested_qty="1.0",
        filled_qty="1.0",
        entry_price="100.0",
        exit_price="110.0",
        net_pnl="10.0 USDT",
        roi_pct="10.0",
        pnl_currency="USDT",
        verification_status=VerificationStatus.VERIFIED,
        fill_verification_status=VerificationStatus.VERIFIED,
        pnl_verification_status=VerificationStatus.VERIFIED,
        roi_verification_status=VerificationStatus.VERIFIED,
    )
    base_data.update(kw)
    return NotificationPayload(**base_data)


# ---------------------------------------------------------------------------
# P1-1: Independent 3-Tier Verification (Fill, PnL, ROI)
# ---------------------------------------------------------------------------

def test_missing_explicit_pnl_status_must_not_inherit_fill_verification():
    """Missing pnl_verification_status must remain unverified even if fill is verified."""
    p = make_payload(
        verification_status=VerificationStatus.VERIFIED,
        fill_verification_status=VerificationStatus.VERIFIED,
        pnl_verification_status=None,  # Missing explicit status
        roi_verification_status=VerificationStatus.VERIFIED,
    )
    fill_v, pnl_v, roi_v, _, _ = de._evaluate_verification_states(p, "USDT", fee_consistent=True)
    assert fill_v is True
    assert pnl_v is False, "Missing pnl_verification_status must NOT be treated as verified"
    assert roi_v is False, "ROI cannot be verified when PnL is not verified"

    embed = de.build_mobile_verified_embed(p).data
    desc = embed["description"]
    assert "확정손익: 미확정" in desc
    assert "수익률: 미확정" in desc
    assert "💰 확정손익:" not in desc


def test_missing_explicit_roi_status_stays_unverified_when_pnl_verified():
    """Missing roi_verification_status must not automatically be promoted to True."""
    p = make_payload(
        verification_status=VerificationStatus.VERIFIED,
        fill_verification_status=VerificationStatus.VERIFIED,
        pnl_verification_status=VerificationStatus.VERIFIED,
        roi_verification_status=None,  # Missing explicit status
    )
    fill_v, pnl_v, roi_v, _, _ = de._evaluate_verification_states(p, "USDT", fee_consistent=True)
    assert fill_v is True
    assert pnl_v is True
    assert roi_v is False, "Missing roi_verification_status must NOT default to True"

    embed = de.build_mobile_verified_embed(p).data
    desc = embed["description"]
    assert "💰 확정손익: +10 USDT" in desc
    assert "수익률: 미확정 (투입 원금/증거금 확인 필요)" in desc
    assert "(+10.00%)" not in desc


def test_both_pnl_and_roi_explicitly_verified():
    """When both statuses are explicitly VERIFIED, both are confirmed in banner."""
    p = make_payload(
        pnl_verification_status=VerificationStatus.VERIFIED,
        roi_verification_status=VerificationStatus.VERIFIED,
    )
    fill_v, pnl_v, roi_v, _, _ = de._evaluate_verification_states(p, "USDT", fee_consistent=True)
    assert fill_v is True
    assert pnl_v is True
    assert roi_v is True

    embed = de.build_mobile_verified_embed(p).data
    desc = embed["description"]
    assert "💰 확정손익: +10 USDT (+10.00%)" in desc
    assert "✅ 손익 검증 완료" in desc


# ---------------------------------------------------------------------------
# P1-2: Classification Between Order Failure vs Partial Fill
# ---------------------------------------------------------------------------

def test_failed_status_with_positive_filled_qty_preserves_fill():
    """FillStatus.FAILED with filled_qty > 0 must NOT state order did not fill."""
    p = make_payload(
        fill_status=FillStatus.FAILED,
        requested_qty="1.0",
        filled_qty="0.4",
        remaining_qty="0.6",
        error_message="INSUFFICIENT_BALANCE",
    )
    embed = de.build_mobile_verified_embed(p).data
    desc = embed["description"]

    assert "주문이 체결되지 않았습니다" not in desc
    assert "체결수량: 0.4 BTC" in desc
    assert "미체결수량: 0.6 BTC" in desc
    assert "상태: 부분 체결 완료 후 잔여 주문 실패" in desc
    assert "조치: 실제 체결 수량 확인 및 잔여 주문 확인 필요" in desc


def test_failed_status_with_zero_filled_qty_shows_unfilled_state():
    """FillStatus.FAILED with zero fill and confirmed rejection states unfilled."""
    p = make_payload(
        fill_status=FillStatus.FAILED,
        requested_qty="1.0",
        filled_qty="0",
        remaining_qty="1.0",
        error_message="INSUFFICIENT_BALANCE",
    )
    embed = de.build_mobile_verified_embed(p).data
    desc = embed["description"]

    assert "상태: 주문이 체결되지 않았습니다" in desc
    assert "체결수량:" not in desc


def test_build_error_embed_preserves_partial_fill_context():
    """build_error_embed must distinguish partial fill from zero-fill rejection."""
    order_with_partial = NS(
        exchange="BINANCE",
        base="BTC",
        quote="USDT",
        amount=1.0,
        filled_qty=0.35,
        is_futures=False,
        is_buy=True,
        verification_status=VerificationStatus.VERIFIED,
    )
    embed = de.build_error_embed("INSUFFICIENT_BALANCE", order_info=order_with_partial).data
    desc = embed["description"]

    assert "주문이 체결되지 않았습니다" not in desc
    assert "체결수량: 0.35" in desc
    assert "상태: 부분 체결 완료 후 잔여 주문 실패" in desc

    order_zero_fill = NS(
        exchange="BINANCE",
        base="BTC",
        quote="USDT",
        amount=1.0,
        filled_qty=0,
        is_futures=False,
        is_buy=True,
        verification_status=VerificationStatus.VERIFIED,
    )
    embed_zero = de.build_error_embed("INSUFFICIENT_BALANCE", order_info=order_zero_fill).data
    assert "상태: 주문이 체결되지 않았습니다" in embed_zero["description"]


# ---------------------------------------------------------------------------
# P2-1: Audit Log Injection and Sensitive Token Exposure in requeue_unknown
# ---------------------------------------------------------------------------

def test_requeue_unknown_never_logs_raw_event_id_or_newlines(tmp_path):
    """Raw event_id and any newline/token payloads must never be printed to logs."""
    store = NotificationOutboxStore(str(tmp_path / "outbox.sqlite"))
    malicious_event_id = "MALICIOUS_TOKEN_CANARY\nINJECTION\r\nLINE_SPLIT_ATTACK" + ("A" * 5000)

    # Insert an item with that event_id in UNKNOWN status
    p = make_payload(event_id=malicious_event_id)
    store.enqueue(p)
    with store._connect() as con:
        con.execute(
            "UPDATE verified_outbox SET status=? WHERE event_id=?",
            (OutboxStatus.UNKNOWN.value, malicious_event_id),
        )

    expected_hash = hashlib.sha256(malicious_event_id.encode("utf-8")).hexdigest()[:16]

    with patch("exchange.verified_pnl.outbox.logger.warning") as mock_warn:
        count = store.requeue_unknown(malicious_event_id, confirm_duplicate_risk=True)
        assert count == 1
        assert mock_warn.call_count == 1
        logged_msg = mock_warn.call_args[0][0]
        assert "MALICIOUS_TOKEN_CANARY" not in logged_msg
        assert "INJECTION" not in logged_msg
        assert "\n" not in logged_msg
        assert "\r" not in logged_msg
        assert f"event_ref={expected_hash}" in logged_msg


# ---------------------------------------------------------------------------
# P2-2: Strict Boolean Enforcement for Operator Requeue
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("invalid_risk", [
    "true", "True", "yes", "1", 1, 0, "0", "false", None, [True], {"confirm": True}
])
def test_requeue_unknown_rejects_non_boolean_confirm_duplicate_risk(tmp_path, invalid_risk):
    """confirm_duplicate_risk must strictly be boolean True."""
    store = NotificationOutboxStore(str(tmp_path / "outbox.sqlite"))
    with pytest.raises(ValueError, match="confirm_duplicate_risk=True"):
        store.requeue_unknown("evt-123", confirm_duplicate_risk=invalid_risk)


@pytest.mark.parametrize("invalid_bulk", [
    "true", "True", 1, "1", 0, "false", None, [True]
])
def test_requeue_unknown_rejects_non_boolean_allow_bulk(tmp_path, invalid_bulk):
    """allow_bulk must strictly be boolean."""
    store = NotificationOutboxStore(str(tmp_path / "outbox.sqlite"))
    with pytest.raises(ValueError, match="allow_bulk must be a boolean"):
        store.requeue_unknown(confirm_duplicate_risk=True, allow_bulk=invalid_bulk)


def test_requeue_unknown_bulk_blocked_without_explicit_allow_bulk(tmp_path):
    """Bulk requeue without allow_bulk=True must raise ValueError."""
    store = NotificationOutboxStore(str(tmp_path / "outbox.sqlite"))
    with pytest.raises(ValueError, match="Bulk requeue blocked by default"):
        store.requeue_unknown(confirm_duplicate_risk=True, allow_bulk=False)


# ---------------------------------------------------------------------------
# P2: MANUAL_REVIEW Internal ID Exposure
# ---------------------------------------------------------------------------

def test_manual_review_hides_raw_signal_id_in_visible_embed():
    """Manual review notification must never expose raw signal_id in description or fields."""
    raw_signal = "SECRET_SIG_UUID_98765_CANARY"
    p = make_payload(
        event_type="manual_review",
        signal_id=raw_signal,
        error_message="MANUAL_REVIEW: check exchange fill",
        fill_status=FillStatus.UNVERIFIED,
    )
    embed = de.build_mobile_verified_embed(p).data
    desc = embed["description"]

    assert raw_signal not in desc
    assert raw_signal not in json.dumps(embed)

    expected_ref = hashlib.sha256(raw_signal.encode("utf-8")).hexdigest()[:8]
    assert f"운영 참조: {expected_ref}" in desc

    # Raw signal_id is still preserved intact in payload object for DB operations
    assert p.signal_id == raw_signal


def test_notification_details_uses_safe_ref_for_signal_id():
    """Diagnostic details helper uses safe hash reference for signal_id."""
    raw_signal = "SECRET_OPERATOR_SIGNAL_54321"
    p = make_payload(signal_id=raw_signal)
    details = de.build_notification_details(p)

    assert not any(raw_signal in str(d) for d in details)
    expected_ref = hashlib.sha256(raw_signal.encode("utf-8")).hexdigest()[:8]
    assert any(d.get("name") == "신호 ID" and d.get("value") == expected_ref for d in details)


# ---------------------------------------------------------------------------
# P3: Test Rollback Honest Skip Semantics
# ---------------------------------------------------------------------------

def test_rollback_skips_when_manifest_missing(tmp_path):
    """When manifest.json is absent, test_rollback raises pytest.skip instead of false PASS."""
    from tests.test_rollback import test_rollback, backup
    with patch("tests.test_rollback.backup", tmp_path / "nonexistent"):
        with pytest.raises(pytest.skip.Exception, match="Rollback baseline files not present"):
            test_rollback()


# ---------------------------------------------------------------------------
# P1: Unconfirmed Order Rejection and Missing Error Classification
# ---------------------------------------------------------------------------

def test_failed_no_error_message_no_filled_qty_stays_ambiguous():
    """FAILED status with error_message=None and filled_qty=None must not claim unexecuted."""
    p = make_payload(
        fill_status=FillStatus.FAILED,
        error_message=None,
        filled_qty=None,
        requested_qty="1.0",
        verification_status=VerificationStatus.UNVERIFIED,
    )
    embed = de.build_mobile_verified_embed(p).data
    assert embed["title"].startswith("⚪")
    assert embed["color"] == de.COLOR_ORDER_AMBIGUOUS
    assert "주문 결과 확인 필요" in embed["title"]
    desc = embed["description"]
    assert "사유: 주문 처리 결과 미확정 (거래소 확인 필요)" in desc
    assert "상태: 실제 체결 여부 확인 중 (미확정)" in desc
    assert "주문이 체결되지 않았습니다" not in desc
    assert "거래소 주문 거절" not in desc


def test_failed_no_error_message_with_zero_filled_qty_stays_ambiguous():
    """FAILED status with error_message=None and filled_qty='0' must not claim unexecuted."""
    p = make_payload(
        fill_status=FillStatus.FAILED,
        error_message=None,
        filled_qty="0",
        requested_qty="1.0",
        verification_status=VerificationStatus.UNVERIFIED,
    )
    embed = de.build_mobile_verified_embed(p).data
    assert embed["title"].startswith("⚪")
    assert embed["color"] == de.COLOR_ORDER_AMBIGUOUS
    desc = embed["description"]
    assert "상태: 실제 체결 여부 확인 중 (미확정)" in desc
    assert "주문이 체결되지 않았습니다" not in desc


def test_failed_generic_error_with_zero_filled_qty_stays_ambiguous():
    """FAILED status with generic unclassified error and filled_qty='0' stays ambiguous."""
    p = make_payload(
        fill_status=FillStatus.FAILED,
        error_message="RuntimeError: disk error occurred during order submission",
        filled_qty="0",
        requested_qty="1.0",
        verification_status=VerificationStatus.UNVERIFIED,
    )
    embed = de.build_mobile_verified_embed(p).data
    assert embed["title"].startswith("⚪")
    assert embed["color"] == de.COLOR_ORDER_AMBIGUOUS
    desc = embed["description"]
    assert "상태: 실제 체결 여부 확인 중 (미확정)" in desc
    assert "주문이 체결되지 않았습니다" not in desc


def test_failed_timeout_with_partial_fill_preserves_filled_qty():
    """FAILED status with timeout and filled_qty='0.25' must preserve positive fill quantity."""
    p = make_payload(
        fill_status=FillStatus.FAILED,
        error_message="RequestTimeout: request to binance timed out after 10000ms",
        filled_qty="0.25",
        remaining_qty="0.75",
        requested_qty="1.0",
        verification_status=VerificationStatus.UNVERIFIED,
        fill_verification_status=VerificationStatus.UNVERIFIED,
    )
    embed = de.build_mobile_verified_embed(p).data
    assert embed["title"].startswith("⚪")
    assert embed["color"] == de.COLOR_ORDER_AMBIGUOUS
    desc = embed["description"]
    assert "체결수량: 0.25 BTC" in desc
    assert "미체결수량: 0.75 BTC" in desc
    assert "상태: 실제 체결 여부 확인 필요 (미확정)" in desc
    assert "일부 체결 확인됨" not in desc
    assert "주문이 체결되지 않았습니다" not in desc


def test_failed_explicit_rejection_with_verified_zero_fill():
    """FAILED status with explicit rejection AND verified zero fill confirms unexecuted order."""
    p = make_payload(
        fill_status=FillStatus.FAILED,
        error_message="INSUFFICIENT_BALANCE",
        filled_qty="0",
        requested_qty="1.0",
        verification_status=VerificationStatus.VERIFIED,
        fill_verification_status=VerificationStatus.VERIFIED,
    )
    embed = de.build_mobile_verified_embed(p).data
    assert embed["title"].startswith("⚠️")
    assert embed["color"] == de.COLOR_ORDER_FAILED
    desc = embed["description"]
    assert "사유: 주문 가능 금액/잔고 부족 (INSUFFICIENT_BALANCE)" in desc
    assert "상태: 주문이 체결되지 않았습니다" in desc


def test_failed_explicit_rejection_with_partial_fill():
    """FAILED status with explicit rejection AND partial fill preserves fill and states remaining failure."""
    p = make_payload(
        fill_status=FillStatus.FAILED,
        error_message="INSUFFICIENT_BALANCE",
        filled_qty="0.4",
        remaining_qty="0.6",
        requested_qty="1.0",
        verification_status=VerificationStatus.VERIFIED,
        fill_verification_status=VerificationStatus.VERIFIED,
    )
    embed = de.build_mobile_verified_embed(p).data
    assert embed["title"].startswith("⚠️")
    assert embed["color"] == de.COLOR_ORDER_FAILED
    desc = embed["description"]
    assert "체결수량: 0.4 BTC" in desc
    assert "미체결수량: 0.6 BTC" in desc
    assert "상태: 부분 체결 완료 후 잔여 주문 실패" in desc
    assert "조치: 실제 체결 수량 확인 및 잔여 주문 확인 필요" in desc
    assert "주문이 체결되지 않았습니다" not in desc


def test_legacy_payload_missing_rejection_evidence_stays_ambiguous():
    """Legacy payload missing explicit rejection evidence must not fabricate rejection reason."""
    p = make_payload(
        fill_status=FillStatus.FAILED,
        error_message=None,
        filled_qty=None,
        fill_verification_status=None,
        verification_status=VerificationStatus.UNVERIFIED,
    )
    embed = de.build_mobile_verified_embed(p).data
    assert embed["title"].startswith("⚪")
    assert embed["color"] == de.COLOR_ORDER_AMBIGUOUS
    desc = embed["description"]
    assert "거래소 주문 거절" not in desc
    assert "주문이 체결되지 않았습니다" not in desc
    assert "상태: 실제 체결 여부 확인 중 (미확정)" in desc


def test_embed_title_description_color_alignment():
    """Confirms alignment among title icon, color, and description status across all branches."""
    # Branch 1: Confirmed FAILED
    p_fail = make_payload(
        fill_status=FillStatus.FAILED,
        error_message="INSUFFICIENT_BALANCE",
        filled_qty="0",
        verification_status=VerificationStatus.VERIFIED,
        fill_verification_status=VerificationStatus.VERIFIED,
    )
    e_fail = de.build_mobile_verified_embed(p_fail).data
    assert e_fail["title"].startswith("⚠️")
    assert e_fail["color"] == de.COLOR_ORDER_FAILED
    assert "상태: 주문이 체결되지 않았습니다" in e_fail["description"]

    # Branch 2: Ambiguous unverified
    p_amb = make_payload(
        fill_status=FillStatus.FAILED,
        error_message=None,
        filled_qty=None,
        verification_status=VerificationStatus.UNVERIFIED,
    )
    e_amb = de.build_mobile_verified_embed(p_amb).data
    assert e_amb["title"].startswith("⚪")
    assert e_amb["color"] == de.COLOR_ORDER_AMBIGUOUS
    assert "상태: 실제 체결 여부 확인 중 (미확정)" in e_amb["description"]


def test_build_error_embed_unverified_order_info_and_none_error():
    """build_error_embed with unverified order_info or None error stays ambiguous and safe."""
    order_unverified = NS(
        exchange="BINANCE",
        base="BTC",
        quote="USDT",
        amount=1.0,
        filled_qty=0,
        verification_status=VerificationStatus.UNVERIFIED,
        is_futures=False,
        is_buy=True,
    )
    # Explicit rejection error, but order is explicitly UNVERIFIED
    e_unverified = de.build_error_embed("INSUFFICIENT_BALANCE", order_info=order_unverified).data
    assert e_unverified["title"].startswith("⚪")
    assert e_unverified["color"] == de.COLOR_ORDER_AMBIGUOUS
    assert "상태: 실제 체결 여부 확인 중 (미확정)" in e_unverified["description"]
    assert "주문이 체결되지 않았습니다" not in e_unverified["description"]

    # Missing error (None) with order_info
    e_none = de.build_error_embed(None, order_info=order_unverified).data
    assert e_none["title"].startswith("⚪")
    assert e_none["color"] == de.COLOR_ORDER_AMBIGUOUS
    assert "사유: 주문 처리 결과 미확정" in e_none["description"]
    assert "UNKNOWN_ORDER_RESULT" in e_none["description"]
    assert "주문이 체결되지 않았습니다" not in e_none["description"]

