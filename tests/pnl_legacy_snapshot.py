"""Characterize existing notifications before modifying application code."""
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch
from harness import ROOT, setup, load, module

root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
target = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "tests/golden_pnl_legacy.json"
schema, _, util = setup(root)
class Embed:
    def __init__(self, **kw): self.data = dict(kw, fields=[])
    def add_field(self, **kw): self.data["fields"].append(kw)
module("dhooks", Webhook=Mock(), Embed=Embed)
with patch("loguru.logger.remove"), patch("loguru.logger.add"):
    log = load("legacy_pnl_log", root / "exchange/utility/LogMaker.py")
log.parse_time = lambda _: "fixed-time"
cases = []
for name, side, reason, entry, price in [
    ("long_entry","entry/buy",None,None,101),
    ("long_close","close/sell",None,100,101),
    ("TP","close/sell","TP",100,99),
    ("SL","close/sell","SL",100,101),
    ("trailing","close/sell","TRAIL",100,101),
    ("missing_entry","close/sell","TP",None,101),
    ("missing_fill","close/sell","SL",100,None),
    ("discord_error","close/sell","TP",100,101),
]:
    info = schema.MarketOrder(password="test-only",exchange="BINANCE",base="BTC",quote="USDT.P",
                              side=side,amount=.01,leverage=2,exit_type=reason)
    result = dict(id="fixture",amount=.01,entry_price=entry,close_price=price,price=price,average=price)
    sent = []
    log.hook = Mock()
    if name == "discord_error": log.hook.send.side_effect = RuntimeError("fake Discord down")
    else: log.hook.send.side_effect = lambda **kw: sent.append(kw["embed"].data)
    try:
        log.log_order_message("BINANCE",result,info)
        error = None
    except Exception as exc:
        error = type(exc).__name__
    cases.append(dict(name=name,exit_type_preserved=info.exit_type,result=result,sent=sent,error=error))
target.write_text(json.dumps(cases,ensure_ascii=True,sort_keys=True,indent=2),encoding="utf-8")
print(f"Recorded {len(cases)} legacy alert fixtures")
