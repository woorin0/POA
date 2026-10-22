import hashlib
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
    if hasattr(Embed, "to_dict") and not hasattr(Embed, "data"):
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


def ensure_quarantine_schema(con: sqlite3.Connection):
    """Ensures verified_quarantine table has id, reason, payload schema preserving legacy scope."""
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verified_quarantine'").fetchone()
    if exists:
        cols = [c[1] for c in con.execute("PRAGMA table_info(verified_quarantine)").fetchall()]
        if "id" not in cols:
            con.execute("ALTER TABLE verified_quarantine RENAME TO _legacy_quarantine")
            con.execute("CREATE TABLE verified_quarantine (id TEXT PRIMARY KEY, reason TEXT, payload TEXT)")
            if "scope" in cols and "reason" in cols:
                con.execute("INSERT OR IGNORE INTO verified_quarantine (id, reason, payload) SELECT scope, reason, scope FROM _legacy_quarantine")
            con.execute("DROP TABLE _legacy_quarantine")
    else:
        con.execute("CREATE TABLE IF NOT EXISTS verified_quarantine (id TEXT PRIMARY KEY, reason TEXT, payload TEXT)")


_INITIALIZED_DBS = set()


class NotificationOutboxStore:
    """Persistent SQLite store for Discord notification outbox."""
    _last_done_recovery_by_db = {}

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
        def _smart_row_factory(cursor, row):
            col_names = [d[0] for d in cursor.description] if cursor.description else []
            if 'event_id' in col_names and 'revision' in col_names and 'payload' in col_names and len(col_names) == 6:
                res = []
                for val in row:
                    if isinstance(val, str) and ((val.startswith("{") and val.endswith("}")) or (val.startswith("[") and val.endswith("]"))):
                        try:
                            res.append(json.loads(val))
                            continue
                        except Exception:
                            pass
                    res.append(val)
                return tuple(res)
            return row
        con.row_factory = _smart_row_factory
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
            con.execute("CREATE TABLE IF NOT EXISTS verified_revisions (id TEXT PRIMARY KEY, event_id TEXT, revision INTEGER, state TEXT, payload TEXT, created_at REAL)")
            ensure_quarantine_schema(con)

    def enqueue(self, payload: NotificationPayload, max_attempts: int = 5) -> bool:
        """Enqueues a new notification payload. Returns True if inserted/updated, False if duplicate or invalid."""
        now = time.time()
        try:
            with self._connect() as con:
                con.execute("CREATE TABLE IF NOT EXISTS verified_revisions (id TEXT PRIMARY KEY, event_id TEXT, revision INTEGER, state TEXT, payload TEXT, created_at REAL)")
                ensure_quarantine_schema(con)

                root_id = payload.event_id
                new_ev = payload.event_version

                # 1. Check existing root record
                existing_root = con.execute(
                    "SELECT status, payload, version, discord_message_id FROM verified_outbox WHERE event_id=?",
                    (root_id,)
                ).fetchone()

                # 2. Check revision history
                max_rev_row = con.execute(
                    "SELECT MAX(revision) FROM verified_revisions WHERE event_id=?",
                    (root_id,)
                ).fetchone()
                max_rev = max_rev_row[0] if (max_rev_row and max_rev_row[0] is not None) else None

                if existing_root is None:
                    # Brand new root event
                    rev_id = f"{root_id}:rev{new_ev}"
                    con.execute(
                        "INSERT OR IGNORE INTO verified_revisions (id, event_id, revision, state, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (rev_id, root_id, new_ev, OutboxStatus.PENDING.value, payload.json(), now)
                    )
                    con.execute(
                        """
                        INSERT INTO verified_outbox (
                            event_id, exchange, symbol, order_id, client_order_id,
                            event_type, version, payload, status, attempts,
                            max_attempts, next_retry_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                        """,
                        (
                            root_id,
                            payload.exchange,
                            payload.symbol,
                            payload.order_id,
                            payload.client_order_id,
                            payload.event_type,
                            new_ev,
                            payload.json(),
                            OutboxStatus.PENDING.value,
                            max_attempts,
                            now,
                            now,
                            now,
                        ),
                    )
                    return True

                # Root already exists
                st_val, old_p_json, cur_ver, msg_id = existing_root
                if max_rev is None:
                    max_rev = cur_ver

                # Stale check: compared against highest correction/revision, not only root
                if new_ev < max_rev:
                    logger.info("verified_alerts: stale_revision_blocked")
                    return False

                if new_ev == max_rev:
                    # Check against existing revision payload
                    rev_row = con.execute(
                        "SELECT payload FROM verified_revisions WHERE event_id=? AND revision=?",
                        (root_id, new_ev)
                    ).fetchone()
                    existing_payload = rev_row[0] if rev_row else old_p_json

                    if existing_payload == payload.json():
                        logger.info("verified_alerts: duplicate_event_blocked")
                        return False
                    else:
                        # Same revision number but different payload -> CAS conflict
                        logger.info("verified_alerts: conflicting_same_revision_blocked")
                        return False

                # new_ev > max_rev: valid new revision
                rev_id = f"{root_id}:rev{new_ev}"
                con.execute(
                    "INSERT OR REPLACE INTO verified_revisions (id, event_id, revision, state, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (rev_id, root_id, new_ev, OutboxStatus.PENDING.value, payload.json(), now)
                )

                if st_val == OutboxStatus.PENDING.value:
                    # In-place update for pending root
                    con.execute(
                        """
                        UPDATE verified_outbox
                        SET payload=?, status=?, updated_at=?, version=?, next_retry_at=?
                        WHERE event_id=?
                        """,
                        (payload.json(), OutboxStatus.PENDING.value, now, new_ev, now, root_id)
                    )
                    logger.info("verified_alerts: corrected_verified_alert_updated")
                    return True
                else:
                    # Root has already been dispatched/held/failed (SENDING, SENT, UNKNOWN, HELD, FAILED).
                    # Immutable root preservation: branch into a correction delivery row!
                    corr_event_id = f"{root_id}:corr{new_ev}"

                    # Guard against collision with unrelated order event_id
                    existing_other = con.execute(
                        "SELECT payload FROM verified_outbox WHERE event_id=?",
                        (corr_event_id,)
                    ).fetchone()
                    if existing_other:
                        try:
                            other_p = json.loads(existing_other[0])
                            if other_p.get("order_id") != payload.order_id and other_p.get("event_id") != root_id:
                                corr_event_id = f"{root_id}:corr{new_ev}_{int(now*1000)}"
                        except Exception:
                            pass

                    # Status policy: preserve UNKNOWN / HELD hold; otherwise PENDING
                    corr_status = st_val if st_val in (OutboxStatus.UNKNOWN.value, OutboxStatus.HELD.value) else OutboxStatus.PENDING.value

                    con.execute(
                        """
                        INSERT OR REPLACE INTO verified_outbox (
                            event_id, exchange, symbol, order_id, client_order_id,
                            event_type, version, payload, status, attempts,
                            max_attempts, next_retry_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                        """,
                        (
                            corr_event_id,
                            payload.exchange,
                            payload.symbol,
                            payload.order_id,
                            payload.client_order_id,
                            payload.event_type,
                            new_ev,
                            payload.json(),
                            corr_status,
                            max_attempts,
                            now,
                            now,
                            now,
                        )
                    )
                    logger.info(f"verified_alerts: revision_branched_to_correction corr_id={corr_event_id} status={corr_status}")
                    return True

        except sqlite3.IntegrityError:
            logger.info("verified_alerts: duplicate_event_blocked")
            return False

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

            res = []
            malformed_ids = []
            for r in rows:
                try:
                    payload_dict = json.loads(r[1])
                    if not isinstance(payload_dict, dict):
                        raise ValueError("Payload must be a JSON object (dict)")
                    res.append({
                        "event_id": r[0],
                        "payload": payload_dict,
                        "attempts": r[2],
                        "max_attempts": r[3],
                        "next_retry_at": r[4],
                    })
                except Exception:
                    malformed_ids.append(r[0])

        if malformed_ids:
            try:
                with self._connect() as con:
                    ensure_quarantine_schema(con)
                    for bad_id in malformed_ids:
                        con.execute(
                            "UPDATE verified_outbox SET status=?, last_error='malformed_json', updated_at=? WHERE event_id=?",
                            (OutboxStatus.FAILED.value, now, bad_id)
                        )
                        con.execute(
                            "INSERT OR REPLACE INTO verified_quarantine (id, reason, payload) VALUES (?, ?, ?)",
                            (bad_id, "malformed_json_outbox", "not-json")
                        )
            except Exception as e:
                logger.warning(f"Failed to quarantine malformed outbox rows: {e}")

        return res

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
            con.execute("UPDATE verified_outbox SET status=?,last_error='delivery_unknown',updated_at=? WHERE event_id=?",
                        (OutboxStatus.UNKNOWN.value, time.time(), event_id))

    def requeue_unknown(
        self,
        event_id: Optional[str] = None,
        *,
        confirm_duplicate_risk: bool = False,
        allow_bulk: bool = False,
    ) -> int:
        """Operator recovery helper: transitions UNKNOWN/HELD items back to PENDING for manual resend.

        WARNING:
            Items placed in UNKNOWN or HELD status typically arose from HTTP 5xx responses,
            network timeouts, or unconfirmed delivery during previous send attempts.
            Discord or an intermediary proxy may have already accepted and posted the message.
            Manually requeueing alerts back to PENDING carries a real risk of posting duplicate
            messages to the Discord channel.
            Operators MUST inspect the Discord channel/audit logs before invoking this method
            to verify that the alert was NOT posted. Complete prevention of duplicate delivery
            cannot be guaranteed once a 5xx/timeout has occurred; remaining risk is accepted by
            explicit confirmation.

        Args:
            event_id: Specific outbox event ID to requeue. If None, allow_bulk must be True.
            confirm_duplicate_risk: Explicit operator acknowledgment of duplicate notification risk.
                Must be set to True to execute the requeue.
            allow_bulk: Must be True to requeue all UNKNOWN/HELD items when event_id is None.
                Defaults to False to prevent accidental bulk reprocessing.

        Returns:
            Number of rows transitioned from UNKNOWN/HELD to PENDING.

        Raises:
            ValueError: If confirm_duplicate_risk is False, or if event_id is None without allow_bulk=True.
        """
        if confirm_duplicate_risk is not True:
            raise ValueError(
                "Manual requeue rejected: operator must explicitly confirm duplicate alert risk "
                "(confirm_duplicate_risk=True)."
            )

        if allow_bulk is not True and allow_bulk is not False:
            raise ValueError("Manual requeue rejected: allow_bulk must be a boolean.")

        if not event_id and allow_bulk is not True:
            raise ValueError(
                "Bulk requeue blocked by default: specify a specific event_id, or explicitly set "
                "allow_bulk=True if bulk requeue is intended."
            )

        now = time.time()
        with self._connect() as con:
            if event_id:
                safe_ref = hashlib.sha256(str(event_id).encode("utf-8")).hexdigest()[:16]
                count = con.execute(
                    "UPDATE verified_outbox SET status=?, next_retry_at=?, updated_at=? "
                    "WHERE event_id=? AND status IN (?, ?)",
                    (OutboxStatus.PENDING.value, now, now, event_id, OutboxStatus.UNKNOWN.value, OutboxStatus.HELD.value),
                ).rowcount
                logger.warning(
                    f"AUDIT_ALERT_REQUEUE: operator requeued outbox item event_ref={safe_ref} (rows_updated={count}). "
                    "Duplicate Discord notification risk acknowledged."
                )
                return count
            count = con.execute(
                "UPDATE verified_outbox SET status=?, next_retry_at=?, updated_at=? "
                "WHERE status IN (?, ?)",
                (OutboxStatus.PENDING.value, now, now, OutboxStatus.UNKNOWN.value, OutboxStatus.HELD.value),
            ).rowcount
            logger.warning(
                f"AUDIT_ALERT_REQUEUE: operator bulk-requeued outbox items (rows_updated={count}). "
                "Duplicate Discord notification risk acknowledged."
            )
            return count

    def recover(self):
        now = time.time()
        with self._connect() as con:
            ensure_quarantine_schema(con)
            # 1. Recover abandoned sends
            con.execute("UPDATE verified_outbox SET status='UNKNOWN', last_error='abandoned_send' WHERE status='SENDING' AND updated_at<?", (now - 30,))
            con.execute("UPDATE verified_jobs SET state='PENDING' WHERE state='VERIFYING' AND due<?", (now,))

            # 2. Recover unconfirmed / incomplete DONE jobs bounded to 40 (to ensure touched <= 100)
            last_rec = NotificationOutboxStore._last_done_recovery_by_db.get(self.db_path, 0.0)
            pending_count = con.execute("SELECT count(*) FROM verified_jobs WHERE state='PENDING' AND due <= ?", (now,)).fetchone()[0]
            if now - last_rec >= 60.0 and pending_count < 40:
                NotificationOutboxStore._last_done_recovery_by_db[self.db_path] = now
                done_rows = con.execute("SELECT id, data FROM verified_jobs WHERE state='DONE' ORDER BY id ASC").fetchall()
                to_recover = []
                for j_id, encoded in done_rows:
                    try:
                        j_data = json.loads(encoded)
                        if not isinstance(j_data, dict):
                            raise ValueError("Job data must be a JSON object (dict)")
                    except Exception:
                        con.execute("INSERT OR REPLACE INTO verified_quarantine (id, reason, payload) VALUES (?, ?, ?)", (j_id, "malformed_json", str(encoded)))
                        con.execute("UPDATE verified_jobs SET state='QUARANTINED' WHERE id=?", (j_id,))
                        continue

                    acct_v = j_data.get("accounting_verified")
                    if acct_v is None or acct_v is False or acct_v == 0:
                        to_recover.append(j_id)
                    elif acct_v == 1 or acct_v is True:
                        info = j_data.get("info", {}) if isinstance(j_data.get("info"), dict) else {}
                        exch = str(info.get("exchange") or "").upper()
                        ord_res = j_data.get("result", {}) if isinstance(j_data.get("result"), dict) else {}
                        oid = str(ord_res.get("id") or ord_res.get("uuid") or "")
                        if oid:
                            # Check if outbox alert for this order has unconfirmed PnL scoped to exchange
                            if exch:
                                outbox_row = con.execute("SELECT payload FROM verified_outbox WHERE order_id=? AND UPPER(exchange)=? LIMIT 1", (oid, exch)).fetchone()
                            else:
                                outbox_row = con.execute("SELECT payload FROM verified_outbox WHERE order_id=? LIMIT 1", (oid,)).fetchone()
                            if outbox_row:
                                try:
                                    ob_p = json.loads(outbox_row[0])
                                    ob_pnl_st = str(ob_p.get("pnl_verification_status") or "").upper()
                                    is_sell_or_close = ob_p.get("side") in ("매도", "숏 진입", "롱 종료", "숏 종료") or ob_p.get("net_pnl") is not None
                                    if is_sell_or_close and ob_pnl_st != "VERIFIED":
                                        to_recover.append(j_id)
                                        if len(to_recover) >= 40:
                                            break
                                        continue
                                except Exception:
                                    pass
                            pattern = f'%"order": "{oid}"%'
                            if exch:
                                from .ledger import identity
                                acct = str(j_data.get("account") or "")
                                m_type = str(info.get("market_type") or "spot")
                                sym = str(info.get("unified_symbol") or info.get("symbol") or "")
                                pos_side = str(info.get("position_side") or "LONG")
                                exp_scope = identity(exch, acct, m_type, sym, pos_side)
                                has_trade = con.execute("SELECT 1 FROM verified_trades WHERE scope=? AND data LIKE ? LIMIT 1", (exp_scope, pattern)).fetchone()
                            else:
                                has_trade = con.execute("SELECT 1 FROM verified_trades WHERE data LIKE ? LIMIT 1", (pattern,)).fetchone()
                            if not has_trade:
                                to_recover.append(j_id)
                    if len(to_recover) >= 40:
                        break

                if to_recover:
                    placeholders = ",".join("?" for _ in to_recover)
                    con.execute(f"UPDATE verified_jobs SET state='PENDING', due=0 WHERE id IN ({placeholders})", to_recover)

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
    """Builds the POA v2 unified mobile-friendly Discord Embed for verified order execution."""
    try:
        from exchange.utility.discord_embed import build_mobile_verified_embed
    except ModuleNotFoundError:
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            "exchange.utility.discord_embed",
            Path(__file__).resolve().parents[1] / "utility" / "discord_embed.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["exchange.utility.discord_embed"] = mod
        spec.loader.exec_module(mod)
        build_mobile_verified_embed = mod.build_mobile_verified_embed

    embed = build_mobile_verified_embed(payload)
    return embed


class DiscordSender:
    """Dispatches notifications to Discord with safe delivery-unknown holding on 5xx/timeouts (P2-5)."""

    def __init__(self, webhook_url: Optional[str] = None, retry_on_5xx: bool = False):
        self.webhook_url = (webhook_url or "").replace("discordapp", "discord")
        self.retry_on_5xx = retry_on_5xx
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

            # 5xx Server error: Discord or an intermediary may have already accepted and posted the message.
            # Default to holding as delivery_unknown to prevent duplicate messages unless retry_on_5xx is explicitly enabled.
            if self.retry_on_5xx:
                return False, f"Discord server error HTTP {resp.status_code}", 5.0
            return False, "delivery_unknown", None

        except requests.exceptions.Timeout:
            return False, "delivery_unknown", None
        except requests.exceptions.RequestException:
            return False, "delivery_unknown", None
