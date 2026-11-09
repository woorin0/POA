"""Four-exchange simple amount alerts: local-only; no exchange or Discord I/O."""
from types import SimpleNamespace
import ast
from pathlib import Path

import pytest
from exchange.utility.simple_order_amount import order_amount, build_simple_amount_alert

ROOT = Path(__file__).resolve().parents[1]
VENUES = ("UPBIT", "BINANCE", "BYBIT", "BITGET")


def info(exchange, side, future=False, **extra):
    return SimpleNamespace(exchange=exchange, side=side, is_buy=side=="buy",
        is_sell=side=="sell", is_futures=future, is_entry=future,
        is_close=False, is_contract=False, contract_size=None,
        base="XRP", quote="KRW" if exchange=="UPBIT" else "USDT",
        unified_symbol="XRP/KRW" if exchange=="UPBIT" else "XRP/USDT"+(":USDT" if future else ""),
        strategy_id="LL", order_name="LL", amount=2, price=100, **extra)


@pytest.mark.parametrize("exchange", VENUES)
@pytest.mark.parametrize("side", ("buy", "sell"))
@pytest.mark.parametrize("future", (False, True))
def test_simple_amount_both_sides_and_markets(exchange, side, future):
    if exchange=="UPBIT" and future:
        return  # Upbit has no futures market.
    request=info(exchange, side, future)
    total,source=order_amount(exchange, {"id":"test-order","cost":"204","filled":"2","average":"102"}, request)
    assert (str(total),source)==("204","response")
    content,embed=build_simple_amount_alert(exchange, {"id":"test-order","cost":"204"}, request)
    body=embed.data["description"]
    assert "204" in body and "(주문 응답·미검증)" in body
    assert "실체결 검증 안 함" in body
    assert "수수료·실현손익: 미확정" in body
    assert "test-order" in body
    assert "체결 확인 완료" not in body


@pytest.mark.parametrize("exchange", VENUES)
@pytest.mark.parametrize("side", ("buy", "sell"))
def test_original_order_amount_price_fallback_is_labeled_estimate(exchange,side):
    req=info(exchange,side)
    value,origin=order_amount(exchange,{"id":"ack"},req)
    assert (str(value),origin)==("200","estimate")
    _,embed=build_simple_amount_alert(exchange,{"id":"ack"},req)
    assert "200" in embed.data["description"]
    assert "수량×가격 추정·미검증" in embed.data["description"]


@pytest.mark.parametrize("exchange,field", [
    ("BINANCE","cumQuote"),("BINANCE","cummulativeQuoteQty"),
    ("BYBIT","cumExecValue"),("BITGET","quoteVolume"),
])
def test_native_quote_fields_used_as_response_only(exchange,field):
    val,kind=order_amount(exchange,{"info":{field:"345.12"}},info(exchange,"buy"))
    assert (str(val),kind)==("345.12","response")


@pytest.mark.parametrize("bad",["NaN","Infinity","-1","0","nope",True,None])
def test_invalid_cost_cannot_be_displayed_as_amount(bad):
    request=info("UPBIT","buy")
    value,kind=order_amount("UPBIT",{"cost":bad},request)
    assert (str(value),kind)==("200","estimate")


def test_unknown_price_is_unavailable_not_fabricated():
    request=info("UPBIT","sell")
    request.price=None
    val,kind=order_amount("UPBIT",{"id":"ack","amount":2,"price":None},request)
    assert val is None and kind=="unavailable"
    _,embed=build_simple_amount_alert("UPBIT",{"id":"ack"},request)
    assert "확인 불가" in embed.data["description"]


def test_no_private_order_query_and_no_order_replay():
    code=(ROOT/"exchange/utility/simple_order_amount.py").read_text()
    assert "fetch_order(" not in code and "create_order(" not in code
    assert "privateGet" not in code
    main=ast.parse((ROOT/"main.py").read_text())
    funcs={n.name:n for n in main.body if isinstance(n,ast.FunctionDef)}
    log=ast.get_source_segment((ROOT/"main.py").read_text(),funcs["log"])
    schedule=ast.get_source_segment((ROOT/"main.py").read_text(),funcs["schedule_order_log"])
    assert '("UPBIT", "BINANCE", "BYBIT", "BITGET")' in log
    assert '("UPBIT", "BINANCE", "BYBIT", "BITGET")' in schedule
    assert "background_tasks.add_task(log" in schedule
