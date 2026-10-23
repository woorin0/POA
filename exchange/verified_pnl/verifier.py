from decimal import Decimal, InvalidOperation, localcontext
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
        return result if result.is_finite() and len(result.as_tuple().digits)<=128 and abs(result.as_tuple().exponent)<=128 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


class FillVerifier:
    def __init__(self, timeout_sec=30):
        self.timeout_sec = timeout_sec

    def verify_order_result(self, bot, exchange_name, symbol, order_result, order_info, evidence_context=None):
        data = order_result if isinstance(order_result, dict) else {}
        try:fill = self._parse_order_dict(exchange_name, symbol, data, order_info)
        except Exception:fill=VerifiedFill(exchange=exchange_name,symbol=symbol,order_id=str(data.get('id') or data.get('uuid') or ''))
        fetched=None
        if bot is not None:
            oid = str(data.get("id") or data.get("uuid") or "")
            cid = str(data.get("clientOrderId") or data.get("identifier") or "")
            reader=getattr(evidence_context,'execution_reader',None)
            if reader is not None:
                from dataclasses import replace
                with _VERIFY_SEMAPHORE:
                    observed=reader.observe(oid,bot.client)
                if observed is not None:
                    evidence_context=replace(evidence_context,rejection_codes=observed.rejection_codes)
                    fetched=self._parse_order_dict(exchange_name,symbol,observed.data,order_info)
                    fetched.source='EXCHANGE_REST_QUERY'
            else:
                fetched = self._fetch_verified_order(bot, exchange_name, symbol, oid, cid, order_info)
            if fetched is not None:
                fill=fetched
                if evidence_context is not None:
                    try:
                        self._certify_execution(fill,evidence_context,bot.client,order_info)
                        self._certify_outcome(fill,evidence_context,bot.client)
                    except Exception:
                        fill.execution_receipt=fill.outcome_receipt=None
        from .evidence import execution_valid,outcome_rejected
        verified=execution_valid(fill,fill.scope_data)
        if not verified:
            fill.execution_receipt=None
            fill.verification_status=VerificationStatus.UNVERIFIED
        reader=getattr(evidence_context,'execution_reader',None)
        if reader is not None:reader.observed_result(verified or outcome_rejected(fill))
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
                fee_currency=currency, timestamp=trade_ts,
                order=str(t.get("order") or t.get("uuid") or oid),
                order_id=str(t.get("order") or t.get("uuid") or oid),
                source=str(t.get("source") or "EXCHANGE_REST_QUERY")))
            raw = t.get("info") or {}
            value = to_decimal(raw.get("realizedPnl")) if exchange_name == "BINANCE" else (
                to_decimal(raw.get("fillPnl")) if exchange_name == "OKX" else None)
            pnl.append(value)
        # Validate individual trades
        completeness_reason = None
        valid_trades = True
        seen_tids = {}
        for raw_t in (data.get("trades") or []):
            if not isinstance(raw_t, dict): continue
            tid = str(raw_t.get("id") or "")
            t_sym = raw_t.get("symbol")
            if t_sym and t_sym != symbol:
                valid_trades = False
                completeness_reason = "wrong_symbol"
                break
            t_ord = raw_t.get("order") or raw_t.get("uuid")
            if t_ord and oid and str(t_ord) != str(oid):
                valid_trades = False
                completeness_reason = "wrong_order"
                break
            t_src = raw_t.get("source")
            if t_src and t_src not in {"EXCHANGE_REST_QUERY", "ORDER_RESULT", "UPBIT_ORDER_QUERY"}:
                valid_trades = False
                completeness_reason = "trade_source_unknown"
                break
            t_px = to_decimal(raw_t.get("price"))
            t_amt = to_decimal(raw_t.get("amount"))
            if tid:
                if tid in seen_tids:
                    prev_px, prev_amt = seen_tids[tid]
                    if prev_px != t_px or prev_amt != t_amt:
                        valid_trades = False
                        completeness_reason = "duplicate_conflicting"
                        break
                else:
                    seen_tids[tid] = (t_px, t_amt)

        trade_qty = sum((t.amount for t in trades), Decimal(0))
        quantity = reported if reported is not None else trade_qty
        has_summary = (reported is not None)
        if trades:
            coverage = bool(valid_trades and has_summary and (trade_qty == reported))
            if not has_summary:
                coverage = False
                if not completeness_reason: completeness_reason = "unknown_summary"
        else:
            coverage = None
        if data.get("page_capped"):
            coverage = False
            if not completeness_reason: completeness_reason = "page_cap"

        reported_count = to_decimal(data.get("trades_count") or (data.get("info") or {}).get("trades_count"))
        if reported_count is not None and reported_count > 0 and len(trades) < reported_count:
            coverage = False
            if not completeness_reason: completeness_reason = "partial_trades"

        rest_avg = to_decimal(data.get("average"))
        trades_vwap = (sum(t.price * t.amount for t in trades) / trade_qty) if trade_qty > 0 else None

        if coverage and rest_avg is not None and trades_vwap is not None:
            if abs(trades_vwap - rest_avg) > Decimal("0.00000001"):
                coverage = False
                completeness_reason = "vwap_mismatch"

        if trades_vwap is not None and (coverage or rest_avg is None):
            average = trades_vwap
        else:
            average = rest_avg

        if average is None and quantity > 0:
            total_funds = to_decimal(data.get("cost") or data.get("executed_funds") or data.get("funds"))
            if total_funds is not None and total_funds > 0:
                average = total_funds / quantity
        if average is not None and average <= 0: average = None
        if not coverage and not fees:
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
            contract_size=factor, trades=trades, trades_complete=coverage, raw_data=data,
            trades_completeness_reason=None if coverage is True else (completeness_reason or ("partial_trades" if trades else None)),
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
                        if len(values) >= 100:
                            data["page_capped"] = True
                except Exception:
                    pass
        if data is None: return None
        fill = self._parse_order_dict(exchange_name, symbol, data, order_info)
        fill.source = "EXCHANGE_REST_QUERY"
        fill.verified_order_type = data.get("type") or data.get("ord_type")
        return fill

    def _certify_execution(self, fill, context, client, info):
        with localcontext() as arithmetic:
            arithmetic.prec=512
            return self._certify_execution_precise(fill,context,client,info)

    def _certify_execution_precise(self, fill, context, client, info):
        from .evidence import ObservationContext,strict_object,fill_facts,canonical
        if not isinstance(context,ObservationContext) or context.client is not client: return
        try:
            scope=strict_object(context.scope_json)
            required={'exchange','account','market_type','symbol','instrument','settlement','contract_size','position_side','position_cycle','book'}
            if not required <= set(scope) or any(scope[k] in (None,'') for k in required): return
            if any(str(scope[k])!=str(getattr(fill,k)) for k in ('exchange','market_type','symbol','position_side')): return
            if scope['settlement']!=fill.pnl_currency or Decimal(str(scope['contract_size']))!=fill.contract_size: return
            if fill.trades_complete is not True or fill.verification_status!=VerificationStatus.VERIFIED: return
            if not fill.order_id or not fill.trades or fill.filled_quantity<=0: return
            raw=fill.raw_data or {};raw_trades=raw.get('trades')
            if raw.get('symbol')!=fill.symbol:return
            market=client.market(fill.symbol)
            if not isinstance(market,dict) or market.get('id')!=scope['instrument'] or market.get('symbol')!=fill.symbol:return
            if fill.market_type=='spot' and market.get('spot') is not True:return
            if fill.market_type!='spot' and market.get('linear') is not True:return
            if not isinstance(raw_trades,list) or len(raw_trades)!=len(fill.trades): return
            if any(str(t.get('order') or '')!=str(fill.order_id) for t in raw_trades): return
            if len({t.id for t in fill.trades})!=len(fill.trades): return
            q=sum((t.amount for t in fill.trades),Decimal(0))
            if q!=fill.filled_quantity: return
            if sum((t.price*t.amount for t in fill.trades),Decimal(0))/q!=fill.average_fill_price: return
            totals={}
            for t in fill.trades:
                if t.fee is None or not t.fee.is_finite() or not t.fee_currency or t.timestamp is None: return
                totals[t.fee_currency]=totals.get(t.fee_currency,Decimal(0))+t.fee
            representations=[]
            fee=raw.get('fee')
            if fee is not None:
                if not isinstance(fee,dict) or fee.get('cost') is None or not fee.get('currency'): return
                representations.append({fee['currency']:Decimal(str(fee['cost']))})
            if raw.get('fees') is not None:
                if not isinstance(raw['fees'],list): return
                grouped={}
                for f in raw['fees']:
                    if not isinstance(f,dict) or f.get('cost') is None or not f.get('currency'): return
                    grouped[f['currency']]=grouped.get(f['currency'],Decimal(0))+Decimal(str(f['cost']))
                if grouped: representations.append(grouped)
            if raw.get('paid_fee') is not None: representations.append({fill.pnl_currency:Decimal(str(raw['paid_fee']))})
            if context.fee_policy!='EXACT_SAME_ORDER_V1' or any(x!=totals for x in representations): return
            actual_side=str(raw.get('side') or '').lower()
            expected_side='buy' if getattr(info,'is_buy',False) else 'sell'
            if actual_side!=expected_side: return
            fill.fees=totals
            fill.fees_complete=True
            fill.execution_role='CLOSE' if (getattr(info,'is_close',False) or (not getattr(info,'is_futures',False) and getattr(info,'is_sell',False))) else 'ENTRY'
            fill.scope_data=scope
            fill.execution_receipt=context.authority._attest('EXECUTION',{'scope':scope,'fill':fill_facts(fill)})
        except (ValueError,TypeError,KeyError,ArithmeticError):
            return

    def certify_allocation(self, fill, context, plan):
        from .evidence import verify,execution_valid,canonical
        if context is None or not execution_valid(fill,fill.scope_data): return None
        history=context.history_receipt
        if not verify(history,'HISTORY'): return None
        if canonical(history['body'].get('scope'))!=canonical(fill.scope_data): return None
        if history['body'].get('opening_quantity')!='0' or history['body'].get('complete') is not True: return None
        # The plan is rebuilt from immutable, signed executions by ledger before certification.
        from .ledger import allocation_plan
        checked=allocation_plan(plan['records'],fill,history)
        if checked is None or canonical(checked)!=canonical({k:v for k,v in plan.items() if k!='records'}): return None
        receipt=context.authority._attest('ACCOUNTING',checked)
        fill.accounting_receipt=receipt
        return receipt

    def certify_financial_result(self, fill, info, result, context, generation_store=None):
        from .evidence import verify,digest,fill_facts
        if context is None or not verify(fill.accounting_receipt,'ACCOUNTING'): return None
        body=fill.accounting_receipt['body']
        from .pnl_calculator import PnlCalculator
        expected=PnlCalculator().calculate_pnl(fill,info,entry_price=Decimal(body['entry_price']),allocated_entry_fee=Decimal(body['entry_fee']),allocated_funding=Decimal(0) if fill.market_type=='spot' else None,allocated_margin=Decimal(body['cost_basis']),generation_store=generation_store)
        if result.dict(exclude={'financial_receipt'})!=expected.dict(exclude={'financial_receipt'}): return None
        if result.net_realized_pnl is None or result.verification_status!=VerificationStatus.VERIFIED: return None
        facts={'scope':fill.scope_data,'order_id':fill.order_id,'execution_digest':digest(fill.execution_receipt['body']),'accounting_digest':digest(body),'fill':fill_facts(fill),'result':result.dict(exclude={'financial_receipt'})}
        if body.get('generation_id') is not None:facts['generation_id']=body['generation_id']
        result.financial_receipt=context.authority._attest('FINANCIAL',facts)
        return result.financial_receipt

    def _certify_outcome(self,fill,context,client):
        from .evidence import ObservationContext,strict_object
        if not isinstance(context,ObservationContext) or context.client is not client or not context.rejection_codes:return
        raw=fill.raw_data or {};code=str((raw.get('info') or {}).get('code') or '')
        reason=context.rejection_codes.get(code)
        if reason not in {'INSUFFICIENT_BALANCE','ORDER_REJECTED'}:return
        if str(raw.get('status') or '').lower()!='rejected' or to_decimal(raw.get('filled'))!=Decimal(0):return
        scope=strict_object(context.scope_json)
        if scope.get('exchange')!=fill.exchange or scope.get('symbol')!=fill.symbol or str(raw.get('id'))!=fill.order_id:return
        if raw.get('symbol')!=fill.symbol:return
        market=client.market(fill.symbol)
        if not isinstance(market,dict) or market.get('id')!=scope.get('instrument') or market.get('symbol')!=fill.symbol:return
        fill.outcome_receipt=context.authority._attest('OUTCOME',{'scope':scope,'order_id':fill.order_id,'outcome':'REJECTED_FINAL','filled_qty':'0','reason':reason})
