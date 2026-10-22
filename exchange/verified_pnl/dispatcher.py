"""Durable read-only verification jobs and independent Discord outbox worker."""
import json
import random
import threading
import time
import uuid
from types import SimpleNamespace
from loguru import logger
from .models import NotificationPayload, FillStatus, ExitReason, VerificationStatus, PnlCalculationResult, PnlResult
from .verifier import FillVerifier
from .pnl_calculator import PnlCalculator
from .exit_reason import classify_exit_reason, classify_pnl_result
from .outbox import NotificationOutboxStore, DiscordSender, format_decimal
from .ledger import identity, allocate

SUPPORTED={"BINANCE","BYBIT","OKX","UPBIT"}
INFO_FIELDS=("exchange","base","quote","unified_symbol","side","is_close","is_entry","is_buy","is_sell",
             "is_futures","is_spot","is_coinm","contract_size","order_name","exit_type","leverage","strategy_id","signal_id")


def warning(code):
    # Never log exception strings, URL, credentials, account IDs or raw webhook JSON.
    logger.warning("verified_alerts: {}", code)


def config_valid(settings):
    return (settings.VERIFIED_DISCORD_ALERTS and settings.VERIFIED_FILL_PRICE and settings.DISCORD_OUTBOX_ENABLED)


def get_side_display(info):
    is_buy = getattr(info, "is_buy", None)
    if is_buy is None:
        side_val = getattr(info, "side", "") or ""
        is_buy = str(side_val).lower() in ("buy", "롱", "매수")
    is_futures = getattr(info, "is_futures", False)
    is_close = getattr(info, "is_close", False)
    if is_futures:
        if is_close: return "숏 종료" if is_buy else "롱 종료"
        return "롱 진입" if is_buy else "숏 진입"
    return "매수" if is_buy else "매도"


def dispatch_verified_alert(exchange_name, order_result, order_info, settings, bot=None):
    """Only a bounded local DB write; never calls a client or Discord here."""
    if not config_valid(settings):
        warning("configuration_incomplete")
        return
    if exchange_name not in SUPPORTED:
        return
    if exchange_name == "UPBIT" and not getattr(settings, "UPBIT_VERIFIED_ALERTS", False):
        return
    try:
        info={k:getattr(order_info,k,None) for k in INFO_FIELDS}
        info={k:v if isinstance(v,(str,int,float,bool,type(None))) else None for k,v in info.items()}
        result=order_result if isinstance(order_result,dict) else {}
        order_id = result.get("id") or result.get("uuid")
        client_order_id = result.get("clientOrderId") or result.get("identifier")
        clean_result = {}
        if order_id is not None:
            clean_result["id"] = str(order_id)
            clean_result["uuid"] = str(order_id)
        if client_order_id is not None:
            clean_result["clientOrderId"] = str(client_order_id)
        result = clean_result
        strat = info.get("strategy_id") or info.get("order_name") or "default"
        sig = info.get("signal_id")
        order_ident = result.get("id")
        if not order_ident:
            if result.get("clientOrderId"):
                order_ident = str(result.get("clientOrderId"))
            elif sig:
                order_ident = f"sig:{strat}:{sig}"
            else:
                order_ident = uuid.uuid4().hex
        sym = info.get("unified_symbol") or (f"{info.get('base')}/{info.get('quote')}" if info.get("base") and info.get("quote") else "unknown")
        account=identity(exchange_name,getattr(settings,exchange_name+"_KEY",None) or "unconfigured")
        key=identity(exchange_name,account,"linear" if info.get("is_futures") else "spot",sym,str(order_ident))
        data=dict(info=info,result=result,account=account,recorded_at=time.time())
        store=NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
        with store._connect() as con:
            inserted=con.execute("INSERT OR IGNORE INTO verified_jobs VALUES (?,?, 'PENDING',?,?)",
                        (key,json.dumps(data),time.time(),time.time())).rowcount
        if not inserted: logger.info("verified_alerts: duplicate_job_blocked")
    except Exception:
        warning("job_persistence_failed")


def dispatch_verified_error_alert(error_message, order_info, settings):
    # Lost API response is not evidence of rejection. No raw exception is persisted.
    dispatch_verified_alert(order_info.exchange,None,order_info,settings)


def make_payload(key, fill, info, pnl, entry_price, settings):
    def money(value): return None if value is None else f"{value:+f} {pnl.pnl_currency}"
    reason=classify_exit_reason(info,{"type":fill.verified_order_type}) if settings.EXIT_REASON_CLASSIFICATION_V2 else ExitReason.UNKNOWN
    notional = None
    if fill.average_fill_price is not None and fill.filled_quantity > 0:
        notional = format_decimal(fill.average_fill_price * fill.filled_quantity)

    is_est_roi = bool(getattr(pnl, "is_roi_estimated", False))
    pnl_ver_status = (
        VerificationStatus.VERIFIED
        if (pnl.verification_status == VerificationStatus.VERIFIED and pnl.net_realized_pnl is not None)
        else VerificationStatus.UNVERIFIED
    )
    if pnl_ver_status == VerificationStatus.VERIFIED and pnl.net_roi_pct is not None and not is_est_roi:
        roi_ver_status = VerificationStatus.VERIFIED
    elif is_est_roi:
        roi_ver_status = VerificationStatus.ESTIMATED
    else:
        roi_ver_status = VerificationStatus.UNVERIFIED

    roi_label = "추정 ROI (레버리지 기반)" if is_est_roi else "ROI"

    return NotificationPayload(
        event_id=key,
        exchange=fill.exchange,
        symbol=fill.symbol,
        side=get_side_display(info),
        order_name=info.order_name or "주문",
        exit_reason=reason,
        pnl_result=classify_pnl_result(pnl),
        fill_status=fill.status,
        order_id=fill.order_id,
        client_order_id=fill.client_order_id,
        requested_qty=format_decimal(fill.requested_quantity),
        filled_qty=format_decimal(fill.filled_quantity),
        remaining_qty=format_decimal(fill.remaining_quantity),
        entry_price=format_decimal(entry_price) if entry_price is not None else None,
        exit_price=format_decimal(fill.average_fill_price) if fill.average_fill_price is not None else None,
        gross_pnl=money(pnl.gross_realized_pnl),
        entry_fee_allocated=money(pnl.entry_fee_allocated),
        exit_fee=money(pnl.exit_fee),
        funding_pnl=money(pnl.funding_pnl_allocated),
        net_pnl=money(pnl.net_realized_pnl),
        price_return_pct=str(pnl.price_return_pct) if pnl.price_return_pct is not None else None,
        roi_pct=str(pnl.net_roi_pct) if pnl.net_roi_pct is not None else None,
        roi_label=roi_label,
        verification_status=fill.verification_status,
        fill_verification_status=fill.verification_status,
        pnl_verification_status=pnl_ver_status,
        roi_verification_status=roi_ver_status,
        is_roi_estimated=is_est_roi,
        verification_source=fill.source + " / PnL: " + pnl.verification_status.value,
        event_timestamp=time.time(),
        fill_timestamp=fill.fill_timestamp,
        leverage=info.leverage,
        fee_breakdown=", ".join(f"{v} {k}" for k, v in fill.fees.items()) or None,
        signal_id=str(getattr(info, "signal_id", None)) if getattr(info, "signal_id", None) is not None else None,
        strategy_id=str(getattr(info, "strategy_id", None)) if getattr(info, "strategy_id", None) is not None else None,
        pnl_currency=getattr(fill, "pnl_currency", None) or getattr(pnl, "pnl_currency", None),
        notional_amount=notional,
        accounting_basis=getattr(fill, "accounting_basis", None) or getattr(pnl, "accounting_basis", None),
        pnl_includes_entry_fee=getattr(fill, "pnl_includes_entry_fee", None) if getattr(fill, "pnl_includes_entry_fee", None) is not None else getattr(pnl, "pnl_includes_entry_fee", None),
        pnl_includes_exit_fee=getattr(fill, "pnl_includes_exit_fee", None) if getattr(fill, "pnl_includes_exit_fee", None) is not None else getattr(pnl, "pnl_includes_exit_fee", None),
        pnl_includes_funding=getattr(fill, "pnl_includes_funding", None) if getattr(fill, "pnl_includes_funding", None) is not None else getattr(pnl, "pnl_includes_funding", None),
        cost_basis=format_decimal(getattr(pnl, "cost_basis", None)) if getattr(pnl, "cost_basis", None) is not None else None,
        cost_basis_currency=getattr(fill, "pnl_currency", None) or getattr(pnl, "pnl_currency", None),
    )


def make_manual_review_payload(key, data, reason, settings):
    raw_info = data.get("info", {}) if isinstance(data, dict) else {}
    info = SimpleNamespace(**raw_info)
    signal_id = getattr(info, "signal_id", None) or raw_info.get("signal_id")
    exchange_name = getattr(info, "exchange", "") or "UNKNOWN"
    symbol = getattr(info, "unified_symbol", None) or f"{getattr(info, 'base', '')}/{getattr(info, 'quote', '')}"
    strategy_id = getattr(info, "strategy_id", None) or getattr(info, "order_name", None) or "default"
    account = data.get("account") if isinstance(data, dict) and data.get("account") else "default"

    res = data.get("result") if (isinstance(data, dict) and isinstance(data.get("result"), dict)) else {}
    order_id = res.get("id") or res.get("uuid")
    raw_client_order_id = res.get("clientOrderId") or res.get("identifier")

    # P2-1 Fix: Preserve distinct manual review alerts when distinct order_id or clientOrderId exists,
    # even if signal_id is shared across multiple orders. Only fall back to signal_id deduplication
    # when both order_id and clientOrderId are absent.
    if order_id is not None and str(order_id).strip():
        event_id = identity(exchange_name, account, strategy_id, symbol, f"ord:{order_id}", "manual_review")
    elif raw_client_order_id is not None and str(raw_client_order_id).strip():
        event_id = identity(exchange_name, account, strategy_id, symbol, f"cid:{raw_client_order_id}", "manual_review")
    elif signal_id:
        event_id = identity(exchange_name, account, strategy_id, symbol, str(signal_id), "manual_review")
    else:
        event_id = f"{key}:manual_review"

    reason_desc = "주문 ID 누락 (missing_order_id)" if reason == "missing_order_id" else f"장기 복구 비활성화 ({reason})"
    err_msg = (
        f"수동 확인 필요 (MANUAL_REVIEW 보류) | 사유: {reason_desc} | "
        f"신호 ID: {signal_id or '없음'} | 거래소: {exchange_name} | 종목: {symbol} | 상태: 미확정(UNVERIFIED)"
    )
    client_order_id = raw_client_order_id or (str(signal_id) if signal_id else None)
    return NotificationPayload(
        event_type="manual_review",
        event_id=event_id,
        exchange=exchange_name,
        symbol=symbol,
        side=get_side_display(info) if hasattr(info, "is_futures") else "주문",
        order_name=getattr(info, "order_name", None) or "주문",
        exit_reason=ExitReason.MANUAL,
        pnl_result=PnlResult.UNKNOWN,
        fill_status=FillStatus.UNVERIFIED,
        order_id=str(order_id) if order_id else None,
        client_order_id=str(client_order_id) if client_order_id else None,
        signal_id=str(signal_id) if signal_id else None,
        strategy_id=str(getattr(info, "strategy_id", None)) if getattr(info, "strategy_id", None) is not None else None,
        verification_status=VerificationStatus.UNVERIFIED,
        fill_verification_status=VerificationStatus.UNVERIFIED,
        pnl_verification_status=VerificationStatus.UNVERIFIED,
        roi_verification_status=VerificationStatus.UNVERIFIED,
        verification_source="수동 검토 보류 (거래소 주문 조회 불가)",
        event_timestamp=time.time(),
        error_message=err_msg,
        leverage=getattr(info, "leverage", None),
    )


def enqueue_manual_review_alert(store, key, data, reason, settings):
    if not isinstance(data, dict) or data.get("manual_review_alert_sent"):
        return
    try:
        payload = make_manual_review_payload(key, data, reason, settings)
        store.enqueue(payload)
        data["manual_review_alert_sent"] = True
    except Exception:
        pass


def verification_tick(settings, client_factory):
    if getattr(settings, "LIGHTWEIGHT_MODE", False):
        from .verifier import configure_concurrency
        configure_concurrency(getattr(settings, "VERIFY_MAX_CONCURRENCY", 2))
    store=NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
    store.recover()
    with store._connect_read() as con:
        jobs=con.execute("SELECT id,data,created FROM verified_jobs WHERE state='PENDING' AND due<=? ORDER BY created LIMIT 20",(time.time(),)).fetchall()
    for key,encoded,created in jobs:
        with store._connect() as con:
            if not con.execute("UPDATE verified_jobs SET state='VERIFYING',due=? WHERE id=? AND state='PENDING'",(time.time()+120,key)).rowcount: continue
        data = None
        attempts = 1
        try:
            data=json.loads(encoded); info=SimpleNamespace(**data["info"])
            attempts = data.get("attempts", 0) + 1
            data["attempts"] = attempts
            client=client_factory(info.exchange,info)
            # Independent client market metadata, never mutate the trading bot.
            market=client.market(info.unified_symbol)
            if info.exchange=="OKX" and info.is_futures:
                info.contract_size=market.get("contractSize")
            fill=FillVerifier().verify_order_result(SimpleNamespace(client=client),info.exchange,info.unified_symbol,data["result"],info)
            is_upbit = (info.exchange == "UPBIT")
            history_complete = settings.VERIFIED_COST_BASIS_COMPLETE and (settings.UPBIT_NET_PNL_ENABLED if is_upbit else settings.NET_REALIZED_PNL_ENABLED)
            entry,entry_fee=allocate(store,fill,info,data["account"],history_complete)
            pnl=PnlCalculationResult()
            pnl_enabled = (settings.UPBIT_NET_PNL_ENABLED if is_upbit else settings.NET_REALIZED_PNL_ENABLED)
            if pnl_enabled:
                # No account-wide funding allocation is guessed. Spot has no funding.
                from decimal import Decimal
                spot_margin = None
                if not info.is_futures and entry is not None and entry > 0 and entry_fee is not None and fill.filled_quantity > 0:
                    spot_margin = (entry * fill.filled_quantity) + entry_fee
                pnl=PnlCalculator().calculate_pnl(fill,info,entry_price=entry,
                    allocated_entry_fee=entry_fee,allocated_funding=Decimal(0) if not info.is_futures else None,
                    allocated_margin=spot_margin)
            partial_alerts = (settings.UPBIT_PARTIAL_FILL_ALERTS if is_upbit else settings.PARTIAL_FILL_ALERTS)
            max_attempts = getattr(settings, "UPBIT_MAX_VERIFY_ATTEMPTS", 5) if is_upbit else 999
            attempts_exceeded = is_upbit and (attempts >= max_attempts)
            terminal=fill.status in {FillStatus.FILLED,FillStatus.CANCELED,FillStatus.FAILED}
            expired=time.time()-created>=settings.FILL_VERIFICATION_TIMEOUT_SEC
            raw_state = str(data.get("result", {}).get("status") or data.get("result", {}).get("state") or getattr(fill, "exchange_status", "") or "").lower()
            is_exchange_terminal = terminal or (raw_state in {"canceled", "cancelled", "expired", "reject", "rejected"})

            if terminal:
                event_type = "final"
                event = identity(key, event_type, [], 2)
                payload = make_payload(event, fill, info, pnl, entry, settings)
                payload.event_type = event_type
                store.enqueue(payload, max_attempts=settings.DISCORD_RETRY_MAX_ATTEMPTS if settings.DISCORD_RETRY_ENABLED else 1)
                data["final_alert_sent"] = True
                if pnl.net_realized_pnl is None: logger.info("verified_alerts: pnl_unconfirmed")
            elif partial_alerts and fill.status == FillStatus.PARTIALLY_FILLED:
                event_type = "partial"
                event = identity(key, event_type, sorted(t.id for t in fill.trades), 2)
                payload = make_payload(event, fill, info, pnl, entry, settings)
                payload.event_type = event_type
                store.enqueue(payload, max_attempts=settings.DISCORD_RETRY_MAX_ATTEMPTS if settings.DISCORD_RETRY_ENABLED else 1)
            elif (expired or attempts_exceeded) and not data.get("unverified_alert_sent"):
                event_type = "unverified"
                event = identity(key, event_type, [], 2)
                payload = make_payload(event, fill, info, pnl, entry, settings)
                payload.event_type = event_type
                store.enqueue(payload, max_attempts=settings.DISCORD_RETRY_MAX_ATTEMPTS if settings.DISCORD_RETRY_ENABLED else 1)
                data["unverified_alert_sent"] = True
                if pnl.net_realized_pnl is None: logger.info("verified_alerts: pnl_unconfirmed")

            now = time.time()
            has_id = bool(data["result"].get("id") or data["result"].get("uuid") or data["result"].get("clientOrderId") or data["result"].get("identifier"))
            long_term_recovery = getattr(settings, "LONG_TERM_RECOVERY_ENABLED", True)
            max_recovery_delay = getattr(settings, "LONG_TERM_RECOVERY_MAX_DELAY", 300.0)

            if is_exchange_terminal:
                next_state = "DONE"
                next_due = now
            elif not has_id:
                # Order ID missing: cannot query exchange safely. Hold for manual review.
                logger.warning("verified_alerts: manual_review_missing_order_id")
                data["stage"] = "manual_review"
                data["reason"] = "missing_order_id"
                next_state = "MANUAL_REVIEW"
                next_due = now + 86400.0
                enqueue_manual_review_alert(store, key, data, "missing_order_id", settings)
            elif not expired and not attempts_exceeded:
                # Initial polling phase with configured staged delays
                if is_upbit and not getattr(settings, "UPBIT_VERIFY_BACKOFF_ENABLED", True):
                    delay = 2.0
                else:
                    staged_delays = getattr(settings, "VERIFY_STAGED_DELAYS", [2.0, 5.0, 10.0, 20.0, 40.0])
                    delay_idx = min(attempts - 1, len(staged_delays) - 1)
                    delay = staged_delays[delay_idx]
                next_state = "PENDING"
                next_due = now + delay
            else:
                # Expired or attempts exceeded, but NOT terminal
                if attempts_exceeded and not data.get("warned_attempts_exceeded"):
                    logger.warning("verified_alerts: upbit_max_attempts_exceeded")
                    data["warned_attempts_exceeded"] = True

                if long_term_recovery:
                    # Apply long-term low-frequency recovery regardless of lightweight mode
                    data["stage"] = "recovery"
                    age = now - created
                    if age >= 86400.0 and not data.get("warned_long_term"):
                        logger.warning("verified_alerts: long_term_unconfirmed")
                        data["warned_long_term"] = True
                    rec_attempts = max(0, attempts - (max_attempts if is_upbit else 5))
                    recon_delay = min(max_recovery_delay, 60.0 * (2 ** min(rec_attempts, 3)))
                    next_state = "PENDING"
                    next_due = now + recon_delay
                else:
                    # LONG_TERM_RECOVERY_ENABLED is False: never disguise as DONE; hold safely for operator review
                    logger.warning("verified_alerts: unconfirmed_held_manual_review")
                    data["stage"] = "manual_review"
                    data["reason"] = "long_term_recovery_disabled"
                    next_state = "MANUAL_REVIEW"
                    next_due = now + 86400.0
                    enqueue_manual_review_alert(store, key, data, "long_term_recovery_disabled", settings)

            with store._connect() as con:
                con.execute("UPDATE verified_jobs SET state=?,due=?,data=? WHERE id=?",(
                    next_state, next_due, json.dumps(data), key))
        except Exception:
            warning("fill_verification_failed")
            now = time.time()
            raw_data = data if data is not None else None
            if raw_data is None:
                try: raw_data = json.loads(encoded)
                except Exception: pass
            if now-created>=settings.FILL_VERIFICATION_TIMEOUT_SEC:
                try:
                    if raw_data and not raw_data.get("unverified_alert_sent"):
                        raw_info = SimpleNamespace(**raw_data["info"])
                        fill=FillVerifier().verify_order_result(None,raw_info.exchange,raw_info.unified_symbol,raw_data["result"],raw_info)
                        payload=make_payload(identity(key,"unverified",[],2),fill,raw_info,PnlCalculationResult(),None,settings)
                        payload.event_type="unverified"
                        store.enqueue(payload)
                        raw_data["unverified_alert_sent"] = True
                except Exception:
                    pass
            is_upbit_exc = False
            try:
                is_upbit_exc = raw_data.get("info", {}).get("exchange") == "UPBIT" if raw_data else False
            except Exception:
                pass
            max_att = getattr(settings, "UPBIT_MAX_VERIFY_ATTEMPTS", 5) if is_upbit_exc else 999
            has_id_exc = False
            if raw_data and isinstance(raw_data.get("result"), dict):
                has_id_exc = bool(raw_data["result"].get("id") or raw_data["result"].get("uuid") or raw_data["result"].get("clientOrderId") or raw_data["result"].get("identifier"))

            long_term_recovery = getattr(settings, "LONG_TERM_RECOVERY_ENABLED", True)
            max_recovery_delay = getattr(settings, "LONG_TERM_RECOVERY_MAX_DELAY", 300.0)

            if raw_data is not None and not has_id_exc:
                logger.warning("verified_alerts: manual_review_missing_order_id")
                if raw_data:
                    raw_data["stage"] = "manual_review"
                    raw_data["reason"] = "missing_order_id"
                next_st = "MANUAL_REVIEW"
                delay = 86400.0
                enqueue_manual_review_alert(store, key, raw_data, "missing_order_id", settings)
            elif attempts >= max_att or (now - created >= settings.FILL_VERIFICATION_TIMEOUT_SEC):
                if is_upbit_exc and attempts >= max_att:
                    logger.warning("verified_alerts: upbit_max_attempts_exceeded")
                if long_term_recovery:
                    next_st = "PENDING"
                    rec_att = max(0, attempts - (max_att if is_upbit_exc else 5))
                    delay = min(max_recovery_delay, 60.0 * (2 ** min(rec_att, 3)))
                else:
                    logger.warning("verified_alerts: unconfirmed_held_manual_review")
                    if raw_data:
                        raw_data["stage"] = "manual_review"
                        raw_data["reason"] = "long_term_recovery_disabled"
                    next_st = "MANUAL_REVIEW"
                    delay = 86400.0
                    enqueue_manual_review_alert(store, key, raw_data, "long_term_recovery_disabled", settings)
            else:
                next_st = "PENDING"
                delay = min(60.0, 10.0 * (2 ** min(attempts, 3)))
            saved_data = json.dumps(raw_data) if raw_data is not None else encoded
            with store._connect() as con:
                con.execute("UPDATE verified_jobs SET state=?,due=?,data=? WHERE id=?",(next_st, now+delay, saved_data, key))


def flush_outbox(settings):
    store=NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
    store.recover()
    retry_on_5xx = bool(getattr(settings, "DISCORD_RETRY_ON_5XX", False))
    for item in store.get_pending(limit=20):
        key=item["event_id"]
        if not store.claim(key): continue
        sender=DiscordSender(settings.DISCORD_WEBHOOK_URL, retry_on_5xx=retry_on_5xx)
        try:
            success,error,delay=sender.send_payload(NotificationPayload(**item["payload"]))
            if success:
                store.mark_sent(key,sender.message_id)
                logger.info("verified_alerts: discord_sent")
            elif error=="delivery_unknown" or str(error or "").startswith("delivery_unknown"):
                store.hold_unknown(key); warning("discord_delivery_unknown")
            else:
                permanent=delay is None or not settings.DISCORD_RETRY_ENABLED
                backoff=delay if delay is not None and "429" in (error or "") else random.uniform(0,min(60,2**item["attempts"]))
                store.mark_failed(key,error or "send_failed",terminal=permanent,delay=backoff)
                warning("discord_failed" if permanent else "discord_retry_scheduled")
        except Exception:
            store.hold_unknown(key)
            warning("discord_delivery_unknown")


class ReadClients:
    """Owns separate CCXT instances used exclusively by the notification thread."""
    def __init__(self,settings): self.settings=settings; self.clients={}
    def __call__(self,exchange,info):
        import ccxt
        category="swap" if info.is_futures else "spot"
        key=(exchange,category)
        if key not in self.clients:
            s=self.settings
            client=getattr(ccxt,exchange.lower())({"apiKey":getattr(s,exchange+"_KEY"),
                "secret":getattr(s,exchange+"_SECRET"),"password":getattr(s,exchange+"_PASSPHRASE",None),
                "timeout":5000,"enableRateLimit":True,"options":{"defaultType":category}})
            client.load_markets()
            # Memory optimization: prune currencies and raw JSON metadata
            if hasattr(client, "currencies") and isinstance(client.currencies, dict):
                client.currencies.clear()
            if hasattr(client, "currencies_by_id") and isinstance(client.currencies_by_id, dict):
                client.currencies_by_id.clear()
            if hasattr(client, "markets") and isinstance(client.markets, dict):
                for m in client.markets.values():
                    if isinstance(m, dict):
                        m.pop("info", None)
            self.clients[key]=client
        return self.clients[key]


def start_worker(settings):
    if not config_valid(settings): warning("configuration_incomplete"); return None
    if settings.EXCHANGE_NATIVE_TPSL_MONITOR:
        warning("native_tpsl_monitor_not_implemented")
    stop=threading.Event()
    def run():
        factory=ReadClients(settings)
        # Run cleanup / recovery on startup
        try:
            init_store = NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
            init_store.recover()
            if hasattr(init_store, "prune_completed"):
                init_store.prune_completed()
        except Exception:
            pass
        tick_count = 0
        while not stop.is_set():
            try:
                verification_tick(settings,factory)
                flush_outbox(settings)
                tick_count += 1
                if tick_count % 60 == 0:
                    try:
                        m_store = NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
                        if hasattr(m_store, "prune_completed"):
                            m_store.prune_completed()
                    except Exception:
                        pass
            except Exception: warning("worker_failure")
            stop.wait(1)
    thread=threading.Thread(target=run,name="verified-notifications",daemon=True)
    thread.start()
    return stop
