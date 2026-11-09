"""Five-venue immediate order-response Discord notifications (no real I/O)."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import ast
import importlib.util
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
from harness import setup
setup(ROOT)
from exchange.model import OrderRequest
from exchange.utility.simple_order_amount import (
    SUPPORTED, order_amount, order_price, build_simple_amount_alert,
    public_reference_price,
)

VENUES = ("UPBIT", "BINANCE", "BYBIT", "BITGET", "OKX")
DERIVATIVES = ("BINANCE", "BYBIT", "BITGET", "OKX")
ACTIONS = ("entry/buy", "entry/sell", "close/sell", "close/buy")


def request(exchange, side, future=False, **extra):
    return OrderRequest(exchange=exchange, base="XRP",
                        quote=("KRW" if exchange=="UPBIT" else "USDT.P" if future else "USDT"),
                        side=side, amount=2, price=100, strategy_id="STRATEGY-TEST", **extra)


@pytest.mark.parametrize("venue",VENUES)
@pytest.mark.parametrize("side",("buy","sell"))
def test_spot_five_exchanges_all_values_and_no_financial_fields(venue,side):
    info=request(venue,side)
    _,embed=build_simple_amount_alert(venue,{"id":"ord-1","cost":"204","filled":"2","average":"102"},info)
    body=embed.data["description"]
    assert "수량: 2 XRP (주문 응답·미검증)" in body
    assert ("가격: 102 KRW" if venue=="UPBIT" else "가격: 102 USDT") in body
    assert (("매수액: " if side=="buy" else "매도액: ") +
             ("204 KRW" if venue=="UPBIT" else "204 USDT") +
             " (주문 응답·미검증)") in body
    assert "수수료" not in body and "실현손익" not in body
    assert "손익" not in body and "수익률" not in body
    assert "실체결 미검증" in body and "ord-1" in body
    assert "매수액" not in body if side=="sell" else "매도액" not in body


@pytest.mark.parametrize("venue",DERIVATIVES)
@pytest.mark.parametrize("side,title,action",[
    ("entry/buy","롱 진입","매수액"),("entry/sell","숏 진입","매도액"),
    ("close/sell","롱 청산","매도액"),("close/buy","숏 청산","매수액"),
])
def test_linear_futures_five_columns_and_correct_action(venue,side,title,action):
    info=request(venue,side,True)
    assert info.unified_symbol=="XRP/USDT:USDT" and info.is_futures
    _,embed=build_simple_amount_alert(venue,{"id":"f-1","cost":"200","filled":"2","average":"100"},info)
    assert title in embed.data["title"]
    body=embed.data["description"]
    assert "수량: 2 XRP" in body
    assert "가격: 100 USDT" in body
    assert action+" (계약 명목가치): 200 USDT (주문 응답·미검증)" in body
    assert "USDT.P" not in body and "수수료" not in body and "손익" not in body


@pytest.mark.parametrize("venue",VENUES)
@pytest.mark.parametrize("side",("buy","sell"))
def test_response_missing_cost_uses_request_estimate_with_provenance(venue,side):
    info=request(venue,side)
    value,source=order_amount(venue,{"id":"ack"},info)
    assert value == 200 and source == "estimate"
    _,embed=build_simple_amount_alert(venue,{"id":"ack"},info)
    body=embed.data["description"]
    assert "수량: 2 XRP (요청·미검증)" in body
    assert ("가격: 100 KRW" if venue=="UPBIT" else "가격: 100 USDT") in body
    assert "요청 참고가·미검증" in body
    assert ("매수액: 200" if side=="buy" else "매도액: 200") in body
    assert "(추정·미검증)" in body


@pytest.mark.parametrize("venue,field",[
    ("BINANCE","cumQuote"),("BINANCE","cummulativeQuoteQty"),
    ("BYBIT","cumExecValue"),("BITGET","quoteVolume"),
    ("BITGET","fillTotalAmount"),
])
def test_exchange_native_quote_receipt_field(venue,field):
    value,kind=order_amount(venue,{"info":{field:"345.12"}},request(venue,"buy"))
    assert (str(value),kind)==("345.12","response")


@pytest.mark.parametrize("bad",["NaN","Infinity","-1","0","invalid",True,None])
def test_untrusted_cost_is_not_displayed(bad):
    value,origin=order_amount("UPBIT",{"cost":bad},request("UPBIT","buy"))
    assert value == 200 and origin == "estimate"


def test_price_is_derived_only_from_consistent_response_quantity_and_cost():
    info=request("UPBIT","buy")
    p,source=order_price("UPBIT",{"cost":"305","filled":"2"},info)
    assert p==152.5 and source=="응답 금액÷수량 추정"
    _,embed=build_simple_amount_alert("UPBIT",{"cost":"305","filled":"2"},info)
    assert "가격: 152.5 KRW (응답 금액÷수량 추정·미검증)" in embed.data["description"]


def test_price_unknown_and_public_ticker_estimate():
    info=request("OKX","sell")
    info.price=None
    assert order_price("OKX",{"id":"ack","amount":2},info)==(None,"확인 불가")
    _,empty=build_simple_amount_alert("OKX",{"id":"ack","amount":2},info)
    assert "가격: 확인 불가" in empty.data["description"]
    assert "매도액: 확인 불가" in empty.data["description"]
    _,estimate=build_simple_amount_alert("OKX",{"id":"ack","amount":2},info,reference_price="103")
    body=estimate.data["description"]
    assert "가격: 103 USDT (공개 현재가 추정·미검증)" in body
    assert "매도액: 206 USDT (추정·미검증)" in body


def test_public_ticker_failure_does_not_fabricate_price(monkeypatch):
    import ccxt
    class Fake:
        def __init__(self,*args,**kwargs): pass
        def fetch_ticker(self,*args): raise RuntimeError("NO_PUBLIC_PRICE")
    monkeypatch.setattr(ccxt,"okx",Fake)
    assert public_reference_price("OKX","XRP/USDT") is None


def test_okx_linear_contract_size_conversion_not_double_applied():
    info=request("OKX","entry/buy",True)
    info.is_contract=True
    info.contract_size=0.1
    value,source=order_amount("OKX",{"id":"okx-c","filled":"20","price":"100"},info)
    assert (str(value),source)==("200.0","estimate")
    _,embed=build_simple_amount_alert("OKX",{"id":"okx-c","filled":"20","price":"100"},info)
    body=embed.data["description"]
    assert "수량: 2 XRP (응답 계약 환산·미검증)" in body
    assert "가격: 100 USDT" in body
    assert "매수액 (계약 명목가치): 200 USDT (추정·미검증)" in body
    assert "20 XRP" not in body


def test_okx_inverse_contract_notional_in_usd_without_price_multiplication():
    info=request("OKX","entry/buy",True)
    info.quote="USD"
    info.unified_symbol="BTC/USD:BTC"
    info.is_contract=True
    info.is_coinm=True
    info.contract_size=100
    info.base="BTC"
    value,source=order_amount("OKX",{"id":"inverse","filled":"3","price":"50000"},info)
    assert (value,source)==(300,"estimate")
    _,embed=build_simple_amount_alert("OKX",{"id":"inverse","filled":"3","price":"50000"},info)
    body=embed.data["description"]
    assert "수량: 3 계약" in body
    assert "가격: 50000 USD" in body
    assert "매수액 (계약 명목가치): 300 USD" in body


@pytest.mark.parametrize("venue",VENUES)
@pytest.mark.parametrize("side",("buy","sell"))
def test_logmaker_single_discord_embed_no_hidden_pnl_fields(venue,side):
    src=ROOT/"exchange"/"utility"/"LogMaker.py"
    with patch("loguru.logger.remove"), patch("loguru.logger.add"):
        spec=importlib.util.spec_from_file_location("exchange.utility.LogMaker",src)
        lm=importlib.util.module_from_spec(spec)
        sys.modules["exchange.utility.LogMaker"]=lm
        spec.loader.exec_module(lm)
    sent=[]
    lm.log_message=lambda message,embed=None: sent.append((message,embed.data))
    lm.log_order_message(venue,{"id":"ack","cost":"256","filled":"2","price":"128"},request(venue,side))
    assert len(sent)==1
    desc=sent[0][1]["description"]
    assert "수량:" in desc and "가격:" in desc
    assert ("매수액:" if side=="buy" else "매도액:") in desc
    assert "수수료" not in desc and "실현손익" not in desc


@pytest.mark.parametrize("venue",VENUES)
@pytest.mark.parametrize("side",("buy","sell"))
def test_rejected_or_missing_order_response_never_schedules_success(venue,side,monkeypatch):
    from exchange.verified_pnl import operational_alerts as ops
    reported=[]
    monkeypatch.setattr(ops,"report",lambda *a,**kw: reported.append((a,kw)))
    src=(ROOT/"main.py").read_text(encoding="utf-8")
    selected=[n for n in ast.parse(src).body if isinstance(n,ast.FunctionDef)
              and n.name in ("log","schedule_order_log")]
    calls=[]
    scope={"settings":SimpleNamespace(VERIFIED_DISCORD_ALERTS=True),
           "log_order_message":lambda *args: calls.append(args),
           "is_verified_exchange":lambda *args: True}
    exec(compile(ast.Module(body=selected,type_ignores=[]),str(ROOT/"main.py"),"exec"),scope)
    class Background:
        def add_task(self,*a): calls.append(a)
    inf=request(venue,side); bg=Background()
    for bad in (None,{"id":"1","status":"rejected","cost":"200"},
                {"id":"2","status":"canceled","filled":"0.5","cost":"50"}):
        scope["log"](venue,bad,inf)
        scope["schedule_order_log"](bg,venue,bad,inf)
    assert calls==[] and len(reported)==3
    scope["schedule_order_log"](bg,venue,{"id":"3","cost":"200"},inf)
    assert len(calls)==1


def test_five_venue_route_and_no_new_private_reads():
    code=(ROOT/"exchange/utility/simple_order_amount.py").read_text()
    assert "fetch_order(" not in code and "create_order(" not in code
    assert "privateGet" not in code
    src=(ROOT/"main.py").read_text(encoding="utf-8")
    defs={n.name:ast.get_source_segment(src,n) for n in ast.parse(src).body if isinstance(n,ast.FunctionDef)}
    for name in ("log","schedule_order_log"):
        assert '("UPBIT", "BINANCE", "BYBIT", "BITGET", "OKX")' in defs[name]
    assert set(SUPPORTED)==set(VENUES)
