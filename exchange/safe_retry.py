"""Opt-in, durable order submission. No imports of application singletons.

Only explicit exchange rejection codes permit retry. Missing evidence never does.
SQLite is for one host/one process; a lifetime file lock enforces one safe worker.
"""
from contextlib import contextmanager, closing
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import logging
import math
import random
import sqlite3
import threading
import time
from pathlib import Path
import uuid


class SafetyError(Exception):
    pass


TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED"}
TRANSITIONS = {
    "RECEIVED": {"VALIDATED", "FAILED", "EXPIRED"},
    "VALIDATED": {"SUBMITTING", "FAILED", "EXPIRED"},
    "SUBMITTING": {"ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED", "UNKNOWN"},
    "REJECTED": {"VALIDATED", "FAILED", "EXPIRED"},
    "ACCEPTED": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "UNKNOWN"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "UNKNOWN"},
    "UNKNOWN": {"ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "UNKNOWN"},
    "FILLED": set(), "CANCELED": set(), "EXPIRED": set(), "FAILED": set(),
}
CLIENT_KEYS = {"BINANCE": "newClientOrderId", "BYBIT": "orderLinkId", "OKX": "clOrdId"}
UPBIT_CLIENT_KEYS = {"UPBIT": "identifier"}
ALL_CLIENT_KEYS = {**CLIENT_KEYS, **UPBIT_CLIENT_KEYS}


@dataclass(frozen=True)
class Config:
    attempts: int = 3
    base_delay: float = .5
    max_delay: float = 8
    jitter: bool = True
    ttl: float = 30
    deviation: float = .1

    def __post_init__(self):
        if not 1 <= self.attempts <= 10:
            raise SafetyError("MAX_RETRY_ATTEMPTS must be 1..10 (total submissions)")
        if any(not math.isfinite(v) or v < 0 for v in
               (self.base_delay, self.max_delay, self.ttl, self.deviation)) or self.ttl == 0:
            raise SafetyError("Invalid retry limits")


def number(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0:
        raise SafetyError("Invalid order quantity/price")
    return value


class Store:
    def __init__(self, path):
        if str(path) == ":memory:":
            raise SafetyError("Persistent storage is required")
        self.path = str(Path(path).resolve())
        if Path(self.path).exists() and Path(self.path).stat().st_size:
            with closing(sqlite3.connect(self.path)) as con:
                exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='safe_orders'").fetchone()
                if not exists:
                    backup = self.path + ".pre-safe-" + uuid.uuid4().hex + ".bak"
                    with closing(sqlite3.connect(backup)) as dst:
                        con.backup(dst)
        with self.connect() as con:
            # Separate database; never migrate the legacy auth or PocketBase DB.
            con.execute("CREATE TABLE IF NOT EXISTS safe_orders ("
                        "signal TEXT PRIMARY KEY, asset TEXT NOT NULL, state TEXT NOT NULL, "
                        "data TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS safe_locks (asset TEXT PRIMARY KEY, signal TEXT UNIQUE)")

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=5)
        try:
            con.execute("PRAGMA synchronous=FULL")
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def claim(self, row):
        with self.connect() as con:
            existing = con.execute("SELECT data FROM safe_orders WHERE signal=?", (row["signal"],)).fetchone()
            if existing:
                return json.loads(existing[0]), False
            legacy_signal = row.get("legacy_signal")
            if legacy_signal:
                existing_legacy = con.execute("SELECT data FROM safe_orders WHERE signal=?", (legacy_signal,)).fetchone()
                if existing_legacy:
                    return json.loads(existing_legacy[0]), False
            if con.execute("SELECT 1 FROM safe_locks WHERE asset=?", (row["asset"],)).fetchone():
                raise SafetyError("Unresolved order on this account/symbol; reconcile first")
            con.execute("INSERT INTO safe_orders VALUES (?,?,?,?)", (
                row["signal"], row["asset"], row["state"], json.dumps(row)))
            con.execute("INSERT INTO safe_locks VALUES (?,?)", (row["asset"], row["signal"]))
            return row, True

    def save(self, row, state, release=False, **changes):
        with self.connect() as con:
            current = con.execute("SELECT state FROM safe_orders WHERE signal=?", (row["signal"],)).fetchone()
            if not current or (state != current[0] and state not in TRANSITIONS[current[0]]):
                raise SafetyError("Invalid state transition")
            updated = dict(row, **changes, state=state)
            con.execute("UPDATE safe_orders SET state=?,data=? WHERE signal=?",
                        (state, json.dumps(updated), row["signal"]))
            if release:
                if state not in TERMINAL:
                    raise SafetyError("Cannot unlock unresolved order")
                con.execute("DELETE FROM safe_locks WHERE signal=?", (row["signal"],))
        row.update(updated)

    def pending(self):
        with self.connect() as con:
            return [json.loads(r[0]) for r in con.execute(
                "SELECT o.data FROM safe_orders o JOIN safe_locks l ON o.signal=l.signal")]

    def recover(self):
        rows = self.pending()
        for row in rows:
            if row["state"] in {"RECEIVED", "VALIDATED", "REJECTED"}:
                self.save(row, "FAILED", release=True)
            elif row["state"] not in TERMINAL:
                self.save(row, "UNKNOWN")
        return rows

    def backup(self, destination):
        with closing(sqlite3.connect(self.path)) as src, closing(sqlite3.connect(str(destination))) as dst:
            src.backup(dst)


def classify(exchange, exc):
    """Whitelist structured response codes, never a substring like 'timeout'."""
    import ccxt
    if isinstance(exc, (ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.InsufficientFunds)):
        return "fatal"
    if isinstance(exc, ccxt.NetworkError) and not isinstance(exc, (ccxt.RateLimitExceeded, ccxt.InvalidNonce)):
        return "unknown"
    # CCXT embeds the structured exchange body in the exception string.
    text = str(exc)
    try:
        body = json.loads(text[text.index("{"):])
    except (ValueError, TypeError):
        return "unknown"
    if exchange == "UPBIT":
        err = body.get("error")
        code = str((err.get("name") if isinstance(err, dict) else None) or body.get("name") or body.get("code") or "")
        fatal_codes_upbit = {
            "insufficient_funds_bid", "insufficient_funds_ask",
            "invalid_query_payload", "jwt_verification", "no_authorization_i_p",
            "out_of_min_order_funds", "out_of_min_order_volume",
            "invalid_identifier", "validation_error", "under_min_total_market_ask",
            "under_min_total_bid", "invalid_funds", "volume_too_large"
        }
        retry_codes_upbit = {"too_many_requests": "rate"}
        if code in fatal_codes_upbit:
            return "fatal"
        if code in retry_codes_upbit or isinstance(exc, ccxt.RateLimitExceeded):
            return "rate"
        return "unknown"
    field = {"BINANCE": "code", "BYBIT": "retCode", "OKX": "code"}[exchange]
    code = str(body.get(field))
    retry_codes = {"BINANCE": {"-1003": "rate", "-1021": "clock", "-1111": "precision"},
                   "BYBIT": {"10006": "rate", "10002": "clock"},
                   "OKX": {"50011": "rate", "50102": "clock"}}
    fatal_codes = {"BINANCE": {"-2014", "-2015", "-2019", "-4061", "-1013"},
                   "BYBIT": {"10003", "10004", "10005", "10010", "110007", "110004", "10001"},
                   "OKX": {"50113", "50111", "51008", "51000", "50007"}}
    if code in fatal_codes[exchange]:
        return "fatal"
    return retry_codes[exchange].get(code, "unknown")


class Adapter:
    def __init__(self, instance, info):
        self.bot, self.client, self.info = instance, instance.client, info
        self.exchange = info.exchange

    def validate(self, args, row, cfg, now):
        info = self.info
        if info.is_coinm or (info.is_futures and info.quote != "USDT"):
            raise SafetyError("Safe mode supports spot and USDT linear contracts only")
        if getattr(self.bot, "position_mode", "one-way") != "one-way":
            raise SafetyError("Hedge mode requires separate validation; safe mode blocked")
        if info.stop_price is not None or info.profit_price is not None:
            raise SafetyError("Multi-leg TP/SL is not supported in safe mode")
        if not 0 <= now - row["created"] <= cfg.ttl:
            raise SafetyError("Signal expired or future dated")
        amount = number(args[3])
        if amount == 0:
            raise SafetyError("Zero quantity")
        last = number(self.client.fetch_ticker(args[0])["last"])
        reference = number(row["reference"])
        if reference == 0 or last == 0 or abs(last / reference - 1) * 100 > number(cfg.deviation):
            raise SafetyError("Price deviation exceeded")
        market = self.client.market(args[0])
        limits = market.get("limits") or {}
        for field, value in [("amount", amount), ("cost", amount * last * number(market.get("contractSize") or 1))]:
            bounds = limits.get(field) or {}
            for bound, invalid in [("min", lambda n: value < n), ("max", lambda n: value > n)]:
                if bounds.get(bound) is not None and invalid(number(bounds[bound])):
                    raise SafetyError("Exchange order limit exceeded")
        if not info.is_futures:
            balances = self.client.fetch_free_balance()
            currency = info.quote if info.is_buy else info.base
            required = amount * last if info.is_buy else amount
            if balances.get(currency) is None or number(balances[currency]) < required:
                raise SafetyError("Insufficient available spot balance")
        if info.exchange != "UPBIT" and info.is_close:
            if args[5].get("reduceOnly") is not True:
                raise SafetyError("Close requires reduceOnly")
            positions = self.client.fetch_positions([args[0]])
            side = "short" if info.is_buy else "long"
            matching = [p for p in positions if p.get("symbol") == args[0] and p.get("side") == side]
            if any(p.get("hedged") for p in positions):
                raise SafetyError("Position mode mismatch")
            available = sum((number(p["contracts"]) for p in matching), Decimal(0))
            if amount > available or available == 0:
                raise SafetyError("Position mismatch / already closed")

    def repair(self, kind, args):
        args = list(args)
        if kind == "clock":
            self.client.load_time_difference()
        elif kind == "precision":
            corrected = float(self.client.amount_to_precision(args[0], args[3]))
            if number(corrected) > number(args[3]) or corrected <= 0:
                raise SafetyError("Unsafe precision correction")
            args[3] = corrected
            if args[4] is not None:
                args[4] = float(self.client.price_to_precision(args[0], args[4]))
        return tuple(args)

    def reconcile(self, row):
        """Read ID, open/history, trades and position. Absence never authorizes send."""
        symbol, cid = row["symbol"], row["client_id"]
        if self.exchange == "UPBIT":
            observations = []
            order_id = row.get("order_id")
            if order_id:
                try:
                    res = self.client.fetch_order(order_id, symbol)
                    if isinstance(res, dict): observations.append(res)
                except Exception:
                    pass
                if not observations and hasattr(self.client, "privateGetOrder"):
                    try:
                        raw = self.client.privateGetOrder({"uuid": order_id})
                        if isinstance(raw, dict):
                            parsed = self.client.parse_order(raw) if hasattr(self.client, "parse_order") else raw
                            observations.append(parsed)
                    except Exception:
                        pass
            if cid and hasattr(self.client, "privateGetOrder"):
                try:
                    raw = self.client.privateGetOrder({"identifier": cid})
                    if isinstance(raw, dict):
                        parsed = self.client.parse_order(raw) if hasattr(self.client, "parse_order") else raw
                        observations.append(parsed)
                except Exception:
                    pass
            for fn, fn_args in [("fetch_open_orders", (symbol,)), ("fetch_closed_orders", (symbol,))]:
                try:
                    val = getattr(self.client, fn)(*fn_args)
                    if isinstance(val, list): observations.extend(val)
                except Exception:
                    pass
            matches = [o for o in observations if isinstance(o, dict) and
                       (o.get("clientOrderId") == cid or o.get("identifier") == cid or (row.get("order_id") and (o.get("id") == row["order_id"] or o.get("uuid") == row["order_id"])))]
            if not matches:
                return None
            status_map = {"done": "closed", "wait": "open", "watch": "open", "cancel": "canceled"}
            states = {status_map.get(str(o.get("status") or o.get("state") or "").lower(), str(o.get("status") or o.get("state") or "").lower()) for o in matches}
            if len(states) > 1:
                return None
            return max(matches, key=lambda o: float(o.get("filled") or o.get("executed_volume") or 0))
        params = {"BINANCE": {"origClientOrderId": cid},
                  "BYBIT": {"orderLinkId": cid, "acknowledged": True}, "OKX": {"clOrdId": cid}}[self.exchange]
        observations = []
        for fn, args in [
            ("fetch_order", (row.get("order_id"), symbol, params)),
            ("fetch_open_orders", (symbol,)),
            ("fetch_closed_orders", (symbol,)),
            ("fetch_my_trades", (symbol,)),
            ("fetch_positions", ([symbol],)) if self.info.is_futures else ("fetch_balance", ()),
        ]:
            try:
                value = getattr(self.client, fn)(*args)
                if fn in {"fetch_order", "fetch_open_orders", "fetch_closed_orders"}:
                    observations.extend(value if isinstance(value, list) else [value])
            except Exception:
                continue
        matches = [o for o in observations if isinstance(o, dict) and
                   (o.get("clientOrderId") == cid or (row.get("order_id") and o.get("id") == row["order_id"]))]
        # A terminal authoritative order observation may resolve uncertainty;
        # conflicting observations are held, never guessed from account position.
        if not matches:
            return None
        states = {o.get("status") for o in matches}
        if len(states) > 1:
            return None
        return max(matches, key=lambda o: float(o.get("filled") or 0))


class Manager:
    def __init__(self, store, config=Config(), alert=None, clock=time.time, sleep=time.sleep):
        self.store, self.cfg, self.alert = store, config, alert
        self.clock, self.sleep = clock, sleep
        self.mutex = threading.RLock()

    def notify(self, event, row):
        if self.alert:
            try:
                # No raw payload, exception, API key, password or exchange response.
                self.alert(f"[SAFE RETRY] {event} | {row['exchange']} | {row['client_id']} | {row['state']}")
            except Exception:
                pass

    def record(self, row, result):
        oid = str(result.get("id") or result.get("uuid") or "") if isinstance(result, dict) else ""
        if not isinstance(result, dict) or not oid:
            self.store.save(row, "UNKNOWN")
            return False
        try:
            filled = number(result.get("filled") or result.get("executed_volume") or 0)
        except Exception:
            self.store.save(row, "UNKNOWN")
            return False
        target = number(row["target"])
        if filled > target or filled < number(row.get("filled", 0)):
            self.store.save(row, "UNKNOWN")
            return False
        raw_status = str(result.get("status") or result.get("state") or "").lower()
        status_map = {"closed": "closed", "done": "closed", "filled": "closed",
                      "open": "open", "wait": "open", "watch": "open",
                      "canceled": "canceled", "cancel": "canceled", "expired": "expired"}
        status = status_map.get(raw_status, result.get("status"))
        if status in {"closed", "canceled", "expired"} and result.get("filled") is None and result.get("executed_volume") is None:
            self.store.save(row, "UNKNOWN")
            return False
        if status not in {None, "open", "closed", "canceled", "expired"}:
            self.store.save(row, "UNKNOWN")
            return False
        state = {"open": "PARTIALLY_FILLED" if filled else "ACCEPTED",
                 "closed": "FILLED" if filled == target else "CANCELED",
                 "canceled": "CANCELED", "expired": "EXPIRED"}.get(status, "ACCEPTED")
        self.store.save(row, state, release=state in TERMINAL, filled=str(filled),
                        remaining=str(target - filled), order_id=oid)
        self.notify(state, row)
        return True

    def execute(self, func, args, info, adapter):
        # Serializes mutable shared client options/order_info within this manager.
        with self.mutex:
            return self._execute(func, args, info, adapter)

    def _execute(self, func, args, info, adapter):
        explicit = getattr(info, "signal_id", None)
        if info.exchange == "UPBIT" and not explicit:
            raise SafetyError("UPBIT safe mode requires 'signal_id' for duplicate protection. Please provide signal_id in TradingView webhook.")

        exchange = str(info.exchange).upper()
        symbol = str(args[0] or getattr(info, "unified_symbol", "")).upper()
        strategy_id = str(getattr(info, "strategy_id", None) or "default_strategy")
        raw_acct = getattr(info, "account_id", None) or getattr(adapter.client, "apiKey", None) or "default_account"
        account_hash = hashlib.sha256(str(raw_acct).encode()).hexdigest()[:12]

        if explicit:
            signal = hashlib.sha256(f"{exchange}:{account_hash}:{strategy_id}:{symbol}:{explicit}".encode()).hexdigest()
            legacy_signal = hashlib.sha256(f"{exchange}:{explicit}".encode()).hexdigest()
        else:
            signal = hashlib.sha256(f"{exchange}:{account_hash}:{strategy_id}:{symbol}:{uuid.uuid4().hex}".encode()).hexdigest()
            legacy_signal = None

        cid = "poa" + uuid.uuid4().hex[:29]  # 32 alphanumeric characters; never reuse.
        now = self.clock()
        reference = info.price or adapter.client.fetch_ticker(args[0])["last"]
        row = dict(signal=signal, legacy_signal=legacy_signal, asset=f"{exchange}:{account_hash}:{args[0]}", state="RECEIVED",
                   exchange=info.exchange, symbol=args[0], client_id=cid, order_id=None,
                   created=getattr(info, "signal_time", None) or now, reference=reference,
                   target=str(number(args[3])), filled="0", remaining=str(number(args[3])), attempts=0,
                   futures=bool(info.is_futures), side=info.side, close=bool(info.is_close))
        row, fresh = self.store.claim(row)
        if not fresh:
            self.notify("DUPLICATE_BLOCKED", row)
            raise SafetyError("Signal already recorded; no order submitted")
        client_key = ALL_CLIENT_KEYS.get(info.exchange) or CLIENT_KEYS.get(info.exchange)
        if not client_key:
            raise SafetyError("Unsupported exchange for safe retry")
        args = tuple(args[:5]) + (dict(args[5], **{client_key: cid}),)
        maximum = self.cfg.attempts if explicit else 1
        for attempt in range(maximum):
            if attempt > 0 and info.exchange == "UPBIT":
                # For Upbit, identifier must not be reused across attempts.
                cid = "poa" + uuid.uuid4().hex[:29]
                row["client_id"] = cid
                args = tuple(args[:5]) + (dict(args[5], **{client_key: cid}),)
            try:
                adapter.validate(args, row, self.cfg, self.clock())
            except Exception:
                state = "EXPIRED" if self.clock() - row["created"] > self.cfg.ttl else "FAILED"
                self.store.save(row, state, release=True)
                self.notify("POSITION_OR_RISK_BLOCKED", row)
                raise SafetyError("Order risk validation failed") from None
            self.store.save(row, "VALIDATED")
            # This commit must succeed BEFORE the exchange is called.
            self.store.save(row, "SUBMITTING", attempts=attempt + 1)
            if attempt:
                self.notify("RETRY_EXECUTING", row)
            try:
                result = func(*args)
            except Exception as exc:
                kind = classify(info.exchange, exc)
                if kind == "unknown":
                    self.store.save(row, "UNKNOWN")
                    result = adapter.reconcile(row)
                    if result and self.record(row, result):
                        return result
                    self.notify("URGENT_CLOSE_FAILURE" if info.is_close else "UNKNOWN", row)
                    raise SafetyError("Order status unknown; automatic resend blocked") from None
                self.store.save(row, "REJECTED")
                if kind == "fatal" or attempt + 1 == maximum:
                    self.store.save(row, "FAILED", release=True)
                    self.notify("FINAL_FAILURE", row)
                    raise SafetyError("Order rejected; retry stopped") from None
                try:
                    args = adapter.repair(kind, args)
                except Exception:
                    self.store.save(row, "FAILED", release=True)
                    raise SafetyError("Repair failed; retry stopped") from None
                self.notify("RETRY_WAITING", row)
                delay = min(self.cfg.max_delay, self.cfg.base_delay * 2 ** attempt)
                self.sleep(random.uniform(0, delay) if self.cfg.jitter else delay)
                continue
            if not self.record(row, result):
                self.notify("UNKNOWN", row)
                raise SafetyError("Invalid order acknowledgement; resend blocked")
            if attempt:
                self.notify("RETRY_ACCEPTED" if row["state"] != "FILLED" else "RETRY_FILLED", row)
            return result


_manager = None
_lock_file = None
_init_lock = threading.Lock()


def get_manager(settings):
    global _manager, _lock_file
    with _init_lock:
        if _manager is not None:
            return _manager
        if not all(getattr(settings, flag) for flag in (
            "SAFE_RETRY_ENABLED", "ORDER_RECONCILIATION_ENABLED",
            "DUPLICATE_SIGNAL_PROTECTION", "PERSIST_ORDER_STATE")):
            raise SafetyError("Safe retry requires reconciliation, deduplication and persistence")
        path = Path(settings.SAFE_RETRY_DB_PATH).resolve()
        if path.name in {"store.db", "data.db", "logs.db"}:
            raise SafetyError("Use a separate safe retry database")
        handle = open(str(path) + ".lock", "a+b")
        try:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cfg = Config(settings.MAX_RETRY_ATTEMPTS, settings.RETRY_BASE_DELAY,
                         settings.RETRY_MAX_DELAY, settings.RETRY_JITTER_ENABLED,
                         settings.ENTRY_SIGNAL_TTL_SECONDS, settings.MAX_ENTRY_PRICE_DEVIATION_PCT)
            store = Store(path)
            alert = None
            if settings.RETRY_DISCORD_ALERTS:
                from exchange.utility import log_message
                alert = log_message
            manager = Manager(store, cfg, alert)
            for row in store.recover():
                manager.notify("RESTART_RECOVERY", row)
            _manager, _lock_file = manager, handle
            return manager
        except BaseException:
            handle.close()
            raise


def safe_submit(func, args, info, instance, settings):
    is_upbit = (info.exchange == "UPBIT")
    if is_upbit:
        if not getattr(settings, "UPBIT_SAFE_RETRY_ENABLED", False):
            raise SafetyError("UPBIT safe mode is disabled. Set UPBIT_SAFE_RETRY_ENABLED=True to enable.")
    elif info.exchange not in CLIENT_KEYS:
        raise SafetyError("Unsupported safe submission path")
    if instance is None or len(args) != 6:
        raise SafetyError("Unsupported safe submission path")
    return get_manager(settings).execute(func, args, info, Adapter(instance, info))


STATE_OPEN = "OPEN"
STATE_CLOSING = "CLOSING"
STATE_CLOSED = "CLOSED"


class ClientEntry:
    def __init__(self, key, client, state=STATE_OPEN):
        self.key = key
        self.client = client
        self.state = state  # "OPEN", "CLOSING", "CLOSED"
        self.active_leases = 0


class ClientLease:
    """An atomic lease on a CCXT client for reconciliation queries.
    Tracks active lease count and triggers deferred close when the last lease releases."""
    def __init__(self, pool, key, client, entry=None, client_lock=None):
        self.pool = pool
        self.key = key
        self.client = client
        self.entry = entry
        self.client_lock = client_lock
        self._released = False
        self._lock = threading.Lock()
        self._entered = False

    def __enter__(self):
        self._entered = True
        if self.client_lock is not None:
            self.client_lock.acquire()
        return self.client

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.client_lock is not None and self._entered:
                self.client_lock.release()
        finally:
            self.release()

    def release(self):
        with self._lock:
            if self._released:
                return
            self._released = True
        self.pool._release_lease(self.key, self.client, self.entry)

    def close(self):
        self.release()


class ReconciliationClients:
    """Owns separate, dedicated read-only CCXT instances for reconciliation.
    Never shares or mutates the trading bot's CCXT instance or its options['defaultType'].
    Uses atomic lease tracking and strict single-owner closing to eliminate race conditions."""
    def __init__(self, settings):
        self.settings = settings
        self.clients = {}
        self.client_locks = {}
        self.active_leases = {}
        self.pending_close_keys = set()
        self.client_states = {}
        self.closed_client_ids = set()
        self.close_results = {}  # Object ID -> actual resource-release outcome.
        self.entries = {}
        self.lock = threading.Lock()
        self.closed = False
        self._state = STATE_OPEN

    def _normalize_key(self, exchange, is_futures=False):
        if isinstance(exchange, tuple):
            if len(exchange) == 3:
                return (str(exchange[0]).upper(), str(exchange[1]), str(exchange[2]))
            elif len(exchange) == 2:
                account = getattr(self.settings, f"{str(exchange[0]).upper()}_KEY", None) or "default"
                return (str(exchange[0]).upper(), str(exchange[1]), account)
        category = "swap" if is_futures else "spot"
        account = getattr(self.settings, f"{str(exchange).upper()}_KEY", None) or "default"
        return (str(exchange).upper(), category, account)

    def _get_key(self, exchange, is_futures=False, account=None):
        if isinstance(exchange, tuple):
            return self._normalize_key(exchange, is_futures)
        category = "swap" if is_futures else "spot"
        acct = account or getattr(self.settings, f"{str(exchange).upper()}_KEY", None) or "default"
        return (str(exchange).upper(), category, acct)

    def _get_or_create_client_locked(self, key, exchange: str, is_futures: bool):
        norm_key = self._normalize_key(key, is_futures)
        if norm_key not in self.clients:
            import ccxt
            s = self.settings
            category = norm_key[1]
            params = {
                "apiKey": getattr(s, f"{exchange.upper()}_KEY", None),
                "secret": getattr(s, f"{exchange.upper()}_SECRET", None),
                "timeout": 5000,
                "enableRateLimit": True,
                "options": {"defaultType": category},
            }
            passphrase = getattr(s, f"{exchange.upper()}_PASSPHRASE", None)
            if passphrase:
                params["password"] = passphrase
            client_cls = getattr(ccxt, exchange.lower(), None)
            if client_cls is None:
                raise SafetyError(f"Unsupported exchange for reconciliation: {exchange}")
            client = client_cls(params)
            try:
                client.load_markets()
                if hasattr(client, "currencies") and isinstance(client.currencies, dict):
                    client.currencies.clear()
                if hasattr(client, "currencies_by_id") and isinstance(client.currencies_by_id, dict):
                    client.currencies_by_id.clear()
                if hasattr(client, "markets") and isinstance(client.markets, dict):
                    for m in client.markets.values():
                        if isinstance(m, dict):
                            m.pop("info", None)
            except Exception as exc:
                try:
                    if hasattr(client, "close"):
                        client.close()
                except Exception:
                    pass
                raise SafetyError(f"Failed to load markets for {exchange} {category}: {exc}")
            self.clients[norm_key] = client
            if norm_key != key:
                self.clients[key] = client
            self.client_states[norm_key] = STATE_OPEN
            self.client_states[key] = STATE_OPEN
            if norm_key not in self.client_locks:
                self.client_locks[norm_key] = threading.Lock()
            if key not in self.client_locks:
                self.client_locks[key] = self.client_locks[norm_key]
        return self.clients.get(norm_key) or self.clients.get(key)

    def _client_for_key_locked(self, key):
        # An exact binding takes precedence over a default-account alias.
        for candidate in (key, self._normalize_key(key)):
            if candidate in self.entries:
                return self.entries[candidate].client
            if candidate in self.clients:
                return self.clients[candidate]
        return None

    def _client_keys_locked(self, client):
        return {k for k, c in self.clients.items() if c is client} | {
            k for k, e in self.entries.items() if e.client is client}

    def _get_or_create_entry_locked(self, key, exchange=None, category=None):
        norm_key = self._normalize_key(key)
        client = self._client_for_key_locked(key)
        if client is None:
            exch, cat = norm_key[:2]
            client = self._get_or_create_client_locked(norm_key, exch, cat == "swap")
        # Only object identity proves aliases share a lease counter and lock.
        entry = next((e for e in self.entries.values() if e.client is client), None)
        if entry is None:
            canonical = norm_key
            bound = self._client_for_key_locked(norm_key)
            if bound is not None and bound is not client:
                canonical = key
            entry = ClientEntry(canonical, client, self.client_states.get(canonical, STATE_OPEN))
        self.entries[key] = entry
        self.entries[entry.key] = entry
        return entry

    def _try_claim_close_locked(self, key):
        """Claim all aliases of one object under the pool lock, never a live lease."""
        client = self._client_for_key_locked(key)
        if client is None:
            return None
        entry = self._get_or_create_entry_locked(key)
        aliases = self._client_keys_locked(client)
        if entry.active_leases:
            self.pending_close_keys.update(aliases)
            return None
        if entry.state != STATE_OPEN or id(client) in self.closed_client_ids:
            return None
        entry.state = STATE_CLOSING
        self.pending_close_keys.update(aliases)
        for alias in aliases:
            self.client_states[alias] = STATE_CLOSING
            self.clients.pop(alias, None)
            self.entries[alias] = entry
        return client

    def _safe_close_client(self, client_or_key, client=None):
        """CLOSED means retired from the pool, not confirmed resource release."""
        target = client_or_key if client is None else client
        if target is None:
            return
        client_id = id(target)
        with self.lock:
            aliases = self._client_keys_locked(target)
            if any(e.active_leases for e in self.entries.values() if e.client is target):
                self.pending_close_keys.update(aliases)
                return
            if client_id in self.closed_client_ids:
                return
            # Existing ownership guard records attempts, including failed attempts.
            self.closed_client_ids.add(client_id)
            self.pending_close_keys.update(aliases)
            for alias in aliases:
                self.client_states[alias] = STATE_CLOSING
                self.clients.pop(alias, None)
                if alias in self.entries:
                    self.entries[alias].state = STATE_CLOSING
        outcome = "UNSUPPORTED"
        try:
            if hasattr(target, "close") and callable(target.close):
                target.close()
                outcome = "SUCCEEDED"
        except Exception:
            outcome = "FAILED"
        with self.lock:
            # Fixed codes only: never retain exception text, credentials or keys.
            self.close_results[client_id] = outcome
            for alias in aliases | self._client_keys_locked(target):
                self.client_states[alias] = STATE_CLOSED
                self.pending_close_keys.discard(alias)
                if alias in self.entries:
                    self.entries[alias].state = STATE_CLOSED
            if self.closed and not self.pending_close_keys:
                self.state = STATE_CLOSED
        if outcome != "SUCCEEDED":
            logging.getLogger(__name__).warning(
                "CCXT close outcome=%s; client retired; resource release unconfirmed", outcome)

    def acquire_lease(self, exchange, is_futures: bool = False):
        """Atomically acquires client reference and registers active lease under pool lock.
        Active leases prevent the client from being closed during lock contention or queries."""
        if isinstance(exchange, tuple):
            key = exchange
            exch = str(key[0]).upper()
            cat = str(key[1]).lower() if len(key) > 1 else ("swap" if is_futures else "spot")
            fut = cat in ("swap", True)
        else:
            exch = str(exchange).upper()
            fut = is_futures
            cat = "swap" if fut else "spot"
            key = (exch, cat)

        norm_key = self._normalize_key(key)

        with self.lock:
            if self.closed or _shutdown_event.is_set() or self.state in ("CLOSING", "CLOSED", STATE_CLOSING, STATE_CLOSED):
                raise SafetyError("Reconciliation stopped: server is shutting down or client pool is closed")

            entry = self._get_or_create_entry_locked(key, exch, cat)
            norm_key = entry.key
            if entry.state in ("CLOSING", "CLOSED", STATE_CLOSING, STATE_CLOSED):
                raise SafetyError(f"Cannot acquire lease: client for {norm_key} is closing or closed")

            if norm_key not in self.client_locks:
                self.client_locks[norm_key] = threading.Lock()
            client_lock = self.client_locks[norm_key]

            self.active_leases[norm_key] = self.active_leases.get(norm_key, 0) + 1
            entry.active_leases = self.active_leases[norm_key]

            return ClientLease(self, key, entry.client, entry, client_lock)

    lease = acquire_lease

    def _release_lease(self, key, client, entry=None):
        target_to_close = None
        with self.lock:
            entry = entry or self.entries.get(key)
            if entry is None or entry.client is not client:
                return
            count = max(0, entry.active_leases - 1)
            entry.active_leases = count
            if count:
                self.active_leases[entry.key] = count
            else:
                self.active_leases.pop(entry.key, None)
            aliases = self._client_keys_locked(client)
            if count == 0 and (self.closed or aliases & self.pending_close_keys):
                target_to_close = self._try_claim_close_locked(entry.key)
        if target_to_close is not None:
            self._safe_close_client(entry.key, target_to_close)

    def safe_deferred_close(self, key, client=None):
        # A caller-supplied client never bypasses the atomic claim.
        with self.lock:
            client_to_close = self._try_claim_close_locked(key)
        if client_to_close is not None:
            self._safe_close_client(key, client_to_close)

    def get_client(self, exchange, is_futures: bool = False):
        norm_key = self._normalize_key(exchange, is_futures)
        with self.lock:
            if self.closed or _shutdown_event.is_set() or self.state in ("CLOSING", "CLOSED", STATE_CLOSING, STATE_CLOSED):
                raise SafetyError("Reconciliation stopped: server is shutting down or client pool is closed")
            if isinstance(exchange, tuple):
                exch = str(exchange[0]).upper()
                fut = exchange[1] in ("swap", True)
            else:
                exch = str(exchange).upper()
                fut = is_futures
            return self._get_or_create_client_locked(norm_key, exch, fut)

    def get_client_lock(self, exchange, is_futures: bool = False):
        norm_key = self._normalize_key(exchange, is_futures)
        with self.lock:
            if norm_key not in self.client_locks:
                self.client_locks[norm_key] = threading.Lock()
            return self.client_locks[norm_key]

    def get_active_leases(self, key=None):
        with self.lock:
            if key is not None:
                client = self._client_for_key_locked(key)
                entry = next((e for e in self.entries.values() if e.client is client), None)
                return entry.active_leases if entry is not None else 0
            return sum(self.active_leases.values())

    @property
    def state(self):
        if self.closed:
            return self._state if self._state in ("CLOSING", "CLOSED") else "CLOSED"
        return self._state

    @state.setter
    def state(self, value):
        self._state = value

    def close(self, timeout: float = 2.0):
        with self.lock:
            self.closed = True
            self.state = "CLOSING"
            keys = list(set(self.clients.keys()) | set(self.entries.keys()))

        clients_to_close = []
        for key in keys:
            lock_to_acquire = None
            with self.lock:
                norm_key = self._normalize_key(key)
                client = self._client_for_key_locked(key)
                if client is None:
                    continue
                entry = self._get_or_create_entry_locked(key)
                if entry.active_leases > 0:
                    self.pending_close_keys.update(self._client_keys_locked(client))
                    continue

                if entry.state != STATE_OPEN:
                    continue
                lock_to_acquire = self.client_locks.get(key) or self.client_locks.get(norm_key)

            if lock_to_acquire is not None:
                acquired = lock_to_acquire.acquire(timeout=timeout)
                if acquired:
                    try:
                        with self.lock:
                            client = self._try_claim_close_locked(key)
                            if client is not None:
                                clients_to_close.append((key, client))
                    finally:
                        lock_to_acquire.release()
                else:
                    with self.lock:
                        client = self._client_for_key_locked(key)
                        if client is not None and self.entries[key].state != STATE_CLOSED:
                            self.pending_close_keys.update(self._client_keys_locked(client))
            else:
                with self.lock:
                    client = self._try_claim_close_locked(key)
                    if client is not None:
                        clients_to_close.append((key, client))

        for k, c in clients_to_close:
            self._safe_close_client(k, c)

        with self.lock:
            if not self.pending_close_keys:
                self.state = "CLOSED"


_recon_clients_pool = None
_recon_clients_lock = threading.Lock()

_reconcile_lock = threading.Lock()
_in_flight_reconciles = set()
_reconcile_semaphore = threading.Semaphore(2)

_shutdown_event = threading.Event()
_active_queries_lock = threading.Lock()
_active_queries_count = 0
_queries_drained_event = threading.Event()
_queries_drained_event.set()


def begin_shutdown():
    _shutdown_event.set()


def is_shutting_down():
    return _shutdown_event.is_set()


def wait_for_reconciliation_drained(timeout: float = 10.0) -> bool:
    return _queries_drained_event.wait(timeout=timeout)


def reset_reconciliation_state():
    """Testing helper to reset shutdown flags and client pools between test cases."""
    global _shutdown_event, _active_queries_count, _queries_drained_event, _recon_clients_pool
    _shutdown_event.clear()
    with _active_queries_lock:
        _active_queries_count = 0
        _queries_drained_event.set()
    with _reconcile_lock:
        _in_flight_reconciles.clear()
    with _recon_clients_lock:
        if _recon_clients_pool is not None:
            _recon_clients_pool.close()
            _recon_clients_pool = None


def get_reconciliation_clients(settings):
    global _recon_clients_pool
    with _recon_clients_lock:
        if _recon_clients_pool is None or _recon_clients_pool.settings != settings or _recon_clients_pool.closed:
            _recon_clients_pool = ReconciliationClients(settings)
        return _recon_clients_pool


def close_reconciliation_clients(timeout: float = 10.0):
    _queries_drained_event.wait(timeout=timeout)
    global _recon_clients_pool
    with _recon_clients_lock:
        if _recon_clients_pool is not None:
            _recon_clients_pool.close(timeout=min(timeout, 2.0))
            _recon_clients_pool = None


def reconcile_pending(settings, get_bot=None):
    if _shutdown_event.is_set():
        return
    manager = get_manager(settings)
    pending_rows = manager.store.pending()
    for row in pending_rows:
        if _shutdown_event.is_set():
            break
        if row["state"] not in {"UNKNOWN", "ACCEPTED", "PARTIALLY_FILLED"}:
            continue
        signal = row["signal"]
        with _reconcile_lock:
            if signal in _in_flight_reconciles or _shutdown_event.is_set():
                continue
            _in_flight_reconciles.add(signal)

        with _active_queries_lock:
            if _shutdown_event.is_set():
                with _reconcile_lock:
                    _in_flight_reconciles.discard(signal)
                break
            global _active_queries_count
            _active_queries_count += 1
            _queries_drained_event.clear()

        try:
            with _reconcile_semaphore:
                if _shutdown_event.is_set():
                    continue

                from types import SimpleNamespace
                info = SimpleNamespace(exchange=row["exchange"], is_futures=row["futures"])

                # Dedicated read-only client from pool: never mutate trading bot or options["defaultType"]
                client = None
                if get_bot is not None:
                    mod = getattr(get_bot, "__module__", "")
                    name = getattr(get_bot, "__name__", "")
                    is_real_trading_bot = (
                        "pexchange" in mod or
                        (name == "get_bot" and "exchange" in mod)
                    )
                    if not is_real_trading_bot:
                        try:
                            client = get_bot(row["exchange"], row["futures"])
                        except TypeError:
                            bot_obj = get_bot(row["exchange"])
                            client = getattr(bot_obj, "client", bot_obj)

                if client is not None:
                    adapter_bot = SimpleNamespace(client=client, position_mode="one-way")
                    result = Adapter(adapter_bot, info).reconcile(row)
                else:
                    pool = get_reconciliation_clients(settings)
                    if isinstance(pool, ReconciliationClients):
                        with pool.acquire_lease(row["exchange"], row["futures"]) as recon_client:
                            adapter_bot = SimpleNamespace(client=recon_client, position_mode="one-way")
                            result = Adapter(adapter_bot, info).reconcile(row)
                    else:
                        recon_client = pool.get_client(row["exchange"], row["futures"])
                        if hasattr(pool, "get_client_lock"):
                            lock = pool.get_client_lock(row["exchange"], row["futures"])
                            with lock:
                                adapter_bot = SimpleNamespace(client=recon_client, position_mode="one-way")
                                result = Adapter(adapter_bot, info).reconcile(row)
                        else:
                            adapter_bot = SimpleNamespace(client=recon_client, position_mode="one-way")
                            result = Adapter(adapter_bot, info).reconcile(row)

                if result:
                    with manager.mutex:
                        manager.record(row, result)
        except Exception:
            manager.notify("RECONCILIATION_FAILED", row)
        finally:
            with _active_queries_lock:
                _active_queries_count -= 1
                if _active_queries_count == 0:
                    _queries_drained_event.set()
            with _reconcile_lock:
                _in_flight_reconciles.discard(signal)


async def reconciliation_loop(settings, get_bot=None):
    import asyncio
    while not _shutdown_event.is_set():
        try:
            await asyncio.to_thread(reconcile_pending, settings, get_bot)
        except asyncio.CancelledError:
            break
        except Exception:
            # A failed query never permits a new submission.
            pass
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
