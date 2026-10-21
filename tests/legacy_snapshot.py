import json
import sys
from pathlib import Path
from harness import ROOT, setup, bot_for, load

schema, retry, util = setup(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT)
root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
records = []
for ex in ["BINANCE", "BYBIT", "OKX"]:
    for side, quote in [("buy", "USDT"), ("sell", "USDT"),
                        ("entry/buy", "USDT.P"), ("close/sell", "USDT.P")]:
        for sizing in [{"amount": .12345}, {"percent": 25}]:
            for margin in [None, "cross", "isolated"]:
                payload = dict(exchange=ex, base="BTC", quote=quote, side=side,
                               leverage=2, margin_mode=margin, **sizing)
                info = schema.OrderRequest(**payload)
                parsed = info.dict()
                for key in ["signal_id", "signal_time", "strategy_id"]:
                    parsed.pop(key, None)
                bot = bot_for(ex, info)
                method = "market_entry" if info.is_entry else "market_close" if info.is_close else "market_buy" if info.is_buy else "market_sell"
                try:
                    result = getattr(bot, method)(info)
                    error = None
                except Exception as exc:
                    result, error = None, str(exc)
                records.append(dict(input=payload, parsed=parsed, calls=bot.client.calls,
                                    result=result, error=error))
from fastapi.testclient import TestClient
main = load("test_main", root / "main.py")
main.whitelist.append("testclient")
main.get_bot = lambda ex, n: bot_for(ex, schema.OrderRequest(exchange=ex, base="BTC", quote="USDT", side="buy", amount=1))
client = TestClient(main.app, base_url="http://127.0.0.1")
for path in ["/", "/order"]:
    for typ in ["market", "limit"]:
        payload = dict(password="test-only", exchange="BINANCE", base="BTC", quote="USDT", side="buy", amount=1, type=typ)
        response = client.post(path, json=payload)
        records.append(dict(path=path, type=typ, status=response.status_code, body=response.json()))
target = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "tests/golden_legacy.json"
target.write_text(json.dumps(records, ensure_ascii=True, sort_keys=True, indent=2), encoding="utf-8")
print(f"Recorded {len(records)} cases")
