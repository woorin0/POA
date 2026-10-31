import sys
from exchange.model import MarketOrder, COST_BASED_ORDER_EXCHANGES, STOCK_EXCHANGES
from exchange.utility import settings
from datetime import datetime, timedelta, timezone
from dhooks import Webhook, Embed
from loguru import logger
from devtools import debug, pformat
import traceback
import os

logger.remove(0)
logger.add(
    "./log/poa.log",
    rotation="1 days",
    retention="7 days",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
)
logger.add(
    sys.stderr,
    colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <level>{message}</level>",
)

hook = None
try:
    url = settings.DISCORD_WEBHOOK_URL.replace("discordapp", "discord")
    hook = Webhook(url)
except Exception:
    print("DISCORD_WEBHOOK_CONFIGURATION_INVALID")


def get_error(e):
    return "ORDER_PROCESSING_FAILED"


KST = timezone(timedelta(hours=9))


def parse_time(utc_timestamp):
    try:
        dt = datetime.fromtimestamp(float(utc_timestamp), tz=timezone.utc).astimezone(KST)
        return dt.strftime("%y-%m-%d %H:%M:%S")
    except Exception:
        timestamp = utc_timestamp + timedelta(hours=9).seconds
        date = datetime.fromtimestamp(timestamp)
        return date.strftime("%y-%m-%d %H:%M:%S")


def format_price(val):
    if val is None:
        return ""
    try:
        val = float(val)
        if val >= 100:
            return f"{val:,.2f}".rstrip("0").rstrip(".")
        elif val >= 1:
            return f"{val:,.4f}".rstrip("0").rstrip(".")
        else:
            return f"{val:,.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(val)


def logger_test():
    date = parse_time(datetime.now(timezone.utc).timestamp())
    logger.info(date)


def log_message(message="None", embed: Embed = None):
    if hook:
        try:
            if embed:
                hook.send(embed=embed)
            else:
                hook.send(message)
        except Exception:
            # Transport exceptions may include the webhook URL/token.
            raise RuntimeError("DISCORD_DELIVERY_FAILED") from None
    else:
        logger.info(message)
        print(message)


def _get_discord_embed_module():
    try:
        import exchange.utility.discord_embed as mod
        return mod
    except ModuleNotFoundError:
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "exchange.utility.discord_embed",
            Path(__file__).resolve().parent / "discord_embed.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["exchange.utility.discord_embed"] = mod
        spec.loader.exec_module(mod)
        return mod


def log_order_message(exchange_name, order_result: dict, order_info: MarketOrder):
    date = parse_time(datetime.now(timezone.utc).timestamp())
    if not order_info.is_futures and order_info.is_buy and exchange_name in COST_BASED_ORDER_EXCHANGES:
        f_name = "비용"
        if order_info.amount is not None:
            if exchange_name == "UPBIT":
                amount = str(order_result.get("cost"))
            elif exchange_name == "BITGET":
                amount = str(order_info.amount * order_info.price)
            elif exchange_name == "BYBIT":
                amount = str(order_result.get("info").get("orderQty"))
        elif order_info.percent is not None:
            f_name = "비율"
            amount = f"{order_info.percent}%"

    else:
        f_name = "수량"
        amount = None
        if exchange_name in ("KRX", "NASDAQ", "AMEX", "NYSE"):
            if order_info.amount is not None:
                amount = str(order_info.amount)
            elif order_info.percent is not None:
                f_name = "비율"
                amount = f"{order_info.percent}%"
        elif order_result.get("amount") is None:
            if order_info.amount is not None:
                if exchange_name == "OKX":
                    if order_info.is_futures:
                        f_name = "계약(수량)"
                        amount = f"{order_info.amount // order_info.contract_size}({order_info.contract_size * (order_info.amount // order_info.contract_size)})"
                    else:
                        amount = f"{order_info.amount}"
                else:
                    amount = str(order_info.amount)
            elif order_info.percent is not None:
                if order_info.amount_by_percent is not None:
                    f_name = "비율(수량)" if order_info.is_contract is None else "비율(계약)"
                    amount = f"{order_info.percent}%({order_info.amount_by_percent})"
                else:
                    f_name = "비율"
                    amount = f"{order_info.percent}%"
        elif order_result.get("amount") is not None:
            if order_info.contract_size is not None:
                f_name = "계약"
                if order_result.get("cost") is not None:
                    f_name = "계약(비용)"
                    amount = f"{order_result.get('amount')}({order_result.get('cost'):.2f})"
                else:
                    amount = f"{order_result.get('amount')}"
            else:
                if order_info.amount is not None:
                    f_name = "수량"
                    amount = f"{order_result.get('amount')}"
                elif order_info.percent is not None:
                    f_name = "비율(수량)" if order_info.is_contract is None else "비율(계약)"
                    amount = f"{order_info.percent}%({order_result.get('amount')})"

    symbol = f"{order_info.base}/{order_info.quote+'.P' if order_info.is_crypto and order_info.is_futures else order_info.quote}"

    side = ""
    if order_info.is_futures:
        if order_info.is_entry:
            if order_info.is_buy:
                side = "롱 진입"
            elif order_info.is_sell:
                side = "숏 진입"
        elif order_info.is_close:
            if order_info.is_buy:
                side = "숏 종료"
            elif order_info.is_sell:
                side = "롱 종료"
    else:
        if order_info.is_buy:
            side = "매수"
        elif order_info.is_sell:
            side = "매도"

    build_legacy_order_embed = _get_discord_embed_module().build_legacy_order_embed

    entry_price = None
    close_price = None
    if order_result and isinstance(order_result, dict):
        entry_price = order_result.get("entry_price")
        close_price = order_result.get("close_price") or order_result.get("price") or order_result.get("average")

    account_label = f"{order_info.kis_number}번째 계좌" if exchange_name in STOCK_EXCHANGES else None
    target_symbol = order_info.base if exchange_name in STOCK_EXCHANGES else symbol
    content, embed = build_legacy_order_embed(
        exchange_name=exchange_name,
        symbol=target_symbol,
        side=side,
        order_name=order_info.order_name,
        amount_label=f_name,
        amount_value=amount,
        leverage=order_info.leverage,
        entry_price=entry_price,
        close_price=close_price,
        account_label=account_label,
        timestamp=0.0 if date == "fixed-time" else None,
    )
    from exchange.verified_pnl.alert_presentation import legacy
    embed=legacy(embed,exchange_name,order_result,order_info,settings)
    if order_result is None:
        from exchange.verified_pnl.operational_alerts import report
        report(settings,'ORDER_RESULT_UNCONFIRMED',phase='ORDER',info=order_info,only_if_no_active=True)
    log_message(content, embed)


def log_hedge_message(exchange, base, quote, exchange_amount, upbit_amount, hedge):
    date = parse_time(datetime.now(timezone.utc).timestamp())
    hedge_type = "헷지" if hedge == "ON" else "헷지 종료"
    content = f"{hedge_type}: {base} ==> {exchange}:{exchange_amount} UPBIT:{upbit_amount}"
    embed = Embed(title="헷지", description=content, color=0x0000FF)
    embed.add_field(name="일시", value=str(date), inline=False)
    embed.add_field(name="거래소", value=f"{exchange}-UPBIT", inline=False)
    embed.add_field(name="심볼", value=f"{base}/{quote}-{base}/KRW", inline=False)
    embed.add_field(name="거래유형", value=hedge_type, inline=False)
    embed.add_field(
        name="수량",
        value=f"{exchange}:{exchange_amount} UPBIT:{upbit_amount}",
        inline=False,
    )
    log_message(content, embed)


def log_error_message(error,name):
    from exchange.verified_pnl.operational_alerts import report
    report(settings,'ORDER_PROCESSING_FAILED',phase='ORDER')
    logger.error('REQUEST_PROCESSING_FAILED')


def log_order_error_message(error: str | Exception,order_info: MarketOrder):
    from exchange.verified_pnl.operational_alerts import order_failure
    order_failure(settings,error,order_info)
    logger.error('ORDER_PROCESSING_FAILED')


def log_validation_error_message(msg):
    from exchange.verified_pnl.operational_alerts import report
    report(settings,'ORDER_PARAMETERS_ERROR',phase='ORDER')
    logger.error("REQUEST_VALIDATION_FAILED")


def print_alert_message(order_info: MarketOrder, result="성공"):
    if result == "성공":
        logger.info("WEBHOOK_ORDER_SUCCESS")
    else:
        logger.error("WEBHOOK_ORDER_FAILED")


def log_alert_message(order_info: MarketOrder,result="성공"):
    if result!="성공":
        from exchange.verified_pnl.operational_alerts import report
        report(settings,'ORDER_PROCESSING_FAILED',phase='ORDER',info=order_info,only_if_no_active=True)
        print_alert_message(order_info,result)
        return
    from exchange.verified_pnl.alert_presentation import PENDING
    embed=Embed(title="웹훅 처리 결과",description="WEBHOOK_ORDER_SUCCESS",color=PENDING)
    log_message(embed=embed)
    print_alert_message(order_info,result)
