"""Additional original-vs-current characterization, including real message formatting."""
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch
from harness import ROOT, setup, bot_for, load, module

root, output = Path(sys.argv[1]), Path(sys.argv[2])
schema, pre, util = setup(root)
records = []
class Embed:
    def __init__(self, **kwargs): self.data = dict(kwargs, fields=[])
    def add_field(self, **kwargs): self.data["fields"].append(kwargs)
module("dhooks", Webhook=Mock(), Embed=Embed)
with patch("loguru.logger.remove"), patch("loguru.logger.add"):
    log = load("test_logs", root / "exchange/utility/LogMaker.py")
log.parse_time = lambda _: "fixed-time"
log.log_message = lambda content, embed=None: records.append({"discord":content, "embed":embed.data if embed else None})
for ex in ["BINANCE", "BYBIT", "OKX"]:
    for side, quote in [("buy","USDT"),("sell","USDT"),("entry/buy","USDT.P"),("close/sell","USDT.P")]:
        info = schema.OrderRequest(exchange=ex, base="BTC", quote=quote, side=side, amount=.1234, type="limit", price=100)
        bot = bot_for(ex, info)
        method = "market_entry" if info.is_entry else "market_close" if info.is_close else "market_buy" if info.is_buy else "market_sell"
        result = getattr(bot, method)(info)
        records.append({"ex":ex,"side":side,"limit_calls":bot.client.calls})
        log.log_order_message(ex, result, info)
    for sizing in [{}, {"amount":1,"percent":10}, {"amount":0}, {"percent":.1}]:
        info = schema.OrderRequest(exchange=ex, base="BTC", quote="USDT.P", side="entry/buy", **sizing)
        bot = bot_for(ex, info)
        try:
            result = bot.market_entry(info)
            error = None
        except Exception as exc:
            result, error = None, str(exc)
        records.append({"ex":ex,"sizing":sizing,"result":result,"error":error,"calls":bot.client.calls})
info = schema.OrderRequest(exchange="BINANCE",base="BTC",quote="USDT.P",side="entry/buy",amount=1)
bot = bot_for("BINANCE",info)
bot.market_sltp_order("BTC","USDT","market","buy",1,90,110)
records.append({"sltp":bot.client.calls})
# Actual exception route response + background notification count.
from fastapi.testclient import TestClient
main = load("test_main", root / "main.py")
main.whitelist.append("testclient")
client = TestClient(main.app)
for exc in [TypeError("test"), RuntimeError("test")]:
    util.log_order_error_message.reset_mock()
    util.log_alert_message.reset_mock()
    main.get_bot = Mock(side_effect=exc)
    response = client.post("/order",json=dict(password="test-only",exchange="BINANCE",base="BTC",quote="USDT",side="buy",amount=1))
    records.append({"exception":type(exc).__name__,"status":response.status_code,"body":response.json(),
                    "error_logs":util.log_order_error_message.call_count,"alert_logs":util.log_alert_message.call_count})
output.write_text(json.dumps(records,ensure_ascii=True,sort_keys=True,indent=2),encoding="utf-8")
print(f"Recorded {len(records)} extended cases")
