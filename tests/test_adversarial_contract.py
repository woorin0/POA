import os, sys, tempfile, json, time, random, string, dataclasses
os.environ["PASSWORD"] = "test"
os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/1/dummy"
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests.test_independent_audit as tia
OrderRequest = tia.t.OrderRequest
from exchange.verified_pnl.models import *
from exchange.verified_pnl.ledger import allocate, identity
from exchange.verified_pnl.outbox import NotificationOutboxStore, build_verified_embed
from exchange.utility.discord_embed import (
    build_mobile_verified_embed, COLOR_ORDER_AMBIGUOUS, COLOR_SELL_COMPLETED, COLOR_BUY_COMPLETED
)
from exchange.verified_pnl.dispatcher import dispatch_verified_alert, verification_tick

def random_string(n=10):
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))

def run_all_adversarial_tests():
    print("=== STARTING ADVERSARIAL AND CONTRACT VERIFICATION TESTS ===")
    
    # -------------------------------------------------------------------------
    # TEST 1: IDENTIFIER INDEPENDENCE
    # -------------------------------------------------------------------------
    print("\n[TEST 1] IDENTIFIER INDEPENDENCE: Randomizing display IDs must not alter verification outcome")
    base_kw = dict(
        exchange="UPBIT", symbol="SOL/KRW", side="매수",
        exit_reason=ExitReason.UNKNOWN, pnl_result=PnlResult.UNKNOWN,
        fill_status=FillStatus.FILLED, filled_qty="5.0", exit_price="150000",
        fill_verification_status=VerificationStatus.VERIFIED,
        verification_source="ORDER_RESULT", event_timestamp=time.time()
    )
    reference_embed = build_verified_embed(NotificationPayload(event_id="ref", order_name="ref_order", **base_kw)).data
    ref_color = reference_embed["color"]
    ref_title_has_completed = ("완료" in reference_embed["title"])
    
    for i in range(20):
        rand_event = f"rand-{random_string()}"
        rand_order = f"order-{random_string()}"
        p = NotificationPayload(event_id=rand_event, order_name=rand_order, **base_kw)
        e = build_verified_embed(p).data
        assert e["color"] == ref_color, f"Color changed for {rand_event}: {e['color']} vs {ref_color}"
        assert ("완료" in e["title"]) == ref_title_has_completed, f"Completion title changed for {rand_event}"
    print("PASS: 20/20 randomized display IDs produced 100% identical verification status and colors!")

    # -------------------------------------------------------------------------
    # TEST 2: MISSING VERIFICATION STATUS SAFE
    # -------------------------------------------------------------------------
    print("\n[TEST 2] MISSING VERIFICATION STATUS SAFE: No bypass on sensitive/fixture IDs without evidence")
    fixture_ids = ["buy-sol-1", "canc-part", "sell-case-c", "round3", "audit", "test", "admin", "production"]
    for fid in fixture_ids:
        p_unverified = NotificationPayload(
            event_id=fid, exchange="UPBIT", symbol="SOL/KRW", side="매수", order_name=fid,
            exit_reason=ExitReason.UNKNOWN, pnl_result=PnlResult.UNKNOWN,
            fill_status=FillStatus.FILLED, filled_qty="5.0", exit_price="150000",
            event_timestamp=time.time()
        )
        e_unv = build_verified_embed(p_unverified).data
        assert e_unv["color"] == COLOR_ORDER_AMBIGUOUS, f"ID {fid} bypassed verification! Color: {e_unv['color']}"
        assert "확인 필요" in e_unv["title"], f"ID {fid} got confirmed title: {e_unv['title']}"
    print(f"PASS: All {len(fixture_ids)} fixture/audit IDs without evidence safely defaulted to AMBIGUOUS!")

    # -------------------------------------------------------------------------
    # TEST 3: TRADES_COMPLETE_NONE_BLOCKED & COMPLETENESS MATRIX
    # -------------------------------------------------------------------------
    print("\n[TEST 3] TRADES_COMPLETE_NONE_BLOCKED: Ingress gate blocks incomplete/None trades_complete")
    with tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False) as tf:
        db_path = tf.name
    store = NotificationOutboxStore(db_path)
    
    # 3a. trades_complete = None -> MUST BE BLOCKED
    f_none = VerifiedFill(
        exchange="BINANCE", symbol="BTC/USDT", order_id="b-none", market_type="spot", position_side="LONG",
        status=FillStatus.FILLED, filled_quantity=D("1"), average_fill_price=D("100"), pnl_currency="USDT",
        verification_status=VerificationStatus.VERIFIED, source="ORDER_RESULT", trades_complete=None,
        trades=[VerifiedTrade(id="t-none", price=D("100"), amount=D("1"), fee=D("0.1"), fee_currency="USDT", timestamp=10.0)]
    )
    info_buy = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT", side="buy", amount=1, strategy_id="s-test")
    res_none = allocate(store, f_none, info_buy, "acct-test", True)
    assert res_none == (None, None), f"trades_complete=None entered ledger! {res_none}"
    
    # 3b. trades_complete = False -> MUST BE BLOCKED
    f_false = f_none.copy(update={"order_id": "b-false", "trades_complete": False})
    res_false = allocate(store, f_false, info_buy, "acct-test", True)
    assert res_false == (None, None), f"trades_complete=False entered ledger! {res_false}"
    
    # 3c. trades_complete = True -> MUST ENTER LEDGER
    f_true = f_none.copy(update={"order_id": "b-true", "trades_complete": True})
    res_true = allocate(store, f_true, info_buy, "acct-test", True)
    with store._connect_read() as con:
        rows = con.execute("SELECT count(*) FROM verified_trades WHERE scope=?", (identity("BINANCE","acct-test","spot","BTC/USDT","LONG"),)).fetchone()[0]
        assert rows == 1, f"trades_complete=True failed to enter ledger! Count: {rows}"
    print("PASS: trades_complete=None and False blocked; trades_complete=True successfully recorded!")

    # -------------------------------------------------------------------------
    # TEST 4: QUANTITY AND VWAP RECONCILED
    # -------------------------------------------------------------------------
    print("\n[TEST 4] QUANTITY AND VWAP RECONCILED: Quantity match but VWAP mismatch blocked")
    with tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False) as tf:
        db_path = tf.name
    store = NotificationOutboxStore(db_path)
    
    f_vwap_mismatch = VerifiedFill(
        exchange="BINANCE", symbol="BTC/USDT", order_id="b-vwap", market_type="spot", position_side="LONG",
        status=FillStatus.FILLED, filled_quantity=D("2"), average_fill_price=D("150"), pnl_currency="USDT",
        verification_status=VerificationStatus.VERIFIED, source="ORDER_RESULT", trades_complete=True,
        trades=[
            VerifiedTrade(id="tv1", price=D("100"), amount=D("1"), fee=D("0.1"), fee_currency="USDT", timestamp=10.0),
            VerifiedTrade(id="tv2", price=D("100"), amount=D("1"), fee=D("0.1"), fee_currency="USDT", timestamp=11.0),
        ]
    )
    res_vwap = allocate(store, f_vwap_mismatch, info_buy, "acct-vwap", True)
    assert res_vwap == (None, None), "VWAP mismatch bypassed ledger check!"
    with store._connect_read() as con:
        rows = con.execute("SELECT count(*) FROM verified_trades WHERE scope=?", (identity("BINANCE","acct-vwap","spot","BTC/USDT","LONG"),)).fetchone()[0]
        assert rows == 0, f"VWAP mismatch inserted rows! Count: {rows}"
    print("PASS: VWAP mismatch (trades VWAP 100 vs average 150) strictly blocked from entering ledger!")

    # -------------------------------------------------------------------------
    # TEST 5: LEGACY LEDGER QUARANTINE
    # -------------------------------------------------------------------------
    print("\n[TEST 5] LEGACY LEDGER QUARANTINE: Unverified/legacy rows in DB never trusted after restart")
    with tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False) as tf:
        db_path = tf.name
    store = NotificationOutboxStore(db_path)
    scope = identity("BINANCE", "acct-legacy", "spot", "BTC/USDT", "LONG")
    
    with store._connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS verified_trades (id TEXT PRIMARY KEY, scope TEXT, data TEXT)")
        dirty_val = dict(order="old-buy", strategy="s-leg", close=False, version=1, verified=False,
                         trade=dict(id="t-old", price="100", amount="1", fee="0.1", fee_currency="USDT", timestamp=1.0))
        con.execute("INSERT INTO verified_trades VALUES (?,?,?)", ("legacy-key", scope, json.dumps(dirty_val)))
    
    sell_info = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT", side="sell", amount=1, strategy_id="s-leg")
    sell_fill = VerifiedFill(
        exchange="BINANCE", symbol="BTC/USDT", order_id="s-leg", market_type="spot", position_side="LONG",
        status=FillStatus.FILLED, filled_quantity=D("1"), average_fill_price=D("120"), pnl_currency="USDT",
        verification_status=VerificationStatus.VERIFIED, source="ORDER_RESULT", trades_complete=True,
        trades=[VerifiedTrade(id="ts-leg", price=D("120"), amount=D("1"), fee=D("0.1"), fee_currency="USDT", timestamp=2.0)]
    )
    res_leg = allocate(store, sell_fill, sell_info, "acct-legacy", True)
    assert res_leg == (None, None), f"Legacy unverified row was trusted! {res_leg}"
    print("PASS: Legacy unproven rows in ledger strictly rejected from cost allocation!")

    # -------------------------------------------------------------------------
    # TEST 6: PNL RECOVERY AFTER TERMINAL & OUTBOX CORRECTION
    # -------------------------------------------------------------------------
    print("\n[TEST 6] PNL RECOVERY AFTER TERMINAL: Incomplete trades query recovers +8 Net PnL upon full data")
    with tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False) as tf:
        db_path = tf.name
    
    settings = tia.settings(db_path)
    settings.NET_REALIZED_PNL_ENABLED = True
    settings.VERIFIED_COST_BASIS_COMPLETE = True
    settings.PARTIAL_FILL_ALERTS = True
    settings.BINANCE_KEY = "key1"
    settings.VERIFY_STAGED_DELAYS = [0.0]
    store = NotificationOutboxStore(db_path)
    
    def native_order(oid, price, qty, status='closed', ts=1700000000000):
        return dict(
            id=oid, status=status, amount=str(qty), filled=str(qty), remaining='0', average=str(price),
            fee={'cost': str(qty), 'currency': 'USDT'},
            trades=[dict(id='t-'+oid, order=oid, symbol='BTC/USDT', price=str(price), amount=str(qty),
                         fee={'cost': str(qty), 'currency': 'USDT'}, timestamp=ts)]
        )

    orders = {
        "buy": native_order("buy", 150, 2),
        "sell": native_order("sell", 160, 1, ts=1700000001000)
    }
    # Step 1: Make buy trades incomplete (only 1 trade of qty 1 at price 100)
    orders["buy"]["trades"][0].update(price="100", amount="1", fee={"cost": "1", "currency": "USDT"})
    
    client = Mock()
    client.market.return_value = {"contractSize": 1}
    client.fetch_order.side_effect = lambda oid, *a: orders[oid]
    client.fetch_my_trades.return_value = []
    
    req_buy = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT", side="buy", amount=2, strategy_id="alpha-v1", order_name="production-alpha")
    req_sell = OrderRequest(exchange="BINANCE", base="BTC", quote="USDT", side="sell", amount=1, strategy_id="alpha-v1", order_name="production-alpha", exit_type="TP")
    
    # Dispatch buy with incomplete trades
    dispatch_verified_alert("BINANCE", {"id": "buy"}, req_buy, settings)
    verification_tick(settings, lambda *a: client)
    
    # Verify buy is not in ledger
    with store._connect_read() as con:
        buy_rows = con.execute("SELECT count(*) FROM verified_trades").fetchone()[0]
        assert buy_rows == 0, "Incomplete buy entered ledger!"
    
    # Step 2: Now trades become complete (buy gets second trade: price 200, amount 1 -> avg cost 151 with fees)
    orders["buy"]["trades"].append({"id": "t-buy-2", "order": "buy", "symbol": "BTC/USDT", "price": "200", "amount": "1", "fee": {"cost": "1", "currency": "USDT"}, "timestamp": 1700000000001})
    
    # Tick recovery: buy re-queries, finds complete trades, enters ledger!
    verification_tick(settings, lambda *a: client)
    with store._connect_read() as con:
        buy_rows_after = con.execute("SELECT count(*) FROM verified_trades").fetchone()[0]
        assert buy_rows_after == 2, f"Complete buy failed to enter ledger! Count: {buy_rows_after}"

    # Now dispatch sell
    dispatch_verified_alert("BINANCE", {"id": "sell"}, req_sell, settings)
    verification_tick(settings, lambda *a: client)
    
    # Check outbox after recovery
    pending_after = store.get_pending()
    sell_after = [r for r in pending_after if r["payload"]["side"] == "매도"]
    assert len(sell_after) == 1
    latest_sell = sell_after[0]
    recovered_net = latest_sell["payload"]["net_pnl"]
    assert D(str(recovered_net).split()[0]) == D(8), f"Failed to recover +8 USDT! Got: {recovered_net}"
    assert "151" in str(latest_sell["payload"]["cost_basis"]), f"Wrong cost basis: {latest_sell['payload']['cost_basis']}"
    print(f"PASS: Incomplete buy safely recovered! Recovered net PnL: {recovered_net}, Cost basis: {latest_sell['payload']['cost_basis']}")

    # -------------------------------------------------------------------------
    # TEST 7: PROTECTED FILES UNION INVARIANCE
    # -------------------------------------------------------------------------
    print("\n[TEST 7] PROTECTED FILES UNION INVARIANCE: 34 files union has zero diff against Base SHA")
    import subprocess
    base_sha = "69e48fc3989f9d957efb6bdef81ba57516cc5dfb"
    union_files = [
        "deployment/build_cloud_config.py", "deployment/deploy_vultr.py", "deployment/fix_alerts.py",
        "deployment/install_once.py", "deployment/preflight_vultr.py", "deployment/prepare_ubuntu.py",
        "deployment/remote_deploy.sh", "deployment/rollback_vultr.py", "deployment/validate_config.py",
        "exchange/binance.py", "exchange/bitget.py", "exchange/bybit.py", "exchange/database.py",
        "exchange/error.py", "exchange/model/schemas.py", "exchange/okx.py", "exchange/pexchange.py",
        "exchange/pocket.py", "exchange/safe_retry.py", "exchange/stock/__init__.py", "exchange/stock/error.py",
        "exchange/stock/kis.py", "exchange/stock/order.py", "exchange/stock/schemas.py", "exchange/stock/utility.py",
        "exchange/upbit.py", "main.py", "requirements.txt", "scripts/notification_db.py", "scripts/run_load_test.py",
        "scripts/smoke_upbit.py", "scripts/verify_release_zip.py", "scripts/verify_vultr_runtime.py", "tools/safe_order_admin.py"
    ]
    existing_files = [f for f in union_files if (ROOT / f).exists()]
    nonexistent_files = [f for f in union_files if not (ROOT / f).exists()]
    assert len(existing_files) == 26, f"Expected 26 existing files, got {len(existing_files)}"
    assert len(nonexistent_files) == 8, f"Expected 8 nonexistent files, got {len(nonexistent_files)}"
    diff_proc = subprocess.run(["git", "diff", base_sha, "--"] + existing_files, cwd=ROOT, capture_output=True, text=True)
    assert diff_proc.stdout.strip() == "", f"Diff found in protected union files!\n{diff_proc.stdout}"
    print(f"PASS: All {len(existing_files)} existing protected files have exact ZERO diff against Base SHA! (8 nonexistent paths acknowledged)")

    print("\n=========================================================================")
    print("ALL 7 ADVERSARIAL & CONTRACT INVARIANT TESTS PASSED WITH 100% SUCCESS!")
    print("=========================================================================")

def test_contract_and_adversarial_suite():
    run_all_adversarial_tests()

if __name__ == "__main__":
    run_all_adversarial_tests()
