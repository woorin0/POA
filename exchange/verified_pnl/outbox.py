import json
import math
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List

import requests
try:
    from dhooks import Embed
    if not hasattr(Embed, "data"):
        try:
            Embed.data = property(lambda self: self.to_dict())
        except Exception:
            pass
except ImportError:
    class Embed:
        def __init__(self, **kw):
            self.data = dict(kw)
            self.data.setdefault("fields", [])

        def add_field(self, **kw):
            self.data.setdefault("fields", []).append(kw)

from loguru import logger

from .models import (
    NotificationPayload,
    OutboxStatus,
    ExitReason,
    PnlResult,
    FillStatus,
    VerificationStatus,
)

KST = timezone(timedelta(hours=9))


def format_kst(utc_timestamp: Optional[float]) -> str:
    if utc_timestamp is None:
        return "미확인"
    try:
        dt = datetime.fromtimestamp(float(utc_timestamp), tz=timezone.utc).astimezone(KST)
        return dt.strftime("%y-%m-%d %H:%M:%S")
    except Exception:
        return str(utc_timestamp)


def format_decimal(val: Optional[Any], decimals: int = 4) -> str:
    if val is None or val == "":
        return "-"
    try:
        d = Decimal(str(val))
        if not d.is_finite():
            return "-"
        formatted = f"{d:,f}"
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
    except Exception:
        return str(val)


_INITIALIZED_DBS = set()


class NotificationOutboxStore:
    """Persistent SQLite store for Discord notification outbox."""

    def __init__(self, db_path: str = "notifications.sqlite3"):
        self.db_path = str(Path(db_path).resolve())
        if Path(self.db_path).name in {"store.db","data.db","logs.db","safe_orders.sqlite3"}:
            raise ValueError("Use a separate notification database")
        if self.db_path not in _INITIALIZED_DBS:
            if Path(self.db_path).exists():
                from contextlib import closing
                with closing(sqlite3.connect(self.db_path)) as con:
                    exists=con.execute("SELECT 1 FROM sqlite_master WHERE name='verified_jobs'").fetchone()
                    if not exists:
                        import uuid
                        with closing(sqlite3.connect(self.db_path+".pre-v2-"+uuid.uuid4().hex+".bak")) as dst:
                            con.backup(dst)
            self._init_db()
            _INITIALIZED_DBS.add(self.db_path)

    @contextmanager
    def _connect(self, timeout: float = 5.0):
        # Bound lock contention on the request-side local enqueue path with busy handler.
        con = sqlite3.connect(self.db_path, timeout=timeout)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @contextmanager
    def _connect_read(self, timeout: float = 5.0):
        # Non-exclusive read-only connection in WAL mode does not block writers.
        con = sqlite3.connect(self.db_path, timeout=timeout)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            yield con
        finally:
            con.close()

    def _init_db(self):
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS verified_outbox (
                    event_id TEXT PRIMARY KEY,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    order_id TEXT,
                    client_order_id TEXT,
                    event_type TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    discord_message_id TEXT
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status ON verified_outbox(status, next_retry_at)")
            con.execute("CREATE TABLE IF NOT EXISTS verified_jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL, state TEXT NOT NULL, due REAL NOT NULL, created REAL NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS verified_trades (id TEXT PRIMARY KEY, scope TEXT NOT NULL, data TEXT NOT NULL)")

    def enqueue(self, payload: NotificationPayload, max_attempts: int = 5) -> bool:
        """Enqueues a new notification payload. Returns True if inserted, False if duplicate."""
        now = time.time()
        with self._connect() as con:
            existing = con.execute("SELECT 1 FROM verified_outbox WHERE event_id = ?", (payload.event_id,)).fetchone()
            if existing:
                logger.info("verified_alerts: duplicate_event_blocked")
                return False

            con.execute(
                """
                INSERT INTO verified_outbox (
                    event_id, exchange, symbol, order_id, client_order_id,
                    event_type, version, payload, status, attempts,
                    max_attempts, next_retry_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    payload.event_id,
                    payload.exchange,
                    payload.symbol,
                    payload.order_id,
                    payload.client_order_id,
                    payload.event_type,
                    payload.event_version,
                    payload.json(),
                    OutboxStatus.PENDING.value,
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
        return True

    def get_pending(self, limit: int = 10) -> List[Dict[str, Any]]:
        now = time.time()
        with self._connect_read() as con:
            rows = con.execute(
                """
                SELECT event_id, payload, attempts, max_attempts, next_retry_at
                FROM verified_outbox
                WHERE status IN (?, ?) AND next_retry_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (OutboxStatus.PENDING.value, OutboxStatus.PENDING.value, now, limit),
            ).fetchall()

            return [
                {
                    "event_id": r[0],
                    "payload": json.loads(r[1]),
                    "attempts": r[2],
                    "max_attempts": r[3],
                    "next_retry_at": r[4],
                }
                for r in rows
            ]

    def prune_completed(self, max_age_seconds: float = 7 * 86400) -> int:
        cutoff = time.time() - max_age_seconds
        with self._connect() as con:
            deleted_outbox = con.execute(
                "DELETE FROM verified_outbox WHERE status IN ('SENT', 'FAILED') AND updated_at < ?",
                (cutoff,),
            ).rowcount
            deleted_jobs = con.execute(
                "DELETE FROM verified_jobs WHERE state IN ('DONE') AND due < ?",
                (cutoff,),
            ).rowcount
            return deleted_outbox + deleted_jobs

    def claim(self, event_id):
        with self._connect() as con:
            return con.execute("UPDATE verified_outbox SET status='SENDING',updated_at=? WHERE event_id=? AND status='PENDING' AND next_retry_at<=?",
                               (time.time(),event_id,time.time())).rowcount == 1

    def hold_unknown(self, event_id):
        with self._connect() as con:
            con.execute("UPDATE verified_outbox SET status='UNKNOWN',last_error='delivery_unknown',updated_at=? WHERE event_id=?",
                        (time.time(),event_id))

    def recover(self):
        with self._connect() as con:
            # Bounded network timeout is 5s; never retry an abandoned send.
            con.execute("UPDATE verified_outbox SET status='UNKNOWN',last_error='abandoned_send' WHERE status='SENDING' AND updated_at<?", (time.time()-30,))
            con.execute("UPDATE verified_jobs SET state='PENDING' WHERE state='VERIFYING' AND due<?", (time.time(),))

    def mark_sent(self, event_id: str, message_id: Optional[str] = None):
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                UPDATE verified_outbox
                SET status = ?, updated_at = ?, discord_message_id = ?
                WHERE event_id = ?
                """,
                (OutboxStatus.SENT.value, now, message_id, event_id),
            )

    def mark_failed(self, event_id: str, error_msg: str, terminal: bool = False, delay: float = 5.0):
        now = time.time()
        with self._connect() as con:
            row = con.execute("SELECT attempts, max_attempts FROM verified_outbox WHERE event_id = ?", (event_id,)).fetchone()
            if not row:
                return
            attempts = row[0] + 1
            max_attempts = row[1]
            new_status = OutboxStatus.FAILED.value if (terminal or attempts >= max_attempts) else OutboxStatus.PENDING.value
            next_retry = 0 if new_status == OutboxStatus.FAILED.value else now + delay

            con.execute(
                """
                UPDATE verified_outbox
                SET status = ?, attempts = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (new_status, attempts, next_retry, str(error_msg)[:500], now, event_id),
            )


def build_verified_embed(payload: NotificationPayload) -> Embed:
    """Builds a rich, color-coded Discord Embed for verified order execution."""
    # Determine color
    if payload.error_message or payload.fill_status == FillStatus.FAILED:
        color = 0xFF0000  # Red
    elif payload.pnl_result == PnlResult.PROFIT:
        color = 0x00FF00  # Green
    elif payload.pnl_result == PnlResult.LOSS:
        color = 0xFF0000  # Red
    elif payload.pnl_result == PnlResult.BREAKEVEN:
        color = 0x95A5A6  # Gray
    else:
        color = 0x0000FF  # Blue (Pending/Unknown)

    # Title & Description
    status_suffix = ""
    if payload.fill_status == FillStatus.FILLED:
        status_suffix = "완료"
    elif payload.fill_status == FillStatus.PARTIALLY_FILLED:
        status_suffix = "부분 체결"
    elif payload.fill_status == FillStatus.PENDING:
        status_suffix = "접수 완료 (체결 확인 중)"
    elif payload.fill_status == FillStatus.FAILED:
        status_suffix = "실패"

    if payload.event_type == "manual_review":
        title = f"[⚠️ 수동 확인 필요] [{payload.symbol} {payload.side}]"
    else:
        title = f"[{payload.symbol} {payload.side} {status_suffix}]"
    if payload.order_name and payload.order_name not in ("주문", "Order"):
        title = f"[{payload.order_name}] {title}"

    embed = Embed(
        title=title[:256],
        description=f"주문 상태: {payload.exchange} {payload.symbol} {payload.side} {payload.fill_status.value}",
        color=color,
    )

    # Date / Time
    display_time = format_kst(payload.fill_timestamp if payload.fill_timestamp is not None else payload.event_timestamp)
    time_label = "체결일시" if payload.fill_timestamp is not None else "일시"
    embed.add_field(name=time_label, value=display_time, inline=False)
    if payload.leverage is not None:
        embed.add_field(name="레버리지 (주문 설정)", value=str(payload.leverage), inline=True)

    embed.add_field(name="거래소", value=payload.exchange, inline=True)
    embed.add_field(name="심볼", value=payload.symbol, inline=True)
    embed.add_field(name="체결 상태", value=payload.fill_status.value, inline=True)

    if payload.exit_reason != ExitReason.UNKNOWN:
        embed.add_field(name="청산 원인", value=payload.exit_reason.value, inline=True)
    embed.add_field(name="손익 결과", value=payload.pnl_result.value, inline=True)
    if payload.net_pnl is None:
        embed.add_field(name="순실현손익", value="미확정", inline=True)
    if payload.exit_price is None:
        embed.add_field(name="평균 체결가격", value="미확인 / 확인 중", inline=True)
    if payload.fee_breakdown:
        embed.add_field(name="확인된 수수료 (통화별)", value=payload.fee_breakdown, inline=False)

    # Quantity
    if payload.filled_qty:
        embed.add_field(name="실제 체결 수량", value=payload.filled_qty, inline=True)
    if payload.remaining_qty and payload.remaining_qty != "0":
        embed.add_field(name="미체결 수량", value=payload.remaining_qty, inline=True)

    # Prices
    if payload.entry_price:
        embed.add_field(name="평균 진입가", value=payload.entry_price, inline=True)
    if payload.exit_price:
        embed.add_field(name="실제 평균 체결가", value=payload.exit_price, inline=True)

    # PnL Details
    if payload.gross_pnl:
        embed.add_field(name="총실현손익", value=payload.gross_pnl, inline=True)
    if payload.entry_fee_allocated:
        embed.add_field(name="진입 수수료 배분액", value=payload.entry_fee_allocated, inline=True)
    if payload.exit_fee:
        embed.add_field(name="청산 수수료", value=payload.exit_fee, inline=True)
    if payload.funding_pnl:
        embed.add_field(name="펀딩비", value=payload.funding_pnl, inline=True)
    if payload.net_pnl:
        embed.add_field(name="순실현손익", value=payload.net_pnl, inline=True)

    # Return & ROI
    if payload.price_return_pct:
        embed.add_field(name="가격 수익률", value=f"{payload.price_return_pct}%", inline=True)
    if payload.roi_pct:
        embed.add_field(name=payload.roi_label, value=f"{payload.roi_pct}%", inline=True)

    if getattr(payload, "signal_id", None):
        embed.add_field(name="신호 ID", value=str(payload.signal_id), inline=True)

    if payload.event_type == "manual_review" or "MANUAL_REVIEW" in (payload.error_message or ""):
        embed.add_field(name="수동 확인 필요 여부", value="필요 (운영자 확인 요망)", inline=False)

    if payload.order_id:
        embed.add_field(name="주문 ID", value=payload.order_id, inline=True)

    embed.add_field(name="정보 확인", value=payload.verification_source, inline=False)

    if payload.error_message:
        embed.add_field(name="오류 상세", value=payload.error_message[:500], inline=False)

    return embed


class DiscordSender:
    """Dispatches notifications to Discord with exponential backoff and rate-limit handling."""

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = (webhook_url or "").replace("discordapp", "discord")
        self.message_id = None

    def send_payload(self, payload: NotificationPayload) -> tuple[bool, Optional[str], Optional[float]]:
        """Sends payload to Discord.
        Returns: (success: bool, error_message: Optional[str], retry_delay: Optional[float])
        """
        url = urlparse(self.webhook_url)
        if url.scheme != "https" or url.hostname not in {"discord.com", "canary.discord.com", "ptb.discord.com"} or not url.path.startswith("/api/webhooks/"):
            return False, "유효한 DISCORD_WEBHOOK_URL이 설정되지 않았습니다", None

        embed = build_verified_embed(payload)
        return self.send_embed(embed)

    def send_embed(self, embed: Embed) -> tuple[bool, Optional[str], Optional[float]]:
        if not self.webhook_url:
            return False, "No webhook URL configured", None

        data = {"embeds": [embed.data], "allowed_mentions": {"parse": []}}
        try:
            resp = requests.post(self.webhook_url, params={"wait":"true"}, json=data, timeout=5, allow_redirects=False)
            if resp.status_code in (200, 204):
                try:
                    body = resp.json()
                    self.message_id = str(body["id"]) if isinstance(body,dict) and body.get("id") else None
                except Exception:
                    pass
                return True, None, None

            if resp.status_code == 429:
                retry_after = 5.0
                try:
                    header = resp.headers.get("Retry-After")
                    retry_after = float(header) if isinstance(header,str) else float(resp.json().get("retry_after", 5.0))
                    if not math.isfinite(retry_after) or retry_after < 0: retry_after = 5.0
                except Exception:
                    pass
                return False, f"Rate limited (429), retry after {retry_after}s", retry_after

            if 400 <= resp.status_code < 500:
                # Client permanent error (e.g. 401 unauthorized, 404 not found)
                return False, f"permanent_http_{resp.status_code}", None

            # 5xx Server error
            return False, f"Discord server error HTTP {resp.status_code}", 5.0

        except requests.exceptions.Timeout:
            return False, "delivery_unknown", None
        except requests.exceptions.RequestException as e:
            return False, "delivery_unknown", None
