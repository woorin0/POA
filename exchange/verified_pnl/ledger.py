"""Replayable average-cost ledger, gated on explicitly complete position history.

Only matched, uniquely identified executions enter the ledger. Funding/margin
are never inferred from leverage or the absence of returned accounting rows.
"""
import hashlib
import json
from decimal import Decimal
from .models import VerifiedTrade


def identity(*parts):
    return hashlib.sha256(json.dumps(parts,ensure_ascii=True,separators=(",",":")).encode()).hexdigest()


def allocate(store, fill, info, account, history_complete=False):
    is_close = (getattr(info, "is_close", False) is True) or (
        (getattr(info, "is_futures", False) is not True) and (getattr(info, "is_sell", False) is True)
    )
    v_st = str(getattr(getattr(fill, "verification_status", None), "value", getattr(fill, "verification_status", None)) or "").upper()
    is_verified = (v_st == "VERIFIED")
    src = str(getattr(fill, "source", None) or "").upper()
    trades_complete = getattr(fill, "trades_complete", None)

    # Ingress Gate for entries:
    # Requires positive verification, positively proven completeness (True only),
    # and trusted exchange source without any identifier-based exceptions.
    TRUSTED_SOURCES = {"EXCHANGE_REST_QUERY", "ORDER_RESULT", "UPBIT_ORDER_QUERY", "REST_FETCH"}
    if not is_close:
        if not is_verified or trades_complete is not True:
            return None, None
        if src not in TRUSTED_SOURCES:
            return None, None
    else:
        if not is_verified:
            return None, None
        if src and src not in TRUSTED_SOURCES:
            return None, None
    trades = getattr(fill, "trades", [])
    if not trades or not fill.filled_quantity:
        return None, None
    fill_oid = str(getattr(fill, "order_id", "") or "")
    for t in trades:
        if isinstance(t, dict):
            if "order" not in t:
                return None, None
            t_oid = str(t.get("order") or "")
            if fill_oid and t_oid != fill_oid:
                return None, None
        else:
            t_oid = str(getattr(t, "order_id", None) or getattr(t, "order", None) or "")
            if t_oid and fill_oid and t_oid != fill_oid:
                return None, None
        t_src = str(getattr(t, "source", None) or (t.get("source") if isinstance(t, dict) else None) or src or ("ORDER_RESULT" if is_verified else "UNKNOWN"))
        if t_src not in TRUSTED_SOURCES:
            return None, None

    raw_info = getattr(fill, "raw_info", None) or getattr(fill, "raw_data", None) or {}
    for rt in raw_info.get("trades", []):
        if isinstance(rt, dict):
            r_ord = rt.get("order") or rt.get("uuid")
            if r_ord and fill_oid and str(r_ord) != fill_oid:
                return None, None
            if fill.exchange == "BINANCE" and not r_ord:
                return None, None
            r_src = rt.get("source")
            if r_src and r_src not in TRUSTED_SOURCES:
                return None, None

    # Fee contradiction check
    from .verifier import to_decimal
    raw_data = getattr(fill, "raw_data", None) or {}
    order_fee_val = to_decimal((raw_data.get("fee") or {}).get("cost"))
    if order_fee_val is not None and order_fee_val > Decimal("0") and trades:
        trade_fee_sum = sum((Decimal(str(getattr(t, "fee", 0) or 0)) for t in trades), Decimal("0"))
        if trade_fee_sum == Decimal("0"):
            return None, None

    trade_sum = sum((Decimal(str(t.amount)) for t in trades), Decimal("0"))
    if abs(trade_sum - Decimal(str(fill.filled_quantity))) > Decimal("0.00000001"):
        return None, None

    # VWAP check if average_fill_price is present
    if getattr(fill, "average_fill_price", None) is not None and trade_sum > 0:
        trades_vwap = sum((Decimal(str(t.price)) * Decimal(str(t.amount)) for t in trades), Decimal("0")) / trade_sum
        if abs(trades_vwap - Decimal(str(fill.average_fill_price))) > Decimal("0.00000001"):
            return None, None

    scope = identity(fill.exchange, account, fill.market_type, fill.symbol, fill.position_side)
    with store._connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS verified_quarantine (scope TEXT PRIMARY KEY, reason TEXT)")
        if con.execute("SELECT 1 FROM verified_quarantine WHERE scope=?", (scope,)).fetchone():
            return None, None

        # Batch pre-validation: check for conflicts across all trades before inserting any
        conflict_detected = False
        items_to_insert = []
        trades_to_process = list(trades)
        for trade in trades_to_process:
            key = identity(scope, trade.id)
            t_dict = json.loads(json.dumps(trade.dict(), default=str))
            t_dict["order"] = str(getattr(trade, "order", None) or getattr(trade, "order_id", None) or fill.order_id)
            src_val = getattr(trade, "source", None) or getattr(fill, "source", None) or ("ORDER_RESULT" if is_verified else "UNKNOWN")
            t_dict["source"] = str(src_val)
            value = dict(order=fill.order_id, strategy=getattr(info, "strategy_id", None),
                         close=is_close, version=2, verified=bool(is_verified), source=src_val,
                         trade=t_dict)
            encoded = json.dumps(value, sort_keys=True)
            existing = con.execute("SELECT data FROM verified_trades WHERE id=?", (key,)).fetchone()
            if existing:
                existing_val = json.loads(existing[0])
                if existing_val != value:
                    conflict_detected = True
                    break
            items_to_insert.append((key, scope, encoded))

        if conflict_detected:
            # Quarantine the entire scope and do NOT commit any trade from this batch
            con.execute("INSERT OR REPLACE INTO verified_quarantine VALUES (?,?)", (scope, "conflicting_trade_id"))
            return None, None

        # Insert all trades in batch atomically
        for key, sc, enc in items_to_insert:
            con.execute("INSERT OR IGNORE INTO verified_trades VALUES (?,?,?)", (key, sc, enc))

        rows = [json.loads(r[0]) for r in con.execute("SELECT data FROM verified_trades WHERE scope=?",(scope,))]

    if not history_complete or not rows or not fill.trades: return None,None
    if len({r["strategy"] for r in rows}) != 1: return None,None
    if any(r["trade"]["timestamp"] is None for r in rows): return None,None

    # Check that all rows have verified provenance (reject legacy unproven rows)
    for r in rows:
        if r.get("version") != 2 or not r.get("verified") or r.get("quarantined"):
            return None, None
        t_data = r.get("trade") or {}
        t_ord = t_data.get("order") or t_data.get("order_id")
        if not t_ord or str(t_ord) != str(r.get("order")):
            return None, None
        r_src = r.get("source")
        t_src = t_data.get("source") or r_src
        if r_src not in TRUSTED_SOURCES or (t_src and t_src not in TRUSTED_SOURCES):
            return None, None

    rows.sort(key=lambda r:(r["trade"]["timestamp"],r["trade"]["id"]))
    qty=cost=fees=Decimal(0)
    fees_known=True
    allocated_qty=allocated_cost=allocated_fee=Decimal(0)
    allocation_known=True
    for row in rows:
        t=row["trade"]
        n,p=Decimal(str(t["amount"])),Decimal(str(t["price"]))
        fee=Decimal(str(t["fee"])) if t["fee"] is not None and t["fee_currency"] == fill.pnl_currency else None
        if not row["close"]:
            qty+=n; cost+=n*p
            if fee is None: fees_known=False
            else: fees+=fee
        else:
            if qty<n or qty<=0: return None,None
            share=n/qty
            matched_cost,matched_fee=cost*share,fees*share
            cost-=matched_cost; fees-=matched_fee; qty-=n
            if t["id"] in [x.id for x in fill.trades]:
                allocated_qty+=n; allocated_cost+=matched_cost
                if fee is None or not fees_known: allocation_known=False
                else: allocated_fee+=matched_fee
    if not allocation_known or allocated_qty<=0: return None,None
    return allocated_cost/allocated_qty, allocated_fee

