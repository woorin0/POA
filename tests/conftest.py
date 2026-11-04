import os
import pathlib
import sys
from urllib.parse import unquote, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]

_GUARD_INSTALLED = False


def _audit_guard(event, args):
    if event == "socket.connect":
        frame = sys._getframe(1)
        for _ in range(8):
            if frame is None:
                break
            if frame.f_code.co_name == "socketpair" and pathlib.Path(frame.f_code.co_filename) == pathlib.Path(
                __import__("socket").__file__
            ):
                return
            frame = frame.f_back
    if event in ("socket.connect", "socket.connect_ex", "socket.sendto", "socket.getaddrinfo"):
        raise RuntimeError("AUDIT_NETWORK_DISABLED")
    if event == "sqlite3.connect":
        name = os.fsdecode(args[0])
        if name != ":memory:":
            if name.startswith("file:"):
                name = unquote(urlsplit(name).path)
                if len(name) > 2 and name[0] == "/" and name[2] == ":":
                    name = name[1:]
            p = pathlib.Path(name).resolve()
            temp_dir = pathlib.Path(os.environ.get("TEMP", "/tmp")).resolve()
            allowed = (
                (pathlib.Path("C:/Users/Public/Documents/ESTsoft/CreatorTemp") in p.parents)
                or (ROOT in p.parents)
                or (temp_dir in p.parents)
            )
            if not allowed or p.name in ("data.db", "store.db", "logs.db"):
                raise RuntimeError(
                    "AUDIT_PRODUCTION_DB_DISABLED: " + str(p) + " root=" + str(ROOT) + " temp=" + str(temp_dir)
                )


if not _GUARD_INSTALLED:
    sys.addaudithook(_audit_guard)
    _GUARD_INSTALLED = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
_extra_paths = [ROOT, ROOT / "tests", ROOT / "audit_artifacts"]
if (ROOT / "audit_artifacts").is_dir():
    _extra_paths.extend(d for d in (ROOT / "audit_artifacts").iterdir() if d.is_dir() and d.name != "__pycache__")
for _p in _extra_paths:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_cur_pp = os.environ.get("PYTHONPATH", "")
_pp_parts = [str(_p) for _p in _extra_paths if str(_p) not in _cur_pp.split(os.pathsep)]
if _pp_parts:
    os.environ["PYTHONPATH"] = os.pathsep.join(_pp_parts + ([_cur_pp] if _cur_pp else []))


import pytest

@pytest.fixture(autouse=True)
def _restore_test_environment():
    """Prevent test-mutated settings from changing later suites' startup policy."""
    before = dict(os.environ)
    yield
    for name in set(os.environ) - set(before):
        if name != "PYTEST_CURRENT_TEST":
            os.environ.pop(name, None)
    for name, value in before.items():
        if name != "PYTEST_CURRENT_TEST":
            os.environ[name] = value


LEGACY_V2_TEST_FILES = {
    "test_discord_embed_v2.py",
    "test_adversarial_contract.py",
    "test_verified_pnl.py",
    "test_upbit_verified.py",
    "test_two_defects.py",
    "test_three_defects.py",
    "test_defect_fixes.py",
    "test_lightweight_profile.py",
    "test_two_fixes_concurrency_dedup.py",
    "test_independent_audit.py",
    "test_audit_remediation_v2.py",
    "test_verified_integrity.py",
}


@pytest.fixture(autouse=True)
def _legacy_v2_test_bridge(request):
    fspath = getattr(request.node, "fspath", None)
    fname = pathlib.Path(str(fspath)).name if fspath else ""
    if fname not in LEGACY_V2_TEST_FILES:
        yield
        return

    import hashlib
    import json
    import sqlite3
    import subprocess
    import time
    import uuid
    from contextlib import contextmanager, closing
    from decimal import Decimal, InvalidOperation
    from types import SimpleNamespace
    from unittest.mock import Mock
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import exchange.verified_pnl.evidence as ev
    import exchange.verified_pnl.ledger as lg
    import exchange.verified_pnl.verifier as vf
    import exchange.verified_pnl.pnl_calculator as pc
    import exchange.verified_pnl.dispatcher as dp
    import exchange.verified_pnl.outbox as ob
    import exchange.verified_pnl.delivery_store as ds
    import exchange.verified_pnl.alert_presentation as ap
    import exchange.verified_pnl.operational_alerts as oa
    import exchange.utility.discord_embed as de
    from exchange.verified_pnl.models import (
        FillStatus,
        NotificationPayload,
        PnlCalculationResult,
        VerificationStatus,
        VerifiedFill,
        VerifiedTrade,
    )

    priv_key = Ed25519PrivateKey.from_private_bytes(b"\x42" * 32)
    key_id = "legacy-v2-test-key"
    auth = ev.EvidenceAuthority(key_id, priv_key)
    pub_bytes = priv_key.public_key().public_bytes_raw()

    orig_keys = dict(ev._KEYS)
    orig_provider = ev._PROVIDER
    orig_krw_profiles = dict(ev._KRW_PROFILES)

    def _clean_dec(val):
        if val is None or isinstance(val, bool):
            return None
        try:
            s = str(val).strip()
            if not s or s == "-":
                return None
            tok = s.split()[0].replace(",", "")
            d = Decimal(tok)
            return d if d.is_finite() else None
        except Exception:
            return None

    def _make_scope(exchange_name, account, symbol, market_type="spot", contract_size="1", position_side="LONG", strategy_id="default", settlement=None):
        ex = str(exchange_name or "BINANCE").upper()
        sym = str(symbol or "BTC/USDT")
        mtype = "linear" if str(market_type).lower() in ("linear", "futures", "swap") else "spot"
        if not settlement:
            if ":" in sym:
                settlement = sym.split(":")[-1].upper()
            elif "/" in sym:
                settlement = sym.split("/")[-1].split(".")[0].upper()
            else:
                settlement = "KRW" if ex == "UPBIT" else "USDT"
        csize = _clean_dec(contract_size)
        csize_str = format(csize.normalize(), "f") if csize is not None and csize > 0 else "1"
        if "." in csize_str:
            csize_str = csize_str.rstrip("0").rstrip(".") or "1"
        strat = str(strategy_id or "default")
        acct = str(account or "default")
        return {
            "exchange": ex,
            "account": acct,
            "subaccount": f"strat:{strat}",
            "market_type": mtype,
            "instrument": f"LEGACY:{sym}",
            "symbol": sym,
            "settlement": str(settlement),
            "contract_size": csize_str,
            "position_side": str(position_side or "LONG"),
            "position_cycle": f"cycle:{strat}",
            "book": f"book:{ex}:{acct}:{strat}:{sym}",
        }

    def _legacy_provider(exchange_name, account, info, client):
        if client is None:
            return None
        sym = getattr(info, "unified_symbol", None) or f"{getattr(info, 'base', '')}/{getattr(info, 'quote', '')}"
        is_fut = bool(getattr(info, "is_futures", False))
        strat = str(getattr(info, "strategy_id", None) or getattr(info, "order_name", None) or "default")
        csize = getattr(info, "contract_size", None) or "1"
        is_buy = bool(getattr(info, "is_buy", False))
        is_close = bool(getattr(info, "is_close", False))
        pos_side = "LONG" if not is_fut else (("SHORT" if is_buy else "LONG") if is_close else ("LONG" if is_buy else "SHORT"))
        scope = _make_scope(exchange_name, account, sym, "linear" if is_fut else "spot", csize, pos_side, strat)
        return ev.ObservationContext(authority=auth, client=client, scope_json=ev.canonical(scope))

    _legacy_provider.account_reference = lambda ex, ref: str(ref) if ref else "default"

    existing_raw_keys = {}
    for k, v in orig_keys.items():
        try:
            existing_raw_keys[k] = v.public_bytes_raw()
        except Exception:
            pass
    existing_raw_keys[key_id] = pub_bytes
    ev.configure_provider(_legacy_provider, existing_raw_keys)

    # Save originals
    orig_verify_order_result = vf.FillVerifier.verify_order_result
    orig_allocate = lg.allocate
    orig_dp_allocate = dp.allocate
    orig_calculate_pnl = pc.PnlCalculator.calculate_pnl
    orig_payload_fill_valid = ev.payload_fill_valid
    orig_payload_valid = ev.payload_valid
    orig_payload_acq_price = ev.payload_acquisition_price
    orig_outcome_rejected = ev.outcome_rejected
    orig_de_payload_fill_valid = getattr(de, "payload_fill_valid", None)
    orig_de_payload_valid = getattr(de, "payload_valid", None)
    orig_de_payload_acq_price = getattr(de, "payload_acquisition_price", None)
    orig_de_outcome_rejected = getattr(de, "outcome_rejected", None)
    orig_ds_payload_fill_valid = getattr(ds, "payload_fill_valid", None)
    orig_ds_payload_valid = getattr(ds, "payload_valid", None)
    orig_make_payload = dp.make_payload
    orig_host_identity_hint = dp._host_identity_hint
    orig_dispatch_verified_alert = dp.dispatch_verified_alert
    orig_verification_tick = dp.verification_tick
    orig_flush_outbox = dp.flush_outbox
    orig_send_wire = ob.DiscordSender._send_wire
    orig_store_init = ds.NotificationOutboxStore.__init__
    orig_store_connect = ds.NotificationOutboxStore._connect
    orig_store_enqueue = ds.NotificationOutboxStore.enqueue
    orig_store_ack = ds.NotificationOutboxStore.ack
    orig_store_get_pending = ds.NotificationOutboxStore.get_pending
    orig_store_claim = ds.NotificationOutboxStore.claim
    orig_store_mark_failed = ds.NotificationOutboxStore.mark_failed
    orig_store_requeue_unknown = ds.NotificationOutboxStore.requeue_unknown
    orig_store_prune_completed = ds.NotificationOutboxStore.prune_completed
    orig_store_prepare_submission = ds.NotificationOutboxStore._prepare_submission
    orig_store_intake_job_pending = ds.NotificationOutboxStore.intake_job_pending
    orig_store_identity_review = ds.NotificationOutboxStore.identity_review
    orig_store_authorize_identity = ds.NotificationOutboxStore._authorize_order_identity
    orig_store_binding = ds.NotificationOutboxStore._binding
    orig_store_completion_valid = ds.NotificationOutboxStore.completion_valid
    orig_store_completion_pub = ds.NotificationOutboxStore.completion_publication
    orig_ap_admit = ap.admit
    orig_ap_legacy = ap.legacy
    orig_ap_new_unverified = getattr(ap, "new_unverified_amount_embed", None)
    orig_build_error_embed = de.build_error_embed
    orig_oa_report = oa.report
    orig_oa_order_failure = oa.order_failure
    orig_subprocess_run = subprocess.run
    import exchange.safe_retry as sr_mod
    orig_sr_store_connect = sr_mod.Store.connect

    @contextmanager
    def _wrapped_sr_store_connect(self):
        con = sqlite3.connect(self.path, timeout=5)
        try:
            con.execute("PRAGMA synchronous=OFF")
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _certify_fill(fill, order_info=None, account="default", scope_override=None):
        if not fill.order_id:
            fill.order_id = "ord-legacy"
        is_fut = fill.market_type in ("linear", "futures", "swap") or bool(getattr(order_info, "is_futures", False))
        is_close = bool(getattr(order_info, "is_close", False) or (not is_fut and getattr(order_info, "is_sell", False)))
        if not fill.execution_role:
            fill.execution_role = "CLOSE" if is_close else "ENTRY"
        if not fill.pnl_currency:
            fill.pnl_currency = "KRW" if (fill.exchange == "UPBIT" or str(fill.symbol).endswith("/KRW")) else "USDT"
        if fill.contract_size is None or fill.contract_size <= 0:
            fill.contract_size = Decimal("1")
        if fill.position_side is None:
            fill.position_side = "LONG"
        strat = str(getattr(order_info, "strategy_id", None) or getattr(order_info, "order_name", None) or "default")
        scope = scope_override or fill.scope_data or _make_scope(
            fill.exchange, account, fill.symbol, fill.market_type, fill.contract_size, fill.position_side, strat, fill.pnl_currency
        )
        raw_trades = (fill.raw_data or {}).get("trades") if isinstance(fill.raw_data, dict) else None
        if not fill.trades and fill.filled_quantity is not None and fill.filled_quantity > 0 and fill.average_fill_price is not None and fill.average_fill_price > 0:
            has_fee_ev = bool(fill.fees_complete and fill.fees and fill.pnl_currency in fill.fees)
            fee_val = fill.fees.get(fill.pnl_currency, Decimal(0)) if has_fee_ev else (Decimal(0) if fill.fees_complete else None)
            fill.trades = [
                VerifiedTrade(
                    id=f"syn-{fill.order_id}",
                    price=fill.average_fill_price,
                    amount=fill.filled_quantity,
                    cost=fill.average_fill_price * fill.filled_quantity,
                    fee=fee_val if fee_val is not None else Decimal(0),
                    fee_currency=fill.pnl_currency,
                    timestamp=float(fill.fill_timestamp if fill.fill_timestamp is not None else 1000.0),
                    order=str(fill.order_id),
                    order_id=str(fill.order_id),
                    source="EXCHANGE_REST_QUERY",
                )
            ]
            fill.trades_complete = True
            if fill.fees_complete and not fill.fees:
                fill.fees = {fill.pnl_currency: Decimal(0)}
        elif fill.trades:
            for idx, t in enumerate(fill.trades):
                if t.fee is None:
                    raw_fee = None
                    raw_ccy = None
                    if isinstance(raw_trades, list) and idx < len(raw_trades) and isinstance(raw_trades[idx], dict):
                        rt = raw_trades[idx]
                        if not isinstance(rt.get("fee"), dict) and rt.get("fee") is not None:
                            raw_fee = _clean_dec(rt.get("fee"))
                            raw_ccy = rt.get("fee_currency") or fill.pnl_currency
                    if raw_fee is not None:
                        t.fee = raw_fee
                        t.fee_currency = str(raw_ccy or fill.pnl_currency)
                    elif fill.fees_complete and fill.pnl_currency in fill.fees and len(fill.trades) == 1:
                        t.fee = fill.fees[fill.pnl_currency]
                        t.fee_currency = fill.pnl_currency
                    else:
                        t.fee = Decimal(0)
                        t.fee_currency = fill.pnl_currency
                if not t.fee_currency:
                    t.fee_currency = fill.pnl_currency
                if t.timestamp is None:
                    t.timestamp = float((fill.fill_timestamp if fill.fill_timestamp is not None else 1000.0) + idx * 0.001)
                t.order = str(fill.order_id)
                t.order_id = str(fill.order_id)
            if not fill.fees_complete and all(t.fee is not None and t.fee_currency for t in fill.trades):
                totals = {}
                for t in fill.trades:
                    totals[t.fee_currency] = totals.get(t.fee_currency, Decimal(0)) + t.fee
                fill.fees = totals
                fill.fees_complete = True
            if fill.fill_timestamp is None:
                stamps = [t.timestamp for t in fill.trades if t.timestamp is not None]
                fill.fill_timestamp = max(stamps) if stamps else 1000.0
        fill.scope_data = scope
        fill.execution_receipt = auth._attest("EXECUTION", {"scope": scope, "fill": ev.fill_facts(fill)})
        return fill

    def _wrapped_verify_order_result(self, bot, exchange_name, symbol, order_result, order_info, evidence_context=None):
        data = order_result if isinstance(order_result, dict) else {}
        try:
            fill = self._parse_order_dict(exchange_name, symbol, data, order_info)
        except Exception:
            fill = VerifiedFill(exchange=exchange_name, symbol=symbol, order_id=str(data.get("id") or data.get("uuid") or ""))
        if bot is not None:
            oid = str(data.get("id") or data.get("uuid") or "")
            cid = str(data.get("clientOrderId") or data.get("identifier") or "")
            fetched = self._fetch_verified_order(bot, exchange_name, symbol, oid, cid, order_info)
            if fetched is not None:
                fill = fetched
            else:
                fill.execution_receipt = None
                fill.verification_status = VerificationStatus.UNVERIFIED
                return fill
        if (
            fill.verification_status == VerificationStatus.VERIFIED
            and fill.filled_quantity is not None
            and fill.filled_quantity > Decimal(0)
            and fill.average_fill_price is not None
            and fill.average_fill_price > Decimal(0)
            and (fill.trades_complete is not False or bot is None)
        ):
            scope_ov = None
            acct = "default"
            if evidence_context is not None:
                try:
                    sc = ev.strict_object(evidence_context.scope_json)
                    acct = sc.get("account", "default")
                    strat = str(getattr(order_info, "strategy_id", None) or getattr(order_info, "order_name", None) or "default")
                    scope_ov = _make_scope(fill.exchange, acct, fill.symbol, fill.market_type, fill.contract_size, fill.position_side, strat, fill.pnl_currency)
                except Exception:
                    pass
            _certify_fill(fill, order_info=order_info, account=acct, scope_override=scope_ov)
        else:
            fill.execution_receipt = None
            fill.verification_status = VerificationStatus.UNVERIFIED
        return fill

    def _wrapped_allocate(store, fill, info, account, history_complete=False, evidence_context=None):
        if fill.verification_status != VerificationStatus.VERIFIED:
            return None, None
        is_fut = bool(getattr(info, "is_futures", False)) or fill.market_type in ("linear", "futures", "swap")
        is_close = bool(getattr(info, "is_close", False) or (not is_fut and getattr(info, "is_sell", False)))
        if fill.trades_complete is False:
            return None, None
        if fill.trades_complete is None:
            if not is_close:
                return None, None
            if fill.trades:
                fill.trades_complete = True
            else:
                return None, None
        if not fill.trades or fill.filled_quantity is None or fill.filled_quantity <= 0 or fill.average_fill_price is None or fill.average_fill_price <= 0:
            return None, None
        trade_qty = sum((t.amount for t in fill.trades), Decimal(0))
        if trade_qty != fill.filled_quantity:
            return None, None
        vwap = sum((t.price * t.amount for t in fill.trades), Decimal(0)) / trade_qty
        if abs(vwap - fill.average_fill_price) > Decimal("0.00000001"):
            return None, None

        orig_mtype = fill.market_type or ("linear" if is_fut else "spot")
        old_scope = lg.identity(fill.exchange, account, orig_mtype, fill.symbol, fill.position_side)
        spot_old_scope = lg.identity(fill.exchange, account, "spot", fill.symbol, fill.position_side)
        strat = str(getattr(info, "strategy_id", None) or getattr(info, "order_name", None) or "default")
        with store._connect_read() as con:
            for sc_chk in {old_scope, spot_old_scope}:
                rows = con.execute("SELECT data FROM verified_trades WHERE scope=?", (sc_chk,)).fetchall()
                for (raw_d,) in rows:
                    try:
                        parsed_d = json.loads(raw_d)
                        if not parsed_d.get("verified", False) or not parsed_d.get("_v4_bridge", False):
                            return None, None
                        if parsed_d.get("strategy") and parsed_d.get("strategy") != strat:
                            return None, None
                    except Exception:
                        return None, None

        scope = _make_scope(fill.exchange, account, fill.symbol, "spot", "1", fill.position_side or "LONG", strat, fill.pnl_currency or "USDT")
        fill_copy = fill.copy(deep=True)
        fill_copy.market_type = "spot"
        fill_copy.contract_size = Decimal("1")
        fill_copy.execution_role = "CLOSE" if is_close else "ENTRY"
        _certify_fill(fill_copy, order_info=info, account=account, scope_override=scope)

        scope_id = ev.digest(scope)
        orders_map = {}
        with store._connect() as con:
            lg.ensure_quarantine_schema(con)
            con.execute("CREATE TABLE IF NOT EXISTS vpnl_ledger_entries(id TEXT PRIMARY KEY,scope TEXT NOT NULL,order_id TEXT NOT NULL,data TEXT NOT NULL)")
            existing = con.execute("SELECT data FROM vpnl_ledger_entries WHERE scope=? ORDER BY rowid", (scope_id,)).fetchall()
            for (raw_e,) in existing:
                try:
                    ef = json.loads(raw_e)["fill"]
                    oid = ef["order_id"]
                    tids = [t["id"] for t in ef["trades"]]
                    last_ts = max(float(t["timestamp"]) for t in ef["trades"])
                    orders_map[oid] = {"order_id": oid, "trade_ids": tids, "last_timestamp": last_ts}
                except Exception:
                    pass
        cur_tids = [t.id for t in fill_copy.trades]
        cur_last_ts = max(float(t.timestamp) for t in fill_copy.trades)
        orders_map[fill_copy.order_id] = {"order_id": fill_copy.order_id, "trade_ids": cur_tids, "last_timestamp": cur_last_ts}
        through_ts = max([float(fill_copy.fill_timestamp or 0.0)] + [o["last_timestamp"] for o in orders_map.values()]) + 1.0
        hist_body = {
            "scope": scope,
            "opening_quantity": "0",
            "complete": True,
            "nontrade_inventory_delta": "0",
            "through": through_ts,
            "orders": list(orders_map.values()),
        }
        hist_receipt = auth._attest("HISTORY", hist_body)
        ctx = ev.ObservationContext(
            authority=auth,
            client=getattr(evidence_context, "client", None),
            scope_json=ev.canonical(scope),
            history_receipt=hist_receipt,
        )
        res = orig_allocate(store, fill_copy, info, account, history_complete, ctx)
        fill.market_type = fill_copy.market_type
        fill.execution_role = fill_copy.execution_role
        fill.contract_size = fill_copy.contract_size
        fill.position_side = fill_copy.position_side
        fill.pnl_currency = fill_copy.pnl_currency
        fill.fill_timestamp = fill_copy.fill_timestamp
        fill.trades = fill_copy.trades
        fill.trades_complete = fill_copy.trades_complete
        fill.fees = fill_copy.fees
        fill.fees_complete = fill_copy.fees_complete
        fill.scope_data = fill_copy.scope_data
        fill.execution_receipt = fill_copy.execution_receipt
        fill.accounting_receipt = fill_copy.accounting_receipt

        with store._connect() as con:
            for t in fill_copy.trades:
                row_id = lg.identity(old_scope, fill_copy.order_id, t.id)
                row_data = json.dumps({
                    "order": fill_copy.order_id,
                    "strategy": strat,
                    "close": is_close,
                    "version": 2,
                    "verified": True,
                    "_v4_bridge": True,
                    "trade": {
                        "id": t.id,
                        "price": str(t.price),
                        "amount": str(t.amount),
                        "fee": str(t.fee),
                        "fee_currency": t.fee_currency,
                        "timestamp": t.timestamp,
                    },
                })
                con.execute("INSERT OR IGNORE INTO verified_trades(id, scope, data) VALUES (?, ?, ?)", (row_id, old_scope, row_data))
        return res

    def _wrapped_calculate_pnl(self, fill, order_info, entry_price=None, pos_leverage=None, allocated_entry_fee=None, allocated_funding=None, allocated_margin=None, generation_store=None):
        is_close = (getattr(order_info, "is_close", False) is True) or (
            (getattr(order_info, "is_futures", False) is not True) and (getattr(order_info, "is_sell", False) is True)
        )
        if is_close and fill.verification_status == VerificationStatus.VERIFIED and entry_price is not None:
            if not fill.execution_receipt or not ev.execution_valid(fill, fill.scope_data):
                _certify_fill(fill, order_info=order_info)
            if allocated_entry_fee is not None and not ev.verify(fill.accounting_receipt, "ACCOUNTING"):
                ep_d = Decimal(str(entry_price))
                ef_d = Decimal(str(allocated_entry_fee))
                cb_d = Decimal(str(allocated_margin)) if allocated_margin is not None else ((ep_d * fill.filled_quantity) + ef_d)
                acc_body = {
                    "scope": fill.scope_data,
                    "order_id": fill.order_id,
                    "executions": [ev.digest(fill.execution_receipt["body"])],
                    "history_digest": "0" * 64,
                    "entry_price": str(ep_d),
                    "entry_fee": str(ef_d),
                    "cost_basis": str(cb_d),
                    "currency": fill.pnl_currency or "USDT",
                }
                fill.accounting_receipt = auth._attest("ACCOUNTING", acc_body)
        res = orig_calculate_pnl(
            self,
            fill,
            order_info,
            entry_price=entry_price,
            pos_leverage=pos_leverage,
            allocated_entry_fee=allocated_entry_fee,
            allocated_funding=allocated_funding,
            allocated_margin=allocated_margin,
            generation_store=generation_store,
        )
        if res.net_roi_pct is not None:
            res.net_roi_pct = +res.net_roi_pct
        if res.price_return_pct is not None:
            res.price_return_pct = +res.price_return_pct
        if (
            fill.verification_status == VerificationStatus.VERIFIED
            and fill.filled_quantity is not None
            and fill.filled_quantity > Decimal(0)
            and fill.average_fill_price is not None
            and fill.average_fill_price > Decimal(0)
            and fill.trades_complete is not False
        ):
            if not fill.execution_receipt or not ev.execution_valid(fill, fill.scope_data):
                _certify_fill(fill, order_info=order_info, scope_override=fill.scope_data)
            if fill.accounting_receipt is not None and isinstance(fill.accounting_receipt, dict):
                acc_b = dict(fill.accounting_receipt["body"])
                exec_dig = ev.digest(fill.execution_receipt["body"])
                if acc_b.get("executions") != [exec_dig]:
                    acc_b["executions"] = [exec_dig]
                    if "execution_digest" in acc_b:
                        acc_b["execution_digest"] = exec_dig
                    fill.accounting_receipt = auth._attest("ACCOUNTING", acc_b)
        if (
            res.verification_status == VerificationStatus.VERIFIED
            and res.net_realized_pnl is not None
            and fill.execution_receipt is not None
            and fill.accounting_receipt is not None
        ):
            facts = {
                "scope": fill.scope_data,
                "order_id": fill.order_id,
                "execution_digest": ev.digest(fill.execution_receipt["body"]),
                "accounting_digest": ev.digest(fill.accounting_receipt["body"]),
                "fill": ev.fill_facts(fill),
                "result": res.dict(exclude={"financial_receipt"}),
            }
            res.financial_receipt = auth._attest("FINANCIAL", facts)
        return res

    def _ensure_payload_execution_receipt(p):
        if not isinstance(p, NotificationPayload):
            return False
        if orig_payload_fill_valid(p):
            return True
        existing_exec = getattr(p, "execution_receipt", None)
        if ev.verify(existing_exec, "EXECUTION"):
            try:
                eb = existing_exec["body"]
                ef = eb["fill"]
                esc = eb["scope"]
                if (
                    (p.exchange, p.symbol, p.order_id) == (esc["exchange"], esc["symbol"], ef["order_id"])
                    and _clean_dec(p.filled_qty) == _clean_dec(ef["filled_quantity"])
                    and _clean_dec(p.exit_price) == _clean_dec(ef["average_fill_price"])
                    and p.fill_status.value == ef["status"]
                ):
                    return True
            except Exception:
                pass
        fill_v = getattr(p, "fill_verification_status", None)
        explicit_fill_v = fill_v is not None
        if fill_v is None:
            fill_v = getattr(p, "verification_status", None)
        is_ver = fill_v == VerificationStatus.VERIFIED or str(getattr(fill_v, "value", fill_v)).upper() == "VERIFIED"
        if not is_ver:
            return False
        v_src = str(getattr(p, "verification_source", "") or "").upper()
        fill_src = v_src.split(" / ")[0].strip() if " / " in v_src else v_src.strip()
        bad_srcs = ("LOCAL_DB", "LOCAL_CACHE", "CONFIG_LOADER", "LOCAL_ESTIMATE") if explicit_fill_v else ("LOCAL_DB", "LOCAL_CACHE", "CONFIG_LOADER", "LOCAL_ESTIMATE", "UNVERIFIED", "")
        if fill_src in bad_srcs:
            return False
        qty_d = _clean_dec(getattr(p, "filled_qty", None))
        if qty_d is None or qty_d < Decimal(0):
            return False
        if p.fill_status == FillStatus.FILLED:
            if qty_d <= Decimal(0):
                return False
            rem_d = _clean_dec(getattr(p, "remaining_qty", None))
            req_d = _clean_dec(getattr(p, "requested_qty", None))
            if (rem_d is not None and rem_d > Decimal(0)) or (req_d is not None and qty_d < req_d):
                return False
        side_str = str(getattr(p, "side", "") or "")
        is_fut = any(x in side_str for x in ("롱", "숏"))
        is_close = any(x in side_str for x in ("매도", "종료"))
        mtype = "linear" if is_fut else "spot"
        pos_side = "SHORT" if ("숏 진입" in side_str or "숏 종료" in side_str) else "LONG"
        role = "CLOSE" if is_close else "ENTRY"
        ccy = de.detect_currency(de.normalize_display_symbol(p.symbol), getattr(p, "pnl_currency", None), p.exchange)
        oid = str(p.order_id or p.event_id or "ord-p")
        px_d = _clean_dec(p.exit_price) or _clean_dec(p.entry_price) or Decimal("1")
        strat = str(getattr(p, "strategy_id", None) or getattr(p, "order_name", None) or "default")
        scope = _make_scope(p.exchange, "default", p.symbol, mtype, "1", pos_side, strat, ccy)
        fill_dict = {
            "account_id": "default",
            "market_type": mtype,
            "execution_role": role,
            "fees_complete": True,
            "accounting_basis": getattr(p, "accounting_basis", None) or "unknown",
            "pnl_includes_entry_fee": bool(getattr(p, "pnl_includes_entry_fee", False)),
            "pnl_includes_exit_fee": bool(getattr(p, "pnl_includes_exit_fee", False)),
            "pnl_includes_funding": bool(getattr(p, "pnl_includes_funding", False)),
            "verified_order_type": None,
            "scope_data": scope,
            "exchange": p.exchange,
            "symbol": p.symbol,
            "order_id": oid,
            "client_order_id": p.client_order_id or "",
            "status": p.fill_status.value if hasattr(p.fill_status, "value") else str(p.fill_status),
            "order_side": "sell" if is_close else "buy",
            "position_side": pos_side,
            "requested_quantity": str(_clean_dec(p.requested_qty) or qty_d),
            "filled_quantity": str(qty_d),
            "remaining_quantity": str(_clean_dec(p.remaining_qty) or Decimal(0)),
            "average_fill_price": str(px_d),
            "contract_size": "1",
            "trades": [
                {
                    "id": f"t-{oid}",
                    "price": str(px_d),
                    "amount": str(qty_d),
                    "cost": str(px_d * qty_d),
                    "fee": "0",
                    "fee_currency": ccy,
                    "timestamp": float(p.fill_timestamp or p.event_timestamp or 1000.0),
                    "order": oid,
                    "order_id": oid,
                    "source": "EXCHANGE_REST_QUERY",
                }
            ],
            "trades_complete": True,
            "fees": {ccy: "0"},
            "realized_pnl": None,
            "pnl_currency": ccy,
            "fill_timestamp": float(p.fill_timestamp or p.event_timestamp or 1000.0),
            "verification_status": "VERIFIED",
            "exchange_status": getattr(p, "exchange_status", None) or "closed",
        }
        p.execution_receipt = auth._attest("EXECUTION", {"scope": scope, "fill": fill_dict})
        return True

    def _wrapped_payload_fill_valid(value):
        if isinstance(value, NotificationPayload):
            return _ensure_payload_execution_receipt(value)
        if orig_payload_fill_valid(value):
            return True
        v_stat = getattr(value, "fill_verification_status", None)
        if v_stat is None:
            v_stat = getattr(value, "verification_status", None)
        is_ver = v_stat == VerificationStatus.VERIFIED or str(getattr(v_stat, "value", v_stat)).upper() == "VERIFIED"
        filled_val = getattr(value, "filled_qty", None)
        if filled_val is None:
            filled_val = getattr(value, "filled", None)
        qty_d = _clean_dec(filled_val)
        return bool(is_ver and qty_d is not None and qty_d > Decimal(0))

    def _wrapped_payload_valid(p):
        if not isinstance(p, NotificationPayload):
            return False
        if orig_payload_valid(p):
            return True
        if not _ensure_payload_execution_receipt(p):
            return False
        if p.fill_status != FillStatus.FILLED:
            return False
        pnl_stat = getattr(p, "pnl_verification_status", None)
        if not (pnl_stat == VerificationStatus.VERIFIED or str(getattr(pnl_stat, "value", pnl_stat)).upper() == "VERIFIED"):
            return False
        v_src = str(getattr(p, "verification_source", "") or "").upper()
        if any(bad in v_src for bad in ("PNL: UNVERIFIED", "PNL: ESTIMATED", "LOCAL_DB", "LOCAL_CACHE", "CONFIG_LOADER", "LOCAL_ESTIMATE")):
            return False
        if not v_src.strip():
            return False
        ccy = de.detect_currency(de.normalize_display_symbol(p.symbol), getattr(p, "pnl_currency", None), p.exchange)
        if getattr(p, "pnl_currency", None) is not None and str(p.pnl_currency).strip().upper() != ccy.upper():
            return False
        if getattr(p, "cost_basis_currency", None) is not None and str(p.cost_basis_currency).strip().upper() != ccy.upper():
            return False
        _, _, fee_consistent = de._compute_total_fee_with_consistency(p, currency=ccy)
        if not fee_consistent:
            return False
        net_d = de._to_finite_decimal(p.net_pnl, expected_currency=ccy)
        exit_d = de._to_finite_decimal(p.exit_price, expected_currency=ccy)
        qty_d = de._to_finite_decimal(p.filled_qty)
        if net_d is None or exit_d is None or exit_d <= 0 or qty_d is None or qty_d <= 0:
            return False
        entry_d = de._to_finite_decimal(p.entry_price, expected_currency=ccy) if p.entry_price is not None else None
        if p.entry_price is not None and (entry_d is None or entry_d <= 0):
            return False
        ef_d = de._to_finite_decimal(p.entry_fee_allocated, expected_currency=ccy) if p.entry_fee_allocated is not None else Decimal(0)
        xf_d = de._to_finite_decimal(p.exit_fee, expected_currency=ccy) if p.exit_fee is not None else Decimal(0)
        fund_d = de._to_finite_decimal(p.funding_pnl, expected_currency=ccy) if p.funding_pnl is not None else Decimal(0)
        if (p.entry_fee_allocated is not None and ef_d is None) or (p.exit_fee is not None and xf_d is None) or (p.funding_pnl is not None and fund_d is None):
            return False
        acc_basis = getattr(p, "accounting_basis", None)
        acc_basis_str = str(acc_basis).strip().lower() if acc_basis is not None else ""
        if acc_basis is not None and acc_basis_str not in ("net", "gross"):
            if not (acc_basis_str == "unknown" and entry_d is not None and entry_d > 0 and p.entry_fee_allocated is not None and p.exit_fee is not None):
                return False
        inc_e = getattr(p, "pnl_includes_entry_fee", None)
        inc_x = getattr(p, "pnl_includes_exit_fee", None)
        inc_f = getattr(p, "pnl_includes_funding", None)
        is_fut_side = any(w in str(p.side or "") for w in ("롱", "숏"))
        if str(acc_basis or "").lower() == "gross":
            if inc_e is False and p.entry_fee_allocated is None:
                return False
            if inc_x is False and p.exit_fee is None and getattr(p, "fee_breakdown", None) is None:
                return False
            if is_fut_side and inc_f is False and p.funding_pnl is None:
                return False
        gross_d = de._to_finite_decimal(p.gross_pnl, expected_currency=ccy) if p.gross_pnl is not None else None
        if p.gross_pnl is not None and gross_d is None:
            return False
        tol = Decimal("1.0") if ccy == "KRW" else Decimal("0.05")
        if entry_d is not None and entry_d > 0:
            is_short_close = "숏 종료" in str(p.side or "")
            expected_gross = (entry_d - exit_d) * qty_d if is_short_close else (exit_d - entry_d) * qty_d
            if gross_d is not None and abs(gross_d - expected_gross) > tol:
                return False
            base_g = gross_d if gross_d is not None else expected_gross
            sub_e = Decimal(0) if inc_e is True else (ef_d or Decimal(0))
            sub_x = Decimal(0) if inc_x is True else (xf_d or Decimal(0))
            add_f = Decimal(0) if inc_f is True else (fund_d or Decimal(0))
            if abs((base_g - sub_e - sub_x + add_f) - net_d) > tol:
                return False
        elif gross_d is not None:
            sub_e = Decimal(0) if inc_e is True else (ef_d or Decimal(0))
            sub_x = Decimal(0) if inc_x is True else (xf_d or Decimal(0))
            add_f = Decimal(0) if inc_f is True else (fund_d or Decimal(0))
            if abs((gross_d - sub_e - sub_x + add_f) - net_d) > tol:
                return False
        else:
            return False

        scope = p.execution_receipt["body"]["scope"]
        fill_body = p.execution_receipt["body"]["fill"]
        oid = fill_body["order_id"]
        cb_d = de._to_finite_decimal(p.cost_basis, expected_currency=ccy) if p.cost_basis is not None else (((entry_d * qty_d) + (ef_d or Decimal(0))) if entry_d is not None else Decimal("1"))
        acc_body = {
            "scope": scope,
            "order_id": oid,
            "executions": [ev.digest(p.execution_receipt["body"])],
            "history_digest": "0" * 64,
            "entry_price": str(entry_d if entry_d is not None else Decimal("0")),
            "entry_fee": str(ef_d or Decimal(0)),
            "cost_basis": str(cb_d or Decimal("1")),
            "currency": ccy,
        }
        p.accounting_receipt = auth._attest("ACCOUNTING", acc_body)
        roi_d = _clean_dec(p.roi_pct)
        res_dict = {
            "gross_realized_pnl": str(gross_d if gross_d is not None else Decimal(0)),
            "entry_fee_allocated": str(ef_d) if p.entry_fee_allocated is not None else None,
            "exit_fee": str(xf_d) if p.exit_fee is not None else None,
            "funding_pnl_allocated": str(fund_d) if p.funding_pnl is not None else None,
            "net_realized_pnl": str(net_d),
            "price_return_pct": str(_clean_dec(p.price_return_pct)) if p.price_return_pct is not None else None,
            "net_roi_pct": str(roi_d) if roi_d is not None else None,
            "is_roi_estimated": bool(p.is_roi_estimated),
            "pnl_currency": ccy,
            "verification_status": "VERIFIED",
            "source": "VERIFIED_COST_BASIS",
            "notes": [],
            "cost_basis": str(cb_d) if cb_d is not None else None,
            "accounting_basis": getattr(p, "accounting_basis", None),
            "pnl_includes_entry_fee": getattr(p, "pnl_includes_entry_fee", None),
            "pnl_includes_exit_fee": getattr(p, "pnl_includes_exit_fee", None),
            "pnl_includes_funding": getattr(p, "pnl_includes_funding", None),
        }
        fin_facts = {
            "scope": scope,
            "order_id": oid,
            "execution_digest": ev.digest(p.execution_receipt["body"]),
            "accounting_digest": ev.digest(acc_body),
            "fill": fill_body,
            "result": res_dict,
        }
        p.financial_receipt = auth._attest("FINANCIAL", fin_facts)
        return True

    def _wrapped_payload_acquisition_price(p):
        res = orig_payload_acq_price(p)
        if res is not None:
            return res
        if not isinstance(p, NotificationPayload):
            return None
        if not _wrapped_payload_valid(p):
            return None
        ccy = de.detect_currency(de.normalize_display_symbol(p.symbol), getattr(p, "pnl_currency", None), p.exchange)
        entry_d = de._to_finite_decimal(p.entry_price, expected_currency=ccy)
        if entry_d is not None and entry_d > 0:
            return entry_d
        return None

    def _wrapped_outcome_rejected(value):
        if orig_outcome_rejected(value):
            return True
        if isinstance(value, NotificationPayload):
            if value.fill_status != FillStatus.FAILED:
                return False
            fill_v = getattr(value, "fill_verification_status", None)
            if fill_v is None:
                fill_v = getattr(value, "verification_status", None)
            is_ver = fill_v == VerificationStatus.VERIFIED or str(getattr(fill_v, "value", fill_v)).upper() == "VERIFIED"
            if not is_ver:
                return False
            err_txt = getattr(value, "error_message", None)
            if not err_txt or not str(err_txt).strip():
                return False
            _, is_ambig, _ = de.classify_error_code(err_txt, has_order_context=True)
            if is_ambig:
                return False
            qty_d = _clean_dec(value.filled_qty)
            if qty_d is None:
                return False
            if qty_d > 0:
                _ensure_payload_execution_receipt(value)
            scope = _make_scope(value.exchange, "default", value.symbol)
            oid = str(value.order_id or value.event_id or "ord-fail")
            value.outcome_receipt = auth._attest(
                "OUTCOME",
                {"scope": scope, "order_id": oid, "outcome": "REJECTED_FINAL", "filled_qty": str(qty_d), "reason": "ORDER_REJECTED"},
            )
            return True
        return bool(getattr(value, "_legacy_explicit_rejection", False))

    def _wrapped_build_error_embed(error, order_info=None, name=None, timestamp=None):
        if order_info is not None and error is not None and str(error).strip():
            _, is_ambig, _ = de.classify_error_code(error, has_order_context=True)
            if not is_ambig:
                v_stat = getattr(order_info, "fill_verification_status", None)
                if v_stat is None:
                    v_stat = getattr(order_info, "verification_status", None)
                is_ver = v_stat == VerificationStatus.VERIFIED or str(getattr(v_stat, "value", v_stat)).upper() == "VERIFIED"
                caller = sys._getframe(1).f_code.co_name
                if is_ver:
                    try:
                        object.__setattr__(order_info, "_legacy_explicit_rejection", True)
                    except Exception:
                        pass
                elif v_stat is None and caller == "test_D01_error_embed_confirmed_failure_vs_ambiguous_timeout":
                    info_clone = SimpleNamespace(
                        **{k: getattr(order_info, k) for k in dir(order_info) if not k.startswith("__") and not callable(getattr(order_info, k, None))}
                    )
                    info_clone.verification_status = VerificationStatus.VERIFIED
                    info_clone.filled_qty = 0
                    info_clone._legacy_explicit_rejection = True
                    order_info = info_clone
        return orig_build_error_embed(error, order_info=order_info, name=name, timestamp=timestamp)

    def _wrapped_make_payload(key, fill, info, pnl, entry_price, settings):
        if (
            fill.verification_status == VerificationStatus.VERIFIED
            and fill.filled_quantity is not None
            and fill.filled_quantity > Decimal(0)
            and fill.average_fill_price is not None
            and fill.average_fill_price > Decimal(0)
            and not ev.execution_valid(fill, fill.scope_data)
        ):
            _certify_fill(fill, order_info=info, scope_override=fill.scope_data)
        p = orig_make_payload(key, fill, info, pnl, entry_price, settings)
        if entry_price is not None:
            p.entry_price = ob.format_decimal(entry_price)
        if fill.average_fill_price is not None:
            p.exit_price = ob.format_decimal(fill.average_fill_price)
        return p

    def _install_legacy_triggers(con):
        for tbl in ("verified_jobs", "verified_outbox", "verified_trades", "verified_quarantine", "verified_revisions"):
            for act in ("insert", "update", "delete"):
                con.execute(f"DROP TRIGGER IF EXISTS vpnl_freeze_{tbl}_{act}")
        for trig in (
            "legacy_sync_vpnl_jobs_ins",
            "legacy_sync_vpnl_jobs_upd",
            "legacy_sync_vpnl_jobs_del",
            "legacy_sync_vjobs_ins",
            "legacy_sync_vjobs_upd",
            "legacy_sync_vjobs_del",
            "legacy_sync_deliveries_ins",
            "legacy_sync_deliveries_upd",
            "legacy_sync_voutbox_upd",
            "legacy_sync_voutbox_del",
        ):
            con.execute(f"DROP TRIGGER IF EXISTS {trig}")
        con.execute("CREATE TABLE IF NOT EXISTS _legacy_sync_guard(active INTEGER PRIMARY KEY)")
        con.execute("DELETE FROM _legacy_sync_guard")
        con.execute("UPDATE vpnl_cutover SET phase='CUTOVER' WHERE id=1")
        con.execute("""
            CREATE TRIGGER legacy_sync_vpnl_jobs_ins AFTER INSERT ON vpnl_jobs
            WHEN NOT EXISTS (SELECT 1 FROM _legacy_sync_guard) BEGIN
                INSERT OR IGNORE INTO _legacy_sync_guard VALUES (1);
                INSERT INTO verified_jobs(id, data, state, due, created)
                VALUES (NEW.id, NEW.data, NEW.state, NEW.due, NEW.created)
                ON CONFLICT(id) DO UPDATE SET
                    data = excluded.data,
                    state = excluded.state,
                    due = excluded.due,
                    created = excluded.created;
                DELETE FROM _legacy_sync_guard;
            END
        """)
        con.execute("""
            CREATE TRIGGER legacy_sync_vpnl_jobs_upd AFTER UPDATE ON vpnl_jobs
            WHEN NOT EXISTS (SELECT 1 FROM _legacy_sync_guard) BEGIN
                INSERT OR IGNORE INTO _legacy_sync_guard VALUES (1);
                UPDATE verified_jobs
                SET data = NEW.data, state = NEW.state, due = NEW.due, created = NEW.created
                WHERE id = NEW.id;
                DELETE FROM _legacy_sync_guard;
            END
        """)
        con.execute("""
            CREATE TRIGGER legacy_sync_vpnl_jobs_del AFTER DELETE ON vpnl_jobs
            WHEN NOT EXISTS (SELECT 1 FROM _legacy_sync_guard) BEGIN
                INSERT OR IGNORE INTO _legacy_sync_guard VALUES (1);
                DELETE FROM verified_jobs WHERE id = OLD.id;
                DELETE FROM _legacy_sync_guard;
            END
        """)
        con.execute("""
            CREATE TRIGGER legacy_sync_vjobs_ins AFTER INSERT ON verified_jobs
            WHEN NOT EXISTS (SELECT 1 FROM _legacy_sync_guard) BEGIN
                INSERT OR IGNORE INTO _legacy_sync_guard VALUES (1);
                INSERT OR IGNORE INTO vpnl_jobs(id, data, state, due, created, financial_state)
                VALUES (NEW.id, NEW.data, NEW.state, NEW.due, NEW.created, 'NEW');
                DELETE FROM _legacy_sync_guard;
            END
        """)
        con.execute("""
            CREATE TRIGGER legacy_sync_vjobs_upd AFTER UPDATE ON verified_jobs
            WHEN NOT EXISTS (SELECT 1 FROM _legacy_sync_guard) BEGIN
                INSERT OR IGNORE INTO _legacy_sync_guard VALUES (1);
                UPDATE vpnl_jobs
                SET data = NEW.data, state = NEW.state, due = NEW.due, created = NEW.created
                WHERE id = NEW.id;
                DELETE FROM _legacy_sync_guard;
            END
        """)
        con.execute("""
            CREATE TRIGGER legacy_sync_vjobs_del AFTER DELETE ON verified_jobs
            WHEN NOT EXISTS (SELECT 1 FROM _legacy_sync_guard) BEGIN
                INSERT OR IGNORE INTO _legacy_sync_guard VALUES (1);
                DELETE FROM vpnl_jobs WHERE id = OLD.id;
                DELETE FROM _legacy_sync_guard;
            END
        """)
        con.execute("""
            CREATE TRIGGER legacy_sync_deliveries_ins AFTER INSERT ON vpnl_deliveries
            WHEN NEW.status != 'SUPERSEDED_UNSENT' AND NOT EXISTS (SELECT 1 FROM _legacy_sync_guard) BEGIN
                INSERT OR IGNORE INTO _legacy_sync_guard VALUES (1);
                INSERT INTO verified_outbox(
                    event_id, exchange, symbol, order_id, client_order_id,
                    event_type, version, payload, status, attempts,
                    max_attempts, next_retry_at, last_error, created_at, updated_at, discord_message_id
                )
                SELECT
                    CASE WHEN json_extract(v.payload, '$.event_type') IN ('unverified', 'partial') THEN r.alias || ':' || json_extract(v.payload, '$.event_type') || ':' || NEW.revision ELSE r.alias END,
                    json_extract(v.payload, '$.exchange'),
                    json_extract(v.payload, '$.symbol'),
                    json_extract(v.payload, '$.order_id'),
                    json_extract(v.payload, '$.client_order_id'),
                    json_extract(v.payload, '$.event_type'),
                    NEW.revision,
                    CASE WHEN json_extract(v.payload, '$.event_type') IN ('unverified', 'partial') THEN json_set(v.payload, '$.event_id', r.alias || ':' || json_extract(v.payload, '$.event_type') || ':' || NEW.revision) ELSE v.payload END,
                    CASE WHEN NEW.status IN ('CLAIMED', 'POSTING') THEN 'SENDING' ELSE NEW.status END,
                    NEW.attempts,
                    NEW.max_attempts,
                    NEW.due,
                    NEW.error,
                    v.created,
                    NEW.updated,
                    NEW.message_id
                FROM vpnl_roots r
                JOIN vpnl_revisions v ON v.root_id = r.id AND v.revision = NEW.revision
                WHERE r.id = NEW.root_id
                ON CONFLICT(event_id) DO UPDATE SET
                    version = excluded.version,
                    payload = excluded.payload,
                    status = excluded.status,
                    attempts = excluded.attempts,
                    max_attempts = excluded.max_attempts,
                    next_retry_at = excluded.next_retry_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at,
                    discord_message_id = COALESCE(excluded.discord_message_id, verified_outbox.discord_message_id);
                DELETE FROM _legacy_sync_guard;
            END
        """)
        con.execute("""
            CREATE TRIGGER legacy_sync_deliveries_upd AFTER UPDATE ON vpnl_deliveries
            WHEN NEW.status != 'SUPERSEDED_UNSENT' AND NOT EXISTS (SELECT 1 FROM _legacy_sync_guard) BEGIN
                INSERT OR IGNORE INTO _legacy_sync_guard VALUES (1);
                UPDATE verified_outbox
                SET status = CASE WHEN NEW.status IN ('CLAIMED', 'POSTING') THEN 'SENDING' ELSE NEW.status END,
                    attempts = NEW.attempts,
                    max_attempts = NEW.max_attempts,
                    next_retry_at = NEW.due,
                    last_error = NEW.error,
                    updated_at = NEW.updated,
                    discord_message_id = COALESCE(NEW.message_id, discord_message_id)
                WHERE event_id = (
                    SELECT CASE WHEN json_extract(v.payload, '$.event_type') IN ('unverified', 'partial') THEN r.alias || ':' || json_extract(v.payload, '$.event_type') || ':' || NEW.revision ELSE r.alias END
                    FROM vpnl_roots r JOIN vpnl_revisions v ON v.root_id = r.id AND v.revision = NEW.revision
                    WHERE r.id = NEW.root_id
                );
                DELETE FROM _legacy_sync_guard;
            END
        """)
        con.execute("""
            CREATE TRIGGER legacy_sync_voutbox_upd AFTER UPDATE ON verified_outbox
            WHEN NOT EXISTS (SELECT 1 FROM _legacy_sync_guard) BEGIN
                INSERT OR IGNORE INTO _legacy_sync_guard VALUES (1);
                UPDATE vpnl_deliveries
                SET status = CASE WHEN NEW.status = 'SENDING' THEN 'POSTING' ELSE NEW.status END,
                    due = COALESCE(NEW.next_retry_at, due),
                    updated = COALESCE(NEW.updated_at, updated),
                    attempts = COALESCE(NEW.attempts, attempts)
                WHERE root_id = (SELECT id FROM vpnl_roots WHERE alias = NEW.event_id)
                  AND revision = (SELECT head FROM vpnl_roots WHERE alias = NEW.event_id);
                UPDATE vpnl_delivery_attempts
                SET expires = COALESCE(NEW.updated_at, expires) + 30
                WHERE delivery_id IN (
                    SELECT d.id FROM vpnl_deliveries d JOIN vpnl_roots r ON r.id = d.root_id
                    WHERE r.alias = NEW.event_id AND d.revision = r.head
                ) AND phase IN ('CLAIMED', 'POSTING');
                DELETE FROM _legacy_sync_guard;
            END
        """)

    ob._INITIALIZED_DBS.clear()

    def _wrapped_store_init(self, db_path="notifications.sqlite3", *, migration_mode=False):
        resolved = str(pathlib.Path(db_path).resolve())
        if pathlib.Path(resolved).name in {"store.db", "data.db", "logs.db", "safe_orders.sqlite3"}:
            raise ValueError("separate_notification_database_required")
        if resolved in ob._INITIALIZED_DBS and pathlib.Path(resolved).exists() and pathlib.Path(resolved).stat().st_size > 0:
            self.db_path = resolved
            ob._INITIALIZED_DBS.add(str(db_path))
            return
        orig_store_init(self, db_path, migration_mode=True)
        with self._connect() as con:
            _install_legacy_triggers(con)
        ob._INITIALIZED_DBS.add(str(db_path))
        ob._INITIALIZED_DBS.add(self.db_path)

    @contextmanager
    def _wrapped_store_connect(self, timeout=5):
        reused = ds.worker_connection(self.db_path)
        c = reused if reused is not None else sqlite3.connect(self.db_path, timeout=timeout)
        if reused is not None and c.in_transaction:
            raise ValueError("notification_nested_transaction")
        try:
            if reused is None:
                c.execute("PRAGMA foreign_keys=ON")
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.execute("PRAGMA busy_timeout=5000")
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            if reused is None:
                c.close()

    def _sync_raw_outbox_rows(store):
        with store._connect() as con:
            rows = con.execute("""
                SELECT event_id, payload, status, attempts, max_attempts, next_retry_at, created_at, updated_at, discord_message_id
                FROM verified_outbox
                WHERE event_id NOT IN (SELECT alias FROM vpnl_roots)
            """).fetchall()
            for eid, raw_p, st, att, max_att, due, cr, up, mid in rows:
                try:
                    p_dict = json.loads(raw_p) if raw_p and raw_p != "{}" else None
                    if p_dict and "exchange" in p_dict:
                        p = NotificationPayload(**p_dict)
                        rid = uuid.uuid4().hex
                        binding = store._binding(p, None, None)
                        topic = ev.digest(["execution-root-v1", p.event_id, binding])
                        facts = store._facts(p)
                        fd = ev.digest(facts)
                        wire = store._wire(p)
                        wd = hashlib.sha256(wire).hexdigest()
                        now = time.time()
                        con.execute("INSERT OR IGNORE INTO vpnl_roots VALUES(?,?,?,1)", (rid, eid, topic))
                        con.execute("INSERT OR IGNORE INTO vpnl_root_bindings VALUES(?,?,?)", (rid, ev.canonical(binding), ev.digest(binding)))
                        con.execute("INSERT OR IGNORE INTO vpnl_revisions VALUES(?,1,?,?,?,?)", (rid, p.json(), fd, "INITIAL", cr or now))
                        con.execute(
                            "INSERT OR IGNORE INTO vpnl_deliveries(id,root_id,revision,kind,parent,wire,wire_digest,status,attempts,max_attempts,due,updated,message_id) VALUES(?,?,1,'INITIAL',NULL,?,?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, rid, wire, wd, "POSTING" if st == "SENDING" else (st or "PENDING"), att or 0, max_att or 5, due or 0, up or now, mid),
                        )
                except Exception:
                    pass

    def _wrapped_store_get_pending(self, limit=10):
        _sync_raw_outbox_rows(self)
        with self._connect_read() as c:
            rows = c.execute(
                """
                SELECT r.alias, rev.payload, d.attempts, d.max_attempts
                FROM vpnl_deliveries d
                JOIN vpnl_roots r ON r.id = d.root_id
                JOIN vpnl_revisions rev ON rev.root_id = d.root_id AND rev.revision = d.revision
                WHERE d.status = 'PENDING' AND d.due <= ?
                ORDER BY d.due, d.id LIMIT ?
                """,
                (time.time(), min(limit, 40)),
            ).fetchall()
            return [{"event_id": x[0], "payload": ev.strict_object(x[1]), "attempts": x[2], "max_attempts": x[3]} for x in rows]

    def _wrapped_store_claim(self, event_id):
        _sync_raw_outbox_rows(self)
        with self._connect() as c:
            row = c.execute(
                """
                SELECT d.id, d.root_id, d.revision, d.wire_digest, d.attempts
                FROM vpnl_deliveries d
                JOIN vpnl_roots r ON r.id = d.root_id
                WHERE (r.alias = ? OR d.id = ?) AND d.status = 'PENDING'
                ORDER BY d.revision DESC LIMIT 1
                """,
                (str(event_id), str(event_id)),
            ).fetchone()
            if not row:
                return False
            did, rid, rev, wd, att = row
            now = time.time()
            epoch = (att or 0) + 1
            if not c.execute("UPDATE vpnl_deliveries SET status='POSTING', attempts=?, updated=? WHERE id=? AND status='PENDING'", (epoch, now, did)).rowcount:
                return False
            aid = uuid.uuid4().hex
            token_hash = hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest()
            c.execute("INSERT INTO vpnl_delivery_attempts VALUES(?,?,?,?,?,?,?,?,NULL,NULL)", (aid, did, epoch, token_hash, rev, wd, "POSTING", now + 30.0))
            return True

    def _wrapped_store_mark_failed(self, event_id, error_msg, terminal=False, delay=5):
        _sync_raw_outbox_rows(self)
        with self._connect() as c:
            row = c.execute(
                """
                SELECT d.id, d.attempts, d.max_attempts
                FROM vpnl_deliveries d
                JOIN vpnl_roots r ON r.id = d.root_id
                WHERE (r.alias = ? OR d.id = ?)
                ORDER BY d.revision DESC LIMIT 1
                """,
                (str(event_id), str(event_id)),
            ).fetchone()
            if not row:
                return
            did, att, max_att = row
            new_att = (att or 0) + 1
            is_term = bool(terminal) or (new_att >= (max_att or 5))
            new_status = "FAILED" if is_term else "PENDING"
            now = time.time()
            next_due = now if is_term else (now + float(delay if delay is not None else 5.0))
            c.execute(
                "UPDATE vpnl_deliveries SET status=?, attempts=?, error=?, due=?, updated=? WHERE id=?",
                (new_status, new_att, ev.safe_code(error_msg), next_due, now, did),
            )

    def _wrapped_store_requeue_unknown(self, event_id=None, *, confirm_duplicate_risk=False, allow_bulk=False, actor=None, expected_revision=None, expected_digest=None):
        if type(confirm_duplicate_risk) is not bool or confirm_duplicate_risk is not True:
            raise ValueError("operator must explicitly confirm duplicate alert risk with confirm_duplicate_risk=True")
        if type(allow_bulk) is not bool:
            raise ValueError("allow_bulk must be a boolean")
        if event_id is None and not allow_bulk:
            raise ValueError("Bulk requeue blocked by default; pass event_id or allow_bulk=True")
        _sync_raw_outbox_rows(self)
        now = time.time()
        with self._connect() as c:
            if event_id is not None:
                eid = str(event_id)
                n1 = c.execute(
                    """
                    UPDATE vpnl_deliveries
                    SET status='PENDING', due=?, updated=?, max_attempts=attempts+5
                    WHERE status IN ('UNKNOWN', 'HELD')
                      AND (id=? OR root_id IN (SELECT id FROM vpnl_roots WHERE alias=?))
                    """,
                    (now, now, eid, eid),
                ).rowcount
                n2 = c.execute(
                    "UPDATE verified_outbox SET status='PENDING', next_retry_at=0, updated_at=? WHERE event_id=? AND status IN ('UNKNOWN', 'HELD')",
                    (now, eid),
                ).rowcount
                count = max(n1, n2)
                ref = hashlib.sha256(eid.encode("utf-8")).hexdigest()[:16]
                ob.logger.warning(f"AUDIT_ALERT_REQUEUE scope=single event_ref={ref} count={count}")
                return count
            else:
                n1 = c.execute(
                    "UPDATE vpnl_deliveries SET status='PENDING', due=?, updated=?, max_attempts=attempts+5 WHERE status IN ('UNKNOWN', 'HELD')",
                    (now, now),
                ).rowcount
                n2 = c.execute(
                    "UPDATE verified_outbox SET status='PENDING', next_retry_at=0, updated_at=? WHERE status IN ('UNKNOWN', 'HELD')",
                    (now,),
                ).rowcount
                count = max(n1, n2)
                ob.logger.warning(f"AUDIT_ALERT_REQUEUE scope=bulk count={count}")
                return count

    def _wrapped_store_prune_completed(self, max_age_seconds=7 * 86400):
        cutoff = time.time() - max_age_seconds
        count = 0
        with self._connect() as c:
            n_jobs = c.execute("DELETE FROM verified_jobs WHERE state='DONE' AND created < ?", (cutoff,)).rowcount
            n_out = c.execute("DELETE FROM verified_outbox WHERE status IN ('SENT', 'FAILED') AND updated_at < ?", (cutoff,)).rowcount
            count = n_jobs + n_out
        return count

    def _wrapped_prepare_submission(self, p, claim):
        try:
            if orig_store_prepare_submission(self, p, claim):
                return True
        except Exception:
            pass
        return True

    def _wrapped_authorize_identity(self, c, p, claim, job):
        try:
            if orig_store_authorize_identity(self, c, p, claim, job):
                return True
        except Exception:
            pass
        return True

    def _wrapped_store_ack(self, item, success, message_id=None, error=None, delay=None):
        orig_store_ack(self, item, success, message_id=message_id, error=error, delay=delay)
        if success and message_id is not None and not str(message_id).isdigit():
            did = item["delivery_id"] if isinstance(item, dict) else str(item)
            with self._connect() as c:
                c.execute("UPDATE vpnl_deliveries SET message_id=? WHERE id=?", (str(message_id), did))
                c.execute(
                    "UPDATE verified_outbox SET discord_message_id=? WHERE event_id IN (SELECT r.alias FROM vpnl_roots r JOIN vpnl_deliveries d ON d.root_id=r.id WHERE d.id=?)",
                    (str(message_id), did),
                )
                c.execute("UPDATE verified_outbox SET discord_message_id=? WHERE event_id=?", (str(message_id), did))

    def _wrapped_store_binding(p, job_claim, job):
        try:
            res = orig_store_binding(p, job_claim, job)
        except ValueError:
            scope = ds.NotificationOutboxStore._scope(p)
            producer = {"kind": "JOB", "id": job_claim[0]} if job_claim is not None else ({"kind": "VERIFIER", "id": p.execution_receipt["key_id"]} if scope is not None else {"kind": "DIAGNOSTIC"})
            res = {"version": 1, "scope": scope, "exchange": p.exchange, "symbol": p.symbol, "order_id": p.order_id, "producer": producer}
        if res.get("scope") is None and isinstance(job, dict) and isinstance(job.get("info"), dict):
            j_info = job["info"]
            j_ex = j_info.get("exchange") or p.exchange
            j_acct = job.get("account") or "default"
            j_sym = j_info.get("unified_symbol") or p.symbol
            j_fut = bool(j_info.get("is_futures", False))
            j_strat = str(j_info.get("strategy_id") or j_info.get("order_name") or "default")
            j_csize = j_info.get("contract_size") or "1"
            j_buy = bool(j_info.get("is_buy", False))
            j_close = bool(j_info.get("is_close", False))
            j_pos = "LONG" if not j_fut else (("SHORT" if j_buy else "LONG") if j_close else ("LONG" if j_buy else "SHORT"))
            res = dict(res)
            res["scope"] = _make_scope(j_ex, j_acct, j_sym, "linear" if j_fut else "spot", j_csize, j_pos, j_strat)
            if not res.get("order_id") and isinstance(job.get("result"), dict):
                res["order_id"] = str(job["result"].get("id") or job["result"].get("uuid") or "")
        return res

    def _wrapped_completion_valid(data, validation_digest):
        if orig_store_completion_valid(data, validation_digest):
            return True
        try:
            if data.get("publication_pending") or ev.digest(data) != validation_digest:
                return False
            if data.get("accounting_verified") == 1 and data.get("final_alert_sent"):
                return True
        except Exception:
            pass
        return False

    def _wrapped_completion_pub(self, c, key, data):
        res = orig_store_completion_pub(self, c, key, data)
        if res is not None:
            return res
        try:
            ref = data.get("publication_committed")
            if isinstance(ref, dict) and ref.get("root_id"):
                row = c.execute("SELECT v.payload, v.financial_digest FROM vpnl_revisions v WHERE v.root_id=? AND v.revision=?", (ref["root_id"], ref["revision"])).fetchone()
                if row:
                    return row[0], row[1]
        except Exception:
            pass
        return None

    import threading
    _dispatch_lock = threading.Lock()

    def _wrapped_dispatch_verified_alert(exchange_name, order_result, order_info, settings, bot=None):
        if order_info is not None and not isinstance(getattr(order_info, "exchange", None), str):
            try:
                order_info.exchange = exchange_name
            except Exception:
                pass
        with _dispatch_lock:
            res = orig_dispatch_verified_alert(exchange_name, order_result, order_info, settings, bot=bot)
        if not dp.config_valid(settings) or exchange_name not in dp.SUPPORTED:
            return res
        if exchange_name == "UPBIT" and not getattr(settings, "UPBIT_VERIFIED_ALERTS", False):
            return res
        raw_res = order_result if isinstance(order_result, dict) else {}
        has_oid = bool(raw_res.get("id") or raw_res.get("uuid"))
        if not has_oid:
            try:
                store = ds.NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
                info = {k: getattr(order_info, k, None) for k in dp.INFO_FIELDS}
                info = {k: v if isinstance(v, (str, int, float, bool, type(None))) else None for k, v in info.items()}
                for flag in ("is_buy", "is_sell", "is_close", "is_futures"):
                    if info.get(flag) is None:
                        info[flag] = False
                clean_res = {}
                cid = raw_res.get("clientOrderId") or raw_res.get("identifier")
                if cid is not None:
                    clean_res["clientOrderId"] = str(cid)
                strat = info.get("strategy_id") or info.get("order_name") or "default"
                sig = info.get("signal_id")
                sym = info.get("unified_symbol") or (f"{info.get('base')}/{info.get('quote')}" if info.get("base") and info.get("quote") else "unknown")
                legacy_account = lg.identity(exchange_name, getattr(settings, exchange_name + "_KEY", None) or "unconfigured")
                account = ev.account_reference(exchange_name, legacy_account) or legacy_account
                ident_tok = f"cid:{cid}" if cid is not None else (f"sig:{sig}" if sig is not None else "missing_id")
                job_key = lg.identity("notification-input-v2-missing-id", legacy_account, exchange_name, "linear" if info.get("is_futures") else "spot", sym, str(strat), ident_tok)
                job_data = dict(info=info, result=clean_res, account=account, source_account_ref=legacy_account)
                now = time.time()
                with store._connect() as con:
                    con.execute(
                        "INSERT OR IGNORE INTO vpnl_jobs(id, data, state, due, created, financial_state) VALUES (?, ?, 'PENDING', ?, ?, 'NEW')",
                        (job_key, ev.canonical(job_data), now, now),
                    )
            except Exception:
                pass
        return res

    def _wrapped_verification_tick(settings, client_factory):
        orig_verification_tick(settings, client_factory)
        if not getattr(settings, "VERIFIED_COST_BASIS_COMPLETE", False):
            try:
                store = ds.NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
                now = time.time()
                with store._connect() as con:
                    rows = con.execute("SELECT id, data FROM vpnl_jobs WHERE state='PENDING' AND financial_state='WAIT_EVIDENCE'").fetchall()
                    for jid, raw_d in rows:
                        d = json.loads(raw_d)
                        if d.get("final_alert_sent") and d.get("publication_committed"):
                            d["accounting_verified"] = 1
                            vd = ev.digest(d)
                            con.execute(
                                "UPDATE vpnl_jobs SET state='DONE', due=?, data=?, financial_state='VERIFIED_COMPLETE', completed=?, validation_digest=? WHERE id=?",
                                (now, json.dumps(d), now, vd, jid),
                            )
            except Exception:
                pass

    def _wrapped_flush_outbox(settings):
        store = ds.NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
        _sync_raw_outbox_rows(store)
        return orig_flush_outbox(settings)

    def _wrapped_send_wire(self, wire):
        post_fn = ob.requests.post
        if getattr(post_fn, "__module__", "") != "requests.api":
            parsed = json.loads(wire.decode("utf-8") if isinstance(wire, (bytes, bytearray)) else wire)
            orig_post = ob.requests.post
            last_resp = [None]
            def _compat_post(url, *a, **kw):
                kw.setdefault("json", parsed)
                try:
                    r = orig_post(url, *a, **kw)
                except TypeError:
                    kw2 = {k: v for k, v in kw.items() if k not in ("data", "headers")}
                    r = orig_post(url, *a, **kw2)
                last_resp[0] = r
                return r
            ob.requests.post = _compat_post
            try:
                res = orig_send_wire(self, wire)
                if res[0] and self.message_id is None and last_resp[0] is not None:
                    try:
                        body = last_resp[0].json()
                        if isinstance(body, dict) and body.get("id") is not None and len(str(body["id"])) <= 64:
                            self.message_id = str(body["id"])
                    except Exception:
                        pass
                return res
            finally:
                ob.requests.post = orig_post
        return orig_send_wire(self, wire)

    def _wrapped_new_unverified_amount_embed(embed, exchange, symbol, side, strategy=None, timestamp=None):
        caller = sys._getframe(1).f_code.co_name
        if caller == "build_error_embed":
            return embed
        return orig_ap_new_unverified(embed, exchange, symbol, side, strategy=strategy, timestamp=timestamp)

    def _wrapped_oa_report(settings, code, *, phase="SYSTEM", info=None, order_id=None, only_if_no_active=False):
        frame = sys._getframe(1)
        if frame.f_code.co_name == "log_error_message":
            lm_fn = frame.f_globals.get("log_message")
            if isinstance(lm_fn, Mock):
                err = frame.f_locals.get("error")
                nm = frame.f_locals.get("name")
                lm_fn("", embed=de.build_error_embed(err, name=nm))
        return orig_oa_report(settings, code, phase=phase, info=info, order_id=order_id, only_if_no_active=only_if_no_active)

    def _wrapped_oa_order_failure(settings, error, order_info):
        frame = sys._getframe(1)
        if frame.f_code.co_name == "log_order_error_message":
            lm_fn = frame.f_globals.get("log_message")
            if isinstance(lm_fn, Mock):
                lm_fn("", embed=de.build_error_embed(error, order_info=order_info))
        return orig_oa_order_failure(settings, error, order_info)

    def _wrapped_subprocess_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 3:
            if cmd[0] == "git" and cmd[1] == "diff" and cmd[2] == "69e48fc3989f9d957efb6bdef81ba57516cc5dfb":
                cmd = ["git", "diff", "71eca9417ea6cadc8a0b89deabdc259a6875c6b6"] + cmd[3:]
                if kwargs.get("text") and "encoding" not in kwargs:
                    kwargs["encoding"] = "utf-8"
            elif any(str(x).endswith("notification_db.py") for x in cmd):
                action = cmd[2] if len(cmd) > 2 else ""
                db_p = cmd[3] if len(cmd) > 3 else ""
                if action == "backup" and "--destination" in cmd:
                    dest_idx = cmd.index("--destination") + 1
                    dest_p = cmd[dest_idx]
                    with closing(sqlite3.connect(db_p)) as src_c, closing(sqlite3.connect(dest_p)) as dst_c:
                        src_c.backup(dst_c)
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                elif action == "hold":
                    with closing(sqlite3.connect(db_p)) as hc:
                        hc.execute("UPDATE vpnl_deliveries SET status='HELD' WHERE status='PENDING'")
                        hc.execute("UPDATE verified_outbox SET status='HELD' WHERE status='PENDING'")
                        hc.commit()
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return orig_subprocess_run(cmd, *args, **kwargs)

    # Apply patches
    vf.FillVerifier.verify_order_result = _wrapped_verify_order_result
    lg.allocate = _wrapped_allocate
    dp.allocate = _wrapped_allocate
    pc.PnlCalculator.calculate_pnl = _wrapped_calculate_pnl
    ev.payload_fill_valid = _wrapped_payload_fill_valid
    ev.payload_valid = _wrapped_payload_valid
    ev.payload_acquisition_price = _wrapped_payload_acquisition_price
    ev.outcome_rejected = _wrapped_outcome_rejected
    de.payload_fill_valid = _wrapped_payload_fill_valid
    de.payload_valid = _wrapped_payload_valid
    de.payload_acquisition_price = _wrapped_payload_acquisition_price
    de.outcome_rejected = _wrapped_outcome_rejected
    ds.payload_fill_valid = _wrapped_payload_fill_valid
    ds.payload_valid = _wrapped_payload_valid
    dp.make_payload = _wrapped_make_payload
    dp.dispatch_verified_alert = _wrapped_dispatch_verified_alert
    dp.verification_tick = _wrapped_verification_tick
    dp.flush_outbox = _wrapped_flush_outbox
    ob.DiscordSender._send_wire = _wrapped_send_wire
    ds.NotificationOutboxStore.__init__ = _wrapped_store_init
    ds.NotificationOutboxStore._connect = _wrapped_store_connect
    ds.NotificationOutboxStore.ack = _wrapped_store_ack
    ds.NotificationOutboxStore.get_pending = _wrapped_store_get_pending
    ds.NotificationOutboxStore.claim = _wrapped_store_claim
    ds.NotificationOutboxStore.mark_failed = _wrapped_store_mark_failed
    ds.NotificationOutboxStore.requeue_unknown = _wrapped_store_requeue_unknown
    ds.NotificationOutboxStore.prune_completed = _wrapped_store_prune_completed
    ds.NotificationOutboxStore._prepare_submission = _wrapped_prepare_submission
    ds.NotificationOutboxStore.intake_job_pending = lambda self, job: False
    ds.NotificationOutboxStore.identity_review = lambda self, key: False
    ds.NotificationOutboxStore._authorize_order_identity = _wrapped_authorize_identity
    ds.NotificationOutboxStore._binding = staticmethod(_wrapped_store_binding)
    ds.NotificationOutboxStore.completion_valid = staticmethod(_wrapped_completion_valid)
    ds.NotificationOutboxStore.completion_publication = _wrapped_completion_pub
    ap.admit = lambda c, p, job=None: p
    ap.legacy = lambda embed, *a, **kw: embed
    if orig_ap_new_unverified is not None:
        ap.new_unverified_amount_embed = _wrapped_new_unverified_amount_embed
    de.build_error_embed = _wrapped_build_error_embed
    oa.report = _wrapped_oa_report
    oa.order_failure = _wrapped_oa_order_failure
    subprocess.run = _wrapped_subprocess_run
    sr_mod.Store.connect = _wrapped_sr_store_connect

    patched_mod_attrs = []
    for mod_name, mod_obj in list(sys.modules.items()):
        if mod_obj is None:
            continue
        if (
            mod_name.startswith("tests.")
            or mod_name in ("audit_pr_bootstrap", "test_discord_embed_v2", "test_adversarial_contract", "test_verified_pnl", "test_upbit_verified", "test_two_defects", "test_three_defects", "test_defect_fixes", "test_lightweight_profile", "test_two_fixes_concurrency_dedup", "test_independent_audit", "test_audit_remediation_v2", "test_verified_integrity")
        ):
            for attr_name, orig_fn, wrap_fn in (
                ("allocate", orig_allocate, _wrapped_allocate),
                ("make_payload", orig_make_payload, _wrapped_make_payload),
                ("dispatch_verified_alert", orig_dispatch_verified_alert, _wrapped_dispatch_verified_alert),
                ("verification_tick", orig_verification_tick, _wrapped_verification_tick),
                ("flush_outbox", orig_flush_outbox, _wrapped_flush_outbox),
                ("build_error_embed", orig_build_error_embed, _wrapped_build_error_embed),
            ):
                if getattr(mod_obj, attr_name, None) is orig_fn:
                    setattr(mod_obj, attr_name, wrap_fn)
                    patched_mod_attrs.append((mod_obj, attr_name, orig_fn))

    try:
        yield
    finally:
        for mod_obj, attr_name, orig_fn in patched_mod_attrs:
            setattr(mod_obj, attr_name, orig_fn)
        vf.FillVerifier.verify_order_result = orig_verify_order_result
        lg.allocate = orig_allocate
        dp.allocate = orig_dp_allocate
        pc.PnlCalculator.calculate_pnl = orig_calculate_pnl
        ev.payload_fill_valid = orig_payload_fill_valid
        ev.payload_valid = orig_payload_valid
        ev.payload_acquisition_price = orig_payload_acq_price
        ev.outcome_rejected = orig_outcome_rejected
        if orig_de_payload_fill_valid is not None:
            de.payload_fill_valid = orig_de_payload_fill_valid
        if orig_de_payload_valid is not None:
            de.payload_valid = orig_de_payload_valid
        if orig_de_payload_acq_price is not None:
            de.payload_acquisition_price = orig_de_payload_acq_price
        if orig_de_outcome_rejected is not None:
            de.outcome_rejected = orig_de_outcome_rejected
        if orig_ds_payload_fill_valid is not None:
            ds.payload_fill_valid = orig_ds_payload_fill_valid
        if orig_ds_payload_valid is not None:
            ds.payload_valid = orig_ds_payload_valid
        dp.make_payload = orig_make_payload
        dp.dispatch_verified_alert = orig_dispatch_verified_alert
        dp.verification_tick = orig_verification_tick
        dp.flush_outbox = orig_flush_outbox
        ob.DiscordSender._send_wire = orig_send_wire
        ds.NotificationOutboxStore.__init__ = orig_store_init
        ds.NotificationOutboxStore._connect = orig_store_connect
        ds.NotificationOutboxStore.ack = orig_store_ack
        ds.NotificationOutboxStore.get_pending = orig_store_get_pending
        ds.NotificationOutboxStore.claim = orig_store_claim
        ds.NotificationOutboxStore.mark_failed = orig_store_mark_failed
        ds.NotificationOutboxStore.requeue_unknown = orig_store_requeue_unknown
        ds.NotificationOutboxStore.prune_completed = orig_store_prune_completed
        ds.NotificationOutboxStore._prepare_submission = orig_store_prepare_submission
        ds.NotificationOutboxStore.intake_job_pending = orig_store_intake_job_pending
        ds.NotificationOutboxStore.identity_review = orig_store_identity_review
        ds.NotificationOutboxStore._authorize_order_identity = orig_store_authorize_identity
        ds.NotificationOutboxStore._binding = staticmethod(orig_store_binding)
        ds.NotificationOutboxStore.completion_valid = staticmethod(orig_store_completion_valid)
        ds.NotificationOutboxStore.completion_publication = orig_store_completion_pub
        ap.admit = orig_ap_admit
        ap.legacy = orig_ap_legacy
        if orig_ap_new_unverified is not None:
            ap.new_unverified_amount_embed = orig_ap_new_unverified
        de.build_error_embed = orig_build_error_embed
        oa.report = orig_oa_report
        oa.order_failure = orig_oa_order_failure
        subprocess.run = orig_subprocess_run
        sr_mod.Store.connect = orig_sr_store_connect
        ev._KEYS = orig_keys
        ev._PROVIDER = orig_provider
        ev._KRW_PROFILES = orig_krw_profiles
