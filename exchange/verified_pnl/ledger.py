import json
from decimal import Decimal
from typing import Optional, Tuple
import hashlib
from .models import VerifiedFill, VerifiedTrade, VerificationStatus


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def allocate(store, fill, info, account, history_complete=False):
    is_close = (getattr(info, "is_close", False) is True) or (
        (getattr(info, "is_futures", False) is not True) and (getattr(info, "is_sell", False) is True)
    )
    v_st = str(getattr(getattr(fill, "verification_status", None), "value", getattr(fill, "verification_status", None)) or "").upper()
    is_verified = (v_st == "VERIFIED")
    src = str(getattr(fill, "source", None) or "").upper()
    trades_complete = getattr(fill, "trades_complete", None)

    # Ingress Gate for entries and closes:
    # Requires positive verification, positively proven completeness (True only),
    # order_id presence, and trusted exchange source without any identifier-based exceptions.
    TRUSTED_SOURCES = {"EXCHANGE_REST_QUERY", "ORDER_RESULT", "UPBIT_ORDER_QUERY", "REST_FETCH"}
    trades = getattr(fill, "trades", [])
    trades_sum = sum((Decimal(str(t.amount if hasattr(t, "amount") else t["amount"])) for t in trades), Decimal("0")) if trades else Decimal("0")
    if trades_complete is False:
        return None, None
    is_complete = (trades_complete is True) or (trades_complete is not False and bool(trades) and abs(trades_sum - Decimal(str(fill.filled_quantity))) <= Decimal("0.00000001"))
    if not is_complete:
        return None, None
    if not is_verified:
        return None, None
    if src == "EXCHANGE_REST_QUERY" and not getattr(fill, "raw_data", None) and not getattr(fill, "raw_info", None):
        return None, None
    if src and src not in TRUSTED_SOURCES:
        return None, None
    if not src:
        src = "ORDER_RESULT" if is_verified else "UNKNOWN"
        if src not in TRUSTED_SOURCES:
            return None, None
    if not fill.order_id or not fill.filled_quantity:
        return None, None

    trades = getattr(fill, "trades", [])
    if not trades:
        return None, None
    fill_oid = str(getattr(fill, "order_id", "") or "")
    for t in trades:
        if isinstance(t, dict):
            t_oid = str(t.get("order") or t.get("order_id") or "")
        else:
            t_oid = str(getattr(t, "order", None) or getattr(t, "order_id", None) or "")
        if t_oid and fill_oid and t_oid != fill_oid:
            return None, None
        t_src = str(getattr(t, "source", None) or (t.get("source") if isinstance(t, dict) else None) or src)
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

    # General Fee Reconciliation Check (R8-F01)
    from .verifier import to_decimal
    raw_data = getattr(fill, "raw_data", None) or getattr(fill, "raw_info", None) or {}
    order_fee_dict = raw_data.get("fee") or {}
    order_fee_val = to_decimal(order_fee_dict.get("cost") if isinstance(order_fee_dict, dict) else None)
    if order_fee_val is None:
        order_fee_val = to_decimal(raw_data.get("paid_fee"))
    order_fee_ccy = str(order_fee_dict.get("currency") or "").upper() if isinstance(order_fee_dict, dict) else ""

    if trades:
        trade_fee_sum = Decimal(0)
        trade_currencies = set()
        has_trade_fees = False
        for t in trades:
            f_val = to_decimal(t.get("fee") if isinstance(t, dict) else getattr(t, "fee", None))
            if f_val is not None:
                has_trade_fees = True
                trade_fee_sum += f_val
            c_val = str((t.get("fee_currency") if isinstance(t, dict) else getattr(t, "fee_currency", None)) or "").upper()
            if c_val:
                trade_currencies.add(c_val)

        if order_fee_val is not None and has_trade_fees:
            if order_fee_ccy and trade_currencies and any(c != order_fee_ccy for c in trade_currencies):
                return None, None
            if abs(order_fee_val - trade_fee_sum) > Decimal("0.00000001"):
                return None, None
        elif order_fee_val is not None and not has_trade_fees and order_fee_val > Decimal(0):
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
        con.execute("CREATE TABLE IF NOT EXISTS verified_quarantine (id TEXT PRIMARY KEY, reason TEXT, payload TEXT)")
        if con.execute("SELECT 1 FROM verified_quarantine WHERE id=?", (scope,)).fetchone():
            return None, None

        # Batch pre-validation: check for conflicts across all trades before inserting any
        conflict_detected = False
        items_to_insert = []
        trades_to_process = list(trades)
        for trade in trades_to_process:
            key = identity(scope, trade.id)
            t_dict = json.loads(json.dumps(trade.dict(), default=str))
            if t_dict.get("cost") is None:
                try:
                    t_dict["cost"] = str(Decimal(str(t_dict.get("price"))) * Decimal(str(t_dict.get("amount"))))
                except Exception:
                    pass
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
            con.execute("INSERT OR REPLACE INTO verified_quarantine (id, reason, payload) VALUES (?,?,?)", (scope, "conflicting_trade_id", json.dumps([t.id for t in trades_to_process])))
            return None, None

        # Insert all trades in batch atomically
        for key, sc, enc in items_to_insert:
            con.execute("INSERT OR IGNORE INTO verified_trades VALUES (?,?,?)", (key, sc, enc))

        rows = [json.loads(r[0]) for r in con.execute("SELECT data FROM verified_trades WHERE scope=?",(scope,))]

    if not history_complete or not rows or not fill.trades: return None,None
    if len({r["strategy"] for r in rows}) != 1: return None,None
    if any(r["trade"]["timestamp"] is None for r in rows): return None,None

    # Check that all rows have verified provenance and evidence (reject legacy unproven rows)
    for r in rows:
        if r.get("version") != 2 or not r.get("verified") or r.get("quarantined"):
            return None, None
        t_data = r.get("trade") or {}
        if t_data.get("cost") is None or t_data.get("id") == "legacy":
            return None, None
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
