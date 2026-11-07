"""Windows-only independent, genuine POA fill math/proof/Discord render verification.

Runs in an isolated checkout of PR4 bf6e66e... with release_checks.isolation:
- Real ReadHost.observe(), request_scope(), validate(), make_payload(), render()
- Synthetic authenticated venue-native HTTP replies only (fixture already tested in PR4)
- No order execution, exchange API calls, operational DB, or actual Discord POST.
- Golden values are calculated without using the reader's results or its math.
"""
import copy
import json
from decimal import Decimal as D, localcontext
from pathlib import Path

import pytest
import fill_confirmation_contracts as F
from exchange.verified_pnl import order_fill_confirmation as C
from exchange.verified_pnl.order_fill_read import (
    ReadHost, ReadRejected, request_scope, validate,
)

EXPECTED_PRODUCT = "bf6e66e259c7ebbd5099e78b24089caa94936ec9"
MARKETS = [
    ("UPBIT", False), ("BYBIT", False), ("BYBIT", True),
    ("BINANCE", False), ("BINANCE", True),
    ("OKX", False), ("OKX", True),
]


def actual_poa_observation(r):
    """No substituting POA reader/validator/renderer with test doubles."""
    request = {
        "info": vars(r.info),
        "result": {"id": r.oid},
        "source_account_ref": r.scope["account"],
    }
    assert type(r.host) is ReadHost
    scope = request_scope(request)
    assert scope == r.scope
    observation = r.host.observe(request)
    proof = json.loads(observation.encoded)
    assert validate(proof), "actual POA validator rejected its own proof"
    payload = C.make_payload(proof)
    assert type(payload).__module__ == "exchange.verified_pnl.models"
    embed = C.render(payload).data
    assert isinstance(embed, dict) and "description" in embed
    return proof, embed


def displayed_decimal(description, prefix):
    matches = [
        line.split(": ", 1)[1].split()[0].removesuffix("원")
        for line in description.splitlines()
        if line.startswith(prefix + ": ")
    ]
    assert len(matches) == 1, "missing/duplicated Discord field: " + prefix
    return D(matches[0])


@pytest.mark.parametrize("exchange,future", MARKETS, ids=lambda x: str(x))
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("count", [1, 2])
def test_genuine_reader_validator_and_embed_amounts(
    tmp_path, monkeypatch, exchange, future, side, count,
):
    """28 cases: 7 markets x 2 sides x 1/2 native fills."""
    r = F.setup(
        tmp_path, monkeypatch, exchange, future, side,
        count=count, base="SOL", close=future and side == "sell",
    )
    proof, embed = actual_poa_observation(r)
    # Independent, fixed fixture oracle; never copy r.total, r.average or proof.
    with localcontext() as ctx:
        ctx.prec = 512
        base_qty = D("0.2") if exchange == "OKX" and future else D("1")
        expected_qty = base_qty * D(count)
        expected_notional = sum((D(100 + i * 10) * base_qty for i in range(count)), D(0))
        expected_avg = expected_notional / expected_qty
    assert D(proof["filled"]) == expected_qty
    assert D(proof["total"]) == expected_notional
    assert D(proof["average"]) == expected_avg
    desc = embed["description"]
    assert displayed_decimal(desc, "실제 평균 체결가") == expected_avg
    assert displayed_decimal(desc, "실제 체결수량") == expected_qty
    label = "계약 명목가치" if future else ("실제 총 매수액" if side == "buy" else "실제 총 매도액")
    assert displayed_decimal(desc, label) == expected_notional
    assert ("원" if exchange == "UPBIT" else "USDT") in desc
    assert exchange in embed["title"] and r.info.unified_symbol in embed["title"]
    if not future:
        assert ("매수" if side == "buy" else "매도") in embed["title"]
    assert r.calls, "fake transport should have observed authenticated GET"
    assert r.posts == [], "no real or synthetic Discord POST should be generated"


@pytest.mark.parametrize("exchange", ["BYBIT", "BINANCE", "OKX"])
@pytest.mark.parametrize("short,close", [
    (False, False), (False, True), (True, False), (True, True),
], ids=["long-entry", "long-exit", "short-entry", "short-exit"])
def test_genuine_futures_embed_explicit_buy_or_sell(
    tmp_path, monkeypatch, exchange, short, close,
):
    """12 cases: disclose live order side even when title states position action.

    Deliberately strict: if PR4 renders only LONG/SHORT without BUY/SELL, FAIL.
    Do not silently xfail or replace its renderer.
    """
    side = "buy" if short == close else "sell"
    r = F.setup(
        tmp_path, monkeypatch, exchange, True, side,
        count=2, base="SOL", close=close, short=short,
    )
    proof, embed = actual_poa_observation(r)
    assert proof["scope"]["side"] == side
    expected_action = ("숏" if short else "롱") + (" 청산" if close else " 진입")
    assert expected_action in embed["title"]
    assert ("매수" if side == "buy" else "매도") in embed["title"], (
        "FUTURES_ORDER_SIDE_NOT_VISIBLE: real POA render() omits explicit BUY/SELL"
    )
    assert r.posts == []


NEGATIVE = [
    ("UPBIT", False, "order-id"),
    ("UPBIT", False, "trade-amount"),
    ("BYBIT", False, "symbol"),
    ("BYBIT", True, "duplicate-fill"),
    ("BINANCE", False, "order-id"),
    ("BINANCE", True, "trade-amount"),
    ("OKX", False, "side"),
    ("OKX", True, "contract-size"),
]


@pytest.mark.parametrize("exchange,future,fault", NEGATIVE)
def test_genuine_reader_rejects_native_corruption(
    tmp_path, monkeypatch, exchange, future, fault,
):
    """8 cases: authenticated-origin data alteration must never validate."""
    r = F.setup(tmp_path, monkeypatch, exchange, future, side="sell", count=2)
    order = r.state["order"]
    trades = order["trades"] if exchange == "UPBIT" else r.state["trades"]
    if fault == "order-id":
        order["uuid" if exchange == "UPBIT" else "orderId"] = (
            "30000000-0000-4000-8000-000000000003" if exchange == "UPBIT" else 99999
        )
    elif fault == "trade-amount":
        trades[0]["funds" if exchange == "UPBIT" else "quoteQty"] = "999"
    elif fault == "symbol":
        order["symbol"] = "WRONG"
    elif fault == "duplicate-fill":
        trades.append(copy.deepcopy(trades[0]))
    elif fault == "side":
        order["side"] = "buy"
    elif fault == "contract-size":
        r.state["metadata"]["ctVal"] = "10"
    else:
        pytest.fail("test configuration invalid")
    request = {
        "info": vars(r.info),
        "result": {"id": r.oid},
        "source_account_ref": r.scope["account"],
    }
    with pytest.raises(ReadRejected):
        r.host.observe(request)
    assert r.posts == [], "no outbound Discord request expected"


def test_real_implementation_is_pinned_and_not_a_fake():
    """Product Python definitions must be sourced from the pinned checkout."""
    root = Path(__file__).resolve().parents[1]
    import subprocess
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    assert sha == EXPECTED_PRODUCT
    assert Path(request_scope.__code__.co_filename).resolve() == (
        root / "exchange/verified_pnl/order_fill_read.py"
    ).resolve()
    assert Path(validate.__code__.co_filename).resolve() == (
        root / "exchange/verified_pnl/order_fill_read.py"
    ).resolve()
    assert Path(C.render.__code__.co_filename).resolve() == (
        root / "exchange/verified_pnl/order_fill_confirmation.py"
    ).resolve()
