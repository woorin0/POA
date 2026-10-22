"""Replayable average-cost ledger, gated on explicitly complete position history.

Only matched, uniquely identified executions enter the ledger. Funding/margin
are never inferred from leverage or the absence of returned accounting rows.
"""
import hashlib
import json
from decimal import Decimal


def identity(*parts):
    return hashlib.sha256(json.dumps(parts,ensure_ascii=True,separators=(",",":")).encode()).hexdigest()


def allocate(store, fill, info, account, history_complete=False):
    if getattr(fill, "trades_complete", None) is False:
        return None, None
    v_st = str(getattr(getattr(fill, "verification_status", None), "value", getattr(fill, "verification_status", None)) or "").upper()
    fill_src = str(getattr(fill, "source", None) or "").upper()
    req_qty = getattr(fill, "requested_quantity", None)
    if v_st in ("UNVERIFIED", "FAILED") and (fill_src in ("ORDER_RESULT", "EXCHANGE_REST_QUERY") or (req_qty is not None and fill.filled_quantity is not None and fill.filled_quantity > req_qty)):
        return None, None
    trades = getattr(fill, "trades", [])
    if fill.filled_quantity is not None and trades:
        trade_sum = sum((Decimal(str(t.amount)) for t in trades), Decimal("0"))
        if abs(trade_sum - Decimal(str(fill.filled_quantity))) > Decimal("0.00000001"):
            return None, None
    scope = identity(fill.exchange,account,fill.market_type,fill.symbol,fill.position_side)
    with store._connect() as con:
        for trade in fill.trades:
            key = identity(scope,trade.id)
            is_close = (getattr(info, "is_close", False) is True) or (
                (getattr(info, "is_futures", False) is not True) and (getattr(info, "is_sell", False) is True)
            )
            value = dict(order=fill.order_id, strategy=getattr(info,"strategy_id",None),
                         close=is_close, trade=json.loads(json.dumps(trade.dict(),default=str)))
            encoded = json.dumps(value,sort_keys=True)
            existing = con.execute("SELECT data FROM verified_trades WHERE id=?",(key,)).fetchone()
            if existing and json.loads(existing[0]) != value:
                return None, None
            con.execute("INSERT OR IGNORE INTO verified_trades VALUES (?,?,?)",(key,scope,encoded))
        rows = [json.loads(r[0]) for r in con.execute("SELECT data FROM verified_trades WHERE scope=?",(scope,))]
    if not history_complete or not rows or not fill.trades: return None,None
    if len({r["strategy"] for r in rows}) != 1: return None,None
    if any(r["trade"]["timestamp"] is None for r in rows): return None,None
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
