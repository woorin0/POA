import json
from decimal import Decimal
from typing import Optional, Tuple
import hashlib
from .models import VerifiedFill, VerifiedTrade, VerificationStatus, FillStatus


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def ensure_quarantine_schema(con):
    tbl = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='verified_quarantine'").fetchone()
    if not tbl:
        con.execute("CREATE TABLE verified_quarantine (id TEXT PRIMARY KEY, reason TEXT, payload TEXT)")
        return
    cols = [r[1] for r in con.execute("PRAGMA table_info(verified_quarantine)").fetchall()]
    if "id" not in cols and "scope" in cols:
        con.execute("ALTER TABLE verified_quarantine RENAME TO verified_quarantine_legacy")
        con.execute("CREATE TABLE verified_quarantine (id TEXT PRIMARY KEY, reason TEXT, payload TEXT)")
        con.execute("INSERT OR REPLACE INTO verified_quarantine (id, reason, payload) SELECT scope, reason, json_object('migrated_from', 'scope') FROM verified_quarantine_legacy")
        con.execute("DROP TABLE verified_quarantine_legacy")


def allocate(store, fill, info, account, history_complete=False):
    from .verifier import to_decimal
    is_close = bool(
        getattr(info, "is_close", False)
        or getattr(info, "is_sell", False)
        or str(getattr(info, "side", "")).lower() in ("sell", "매도", "숏 진입", "롱 종료", "숏 종료")
    )
    v_st = str(getattr(getattr(fill, "verification_status", None), "value", getattr(fill, "verification_status", None)) or "").upper()
    is_verified = (v_st == "VERIFIED")
    src = str(getattr(fill, "source", None) or "").upper()
    trades_complete = getattr(fill, "trades_complete", None)

    # Ingress Gate for entries and closes:
    # Entries strictly require explicitly confirmed completeness (trades_complete is True).
    # Closes require completeness not to be explicitly False and trades matching quantity.
    TRUSTED_SOURCES = {"EXCHANGE_REST_QUERY", "ORDER_RESULT", "UPBIT_ORDER_QUERY", "REST_FETCH"}
    if not is_close:
        if trades_complete is not True:
            return None, None
    else:
        if trades_complete is False:
            return None, None
    if not is_verified:
        return None, None
    if not fill.order_id or not fill.filled_quantity:
        return None, None

    # Validate raw exchange receipt structure if present
    raw_data = getattr(fill, "raw_data", None)
    raw_info = getattr(fill, "raw_info", None)
    raw_obj = raw_data if (raw_data is not None and isinstance(raw_data, dict)) else (raw_info if (raw_info is not None and isinstance(raw_info, dict)) else None)
    if raw_obj is not None:
        RECEIPT_KEYS = {"id", "uuid", "orderId", "order_id", "status", "state", "executedQty", "filled", "trades", "fee", "price", "avg_price", "average", "cost"}
        if not any(k in raw_obj for k in RECEIPT_KEYS):
            return None, None
        # Contradictory raw summary validation
        raw_filled = to_decimal(raw_obj.get("filled") if raw_obj.get("filled") is not None else raw_obj.get("executedQty"))
        if raw_filled is not None and abs(raw_filled - Decimal(str(fill.filled_quantity))) > Decimal("1e-8"):
            return None, None
        raw_avg = to_decimal(raw_obj.get("average") if raw_obj.get("average") is not None else raw_obj.get("avg_price"))
        if raw_avg is not None and raw_avg > 0 and getattr(fill, "average_fill_price", None) is not None:
            if abs(raw_avg - Decimal(str(fill.average_fill_price))) > Decimal("1e-8"):
                return None, None

    if src and src not in TRUSTED_SOURCES:
        return None, None
    if not src:
        src = "ORDER_RESULT" if is_verified else "UNKNOWN"
        if src not in TRUSTED_SOURCES:
            return None, None

    trades = getattr(fill, "trades", [])
    if not trades:
        return None, None
    fill_oid = str(getattr(fill, "order_id", "") or "")
    for t in trades:
        if isinstance(t, dict):
            t_oid = str(t.get("order") or t.get("order_id") or "")
            raw_t_src = t.get("source")
        else:
            t_oid = str(getattr(t, "order", None) or getattr(t, "order_id", None) or "")
            raw_t_src = getattr(t, "source", None)
        # R9-F02: Trade executions cannot be fabricated using pure order-result tags without raw receipt
        if raw_t_src == "ORDER_RESULT" and raw_obj is None:
            return None, None
        if t_oid and fill_oid and t_oid != fill_oid:
            return None, None
        t_src = str(raw_t_src or src)
        if t_src not in TRUSTED_SOURCES:
            return None, None

    for rt in (raw_obj or {}).get("trades", []):
        if isinstance(rt, dict):
            r_ord = rt.get("order") or rt.get("uuid")
            if r_ord and fill_oid and str(r_ord) != fill_oid:
                return None, None
            r_src = str(rt.get("source") or "")
            if r_src and r_src not in TRUSTED_SOURCES:
                return None, None

    # Fee Reconciliation (R9-F01): Strict exact equality; no arbitrary epsilon or sign flip
    order_fee_dict = (raw_obj or {}).get("fee") or {}
    order_fee_val = to_decimal(order_fee_dict.get("cost") if isinstance(order_fee_dict, dict) else None)
    if order_fee_val is None:
        order_fee_val = to_decimal((raw_obj or {}).get("paid_fee"))
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
            if order_fee_val != trade_fee_sum:
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
        ensure_quarantine_schema(con)
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

            # Generate structured verifiable evidence receipt
            evidence_receipt = identity("VERIFIED_EVIDENCE_V3", scope, fill.order_id, t_dict["id"], str(t_dict.get("price")), str(t_dict.get("amount")), str(t_dict.get("timestamp")))
            value = dict(
                order=fill.order_id,
                strategy=getattr(info, "strategy_id", None),
                close=is_close,
                version=2,
                verified=bool(is_verified),
                source=src_val,
                evidence_receipt=evidence_receipt,
                trade=t_dict,
            )
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
        con.executemany("INSERT OR REPLACE INTO verified_trades VALUES (?,?,?)", items_to_insert)

    if not is_close: return None,None
    with store._connect_read() as con:
        rows = [json.loads(x[0]) for x in con.execute("SELECT data FROM verified_trades WHERE scope=?",(scope,)).fetchall()]
    if len({r["strategy"] for r in rows}) != 1: return None,None
    if any(r["trade"]["timestamp"] is None for r in rows): return None,None

    # Check that all rows have verified provenance and structured evidence (reject unproven rows without ID heuristics)
    for r in rows:
        if r.get("version") != 2 or not r.get("verified") or r.get("quarantined"):
            return None, None
        t_data = r.get("trade") or {}
        if not r.get("evidence_receipt") or t_data.get("cost") is None:
            return None, None
        t_ord = t_data.get("order") or t_data.get("order_id")
        if not t_ord or str(t_ord) != str(r.get("order")):
            return None, None

    sorted_trades = sorted(rows, key=lambda x: (x["trade"]["timestamp"], x["trade"]["id"]))
    qty = cost = fees = Decimal(0)
    fees_known = True
    allocated_qty = allocated_cost = allocated_fee = Decimal(0)
    allocation_known = True
    for row in sorted_trades:
        t = row["trade"]
        n, p = Decimal(str(t["amount"])), Decimal(str(t["price"]))
        fee = Decimal(str(t["fee"])) if t.get("fee") is not None and t.get("fee_currency") == fill.pnl_currency else None
        if not row["close"]:
            qty += n
            cost += n * p
            if fee is None:
                fees_known = False
            else:
                fees += fee
        else:
            if qty < n or qty <= 0:
                return None, None
            share = n / qty
            matched_cost, matched_fee = cost * share, fees * share
            if row["order"] == fill.order_id:
                allocated_qty += n
                allocated_cost += matched_cost
                allocated_fee += matched_fee
                allocation_known &= fees_known
            qty -= n
            cost -= matched_cost
            fees -= matched_fee
            if qty == 0:
                fees_known = True

    if allocated_qty != Decimal(str(fill.filled_quantity)):
        return None, None
    if not allocation_known or allocated_qty <= 0:
        return None, None
    return allocated_cost / allocated_qty, allocated_fee if allocation_known else None
