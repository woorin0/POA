"""Read-only provenance checks for historical SENT consumers.

No renderer version is inferred, no past display field gains new trust, and no
HTTP/claim/ACK path calls this compatibility validator. SQLite integrity and the
host-pinned evidence keys remain trust boundaries; a fully rewritten database
and its independent witnesses cannot be authenticated by local hashes alone.
"""
import hashlib
import math
import re
import sqlite3
from pathlib import Path
from contextlib import closing
from decimal import Decimal, localcontext, ROUND_HALF_EVEN, ROUND_HALF_UP
from . import evidence as E


def _post_record(c, attempt):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vpnl_post_lineage'").fetchone():
        return []
    return c.execute("SELECT serial,record FROM vpnl_post_lineage WHERE CASE WHEN json_valid(record) THEN json_extract(record,'$.body.attempt_id') END=? ORDER BY serial LIMIT 2", (attempt,)).fetchall()


def _lineage(store, c, job, attempt, delivery, root, revision, wd, epoch, token):
    records = _post_record(c, attempt)
    submission = c.execute('SELECT 1 FROM vpnl_submission_bindings WHERE job_id=?', (job,)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='vpnl_submission_bindings'").fetchone() else None
    path = Path(str(store.db_path) + '.intake.sqlite3')
    right = []
    if path.is_file():
        # Never create or migrate a sidecar during historical inspection.
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as sc:
            if sc.execute("SELECT 1 FROM sqlite_master WHERE name='intake_post_lineage'").fetchone():
                right = sc.execute("SELECT serial,record FROM intake_post_lineage WHERE CASE WHEN json_valid(record) THEN json_extract(record,'$.body.attempt_id') END=? ORDER BY serial LIMIT 2", (attempt,)).fetchall()
    if not records and not right and not submission:
        # Legacy acknowledgement chain only. This does not assign a renderer or
        # acquisition-price policy to the old record, or authorize any new POST.
        return
    if len(records) != 1 or records != right:
        raise ValueError('historical_post_lineage')
    serial, encoded = records[0]
    record = E.strict_object(encoded); body = record['body']
    db = c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone()
    expected = (attempt, delivery, root, revision, wd, epoch, token)
    actual = tuple(body[k] for k in ('attempt_id','delivery_id','root_id','revision','wire_digest','epoch','token_hash'))
    if (record['digest'] != E.digest(body) or body['version'] != 1 or body['serial'] != serial
            or not body.get('nonce') or db != (body['db_id'], body['cutover_id']) or actual != expected):
        raise ValueError('historical_post_lineage')


def _financial_display(envelope, receipt, payload):
    # Compare economic content, not current renderer output or a guessed old
    # renderer version. Unknown legacy layouts require manual REVIEW.
    text = envelope['embeds'][0].get('description')
    if not isinstance(text, str) or len(text) > 4096:
        raise ValueError('historical_financial_display')
    net = re.findall(r'(?:확정손익|순실현손익)\s*:\s*([+-]?[0-9][0-9,]*(?:\.[0-9]+)?)\s+([A-Z][A-Z0-9]*)', text)
    if len(net) != 1 or net[0][1] != receipt['body']['scope']['settlement']:
        raise ValueError('historical_financial_display')
    def matches(shown, actual):
        n = Decimal(shown.replace(',', '')); value = Decimal(actual)
        if not n.is_finite() or not value.is_finite():return False
        if n != 0 and n.is_signed() != value.is_signed():return False
        with localcontext() as arithmetic:
            arithmetic.prec = 512
            unit = Decimal(1).scaleb(n.as_tuple().exponent)
            return n in (value.quantize(unit, rounding=ROUND_HALF_EVEN), value.quantize(unit, rounding=ROUND_HALF_UP))
    if not matches(net[0][0], receipt['body']['result']['net_realized_pnl']):
        raise ValueError('historical_financial_display')
    roi = receipt['body']['result'].get('net_roi_pct')
    percentages = re.findall(r'([+-]?[0-9][0-9,]*(?:\.[0-9]+)?)%', text)
    roi_status = getattr(payload.roi_verification_status, 'value', payload.roi_verification_status)
    if roi is not None and roi_status == 'VERIFIED' and not payload.is_roi_estimated:
        if len(percentages) != 1 or not matches(percentages[0], roi):
            raise ValueError('historical_financial_display')
    elif percentages:
        raise ValueError('historical_financial_display')


def _validate_success_attempt(store, c, payload, root, revision, binding, states):
    """Verify a persisted SENT publication, never regenerate it for comparison.

    The caller still verifies financial facts and calls record_consumer within a
    savepoint to validate the producer/job/order, historical generation and edge.
    Failed or ambiguous chains remain consumer REVIEW. Invalidation is handled
    by the existing historical-generation/withdrawal logic, not new authority.
    """
    if not E.payload_valid(payload) or not E.payload_fill_valid(payload):
        raise ValueError('historical_signed_evidence')
    f = payload.financial_receipt; e = payload.execution_receipt
    scope = f['body']['scope']
    if scope['market_type'] != 'spot' or scope['settlement'] != 'USDT' or Decimal(str(scope['contract_size'])) != 1:
        raise ValueError('historical_unsupported_scope')
    if f['key_id'] != e['key_id'] or f['body']['execution_digest'] != E.digest(e['body']):
        raise ValueError('historical_execution_binding')
    if E.canonical(binding['scope']) != E.canonical(f['body']['scope']) or E.canonical(e['body']['scope']) != E.canonical(f['body']['scope']):
        raise ValueError('historical_scope')
    a = getattr(payload, 'accounting_receipt', None)
    if a is not None and (not E.verify(a, 'ACCOUNTING') or a['key_id'] != f['key_id'] or E.digest(a['body']) != f['body']['accounting_digest']):
        raise ValueError('historical_accounting')
    deliveries = c.execute('SELECT id,root_id,revision,wire,wire_digest,status,message_id,attempts,updated FROM vpnl_deliveries WHERE root_id=? AND revision=? ORDER BY id LIMIT 2', (root, revision)).fetchall()
    if len(deliveries) != 1:
        raise ValueError('historical_delivery')
    did, rid, rev, wire, wd, state, message, epoch, updated = deliveries[0]
    if (state not in states or rid != root or rev != revision or not isinstance(wire, bytes)
            or hashlib.sha256(wire).hexdigest() != wd or not isinstance(message, str)
            or not re.fullmatch(r'[0-9]{1,32}', message) or type(epoch) is not int or epoch < 1):
        raise ValueError('historical_wire')
    envelope = E.strict_object(wire.decode('utf8'))
    if (set(envelope) != {'embeds','allowed_mentions'} or envelope['allowed_mentions'] != {'parse': []}
            or not isinstance(envelope['embeds'], list) or len(envelope['embeds']) != 1
            or not isinstance(envelope['embeds'][0], dict)):
        raise ValueError('historical_wire_shape')
    _financial_display(envelope, f, payload)
    attempts = c.execute("SELECT id,epoch,token_hash,revision,wire_digest,message_id,outcome,expires FROM vpnl_delivery_attempts WHERE delivery_id=? AND phase='SENT' ORDER BY epoch LIMIT 2", (did,)).fetchall()
    if len(attempts) != 1:
        raise ValueError('historical_ack')
    aid, ae, token, ar, aw, am, outcome, expiry = attempts[0]
    if (ae != epoch or ar != revision or aw != wd or am != message or outcome is not None
            or not isinstance(token, str) or not re.fullmatch('[0-9a-f]{64}', token)
            or not isinstance(updated, (int, float)) or not isinstance(expiry, (int, float))
            or not math.isfinite(updated) or not math.isfinite(expiry) or not 0 < updated <= expiry):
        raise ValueError('historical_ack')
    _lineage(store, c, binding['producer']['id'], aid, did, root, revision, wd, epoch, token)


def validate_sent(store, c, payload, root, revision, binding):
    return _validate_success_attempt(store, c, payload, root, revision, binding, ('SENT',))


def inspect_uncertain_success(store, c, payload, root, revision, binding):
    """Evidence classification only; never changes UNKNOWN/HELD into SENT."""
    try:
        _validate_success_attempt(store, c, payload, root, revision, binding, ('UNKNOWN', 'HELD'))
        return 'SUCCESS_ACK_STATE_CONFLICT'
    except (ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error):
        return 'UNPROVEN_PUBLICATION'
