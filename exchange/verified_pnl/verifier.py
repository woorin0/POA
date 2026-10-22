from decimal import Decimal, InvalidOperation
import threading
from .models import VerifiedFill, VerifiedTrade, FillStatus, VerificationStatus

_VERIFY_SEMAPHORE = threading.Semaphore(2)


def configure_concurrency(max_concurrency: int = 2):
    global _VERIFY_SEMAPHORE
    val = max(1, int(max_concurrency or 2))
    _VERIFY_SEMAPHORE = threading.Semaphore(val)
    return _VERIFY_SEMAPHORE


def to_decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


class FillVerifier:
    def __init__(self, timeout_sec=30):
        self.timeout_sec = timeout_sec

    def verify_order_result(self, bot, exchange_name, symbol, order_result, order_info):
        data = order_result if isinstance(order_result, dict) else {}
        fill = self._parse_order_dict(exchange_name, symbol, data, order_info)
        if bot is not None:
            oid = str(data.get("id") or data.get("uuid") or "")
            cid = str(data.get("clientOrderId") or data.get("identifier") or "")
            fetched = self._fetch_verified_order(bot, exchange_name, symbol, oid, cid, order_info)
            if fetched is not None:
                return fetched
        return fill

    def _parse_order_dict(self, exchange_name, symbol, data, order_info):
        futures = getattr(order_info, "is_futures", False) is True
        inverse = getattr(order_info, "is_coinm", False) is True
        if ":" in symbol:
            settlement = symbol.split(":")[-1]
        elif "/" in symbol:
            settlement = symbol.split("/")[-1]
        elif "-" in symbol:
            settlement = symbol.split("-")[0]
        else:
            settlement = symbol
        settlement = str(settlement).upper()
        factor = Decimal(1)
        if futures and exchange_name == "OKX":
            factor = to_decimal(getattr(order_info, "contract_size", None))
        if inverse or (futures and settlement != "USDT") or factor is None or factor <= 0:
            return VerifiedFill(exchange=exchange_name, symbol=symbol, order_id=str(data.get("id") or data.get("uuid") or ""),
                                source="UNSUPPORTED_OR_UNKNOWN_CONTRACT")
        def qty(value):
            n = to_decimal(value)
            return n * factor if n is not None and n >= 0 else None
        oid = str(data.get("id") or data.get("uuid") or "")
        requested = qty(data.get("amount") or data.get("volume"))
        reported = qty(data.get("filled") or data.get("executed_volume"))
        remaining = qty(data.get("remaining") or data.get("remaining_volume"))
        trades, fees, seen, pnl = [], {}, {}, []
        complete_fees = True
        raw_trades = data.get("trades") or []
        if not isinstance(raw_trades, list): raw_trades = []
        for t in raw_trades:
            tid = t.get("id") or t.get("uuid")
            if tid is None or (t.get("order") is not None and str(t["order"]) != oid and str(t["order"]) != ""):
                continue
            tid = str(tid)
            if tid in seen:
                if seen[tid] != t:
                    return VerifiedFill(exchange=exchange_name, symbol=symbol, order_id=oid, source="CONFLICTING_TRADES")
                continue
            seen[tid] = t
            px = to_decimal(t.get("price") or t.get("trade_price"))
            amount = qty(t.get("amount") or t.get("volume") or t.get("trade_volume"))
            if px is None or amount is None or px <= 0 or amount <= 0: continue
            fee = t.get("fee") or {}
            cost = to_decimal(fee.get("cost", t.get("commission"))) if isinstance(fee, dict) else None
            currency = (fee.get("currency") or t.get("commissionAsset")) if isinstance(fee, dict) else None
            if cost is None:
                raw_fee = t.get("paid_fee") or (t.get("ask_fee") if getattr(order_info, "is_sell", False) is True else t.get("bid_fee"))
                if raw_fee is not None:
                    cost = to_decimal(raw_fee)
                    currency = settlement
            complete_fees &= cost is not None and bool(currency)
            if cost is not None and currency:
                fees[currency] = fees.get(currency, Decimal(0)) + cost
            raw_ts = t.get("timestamp") or t.get("created_at") or data.get("timestamp") or data.get("created_at")
            trade_ts = None
            if raw_ts is not None:
                if isinstance(raw_ts, (int, float)):
                    trade_ts = float(raw_ts / 1000 if raw_ts > 1e11 else raw_ts)
                else:
                    from datetime import datetime
                    try: trade_ts = datetime.fromisoformat(str(raw_ts)).timestamp()
                    except Exception: pass
            trades.append(VerifiedTrade(id=tid, price=px, amount=amount, fee=cost,
                fee_currency=currency, timestamp=trade_ts))
            raw = t.get("info") or {}
            value = to_decimal(raw.get("realizedPnl")) if exchange_name == "BINANCE" else (
                to_decimal(raw.get("fillPnl")) if exchange_name == "OKX" else None)
            pnl.append(value)
        trade_qty = sum((t.amount for t in trades), Decimal(0))
        quantity = reported if reported is not None else trade_qty
        coverage = bool(trades) and (reported is None or trade_qty == reported)
        reported_count = to_decimal(data.get("trades_count") or (data.get("info") or {}).get("trades_count"))
        if reported_count is not None and reported_count > 0 and len(trades) < reported_count:
            coverage = False
        average = (sum(t.price * t.amount for t in trades) / trade_qty) if coverage and trade_qty > 0 else None
        if average is None and quantity > 0:
            average = to_decimal(data.get("average"))
        if average is None and quantity > 0:
            total_funds = to_decimal(data.get("cost") or data.get("executed_funds") or data.get("funds"))
            if total_funds is not None and total_funds > 0:
                average = total_funds / quantity
        if average is not None and average <= 0: average = None
        if not coverage:
            fees = {}
            fee = data.get("fee") or {}
            cost = to_decimal(fee.get("cost")) if isinstance(fee, dict) else None
            complete_fees = cost is not None and bool(fee.get("currency"))
            if complete_fees: fees[fee["currency"]] = cost
        if (not fees or not complete_fees) and quantity > 0:
            order_fee = data.get("fee") or {}
            order_fee_cost = to_decimal(order_fee.get("cost") if isinstance(order_fee, dict) else None)
            if order_fee_cost is None:
                order_fee_cost = to_decimal(data.get("paid_fee"))
            if order_fee_cost is not None and order_fee_cost >= 0:
                fee_curr = (order_fee.get("currency") if isinstance(order_fee, dict) else None) or settlement
                fees = {fee_curr: order_fee_cost}
                complete_fees = True
        if requested is not None and quantity > requested:
            average = None
        raw_status = str(data.get("status") or data.get("state") or "").lower()
        status = FillStatus.PENDING if oid else FillStatus.UNVERIFIED
        if quantity > 0: status = FillStatus.PARTIALLY_FILLED
        if raw_status in {"closed", "filled", "done"} and quantity > 0 and average is not None:
            if requested is not None and quantity < requested:
                status = FillStatus.PARTIALLY_FILLED
            else:
                status = FillStatus.FILLED
        elif raw_status in {"canceled", "cancelled", "expired", "cancel"}:
            status = FillStatus.CANCELED if quantity == 0 else FillStatus.PARTIALLY_FILLED
        elif raw_status in {"rejected", "failed"}: status = FillStatus.FAILED
        if remaining is None and requested is not None:
            remaining = max(Decimal(0), requested - quantity)
        verified = average is not None and quantity > 0 and (requested is None or quantity <= requested)
        gross = sum(pnl, Decimal(0)) if coverage and pnl and all(v is not None for v in pnl) else None
        stamps = [t.timestamp for t in trades if t.timestamp is not None]
        last = to_decimal(data.get("lastTradeTimestamp"))
        pos_side = "LONG" if not futures else (("SHORT" if getattr(order_info,"is_buy",False) else "LONG") if getattr(order_info,"is_close",False) else ("LONG" if getattr(order_info,"is_buy",False) else "SHORT"))
        return VerifiedFill(exchange=exchange_name, symbol=symbol, order_id=oid,
            client_order_id=str(data.get("clientOrderId") or data.get("identifier") or ""), status=status,
            order_side=getattr(order_info, "side", None) if isinstance(getattr(order_info,"side",None),str) else None,
            position_side=pos_side,
            market_type="linear" if futures else "spot", requested_quantity=requested,
            filled_quantity=quantity, remaining_quantity=remaining, average_fill_price=average,
            contract_size=factor, trades=trades, trades_complete=bool(coverage),
            trades_completeness_reason=None if coverage else ("partial_trades" if trades else "no_trades"),
            fees=fees, fees_complete=bool(complete_fees),
            realized_pnl=gross, pnl_currency="USDT" if futures else settlement,
            accounting_basis="gross" if gross is not None else "unknown",
            fill_timestamp=max(stamps) if stamps else (float(last / 1000) if last is not None else None),
            verification_status=VerificationStatus.VERIFIED if verified else VerificationStatus.UNVERIFIED,
            source="ORDER_RESULT",
            exchange_status=raw_status)

    def _fetch_verified_order(self, bot, exchange_name, symbol, order_id, client_order_id, order_info):
        if not order_id: return None
        client = bot.client
        data = None
        with _VERIFY_SEMAPHORE:
            try:
                params = {"acknowledged": True} if exchange_name == "BYBIT" else {}
                value = client.fetch_order(order_id, symbol, params)
                if isinstance(value, dict) and (str(value.get("id")) == order_id or str(value.get("uuid")) == order_id):
                    data = value.copy()
            except Exception:
                pass
            if data is None and exchange_name == "UPBIT" and hasattr(client, "privateGetOrder"):
                try:
                    res = client.privateGetOrder({"uuid": order_id})
                    if isinstance(res, dict) and (str(res.get("uuid")) == order_id or str(res.get("id")) == order_id):
                        data = res.copy()
                except Exception:
                    pass
            need_trades = False
            if data is None:
                need_trades = True
            else:
                status = str(data.get("status") or data.get("state") or "").lower()
                filled = to_decimal(data.get("filled") or data.get("executed_volume")) or Decimal(0)
                has_trades = bool(data.get("trades"))
                if filled > 0 and not has_trades:
                    avg = to_decimal(data.get("average"))
                    fee = data.get("fee")
                    fee_ok = isinstance(fee, dict) and fee.get("cost") is not None and bool(fee.get("currency"))
                    if avg is None or not fee_ok:
                        need_trades = True
                elif status in {"open", "wait", "watch"} and filled == 0:
                    need_trades = False
                elif status in {"canceled", "cancelled", "rejected", "expired", "cancel"} and filled == 0:
                    need_trades = False

            if need_trades:
                try:
                    values = client.fetch_my_trades(symbol, None, 100)
                    matched = [t for t in values if (str(t.get("order")) == order_id or str(t.get("uuid")) == order_id) and t.get("symbol",symbol) == symbol]
                    if matched:
                        if data is None: data = {"id":order_id, "status":"open"}
                        data["trades"] = matched
                except Exception:
                    pass
        if data is None: return None
        fill = self._parse_order_dict(exchange_name, symbol, data, order_info)
        fill.source = "EXCHANGE_REST_QUERY"
        fill.verified_order_type = data.get("type") or data.get("ord_type")
        return fill
