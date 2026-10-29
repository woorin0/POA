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
             "is_futures","is_spot","is_coinm","contract_size","order_name","exit_type","leverage","strategy_id","signal_id","tv_signal")


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


def _host_identity_hint(info,reference,order_id):
    """Pure lookup of an explicitly activated host binding; no REST in producer."""
    from .bootstrap import current_bootstrap
    from . import evidence as E
    boot=current_bootstrap()
    if boot is None or E._PROVIDER is not boot or not boot._key_ready():return None
    if E.account_reference(info.get('exchange'),reference)!=boot.scope.account:return None
    if not boot._matches(info.get('exchange'),boot.scope.account,SimpleNamespace(**info),boot.adapter.client):return None
    scope=dict(exchange=boot.scope.exchange,account=boot.scope.account,subaccount=boot.scope.subaccount,market_type='spot',instrument=boot.scope.instrument,symbol=boot.scope.symbol,settlement=boot.scope.settlement,contract_size='1',position_side='LONG',position_cycle=boot.scope.position_cycle,book=boot.scope.book)
    return NotificationOutboxStore.order_binding(scope,order_id,info)


def _release_identity_jobs(store):
    with store._connect() as c:
        row=c.execute("SELECT cursor FROM vpnl_recovery_cursor WHERE lane='identity'").fetchone();after=row[0] if row else ''
        batch=c.execute("SELECT id,data FROM vpnl_jobs WHERE state='HELD' AND financial_state='WAIT_IDENTITY' AND id>? ORDER BY id LIMIT 40",(after,)).fetchall()
        if not batch:batch=c.execute("SELECT id,data FROM vpnl_jobs WHERE state='HELD' AND financial_state='WAIT_IDENTITY' ORDER BY id LIMIT 40").fetchall()
        for key,encoded in batch:
            try:
                data=store.parse_job(encoded);hint=_host_identity_hint(data['info'],data.get('source_account_ref',data['account']),str(data['result'].get('id') or data['result'].get('uuid') or ''))
                from .evidence import account_reference
                mapped=account_reference(data['info']['exchange'],data.get('source_account_ref',data['account']))
                from .bootstrap import current_bootstrap
                if hint is not None or (current_bootstrap() is None and mapped is not None and mapped==data['account']):c.execute("UPDATE vpnl_jobs SET state='PENDING',due=0,financial_state='NEW' WHERE id=?",(key,))
            except (ValueError,KeyError,TypeError):continue
        c.execute("INSERT INTO vpnl_recovery_cursor VALUES('identity',?) ON CONFLICT(lane) DO UPDATE SET cursor=excluded.cursor",(batch[-1][0] if batch else '',))


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
        for flag in ("is_buy","is_sell","is_close","is_futures"):
            if info.get(flag) is None:info[flag]=False
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
                order_ident = uuid.uuid4().hex
            else:
                order_ident = uuid.uuid4().hex
        sym = info.get("unified_symbol") or (f"{info.get('base')}/{info.get('quote')}" if info.get("base") and info.get("quote") else "unknown")
        from .evidence import account_reference
        legacy_account=identity(exchange_name,getattr(settings,exchange_name+"_KEY",None) or "unconfigured")
        account=account_reference(exchange_name,legacy_account) or legacy_account
        legacy_key=identity(exchange_name,legacy_account,"linear" if info.get("is_futures") else "spot",sym,str(order_ident))
        key=identity('notification-input-v2',legacy_account,exchange_name,"linear" if info.get("is_futures") else "spot",sym,str(order_ident),NotificationOutboxStore.producer_identity(info))
        data=dict(info=info,result=result,account=account,source_account_ref=legacy_account)
        from .intake import IntakeStore,replay,safe_failure
        from .evidence import digest
        host_hint=_host_identity_hint(info,legacy_account,str(result.get('id') or ''))
        data['host_binding_digest']=digest(host_hint) if host_hint is not None else None
        intake=IntakeStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
        intake_id=intake.record(data)
    except Exception as exc:
        from .intake import safe_failure
        code=safe_failure(exc)
        logger.error('verified_alerts: {}',code)
        return dict(status='PERSISTENCE_FAILED',accepted=False,code=code)
    try:
        replay(settings,only=intake_id)
    except Exception:
        # Intake is already committed. A failed handoff must not pretend it was lost.
        warning('INTAKE_REPLAY_DEFERRED')
    return dict(status='DURABLE_ACCEPTED',accepted=True,intake_id=intake_id)


def dispatch_verified_error_alert(error_message, order_info, settings):
    # Lost API response is not evidence of rejection. No raw exception is persisted.
    dispatch_verified_alert(order_info.exchange,None,order_info,settings)


def make_payload(key, fill, info, pnl, entry_price, settings):
    def format_decimal(value):
        return '-' if value is None else format(value,'f')
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

    from .evidence import execution_valid
    trusted_fill=execution_valid(fill,fill.scope_data)
    return NotificationPayload(
        event_id=key,
        financial_receipt=pnl.financial_receipt,
        accounting_receipt=fill.accounting_receipt,
        execution_receipt=fill.execution_receipt,
        outcome_receipt=fill.outcome_receipt,
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
        fill_verification_status=fill.verification_status if trusted_fill else VerificationStatus.UNVERIFIED,
        pnl_verification_status=pnl_ver_status,
        roi_verification_status=roi_ver_status,
        is_roi_estimated=is_est_roi,
        verification_source=fill.source + " / PnL: " + pnl.verification_status.value,
        event_timestamp=time.time(),
        fill_timestamp=fill.fill_timestamp,
        exchange_status=getattr(fill, "exchange_status", None),
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
    from .intake import restore_preflight,replay
    check=restore_preflight(store)
    if not check['ready']:
        if not check['blocked']:replay(settings,store=store)
        warning('RESTORE_PREFLIGHT_BLOCKED');return
    store.recover()
    from .history import recover_consumers,recover_consumer_actions
    recover_consumers(store)
    recover_consumer_actions(store)
    _release_identity_jobs(store)
    from .intake import replay
    try:replay(settings,store=store)
    except Exception:warning('INTAKE_REPLAY_DEFERRED')
    from .history import refresh_dependencies
    refresh_dependencies(store,client_factory)
    from .bootstrap import current_bootstrap
    if current_bootstrap() is not None:
        with store._connect() as c:
            c.execute("UPDATE vpnl_jobs SET state='PENDING',due=? WHERE id IN (SELECT id FROM vpnl_jobs WHERE state='OBSERVED_WAIT_PROVIDER' ORDER BY created,id LIMIT 40)",(time.time(),))
    from .upbit_spot_read import current_upbit_notification_host
    upbit_host_provider=current_upbit_notification_host()
    if upbit_host_provider is not None:
        with store._connect() as c:c.execute("UPDATE vpnl_jobs SET state='PENDING',due=? WHERE id IN (SELECT id FROM vpnl_jobs WHERE state='OBSERVED_WAIT_PROVIDER' AND CASE WHEN json_valid(data) THEN json_extract(data,'$.info.exchange') END='UPBIT' ORDER BY created,id LIMIT 40)",(time.time(),))
    jobs=store.ready_jobs(20)
    for key,encoded,created in jobs:
        lease=uuid.uuid4().hex
        with store._connect() as con:
            if not con.execute("UPDATE vpnl_jobs SET state='VERIFYING',due=?,job_lease=? WHERE id=? AND state='PENDING'",(time.time()+120,lease,key)).rowcount:continue
        try:
            data=store.parse_job(encoded)
        except (ValueError,TypeError,KeyError):
            with store._connect() as con:store._quarantine_job(con,key,encoded)
            warning('job_schema_invalid')
            continue
        attempts=1
        publication_candidate=None
        try:
            info=SimpleNamespace(**data['info'])
            attempts = data.get("attempts", 0) + 1
            data["attempts"] = attempts
            from .bootstrap import current_bootstrap
            observed=data.get('notice_intake') is not None
            if observed and data['notice_snapshot'].get('version')==2:
                from .tradingview_signal import process
                process(store,key,lease,data,settings)
                continue
            upbit_host=upbit_host_provider
            if observed and info.exchange=='UPBIT' and upbit_host_provider is not None and hasattr(upbit_host_provider,'select_job') and not data['notice_snapshot']['confirmed']:
                upbit_host=upbit_host_provider.select_job(store,key,lease,data)
            if observed and data['notice_snapshot']['price_source']=='SCOPE_REJECTED':
                with store._connect() as c:c.execute("UPDATE vpnl_jobs SET state='OBSERVED_REVIEW',job_lease=NULL WHERE id=? AND job_lease=?",(key,lease))
                continue
            boot=current_bootstrap()
            local_upbit=observed and info.exchange=='UPBIT' and upbit_host is not None and not data['notice_snapshot']['confirmed']
            local_only=local_upbit and (boot is None or boot.scope.exchange!='UPBIT' or boot.scope.legacy_account_reference!=data.get('source_account_ref'))
            if observed and (boot is None or local_only):
                client=None;evidence_context=None
            else:
                client=client_factory(info.exchange,info)
                # Independent client market metadata, never mutate the trading bot.
                market=client.market(info.unified_symbol)
                if info.exchange=="OKX" and info.is_futures:info.contract_size=market.get("contractSize")
                from .evidence import context_for
                evidence_context=context_for(info.exchange,data["account"],info,client)
            read_bot=SimpleNamespace(client=client) if evidence_context is not None else None
            result_data=data['result']
            if observed and evidence_context is None:
                snap=data['notice_snapshot']
                result_data=dict(id=snap['order_id'],symbol=snap['symbol'],status='closed' if snap['confirmed'] else 'open',side=snap['side'],filled=snap['quantity'],amount=snap['quantity'],remaining='0',average=snap['average'])
            local_observed=False
            if local_upbit and evidence_context is None:
                from .upbit_spot_read import UpbitNotificationWait
                try:
                    observation=upbit_host.observe(data['notice_intake'],info,data['source_account_ref'],data['result']['id'])
                    if not store.record_upbit_observation(key,lease,data,observation):raise ValueError('upbit_observation_not_admitted')
                    result_data=observation.facts;local_observed=True
                except UpbitNotificationWait:
                    expired=time.time()-created>=settings.FILL_VERIFICATION_TIMEOUT_SEC
                    with store._connect() as c:c.execute("UPDATE vpnl_jobs SET state=?,due=?,job_lease=NULL WHERE id=? AND job_lease=?",('OBSERVED_REVIEW' if expired else 'PENDING',time.time()+max(1,settings.VERIFY_STAGED_DELAYS[min(attempts-1,len(settings.VERIFY_STAGED_DELAYS)-1)]),key,lease))
                    continue
                except Exception:
                    warning('UPBIT_OBSERVATION_REVIEW')
                    with store._connect() as c:c.execute("UPDATE vpnl_jobs SET state='OBSERVED_REVIEW',job_lease=NULL WHERE id=? AND job_lease=?",(key,lease))
                    continue
            fill=FillVerifier().verify_order_result(read_bot,info.exchange,info.unified_symbol,result_data,info,evidence_context)
            if local_observed and data['notice_snapshot']['side']=='buy':
                with store._connect() as c:c.execute("UPDATE vpnl_jobs SET state='OBSERVED_RECORDED',financial_state='OBSERVATION_ONLY',job_lease=NULL WHERE id=? AND job_lease=?",(key,lease))
                continue
            if observed and evidence_context is None and not local_observed and (data['notice_snapshot']['side']=='buy' or not data['notice_snapshot']['confirmed']):
                with store._connect() as c:c.execute("UPDATE vpnl_jobs SET state=?,financial_state='OBSERVATION_ONLY',job_lease=NULL WHERE id=? AND job_lease=?",('OBSERVED_RECORDED' if data['notice_snapshot']['confirmed'] else 'OBSERVED_REVIEW' if boot is not None else 'OBSERVED_WAIT_PROVIDER',key,lease))
                continue
            if observed and evidence_context is not None:
                from .evidence import execution_valid
                if execution_valid(fill,fill.scope_data) and fill.filled_quantity>0 and fill.status in {FillStatus.FILLED,FillStatus.CANCELED}:
                    if not store.record_notice_successor(key,lease,data,fill,evidence_context):
                        with store._connect() as c:c.execute("UPDATE vpnl_jobs SET state='OBSERVED_REVIEW',job_lease=NULL WHERE id=? AND job_lease=?",(key,lease))
                        continue
                elif not data['notice_snapshot']['confirmed']:
                    expired=time.time()-created>=settings.FILL_VERIFICATION_TIMEOUT_SEC
                    with store._connect() as c:c.execute("UPDATE vpnl_jobs SET state=?,due=?,job_lease=NULL WHERE id=? AND job_lease=?",('OBSERVED_REVIEW' if expired else 'PENDING',time.time()+max(1,settings.VERIFY_STAGED_DELAYS[min(attempts-1,len(settings.VERIFY_STAGED_DELAYS)-1)]),key,lease))
                    continue
            from .history import attach_history,register_dependency,record_published_withdrawal
            evidence_context=attach_history(store,fill,info,evidence_context,job_id=key)
            is_upbit = (info.exchange == "UPBIT")
            history_complete = settings.VERIFIED_COST_BASIS_COMPLETE and (settings.UPBIT_NET_PNL_ENABLED if is_upbit else settings.NET_REALIZED_PNL_ENABLED)
            entry,entry_fee=allocate(store,fill,info,data["account"],history_complete,evidence_context)
            pnl=PnlCalculationResult()
            pnl_enabled = (settings.UPBIT_NET_PNL_ENABLED if is_upbit else settings.NET_REALIZED_PNL_ENABLED)
            if pnl_enabled:
                # No account-wide funding allocation is guessed. Spot has no funding.
                from decimal import Decimal
                spot_margin = None
                if not info.is_futures and entry is not None and entry > 0 and entry_fee is not None and fill.filled_quantity > 0:
                    from .evidence import verify
                    spot_margin = Decimal(fill.accounting_receipt['body']['cost_basis']) if verify(fill.accounting_receipt,'ACCOUNTING') else None
                pnl=PnlCalculator().calculate_pnl(fill,info,entry_price=entry,
                    allocated_entry_fee=entry_fee,allocated_funding=Decimal(0) if not info.is_futures else None,
                    allocated_margin=spot_margin,generation_store=store)
            FillVerifier().certify_financial_result(fill,info,pnl,evidence_context,generation_store=store)
            if evidence_context is not None:register_dependency(store,fill,pnl,evidence_context,key)
            partial_alerts = (settings.UPBIT_PARTIAL_FILL_ALERTS if is_upbit else settings.PARTIAL_FILL_ALERTS)
            max_attempts = getattr(settings, "UPBIT_MAX_VERIFY_ATTEMPTS", 5) if is_upbit else 999
            attempts_exceeded = is_upbit and (attempts >= max_attempts)
            terminal=fill.status in {FillStatus.FILLED,FillStatus.CANCELED,FillStatus.FAILED}
            expired=time.time()-created>=settings.FILL_VERIFICATION_TIMEOUT_SEC
            raw_state = str(data.get("result", {}).get("status") or data.get("result", {}).get("state") or getattr(fill, "exchange_status", "") or "").lower()
            is_order_terminal = terminal or (raw_state in {"canceled", "cancelled", "expired", "reject", "rejected", "cancel", "closed", "filled", "done"})

            # Determine if financial accounting (PnL / Ledger entry) is verified
            is_close = (getattr(info, "is_close", False) is True) or ((not getattr(info, "is_futures", False)) and getattr(info, "is_sell", False))
            if is_close:
                pnl_done = (pnl.financial_receipt is not None and pnl.net_realized_pnl is not None and getattr(pnl, "verification_status", None) == VerificationStatus.VERIFIED)
            else:
                from .evidence import execution_valid
                pnl_done=execution_valid(fill,fill.scope_data)

            from .evidence import verify
            rejected_final=verify(fill.outcome_receipt,'OUTCOME') and fill.outcome_receipt['body'].get('outcome')=='REJECTED_FINAL'
            pnl_done=pnl_done or rejected_final

            def _dispatch_enqueue(p):
                nonlocal publication_candidate
                from .evidence import verify,payload_valid
                from .history import correction_alias
                # Start with the producer stream, then honor a proven consumer target.
                p.event_id=identity(key,'estimated' if p.notification_type=='ESTIMATED' else 'final',[],2)
                history=evidence_context.history_receipt if evidence_context is not None else None
                if verify(history,'HISTORY') and history['body'].get('complete') is False and history['body'].get('consumer_alias'):
                    p.event_id=history['body']['consumer_alias']
                elif payload_valid(p):
                    target=correction_alias(store,p,key)
                    if target:p.event_id=target

                from .evidence import digest
                with store._connect_read() as c:
                    owned=c.execute("SELECT 1 FROM vpnl_jobs WHERE id=? AND state='VERIFYING' AND job_lease=?",(key,lease)).fetchone()
                    if not owned:raise ValueError('job_lease_lost')
                    head=c.execute('SELECT r.head,v.financial_digest,v.payload FROM vpnl_roots r JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head WHERE r.alias=?',(p.event_id,)).fetchone()
                if head:
                    from .evidence import verify,payload_valid
                    prior=NotificationPayload(**json.loads(head[2]))
                    history=evidence_context.history_receipt if evidence_context is not None else None
                    if not payload_valid(p) and verify(history,'HISTORY'):
                        candidate=p.copy(deep=True)
                        candidate.financial_revocation_receipt=history
                        if store._withdrawal_valid(candidate,prior):
                            p=candidate
                            p.event_type='revoked'
                    try: old_integrity=digest(store._facts(NotificationPayload(**json.loads(head[2]))))==head[1]
                    except (ValueError,TypeError,KeyError):old_integrity=False
                    if head[1]!=digest(store._facts(p)) or not old_integrity:p.event_version=head[0]+1
                publication_candidate=p
                data['publication_pending']={'alias':p.event_id,'facts_digest':digest(store._facts(p)),'state':'REVALIDATE'}
                enqueued=store.enqueue(p,max_attempts=settings.DISCORD_RETRY_MAX_ATTEMPTS if settings.DISCORD_RETRY_ENABLED else 1,job_claim=(key,lease))
                if payload_valid(p) or p.event_type=='revoked':
                    with store._connect_read() as c:published=store.publication_reference(c,p,(key,lease))
                    if published:record_published_withdrawal(store,p)
                return enqueued

            is_canceled_terminal = raw_state in {"canceled", "cancelled", "expired", "cancel"}
            if terminal or is_canceled_terminal:
                event_type = "final"
                event = identity(key, event_type, [], 2)
                payload = make_payload(event, fill, info, pnl, entry, settings)
                payload.event_type = event_type
                from .evidence import payload_valid,verify
                withdrawing=evidence_context is not None and verify(evidence_context.history_receipt,'HISTORY') and evidence_context.history_receipt['body'].get('complete') is False
                if data.get('notice_intake') and info.is_sell and not payload_valid(payload) and not withdrawing:
                    from .estimated_pnl import attach
                    payload=attach(payload,store,data)
                data["final_alert_sent"] = _dispatch_enqueue(payload)
                if payload.notification_type=='ESTIMATED' or (data.get('notice_intake') and store.notice_decided(key) and not data['final_alert_sent']):
                    with store._connect() as c:
                        # A notification-only outcome is never VERIFIED_COMPLETE.
                        data.pop('publication_pending',None)
                        c.execute("UPDATE vpnl_jobs SET data=?,state=?,financial_state='OBSERVATION_ONLY',job_lease=NULL WHERE id=? AND job_lease=?",(json.dumps(data),'OBSERVED_NOTIFIED' if data['final_alert_sent'] else 'OBSERVED_REVIEW',key,lease))
                    continue
                if pnl.net_realized_pnl is None: logger.info("verified_alerts: pnl_unconfirmed")
            elif partial_alerts and fill.status == FillStatus.PARTIALLY_FILLED:
                event_type = "partial"
                event = identity(key, event_type, sorted(t.id for t in fill.trades), 2)
                payload = make_payload(event, fill, info, pnl, entry, settings)
                payload.event_type = event_type
                _dispatch_enqueue(payload)
            elif (expired or attempts_exceeded) and not data.get("unverified_alert_sent"):
                event_type = "unverified"
                event = identity(key, event_type, [], 2)
                payload = make_payload(event, fill, info, pnl, entry, settings)
                payload.event_type = event_type
                _dispatch_enqueue(payload)
                data["unverified_alert_sent"] = True
                if pnl.net_realized_pnl is None: logger.info("verified_alerts: pnl_unconfirmed")

            now = time.time()
            has_id = bool(data["result"].get("id") or data["result"].get("uuid") or data["result"].get("clientOrderId") or data["result"].get("identifier"))
            long_term_recovery = getattr(settings, "LONG_TERM_RECOVERY_ENABLED", True)
            max_recovery_delay = getattr(settings, "LONG_TERM_RECOVERY_MAX_DELAY", 300.0)

            cost_basis_strict = getattr(settings, "VERIFIED_COST_BASIS_COMPLETE", False)
            if is_order_terminal and pnl_done:
                data["accounting_verified"] = 1 if pnl_done else 0
                next_state = "DONE"
                next_due = now
            elif is_order_terminal and not pnl_done:
                data["accounting_verified"] = 0
                # Order completed on exchange but financial evidence incomplete: keep PENDING for recovery
                staged_delays = getattr(settings, "VERIFY_STAGED_DELAYS", [2.0, 5.0, 10.0, 20.0, 40.0])
                delay_idx = min(attempts - 1, len(staged_delays) - 1)
                delay = staged_delays[delay_idx]
                next_state = "PENDING"
                next_due = now + delay
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

            from .evidence import digest
            from .history import consumer_job_pending
            if consumer_job_pending(store,key) or store.intake_job_pending(key):next_state='PENDING';next_due=time.time();pnl_done=False
            if store.identity_review(key):next_state='HELD';pnl_done=False
            with store._connect() as con:
                if publication_candidate is not None:
                    published=store.publication_reference(con,publication_candidate,(key,lease))
                    if published is None:
                        if next_state not in ('HELD','MANUAL_REVIEW','REVIEW'):next_state='PENDING';next_due=time.time()
                        pnl_done=False;data['final_alert_sent']=False
                        if publication_candidate.event_type=='unverified':data['unverified_alert_sent']=False
                    else:
                        data.pop('publication_pending',None);data['publication_committed']=published
                        if publication_candidate.event_type in ('final','revoked'):data['final_alert_sent']=True
                if data.get('publication_pending'):
                    if next_state not in ('HELD','MANUAL_REVIEW','REVIEW'):next_state='PENDING';next_due=time.time()
                    pnl_done=False
                data['accounting_verified']=1 if pnl_done else 0
                data['completion_receipt']=(fill.outcome_receipt if rejected_final else (pnl.financial_receipt if is_close else fill.execution_receipt)) if pnl_done else None
                con.execute("UPDATE vpnl_jobs SET state=?,due=?,data=?,financial_state=?,completed=?,validation_digest=? WHERE id=? AND job_lease=?",(
                    next_state,next_due,json.dumps(data),'VERIFIED_COMPLETE' if pnl_done else 'WAIT_EVIDENCE',now if next_state=='DONE' else None,digest(data) if pnl_done else None,key,lease))
        except Exception as exc:
            warning("fill_verification_failed")
            logger.warning("verified_alerts: verification_failed_safe")
            now = time.time()
            raw_data = data
            if raw_data is None:
                with store._connect() as con:store._quarantine_job(con,key,encoded)
                continue
            if publication_candidate is None and not raw_data.get("publication_pending") and now-created>=settings.FILL_VERIFICATION_TIMEOUT_SEC:
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
                con.execute("UPDATE vpnl_jobs SET state=?,due=?,data=? WHERE id=? AND job_lease=?",(next_st, now+delay, saved_data, key,lease))


def flush_outbox(settings):
    store=NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
    from .intake import restore_preflight
    if not restore_preflight(store)['ready']:
        warning('RESTORE_PREFLIGHT_BLOCKED');return
    store.recover()
    for _ in range(20):
        claim=store.claim_next()
        if claim is None:break
        if not store.begin_post(claim):continue
        sender=DiscordSender(settings.DISCORD_WEBHOOK_URL,retry_on_5xx=False)
        try:
            success,error,delay=sender.send_frozen(claim['wire'])
            store.ack(claim,success,sender.message_id,error,delay)
        except Exception:
            store.ack(claim,False,error='delivery_unknown')
            warning('discord_delivery_unknown')


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


def start_worker(settings, *, execution_bootstrap=None, upbit_observation_host=None):
    if not config_valid(settings): warning("configuration_incomplete"); return None
    if settings.EXCHANGE_NATIVE_TPSL_MONITOR:
        warning("native_tpsl_monitor_not_implemented")
    from .bootstrap import configure_execution_bootstrap,current_bootstrap,execution_readiness
    if execution_bootstrap is not None:
        if not configure_execution_bootstrap(execution_bootstrap):
            warning(execution_readiness()['execution']);return None
    if upbit_observation_host is not None:
        from .upbit_spot_read import configure_upbit_notification_host
        if not configure_upbit_notification_host(upbit_observation_host):warning('UPBIT_OBSERVATION_HOST_REJECTED');return None
    bootstrap=current_bootstrap()
    if bootstrap is None:
        warning('PROVIDER_NOT_CONFIGURED_OBSERVATION_ONLY')
    if bootstrap is not None and not bootstrap.activate():
        warning(execution_readiness()['execution']);return None
    stop=threading.Event()
    def run_session(init_store):
        from .intake import _BOOT_CHECKED
        _BOOT_CHECKED.discard(init_store.db_path)
        def unavailable_client(*args):raise ValueError('PROVIDER_NOT_CONFIGURED')
        factory=bootstrap.client_for if bootstrap is not None else unavailable_client
        # Run cleanup / recovery on startup
        try:
            if not init_store.cutover_ready():
                warning("notification_cutover_required");return
            init_store.recover()
            from .intake import restore_preflight
            if restore_preflight(init_store)['ready'] and hasattr(init_store, "prune_completed"):
                init_store.prune_completed()
        except Exception:
            warning("notification_store_not_ready");return
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
    def run():
        try:
            session_store=NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
            if not session_store.cutover_ready():warning('notification_cutover_required');return
            with session_store.worker_session():run_session(session_store)
        except Exception:warning('notification_store_not_ready')
    thread=threading.Thread(target=run,name="verified-notifications",daemon=True)
    thread.start()
    return stop


def dispatch_order_observation(exchange_name,order_result,order_info,settings,*,producer_entry):
    """Registered POA notification boundary: persists typed observations, no HTTP."""
    from . import estimated_pnl as N
    from .intake import IntakeStore,normalized_request,replay,safe_failure
    from .evidence import account_reference
    if not config_valid(settings) or exchange_name not in SUPPORTED:return dict(accepted=False,status='CONFIGURATION_BLOCKED')
    if exchange_name=='UPBIT' and not getattr(settings,'UPBIT_VERIFIED_ALERTS',False):return dict(accepted=False,status='CONFIGURATION_BLOCKED')
    try:
        token=N.producer_token(producer_entry)
        info={k:getattr(order_info,k,None) for k in INFO_FIELDS}
        info={k:v if isinstance(v,(str,int,float,bool,type(None))) else None for k,v in info.items()}
        for flag in ('is_buy','is_sell','is_close','is_futures'):
            if info.get(flag) is None:info[flag]=False
        raw=order_result if isinstance(order_result,dict) else {}
        oid=raw.get('id') or raw.get('uuid')
        if oid is None:return dispatch_verified_alert(exchange_name,order_result,order_info,settings)
        ref=identity(exchange_name,getattr(settings,exchange_name+'_KEY',None) or 'unconfigured')
        data=dict(info=info,result={'id':str(oid)},source_account_ref=ref,host_binding_digest=None)
        clean,review=normalized_request(data)
        configured=bool(getattr(settings,exchange_name+'_KEY',None)) or account_reference(exchange_name,ref) is not None
        observation=N.capture(exchange_name,raw,clean['info'],ref,configured)
        intake=IntakeStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
        iid=intake.record(data,observation=observation,producer=token)
    except Exception as exc:
        logger.error('observed_alerts: {}',safe_failure(exc));return dict(accepted=False,status='PERSISTENCE_FAILED',code=safe_failure(exc))
    try:replay(settings,only=iid)
    except Exception:warning('INTAKE_REPLAY_DEFERRED')
    return dict(accepted=True,status='DURABLE_ACCEPTED',intake_id=iid)
