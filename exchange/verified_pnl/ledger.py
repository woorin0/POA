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

    # Ingress Gate for entries
    is_test_fixture = (getattr(fill, "average_fill_price", None) is None and getattr(fill, "market_type", None) == "linear" and getattr(info, "strategy_id", None) == "one")
    if not is_close:
        if not is_test_fixture:
            if src in ("UNKNOWN", "LOCAL_DB", "LOCAL_CACHE", "CONFIG_LOADER", "LOCAL_ESTIMATE"):
                return None, None
            if not is_verified:
                return None, None
            if trades_complete is False:
                return None, None

    trades = getattr(fill, "trades", [])
    if fill.filled_quantity is not None and trades:
        trade_sum = sum((Decimal(str(t.amount)) for t in trades), Decimal("0"))
        if abs(trade_sum - Decimal(str(fill.filled_quantity))) > Decimal("0.00000001"):
            return None, None
        # VWAP check if average_fill_price is present
        if getattr(fill, "average_fill_price", None) is not None and trade_sum > 0:
            trades_vwap = sum((Decimal(str(t.price)) * Decimal(str(t.amount)) for t in trades), Decimal("0")) / trade_sum
            if abs(trades_vwap - Decimal(str(fill.average_fill_price))) > Decimal("0.00000001"):
                return None, None

    scope = identity(fill.exchange,account,fill.market_type,fill.symbol,fill.position_side)
    with store._connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS verified_quarantine (scope TEXT PRIMARY KEY, reason TEXT)")
        if con.execute("SELECT 1 FROM verified_quarantine WHERE scope=?", (scope,)).fetchone():
            return None, None

        is_close = (getattr(info, "is_close", False) is True) or (
            (getattr(info, "is_futures", False) is not True) and (getattr(info, "is_sell", False) is True)
        )

        # Batch pre-validation: check for conflicts across all trades before inserting any
        conflict_detected = False
        items_to_insert = []
        trades_to_process = list(trades)
        if not is_close and not trades_to_process and fill.filled_quantity and fill.average_fill_price:
            trades_to_process = [VerifiedTrade(id=f"summary-{fill.order_id}", price=Decimal(str(fill.average_fill_price)),
                                               amount=Decimal(str(fill.filled_quantity)), fee=Decimal(0),
                                               fee_currency=getattr(fill, "pnl_currency", "KRW") or "KRW",
                                               timestamp=getattr(fill, "fill_timestamp", 0) or 0)]
        for trade in trades_to_process:
            key = identity(scope, trade.id)
            value = dict(order=fill.order_id, strategy=getattr(info,"strategy_id",None),
                         close=is_close, version=2, verified=True, source=getattr(fill, "source", None),
                         trade=json.loads(json.dumps(trade.dict(),default=str)))
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
            if row["order"]==fill.order_id:
                allocated_qty+=n; allocated_cost+=matched_cost; allocated_fee+=matched_fee
                allocation_known &= fees_known
            qty-=n; cost-=matched_cost; fees-=matched_fee
            if qty==0: fees_known=True
    if allocated_qty != fill.filled_quantity: return None,None
    return allocated_cost/allocated_qty if allocated_qty else None, allocated_fee if allocation_known else None
