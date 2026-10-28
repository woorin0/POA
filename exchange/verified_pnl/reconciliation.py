"""Append-only manual review protocol. No delivery mutation or signing authority.

Only a host-registered read capability may confirm external message evidence.
This is not a Discord signature and does not authenticate a compromised host.
No production capability is supplied here. Unknown delivery does not mean absent.
"""
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import math
import re
import secrets
import sqlite3
import time
import uuid
from functools import wraps
from . import evidence as E

DECISIONS = frozenset({'EXTERNAL_DELIVERY_CONFIRMED', 'EXTERNAL_DELIVERY_UNCONFIRMED', 'EVIDENCE_INSUFFICIENT', 'EVIDENCE_CONFLICT'})
_CAPABILITY = None

class ReviewError(ValueError):
    pass

@dataclass(frozen=True)
class ExternalQuery:
    nonce: str
    binding_digest: str
    scope_json: str
    message_id: str
    reference_digest: str

@dataclass(frozen=True)
class ExternalObservation:
    decision: str
    nonce: str
    binding_digest: str
    scope_json: str
    message_id: str
    channel_id: str
    normalized_wire: bytes | None
    observed_at: float

@dataclass(frozen=True)
class MessageEvidenceCapability:
    scope_json: str
    channel_id: str
    source_reference: str
    observe: object
    contract: str = 'FROZEN_EMBED_ENVELOPE_SHA256_V1'


def configure_evidence_capability(capability):
    """Trusted service bootstrap only; never called from operator/DB input.

    A real adapter must independently authenticate its channel/read client and
    normalize the retrieved message to the frozen envelope contract. Merely
    setting a flag/source string is not that implementation. Default is absent.
    """
    global _CAPABILITY
    if capability is not None:
        if type(capability) is not MessageEvidenceCapability or not callable(capability.observe):raise ReviewError('evidence_capability_invalid')
        scope=E.strict_object(capability.scope_json)
        if E.canonical(scope)!=capability.scope_json or scope.get('market_type')!='spot' or scope.get('settlement')!='USDT':raise ReviewError('evidence_scope_invalid')
        if not re.fullmatch('[0-9]{1,32}',capability.channel_id) or not capability.source_reference or capability.contract!='FROZEN_EMBED_ENVELOPE_SHA256_V1':raise ReviewError('evidence_contract_invalid')
    _CAPABILITY=capability


def install(c):
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_reconciliation_reviews(id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,binding TEXT NOT NULL,binding_digest TEXT NOT NULL,actor_digest TEXT NOT NULL,decision TEXT NOT NULL,external_evidence TEXT,created REAL NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_reconciliation_actions(id TEXT PRIMARY KEY,review_id TEXT NOT NULL,kind TEXT NOT NULL,binding_digest TEXT NOT NULL,result TEXT NOT NULL,actor_digest TEXT NOT NULL,created REAL NOT NULL)')
    for table in ('vpnl_reconciliation_reviews','vpnl_reconciliation_actions'):
        for verb in ('UPDATE','DELETE'):
            c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_reconciliation_record'); END")


def _actor(actor,approved):
    if approved is not True or type(approved) is not bool:raise ReviewError('explicit_approval_required')
    if not isinstance(actor,str) or not 1<=len(actor)<=128 or not actor.isprintable() or re.search(r'(?i)bearer|api.?key|secret|token|webhook|https?:|canary',actor):raise ReviewError('actor_invalid')
    return E.digest(actor)


def _uuid(value):
    try:
        if not isinstance(value,str) or value not in (uuid.UUID(value).hex,str(uuid.UUID(value))):raise ValueError()
        return uuid.UUID(value).hex
    except (ValueError,TypeError,AttributeError):raise ReviewError('selector_invalid') from None


def _selector(delivery,root,revision,wire_digest,obligation):
    if type(revision) is not int or revision<1 or not isinstance(wire_digest,str) or not re.fullmatch('[0-9a-f]{64}',wire_digest) or not isinstance(obligation,str) or not re.fullmatch('[0-9a-f]{64}',obligation):raise ReviewError('selector_invalid')
    return dict(delivery=_uuid(delivery),root=_uuid(root),revision=revision,wire_digest=wire_digest,obligation=obligation)


def _snapshot(store,c,sel):
    from .history import _consumer_identity,_generation_check
    from .historical_delivery import inspect_uncertain_success
    try:
        d=c.execute('SELECT root_id,revision,wire,wire_digest,status,message_id,attempts,updated FROM vpnl_deliveries WHERE id=?',(sel['delivery'],)).fetchone()
        if not d or (d[0],d[1],d[3])!=(sel['root'],sel['revision'],sel['wire_digest']) or d[4] not in ('UNKNOWN','HELD'):raise ReviewError('delivery_cas_conflict')
        if not isinstance(d[2],bytes) or hashlib.sha256(d[2]).hexdigest()!=d[3]:raise ReviewError('wire_invalid')
        if c.execute("SELECT 1 FROM vpnl_delivery_attempts WHERE delivery_id=? AND phase IN ('CLAIMED','POSTING') LIMIT 1",(sel['delivery'],)).fetchone():raise ReviewError('active_lease')
        row=c.execute('SELECT p.id,p.generation_id,p.scope,p.financial,o.successor,o.change_receipt,o.state FROM vpnl_uncertain_publications p JOIN vpnl_uncertain_obligations o ON o.publication_id=p.id WHERE p.root_id=? AND p.revision=? AND o.id=?',(sel['root'],sel['revision'],sel['obligation'])).fetchone()
        if not row or row[6]!='REVIEW_REQUIRED':raise ReviewError('obligation_unverified')
        raw=c.execute('SELECT payload,financial_digest FROM vpnl_revisions WHERE root_id=? AND revision=?',(sel['root'],sel['revision'])).fetchone()
        bound=c.execute('SELECT binding,binding_digest FROM vpnl_root_bindings WHERE root_id=?',(sel['root'],)).fetchone()
        from .models import NotificationPayload
        p=NotificationPayload(**E.strict_object(raw[0]));binding=E.strict_object(bound[0])
        if E.digest(binding)!=bound[1] or E.digest(store._facts(p))!=raw[1] or E.canonical(p.financial_receipt)!=row[3]:raise ReviewError('binding_invalid')
        fin,body,gid,scope,job,data,order=_consumer_identity(store,c,p,sel['root'],binding)
        change=E.strict_object(row[5])
        expected=dict(scope=body['scope'],invalidated_generation=gid,successor_generation=row[4])
        if not E.verify(change,'HISTORY_CHANGE',expected) or change['key_id']!=fin['key_id']:raise ReviewError('successor_unverified')
        head=c.execute('SELECT generation_id,reconciling FROM vpnl_history_heads WHERE scope=?',(scope,)).fetchone()
        if not head or head[0]!=row[4] or not _generation_check(store,row[4],historical=True):raise ReviewError('successor_stale')
        ack=c.execute('SELECT id,epoch,token_hash,revision,wire_digest,phase,message_id,outcome,expires FROM vpnl_delivery_attempts WHERE delivery_id=? ORDER BY epoch DESC LIMIT 40',(sel['delivery'],)).fetchall()
        snapshot=dict(selector=sel,scope=body['scope'],job=job,order=order,root_head=c.execute('SELECT head FROM vpnl_roots WHERE id=?',(sel['root'],)).fetchone()[0],producer_binding_digest=bound[1],financial_body_digest=E.digest(body),financial_facts_digest=raw[1],generation=gid,successor=row[4],change_digest=E.digest(change),publication_id=row[0],state=d[4],message_id=d[5],attempts=ack,delivery_epoch=d[6],delivery_updated=d[7],historical_evidence=inspect_uncertain_success(store,c,p,sel['root'],sel['revision'],binding))
        return snapshot,d[2]
    except ReviewError:raise
    except Exception:raise ReviewError('review_binding_invalid') from None


def _answer(row):
    external=E.strict_object(row[6]) if row[6] else None
    return dict(review_id=row[0],decision=row[5],binding_digest=row[3],external_evidence_verified=bool(external and external.get('delivery_verified')),review_state='REVIEW_RECORDED',correction_prepared=False,delivery_authorized=False)


def record_review(store,*,request_id,delivery,root,revision,wire_digest,obligation,actor,approved,decision,external_reference):
    """Commit an idempotent review record; it is never a resend/release command."""
    actor_digest=_actor(actor,approved);rid=_uuid(request_id);sel=_selector(delivery,root,revision,wire_digest,obligation)
    if not isinstance(decision,str) or decision not in DECISIONS or not isinstance(external_reference,str) or not 1<=len(external_reference)<=4096:raise ReviewError('review_decision_invalid')
    ref=E.digest(external_reference);request_digest=E.digest([sel,actor_digest,decision,ref])
    try:
        with store._connect_read() as c:
            c.execute('BEGIN')
            prior=c.execute('SELECT * FROM vpnl_reconciliation_reviews WHERE id=?',(rid,)).fetchone()
            if prior:
                if prior[1]!=request_digest:raise ReviewError('review_id_conflict')
                return _answer(prior)
            snapshot,wire=_snapshot(store,c,sel);version=E.digest(snapshot)
        cap=_CAPABILITY;external=None
        if cap is None:
            if decision=='EXTERNAL_DELIVERY_CONFIRMED':raise ReviewError('external_capability_not_configured')
        else:
            if cap.scope_json!=E.canonical(snapshot['scope']):raise ReviewError('external_scope_mismatch')
            query=ExternalQuery(secrets.token_hex(24),version,cap.scope_json,snapshot['message_id'],ref)
            try:observed=cap.observe(query)
            except Exception:raise ReviewError('external_evidence_unavailable') from None
            if type(observed) is not ExternalObservation:raise ReviewError('external_evidence_invalid')
            if observed.nonce!=query.nonce or observed.binding_digest!=version or observed.scope_json!=cap.scope_json or observed.channel_id!=cap.channel_id or observed.message_id!=snapshot['message_id']:raise ReviewError('external_evidence_binding')
            if observed.decision!=decision or type(observed.observed_at) not in (int,float) or not math.isfinite(observed.observed_at) or abs(time.time()-observed.observed_at)>60:raise ReviewError('external_evidence_stale_or_conflict')
            verified=False
            if decision=='EXTERNAL_DELIVERY_CONFIRMED':
                if snapshot['historical_evidence']!='SUCCESS_ACK_STATE_CONFLICT' or not snapshot['message_id'] or type(observed.normalized_wire) is not bytes or observed.normalized_wire!=wire:raise ReviewError('external_confirmation_insufficient')
                verified=True
            external=dict(source_digest=E.digest(cap.source_reference),reference_digest=ref,observation_digest=E.digest([observed.decision,observed.nonce,observed.binding_digest,observed.scope_json,observed.message_id,observed.channel_id,hashlib.sha256(observed.normalized_wire).hexdigest() if type(observed.normalized_wire) is bytes else None,observed.observed_at]),message_id=observed.message_id,channel_id=observed.channel_id,contract=cap.contract,delivery_verified=verified)
        with store._connect() as c:
            prior=c.execute('SELECT * FROM vpnl_reconciliation_reviews WHERE id=?',(rid,)).fetchone()
            if prior:
                if prior[1]!=request_digest:raise ReviewError('review_id_conflict')
                return _answer(prior)
            fresh,_=_snapshot(store,c,sel)
            if E.digest(fresh)!=version or _CAPABILITY is not cap:raise ReviewError('review_cas_conflict')
            c.execute('INSERT INTO vpnl_reconciliation_reviews VALUES(?,?,?,?,?,?,?,?)',(rid,request_digest,E.canonical(snapshot),version,actor_digest,decision,E.canonical(external) if external else None,time.time()))
            result=_answer(c.execute('SELECT * FROM vpnl_reconciliation_reviews WHERE id=?',(rid,)).fetchone())
        return result
    except ReviewError:raise
    except Exception:raise ReviewError('review_persistence_failed') from None


def prepare_correction(store,*,review_id,actor,approved):
    """Inspect the real guard. No fake FINANCIAL signature or bypass is supplied."""
    actor_digest=_actor(actor,approved);rid=_uuid(review_id)
    from .history import generation_valid
    with store._connect() as c:
        row=c.execute('SELECT * FROM vpnl_reconciliation_reviews WHERE id=?',(rid,)).fetchone()
        if not row or not row[6] or not E.strict_object(row[6]).get('delivery_verified') or row[5]!='EXTERNAL_DELIVERY_CONFIRMED':raise ReviewError('confirmed_review_required')
        if row[4]!=actor_digest:raise ReviewError('review_actor_mismatch')
        binding=E.strict_object(row[2]);fresh,_=_snapshot(store,c,binding['selector'])
        if E.digest(fresh)!=row[3]:raise ReviewError('review_cas_conflict')
        if generation_valid(store,binding['successor']):raise ReviewError('trusted_producer_context_required')
        result='BLOCKED_REVIEW_GUARD';key=E.digest(['prepare-review',rid,row[3]])
        old=c.execute('SELECT review_id,binding_digest,result FROM vpnl_reconciliation_actions WHERE id=?',(key,)).fetchone()
        if old is None:c.execute('INSERT INTO vpnl_reconciliation_actions VALUES(?,?,?,?,?,?,?)',(key,rid,'PREPARE',row[3],result,actor_digest,time.time()))
        elif old!=(rid,row[3],result):raise ReviewError('prepare_conflict')
    return dict(status=result,correction_prepared=False,delivery_authorized=False)


def release_correction(store,*,review_id,delivery,root,revision,wire_digest,obligation,actor,approved):
    actor_digest=_actor(actor,approved);rid=_uuid(review_id);sel=_selector(delivery,root,revision,wire_digest,obligation)
    with store._connect_read() as c:
        row=c.execute('SELECT * FROM vpnl_reconciliation_reviews WHERE id=?',(rid,)).fetchone()
        if not row or row[4]!=actor_digest or E.strict_object(row[2])['selector']!=sel:raise ReviewError('release_selector_conflict')
    # The review reference is the uncertain predecessor, never a resend target.
    # No HELD correction has been safely prepared under the current guard.
    raise ReviewError('correction_not_prepared')


def _safe_operation(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:return function(*args, **kwargs)
        except ReviewError:raise
        except Exception:raise ReviewError('reconciliation_unavailable') from None
    return call

prepare_correction = _safe_operation(prepare_correction)
release_correction = _safe_operation(release_correction)
