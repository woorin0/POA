from fastapi.exception_handlers import (
    request_validation_exception_handler,
)
from pprint import pprint
from fastapi import FastAPI, Request, status, BackgroundTasks
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
import httpx
from exchange.stock.kis import KoreaInvestment
from exchange.model import MarketOrder, PriceRequest, HedgeData, OrderRequest
from exchange.utility import (
    settings,
    log_order_message,
    log_alert_message,
    print_alert_message,
    logger_test,
    log_order_error_message,
    log_validation_error_message,
    log_hedge_message,
    log_error_message,
    log_message,
)
import traceback
from exchange import get_exchange, log_message, db, settings, get_bot, pocket
import ipaddress
import os
import sys
import logging
from devtools import debug

VERSION = "0.1.7"
app = FastAPI(default_response_class=ORJSONResponse)


class _AccessMetadataOnly(logging.Filter):
    """Keep Uvicorn's access formatter compatible without logging URL queries."""
    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            _, method, target, version, code = args
            path = target.partition("?")[0] if isinstance(target, str) else ""
            record.msg = '%s - "%s %s HTTP/%s" %d'
            record.args = (
                "client", method if method in ("GET", "POST", "HEAD", "OPTIONS") else "OTHER",
                path if path in ("/", "/order", "/price", "/hedge", "/hi", "/ip") else "/[omitted]",
                version if version in ("1.0", "1.1", "2") else "unknown",
                code if type(code) is int else 0,
            )
        else:
            record.msg, record.args = "HTTP_ACCESS", ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


_access_logger = logging.getLogger("uvicorn.access")
if not any(f.name == "poa_access_metadata" for f in _access_logger.filters):
    _access_logger.addFilter(_AccessMetadataOnly("poa_access_metadata"))


def get_error(e):
    # Exception messages, source lines and locals can contain credentials.
    return ["ORDER_PROCESSING_FAILED"]


@app.on_event("startup")
async def startup():
    if settings.SAFE_RETRY_ENABLED:
        import asyncio
        from exchange.safe_retry import get_manager, reconciliation_loop
        get_manager(settings)
        # Non-blocking startup: background reconciliation handles initial recovery and periodic ticks without blocking event loop
        app.state.safe_reconciliation = asyncio.create_task(reconciliation_loop(settings))
    if getattr(settings, "VERIFIED_DISCORD_ALERTS", False) and getattr(settings, "DISCORD_OUTBOX_ENABLED", False):
        try:
            from exchange.verified_pnl.dispatcher import start_worker
            from exchange.verified_pnl.upbit_notification_bootstrap import prepare_upbit_notification_host
            from exchange.verified_pnl.upbit_notification_local import configure_local_upbit_sources
            local_status = configure_local_upbit_sources()
            if local_status == 'LOCAL_SOURCES_UNAVAILABLE':
                from loguru import logger
                logger.warning('upbit_notification_read: LOCAL_SOURCES_UNAVAILABLE')
            read_host = prepare_upbit_notification_host()
            if read_host.status not in ('DISABLED', 'HOST_CONFIGURED_EXTERNAL_EVIDENCE_REQUIRED'):
                from loguru import logger
                logger.warning('upbit_notification_read: {}', read_host.status)
            if read_host.host is None:
                app.state.verified_notifications_stop = start_worker(settings)
            else:
                app.state.verified_notifications_stop = start_worker(settings, upbit_observation_host=read_host.host)
        except Exception:
            pass
    log_message(f"POABOT 실행 완료! - 버전:{VERSION}")


@app.on_event("shutdown")
async def shutdown():
    import asyncio
    notification_stop = getattr(app.state, "verified_notifications_stop", None)
    if notification_stop is not None:
        notification_stop.set()

    if settings.SAFE_RETRY_ENABLED:
        try:
            from exchange.safe_retry import begin_shutdown
            # 1. 신규 복구 작업 접수 즉시 차단
            begin_shutdown()
        except Exception:
            pass

    # 2. 복구 루프 태스크 취소 및 정리
    task = getattr(app.state, "safe_reconciliation", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # 3. 진행 중인 동기식 CCXT 조회 완료 확인 (이벤트 루프 차단 방지를 위해 to_thread로 대기)
    if settings.SAFE_RETRY_ENABLED:
        try:
            from exchange.safe_retry import wait_for_reconciliation_drained, close_reconciliation_clients
            drained = await asyncio.to_thread(wait_for_reconciliation_drained, 5.0)
            if not drained:
                from loguru import logger
                logger.warning("safe_retry: drain timed out; active queries will be finalized via safe deferred cleanup")
            # 4. 조회 완료된 클라이언트는 즉시 닫고, 진행 중인 클라이언트는 지연 정리로 안전 종결 (강제 종료 없음)
            await asyncio.to_thread(close_reconciliation_clients, 1.0)
        except Exception:
            pass

    db.close()


whitelist = [
    "52.89.214.238",
    "34.212.75.30",
    "54.218.53.128",
    "52.32.178.7",
    "127.0.0.1",
]
whitelist = whitelist + settings.WHITELIST


# @app.middleware("http")
# async def add_process_time_header(request: Request, call_next):
#     start_time = time.perf_counter()
#     response = await call_next(request)
#     process_time = time.perf_counter() - start_time
#     response.headers["X-Process-Time"] = str(process_time)
#     return response


@app.middleware("http")
async def whitelist_middleware(request: Request, call_next):
    try:
        if (
            request.client.host not in whitelist
            and not ipaddress.ip_address(request.client.host).is_private
        ):
            msg = f"{request.client.host}는 안됩니다"
            print(msg)
            return ORJSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content=f"{request.client.host}는 허용되지 않습니다",
            )
    except:
        log_error_message("REQUEST_PROCESSING_FAILED", "미들웨어 에러")
    else:
        try:
            return await call_next(request)
        except Exception:
            # Do not propagate request/validation exception text to ASGI logs.
            log_error_message("REQUEST_PROCESSING_FAILED", "요청 에러")
            return ORJSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    # Pydantic ctx.given and JSON decode ctx.doc can echo the entire request.
    # Preserve the 422 detail envelope, but build it from trusted constants.
    messages = {
        "value_error.missing": "field required",
        "type_error.float": "value is not a valid float",
        "type_error.integer": "value is not a valid integer",
        "type_error.str": "str type expected",
        "value_error.jsondecode": "Invalid JSON",
    }
    fields = set(MarketOrder.__fields__) | set(PriceRequest.__fields__) | set(HedgeData.__fields__)
    details = []
    for error in exc.errors():
        location = error.get("loc", ())
        loc = [part for part in location if type(part) is int or
               (type(part) is str and part in fields | {"body", "__root__"})]
        code = error.get("type")
        detail = {"loc": loc or ["body"], "msg": messages.get(code, "Invalid request"),
                  "type": code if code in messages else "value_error"}
        if location == ("body", "type") and code == "value_error.const":
            # Historical type=limit response is a public, non-secret constant.
            given = error.get("ctx", {}).get("given")
            if given == "limit":
                detail.update(msg="unexpected value; permitted: 'market'",
                              type="value_error.const",
                              ctx={"given": "limit", "permitted": ["market"]})
        details.append(detail)
    log_validation_error_message("REQUEST_VALIDATION_FAILED")
    return ORJSONResponse(status_code=422, content={"detail": details})


@app.get("/ip")
async def get_ip():
    data = httpx.get("https://ipv4.jsonip.com").json()["ip"]
    log_message(data)


@app.get("/hi")
async def welcome():
    return "hi!!"


@app.post("/price")
async def price(price_req: PriceRequest, background_tasks: BackgroundTasks):
    exchange = get_exchange(price_req.exchange)
    price = exchange.dict()[price_req.exchange].fetch_price(
        price_req.base, price_req.quote
    )
    return price


def is_verified_exchange(exchange_name):
    if exchange_name in ("BINANCE", "BYBIT", "OKX"):
        return True
    if exchange_name == "UPBIT" and getattr(settings, "UPBIT_VERIFIED_ALERTS", False):
        return True
    return False


def log(exchange_name, result, order_info):
    if getattr(settings, "VERIFIED_DISCORD_ALERTS", False) and is_verified_exchange(exchange_name):
        if not getattr(order_info,'is_futures',False) and exchange_name in {'BINANCE','BYBIT','OKX','UPBIT'}:
            from exchange.verified_pnl.dispatcher import dispatch_order_observation
            return dispatch_order_observation(exchange_name,result,order_info,settings,producer_entry=log)
        from exchange.verified_pnl import dispatch_verified_alert
        dispatch_verified_alert(exchange_name, result, order_info, settings)
    else:
        log_order_message(exchange_name, result, order_info)
        print_alert_message(order_info)


def log_error(error_message, order_info):
    if getattr(settings, "VERIFIED_DISCORD_ALERTS", False) and is_verified_exchange(order_info.exchange):
        from exchange.verified_pnl import dispatch_verified_error_alert
        dispatch_verified_error_alert(error_message, order_info, settings)
    else:
        log_order_error_message(error_message, order_info)
        log_alert_message(order_info, "실패")


def schedule_order_log(background_tasks, exchange_name, result, order_info):
    if settings.VERIFIED_DISCORD_ALERTS and is_verified_exchange(exchange_name):
        # Persist identifiers before the HTTP response. No network call here.
        # Snapshotting also avoids another request mutating the cached bot state.
        try:
            log(exchange_name, result, order_info.copy(deep=True))
        except Exception:
            from loguru import logger
            logger.warning("verified_alerts: job_enqueue_failed")
    else:
        background_tasks.add_task(log, exchange_name, result, order_info)


@app.post("/order")
@app.post("/")
async def order(order_info: MarketOrder, background_tasks: BackgroundTasks):
    order_result = None
    try:
        exchange_name = order_info.exchange
        if settings.SAFE_RETRY_ENABLED:
            from exchange.safe_retry import get_manager, CLIENT_KEYS, SafetyError
            get_manager(settings)
            if exchange_name == "UPBIT":
                if not getattr(settings, "UPBIT_SAFE_RETRY_ENABLED", False):
                    raise SafetyError("UPBIT safe mode is disabled. Set UPBIT_SAFE_RETRY_ENABLED=True to enable.")
            elif exchange_name not in CLIENT_KEYS:
                raise SafetyError("This exchange is not supported by safe mode")
        bot = get_bot(exchange_name, order_info.kis_number)
        bot.init_info(order_info)

        if bot.order_info.is_crypto:
            if bot.order_info.is_entry:
                order_result = bot.market_entry(bot.order_info)
            elif bot.order_info.is_close:
                order_result = bot.market_close(bot.order_info)
            elif bot.order_info.is_buy:
                order_result = bot.market_buy(bot.order_info)
            elif bot.order_info.is_sell:
                order_result = bot.market_sell(bot.order_info)
            schedule_order_log(background_tasks, exchange_name, order_result, order_info)
        elif bot.order_info.is_stock:
            order_result = bot.create_order(
                bot.order_info.exchange,
                bot.order_info.base,
                order_info.type.lower(),
                order_info.side.lower(),
                order_info.amount,
            )
            schedule_order_log(background_tasks, exchange_name, order_result, order_info)

    except TypeError as e:
        error_msg = get_error(e)
        if settings.VERIFIED_DISCORD_ALERTS and is_verified_exchange(order_info.exchange):
            schedule_order_log(background_tasks, order_info.exchange, None, order_info)
        else:
            background_tasks.add_task(log_order_error_message, "\n".join(error_msg), order_info)

    except Exception as e:
        error_msg = get_error(e)
        if settings.VERIFIED_DISCORD_ALERTS and is_verified_exchange(order_info.exchange):
            schedule_order_log(background_tasks, order_info.exchange, None, order_info)
        else:
            background_tasks.add_task(log_error, "\n".join(error_msg), order_info)

    else:
        return {"result": "success"}

    finally:
        pass


def get_hedge_records(base):
    records = pocket.get_full_list("kimp", query_params={"filter": f'base = "{base}"'})
    binance_amount = 0.0
    binance_records_id = []
    upbit_amount = 0.0
    upbit_records_id = []
    for record in records:
        if record.exchange == "BINANCE":
            binance_amount += record.amount
            binance_records_id.append(record.id)
        elif record.exchange == "UPBIT":
            upbit_amount += record.amount
            upbit_records_id.append(record.id)

    return {
        "BINANCE": {"amount": binance_amount, "records_id": binance_records_id},
        "UPBIT": {"amount": upbit_amount, "records_id": upbit_records_id},
    }


@app.post("/hedge")
async def hedge(hedge_data: HedgeData, background_tasks: BackgroundTasks):
    if settings.SAFE_RETRY_ENABLED:
        # Hedge submits multiple legs outside the single-order lifecycle.
        return {"result": "error"}
    exchange_name = hedge_data.exchange.upper()
    bot = get_bot(exchange_name)
    upbit = get_bot("UPBIT")

    base = hedge_data.base
    quote = hedge_data.quote
    amount = hedge_data.amount
    leverage = hedge_data.leverage
    hedge = hedge_data.hedge

    foreign_order_info = OrderRequest(
        exchange=exchange_name,
        base=base,
        quote=quote,
        side="entry/sell",
        type="market",
        amount=amount,
        leverage=leverage,
    )
    bot.init_info(foreign_order_info)
    if hedge == "ON":
        try:
            if amount is None:
                raise Exception("헷지할 수량을 요청하세요")
            binance_order_result = bot.market_entry(foreign_order_info)
            binance_order_amount = binance_order_result["amount"]
            pocket.create(
                "kimp",
                {
                    "exchange": "BINANCE",
                    "base": base,
                    "quote": quote,
                    "amount": binance_order_amount,
                },
            )
            if leverage is None:
                leverage = 1
            try:
                korea_order_info = OrderRequest(
                    exchange="UPBIT",
                    base=base,
                    quote="KRW",
                    side="buy",
                    type="market",
                    amount=binance_order_amount,
                )
                upbit.init_info(korea_order_info)
                upbit_order_result = upbit.market_buy(korea_order_info)
            except Exception as e:
                hedge_records = get_hedge_records(base)
                binance_records_id = hedge_records["BINANCE"]["records_id"]
                binance_amount = hedge_records["BINANCE"]["amount"]
                binance_order_result = bot.market_close(
                    OrderRequest(
                        exchange=exchange_name,
                        base=base,
                        quote=quote,
                        side="close/buy",
                        amount=binance_amount,
                    )
                )
                for binance_record_id in binance_records_id:
                    pocket.delete("kimp", binance_record_id)
                log_message(
                    "[헷지 실패] 업비트에서 에러가 발생하여 바이낸스 포지션을 종료합니다"
                )
            else:
                upbit_order_info = upbit.get_order(upbit_order_result["id"])
                upbit_order_amount = upbit_order_info["filled"]
                pocket.create(
                    "kimp",
                    {
                        "exchange": "UPBIT",
                        "base": base,
                        "quote": "KRW",
                        "amount": upbit_order_amount,
                    },
                )
                log_hedge_message(
                    exchange_name,
                    base,
                    quote,
                    binance_order_amount,
                    upbit_order_amount,
                    hedge,
                )

        except Exception as e:
            # log_message(f"{e}")
            background_tasks.add_task(
                log_error_message, "ORDER_PROCESSING_FAILED", "헷지 에러"
            )
            return {"result": "error"}
        else:
            return {"result": "success"}

    elif hedge == "OFF":
        try:
            records = pocket.get_full_list(
                "kimp", query_params={"filter": f'base = "{base}"'}
            )
            binance_amount = 0.0
            binance_records_id = []
            upbit_amount = 0.0
            upbit_records_id = []
            for record in records:
                if record.exchange == "BINANCE":
                    binance_amount += record.amount
                    binance_records_id.append(record.id)
                elif record.exchange == "UPBIT":
                    upbit_amount += record.amount
                    upbit_records_id.append(record.id)

            if binance_amount > 0 and upbit_amount > 0:
                # 바이낸스
                order_info = OrderRequest(
                    exchange="BINANCE",
                    base=base,
                    quote=quote,
                    side="close/buy",
                    amount=binance_amount,
                )
                binance_order_result = bot.market_close(order_info)
                for binance_record_id in binance_records_id:
                    pocket.delete("kimp", binance_record_id)
                # 업비트
                order_info = OrderRequest(
                    exchange="UPBIT",
                    base=base,
                    quote="KRW",
                    side="sell",
                    amount=upbit_amount,
                )
                upbit_order_result = upbit.market_sell(order_info)
                for upbit_record_id in upbit_records_id:
                    pocket.delete("kimp", upbit_record_id)

                log_hedge_message(
                    exchange_name, base, quote, binance_amount, upbit_amount, hedge
                )
            elif binance_amount == 0 and upbit_amount == 0:
                log_message(f"{exchange_name}, UPBIT에 종료할 수량이 없습니다")
            elif binance_amount == 0:
                log_message(f"{exchange_name}에 종료할 수량이 없습니다")
            elif upbit_amount == 0:
                log_message("UPBIT에 종료할 수량이 없습니다")
        except Exception as e:
            background_tasks.add_task(
                log_error_message, "ORDER_PROCESSING_FAILED", "헷지종료 에러"
            )
            return {"result": "error"}
        else:
            return {"result": "success"}
