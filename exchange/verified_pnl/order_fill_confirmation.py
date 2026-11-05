"""Opt-in notification-only submissions. No financial authority, orders or sender.

Original Intake rows and financial jobs are untouched. New tables are additive;
existing database/sidecar restore and cutover gates remain mandatory.
"""
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from .evidence import canonical, digest, strict_object, payload_fill_valid
from .ledger import identity
from .order_fill_read import CONTRACT, ReadHost, ReadRejected, request_scope, validate

_HOSTS = {}


def prepare(settings):
    if getattr(settings, 'POA_EXCHANGE_FILL_CONFIRMATION_ENABLED', False) is not True:
        path=getattr(settings,'VERIFIED_NOTIFICATIONS_DB_PATH',None)
        if isinstance(path,str):_HOSTS.pop(str(Path(path).resolve()),None)
        return None
    if not (settings.VERIFIED_DISCORD_ALERTS and settings.VERIFIED_FILL_PRICE and settings.DISCORD_OUTBOX_ENABLED):
        return None
    path = str(Path(settings.VERIFIED_NOTIFICATIONS_DB_PATH).resolve())
    host = _HOSTS.get(path)
    if host is None or host.settings is not settings:
        host = ReadHost(settings)
        _HOSTS[path] = host
    return host


def host_for(store):
    host = _HOSTS.get(store.db_path)
    return host if host and prepare(host.settings) is host else None


def capture_options(settings,entry=None):
    """Producer capability before, not after, the original sidecar commit."""
    host=prepare(settings)
    if host is None:return {}
    from .estimated_pnl import producer_token
    if entry is None:
        import inspect
        frame=inspect.currentframe().f_back.f_back
        candidate=frame.f_globals.get('log')
        if callable(candidate) and getattr(candidate,'__code__',None) is frame.f_code:entry=candidate
        del frame
    try:return dict(confirmation_host=host,producer=producer_token(entry))
    except ValueError:return {}


def record_intent(c,iid,request,producer,host):
    """Atomic with original Intake, unchanged request bytes/digest and old status."""
    from .estimated_pnl import producer_allowed
    if type(host) is not ReadHost or not producer_allowed(producer) or prepare(host.settings) is not host:return False
    expected=Path(str(Path(host.settings.VERIFIED_NOTIFICATIONS_DB_PATH).resolve())+'.intake.sqlite3')
    if Path(c.execute('PRAGMA database_list').fetchone()[2]).resolve()!=expected:return False
    try:
        scope=request_scope(request)
        key,_,_=host.credential(scope['exchange'])
        if identity(scope['exchange'],key)!=scope['account']:return False
    except ReadRejected:return False
    c.execute("CREATE TABLE IF NOT EXISTS notification_fill_intents(seq INTEGER PRIMARY KEY AUTOINCREMENT,intake_id TEXT UNIQUE NOT NULL,record TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'CAPTURED')")
    c.execute("CREATE TRIGGER IF NOT EXISTS notification_fill_intent_immutable BEFORE UPDATE OF seq,intake_id,record ON notification_fill_intents BEGIN SELECT RAISE(ABORT,'immutable_fill_intent'); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS notification_fill_intent_retain BEFORE DELETE ON notification_fill_intents BEGIN SELECT RAISE(ABORT,'retained_fill_intent'); END")
    if c.execute('SELECT 1 FROM notification_fill_intents WHERE intake_id=?',(iid,)).fetchone():return True
    rd,created=c.execute('SELECT request_digest,created FROM notification_intake WHERE id=?',(iid,)).fetchone()
    if created<host.created:return False
    old=c.execute("SELECT 1 FROM notification_intake WHERE created<? AND id!=? AND CASE WHEN json_valid(request) THEN json_extract(request,'$.source_account_ref') END=? AND CASE WHEN json_valid(request) THEN json_extract(request,'$.info.exchange') END=? AND CASE WHEN json_valid(request) THEN json_extract(request,'$.info.unified_symbol') END=? AND CASE WHEN json_valid(request) THEN json_extract(request,'$.result.id') END=? LIMIT 1",(host.created,iid,scope['account'],scope['exchange'],scope['symbol'],scope['order_id'])).fetchone()
    if old:return False
    body=dict(contract=CONTRACT,intake_id=iid,request_digest=rd,sidecar_id=c.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone()[0],scope_digest=digest(scope),producer='POA_MAIN_LOG_V1',captured=created)
    c.execute('INSERT INTO notification_fill_intents(intake_id,record) VALUES(?,?)',(iid,canonical(dict(body=body,digest=digest(body)))))
    return True


def _intent(c,iid,request,rd,sid):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='notification_fill_intents'").fetchone():return None
    row=c.execute('SELECT record FROM notification_fill_intents WHERE intake_id=?',(iid,)).fetchone()
    if not row:return None
    record=strict_object(row[0]);b=record['body']
    expected=dict(contract=CONTRACT,intake_id=iid,request_digest=rd,sidecar_id=sid,scope_digest=digest(request_scope(request)),producer='POA_MAIN_LOG_V1',captured=b['captured'])
    if b!=expected or record['digest']!=digest(b):raise ReadRejected('FILL_INTENT_INVALID')
    return b


def install(c):
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_fill_requests(id TEXT PRIMARY KEY,intake_id TEXT NOT NULL,request_digest TEXT NOT NULL,sidecar_id TEXT NOT NULL,request_data TEXT NOT NULL,enrolled REAL NOT NULL,admission_digest TEXT NOT NULL,state TEXT NOT NULL,lease TEXT,expires REAL NOT NULL,due REAL NOT NULL,error TEXT,attempts INTEGER NOT NULL DEFAULT 0)')
    c.execute('CREATE INDEX IF NOT EXISTS vpnl_fill_requests_due ON vpnl_fill_requests(state,due,id)')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_fill_submissions(id TEXT PRIMARY KEY,proof TEXT NOT NULL,proof_digest TEXT NOT NULL,lineage TEXT NOT NULL,lineage_digest TEXT NOT NULL,FOREIGN KEY(id) REFERENCES vpnl_fill_requests(id))')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_fill_reviews(id TEXT PRIMARY KEY,reason TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_fill_read_attempts(id TEXT NOT NULL,epoch INTEGER NOT NULL,token_hash TEXT NOT NULL,expires REAL NOT NULL,PRIMARY KEY(id,epoch),FOREIGN KEY(id) REFERENCES vpnl_fill_requests(id))')
    c.execute("CREATE TRIGGER IF NOT EXISTS vpnl_fill_request_immutable BEFORE UPDATE OF id,intake_id,request_digest,sidecar_id,request_data,enrolled,admission_digest ON vpnl_fill_requests BEGIN SELECT RAISE(ABORT,'immutable_fill_origin'); END")
    for table in ('vpnl_fill_requests', 'vpnl_fill_submissions', 'vpnl_fill_reviews','vpnl_fill_read_attempts'):
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_retain BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'retained_fill_confirmation'); END")
    for table in ('vpnl_fill_submissions', 'vpnl_fill_reviews','vpnl_fill_read_attempts'):
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'immutable_fill_confirmation'); END")


def _origin(store, row):
    from .intake import IntakeStore
    iid, rd, sid, encoded = row[:4]
    path = Path(str(store.db_path) + '.intake.sqlite3')
    c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        saved = c.execute('SELECT request,request_digest,status FROM notification_intake WHERE id=?', (iid,)).fetchone()
        if saved is None or saved[:2] != (encoded, rd) or saved[2] == 'REVIEW' or iid != rd:
            raise ReadRejected('FILL_INTAKE_MISMATCH')
        if c.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone() != (sid,):
            raise ReadRejected('FILL_SIDECAR_MISMATCH')
        request=IntakeStore.parse(encoded, rd)
        intent=_intent(c,iid,request,rd,sid)
        if intent is None or intent['captured']!=row[4]:raise ReadRejected('FILL_INTENT_INVALID')
        return request
    finally:
        c.close()


def enroll(settings, iid, producer=None):
    """Called only at the registered POA producer after its original Intake commit."""
    host = prepare(settings)
    if host is None:
        return False
    from .estimated_pnl import producer_allowed
    from .intake import IntakeStore
    from .delivery_store import NotificationOutboxStore
    if producer is not None and not producer_allowed(producer):
        raise ReadRejected('FILL_PRODUCER_REJECTED')
    intake = IntakeStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
    with intake.connect() as sc:
        row = sc.execute('SELECT request,request_digest,status,created FROM notification_intake WHERE id=?', (iid,)).fetchone()
        if row is None or row[2] == 'REVIEW':
            return False
        request = intake.parse(row[0], row[1])
        scope = request_scope(request)
        key, _, _ = host.credential(scope['exchange'])
        if identity(scope['exchange'], key) != scope['account']:
            raise ReadRejected('FILL_ACCOUNT_MISMATCH')
        if scope['exchange'] == 'UPBIT' and not settings.UPBIT_VERIFIED_ALERTS:
            return False
        sid = sc.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone()[0]
        intent=_intent(sc,iid,request,row[1],sid)
        if intent is None:return False
    job = identity(CONTRACT, scope['exchange'], scope['account'], scope['market_type'], scope['symbol'], scope['order_id'])
    store = NotificationOutboxStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
    with store._connect() as c:
        install(c)
        prior = c.execute('SELECT request_data FROM vpnl_fill_requests WHERE id=?', (job,)).fetchone()
        if prior:
            # Same physical order cannot acquire a second strategy/symbol/side.
            if canonical(request_scope(strict_object(prior[0]))) != canonical(scope):
                c.execute('INSERT OR IGNORE INTO vpnl_fill_reviews VALUES(?,?)', (job, 'FILL_ORIGIN_CONFLICT'))
            outcome='REVIEW' if canonical(request_scope(strict_object(prior[0])))!=canonical(scope) else 'ENROLLED'
        else:
            body = dict(contract=CONTRACT, job=job, intake_id=iid, request_digest=row[1], sidecar_id=sid,
                        scope=scope, producer='POA_MAIN_LOG_V1', enrolled=intent['captured'])
            c.execute('INSERT INTO vpnl_fill_requests(id,intake_id,request_digest,sidecar_id,request_data,enrolled,admission_digest,state,lease,expires,due,error) VALUES(?,?,?,?,?,?,?,?,NULL,0,0,NULL)',
                      (job, iid, row[1], sid, row[0], body['enrolled'], digest(body), 'WAIT_READ'))
            outcome='ENROLLED'
    # Main commit precedes this sidecar acknowledgement. Lost acknowledgement is
    # replayable through the immutable intent and unique physical-order key.
    with intake.connect() as sc:sc.execute('UPDATE notification_fill_intents SET status=? WHERE intake_id=?',(outcome,iid))
    return outcome=='ENROLLED'


def recover_intents(settings,store):
    from .intake import IntakeStore
    side=IntakeStore(store.db_path)
    with side.connect() as sc:
        if not sc.execute("SELECT 1 FROM sqlite_master WHERE name='notification_fill_intents'").fetchone():return
        rows=sc.execute("SELECT intake_id FROM notification_fill_intents WHERE status='CAPTURED' ORDER BY seq LIMIT 40").fetchall()
    for (iid,) in rows:
        try:enroll(settings,iid)
        except sqlite3.Error:continue # durable CAPTURED intent remains; no GET yet
        except (ValueError,KeyError,TypeError):
            with side.connect() as sc:sc.execute("UPDATE notification_fill_intents SET status='REVIEW' WHERE intake_id=?",(iid,))


def _enrollment(job, row):
    request = strict_object(row[3])
    return dict(contract=CONTRACT, job=job, intake_id=row[0], request_digest=row[1], sidecar_id=row[2],
                scope=request_scope(request), producer='POA_MAIN_LOG_V1', enrolled=row[4])


def validate_stored(store, c, payload, binding=None):
    """Recheck persisted authenticated-read provenance, math and restore lineage."""
    try:
        if payload.notification_type != CONTRACT or payload.event_type != 'fill_confirmation' or payload.event_version != 1:
            return False
        if any(getattr(payload, k) is not None for k in ('execution_receipt', 'accounting_receipt', 'financial_receipt', 'outcome_receipt', 'financial_revocation_receipt', 'estimated_pnl', 'net_pnl', 'roi_pct', 'cost_basis')):
            return False
        proof = payload.fill_confirmation
        if not validate(proof):
            return False
        if canonical(payload.dict())!=canonical(make_payload(proof).dict()):
            return False
        scope = proof['scope']
        job = identity(CONTRACT, scope['exchange'], scope['account'], scope['market_type'], scope['symbol'], scope['order_id'])
        if (payload.event_id, payload.exchange, payload.symbol, payload.order_id) != (job, scope['exchange'], scope['symbol'], scope['order_id']):
            return False
        row = c.execute('SELECT intake_id,request_digest,sidecar_id,request_data,enrolled,admission_digest FROM vpnl_fill_requests WHERE id=?', (job,)).fetchone()
        if not row or c.execute('SELECT 1 FROM vpnl_fill_reviews WHERE id=?', (job,)).fetchone():
            return False
        if digest(_enrollment(job, row)) != row[5]:
            return False
        original = _origin(store, row)
        if canonical(request_scope(original)) != canonical(scope):
            return False
        record = c.execute('SELECT proof,proof_digest,lineage,lineage_digest FROM vpnl_fill_submissions WHERE id=?', (job,)).fetchone()
        if record is None or record[0] != canonical(proof) or record[1] != digest(proof) or digest(strict_object(record[2])) != record[3]:
            return False
        lineage = strict_object(record[2])
        dbid, cutid = c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone()
        witness=c.execute('SELECT token_hash FROM vpnl_fill_read_attempts WHERE id=? AND epoch=?',(job,lineage.get('read_epoch'))).fetchone()
        if witness is None or lineage != dict(contract=CONTRACT, job=job, db_id=dbid, cutover_id=cutid, intake_id=row[0], request_digest=row[1], sidecar_id=row[2], admission_digest=row[5], proof_digest=record[1],read_epoch=lineage['read_epoch'],read_lease_hash=witness[0]):
            return False
        expected = root_binding(proof, lineage)
        if binding is not None and canonical(binding) != canonical(expected):
            return False
        host = host_for(store)
        if host is None or identity(scope['exchange'], host.credential(scope['exchange'])[0]) != scope['account']:
            return False
        return True
    except (ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        return False


def root_binding(proof, lineage):
    scope = proof['scope']
    return dict(version=1, scope=scope, exchange=scope['exchange'], symbol=scope['symbol'], order_id=scope['order_id'],
                producer={'kind': CONTRACT, 'id': lineage['job']}, read_proof_digest=digest(proof), submission_digest=digest(lineage))


def predecessors(c, proof, own):
    """Only SENT or no prior messages permit a first/followup confirmation."""
    scope = proof['scope']
    rows = c.execute('SELECT r.alias,b.binding,v.payload,d.status,d.id,d.wire,d.wire_digest,d.message_id,d.revision,b.binding_digest,v.financial_digest FROM vpnl_roots r JOIN vpnl_root_bindings b ON b.root_id=r.id JOIN vpnl_revisions v ON v.root_id=r.id JOIN vpnl_deliveries d ON d.root_id=r.id AND d.revision=v.revision WHERE CASE WHEN json_valid(b.binding) THEN json_extract(b.binding,\'$.exchange\') END=? AND CASE WHEN json_valid(b.binding) THEN json_extract(b.binding,\'$.order_id\') END=? AND CASE WHEN json_valid(b.binding) THEN json_extract(b.binding,\'$.symbol\') END=? LIMIT 41', (scope['exchange'], scope['order_id'],scope['symbol'])).fetchall()
    if len(rows) > 40:
        return 'FILL_PREDECESSOR_REVIEW'
    from .evidence import account_reference
    accounts = {scope['account'], account_reference(scope['exchange'], scope['account'])}
    accounts.discard(None)
    for alias, encoded, raw, state, did, wire, wd, message_id, revision, bd, fd in rows:
        if alias == own:
            continue
        bound = strict_object(encoded)
        prior = strict_object(raw)
        old_scope = bound.get('scope') or {}
        if old_scope.get('account') not in accounts:
            if old_scope.get('account') is not None:
                continue
            return 'FILL_PREDECESSOR_REVIEW'
        if old_scope.get('market_type') != scope['market_type'] or prior.get('symbol') != scope['symbol']:
            return 'FILL_PREDECESSOR_REVIEW'
        if (prior.get('strategy_id') or prior.get('order_name') or 'default') != scope['strategy']:
            return 'FILL_STRATEGY_REVIEW'
        if state in ('PENDING', 'CLAIMED'):
            return 'FILL_PREDECESSOR_PENDING'
        if state != 'SENT':
            return 'FILL_PREDECESSOR_REVIEW'
        from .models import NotificationPayload
        from .delivery_store import NotificationOutboxStore
        if digest(bound)!=bd or hashlib.sha256(wire).hexdigest()!=wd or digest(NotificationOutboxStore._facts(NotificationPayload(**prior)))!=fd:
            return 'FILL_PREDECESSOR_REVIEW'
        if not c.execute("SELECT 1 FROM vpnl_delivery_attempts WHERE delivery_id=? AND revision=? AND wire_digest=? AND phase='SENT' AND outcome IS NULL AND message_id IS ? AND length(token_hash)=64",(did,revision,wd,message_id)).fetchone():
            return 'FILL_PREDECESSOR_REVIEW'
        if payload_fill_valid(NotificationPayload(**prior)) or prior.get('notification_type') == CONTRACT:
            return 'FILL_ALREADY_CONFIRMED'
    return None


def make_payload(proof):
    from .models import NotificationPayload, FillStatus, ExitReason, PnlResult
    s = proof['scope']
    return NotificationPayload(event_id=identity(CONTRACT, s['exchange'], s['account'], s['market_type'], s['symbol'], s['order_id']),
        event_type='fill_confirmation', event_version=1, notification_type=CONTRACT, fill_confirmation=proof,
        exchange=s['exchange'], symbol=s['symbol'], order_id=s['order_id'], client_order_id=s['client_order_id'],
        side='매수' if s['side'] == 'buy' else '매도', order_name=s['order_name'], strategy_id=s['strategy'],
        exit_reason=ExitReason.UNKNOWN, pnl_result=PnlResult.UNKNOWN, fill_status=FillStatus.UNVERIFIED,
        event_timestamp=proof['observed_at'], verification_source=CONTRACT)


def enqueue(store, job):
    from .migrations import require_cutover
    with store._connect() as c:
        require_cutover(c)
        if not store._restore_ready(c):
            return False
        saved = c.execute('SELECT proof,lineage FROM vpnl_fill_submissions WHERE id=?', (job,)).fetchone()
        if not saved:
            return False
        proof, lineage = map(strict_object, saved)
        p = make_payload(proof)
        binding = root_binding(proof, lineage)
        if not validate_stored(store, c, p, binding):
            return False
        existing = c.execute('SELECT id FROM vpnl_roots WHERE alias=?', (job,)).fetchone()
        if existing:
            return False  # never requeue, revise or rewrite any existing delivery
        reason = predecessors(c, proof, job)
        if reason:
            c.execute('UPDATE vpnl_fill_requests SET error=?,due=? WHERE id=?', (reason, time.time()+5, job))
            return False
        rid, did = uuid.uuid4().hex, uuid.uuid4().hex
        wire = store._wire(p)
        now = time.time()
        c.execute('INSERT INTO vpnl_roots VALUES(?,?,?,1)', (rid, job, digest(['execution-root-v1', job, binding])))
        c.execute('INSERT INTO vpnl_root_bindings VALUES(?,?,?)', (rid, canonical(binding), digest(binding)))
        c.execute('INSERT INTO vpnl_revisions VALUES(?,?,?,?,?,?)', (rid, 1, p.json(), digest(store._facts(p)), CONTRACT, now))
        settings=host_for(store).settings
        attempts=settings.DISCORD_RETRY_MAX_ATTEMPTS if settings.DISCORD_RETRY_ENABLED else 1
        c.execute("INSERT INTO vpnl_deliveries(id,root_id,revision,kind,parent,wire,wire_digest,status,due,updated,max_attempts) VALUES(?,?,1,'INITIAL',NULL,?,?,'PENDING',?,?,?)", (did, rid, wire, hashlib.sha256(wire).hexdigest(), now, now,attempts))
        c.execute("UPDATE vpnl_fill_requests SET state='ENQUEUED',lease=NULL,error=NULL WHERE id=?", (job,))
        return True


def tick(settings, store):
    host = prepare(settings)
    if host is None:
        return
    from .intake import restore_preflight
    if not restore_preflight(store)['ready']:
        return
    recover_intents(settings,store)
    with store._connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vpnl_fill_requests'").fetchone():
            return
        now = time.time()
        # Two consecutive abandoned read leases are a fault. Completed pending
        # reads and recoverable HTTP errors must not exhaust this crash guard.
        c.execute("UPDATE vpnl_fill_requests SET state='REVIEW',lease=NULL,error='FILL_READ_ATTEMPTS_EXHAUSTED' WHERE state='READING' AND expires<? AND (error='FILL_READ_LEASE_EXPIRED' OR (attempts>=2 AND error IS NULL))",(now,))
        c.execute("UPDATE vpnl_fill_requests SET state='WAIT_READ',lease=NULL,error='FILL_READ_LEASE_EXPIRED',due=? WHERE state='READING' AND expires<?", (now+5,now))
        jobs = c.execute("SELECT id,state FROM vpnl_fill_requests WHERE (state='PROVED' OR (state='WAIT_READ' AND due<=?)) AND id NOT IN (SELECT id FROM vpnl_fill_reviews) ORDER BY enrolled,id LIMIT 20", (time.time(),)).fetchall()
    for job, state in jobs:
        if state == 'PROVED':
            store.enqueue_confirmation(job)
            continue
        lease = uuid.uuid4().hex
        with store._connect() as c:
            expires=time.time()+120
            if not c.execute("UPDATE vpnl_fill_requests SET state='READING',lease=?,expires=?,attempts=attempts+1 WHERE id=? AND state='WAIT_READ'", (lease, expires, job)).rowcount:
                continue
            epoch=c.execute('SELECT attempts FROM vpnl_fill_requests WHERE id=?',(job,)).fetchone()[0]
            c.execute('INSERT INTO vpnl_fill_read_attempts VALUES(?,?,?,?)',(job,epoch,digest(lease),expires))
            row = c.execute('SELECT intake_id,request_digest,sidecar_id,request_data,enrolled,admission_digest FROM vpnl_fill_requests WHERE id=?', (job,)).fetchone()
        try:
            if digest(_enrollment(job, row)) != row[5]:
                raise ReadRejected('FILL_ORIGIN_INVALID')
            request = _origin(store, row)
            observation = host.observe(request)
            with store._connect() as c:
                if not c.execute("SELECT 1 FROM vpnl_fill_requests WHERE id=? AND state='READING' AND lease=? AND expires>?", (job, lease, time.time())).fetchone():
                    continue
                if not store._restore_ready(c) or not host.consume(observation):
                    raise ReadRejected('FILL_CAPABILITY_REJECTED')
                if canonical(request_scope(_origin(store, row))) != canonical(strict_object(observation.encoded)['scope']):
                    raise ReadRejected('FILL_ORIGIN_INVALID')
                dbid, cutid = c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone()
                lineage = dict(contract=CONTRACT, job=job, db_id=dbid, cutover_id=cutid, intake_id=row[0], request_digest=row[1], sidecar_id=row[2], admission_digest=row[5], proof_digest=digest(strict_object(observation.encoded)),read_epoch=epoch,read_lease_hash=digest(lease))
                c.execute('INSERT INTO vpnl_fill_submissions VALUES(?,?,?,?,?)', (job, observation.encoded, lineage['proof_digest'], canonical(lineage), digest(lineage)))
                c.execute("UPDATE vpnl_fill_requests SET state='PROVED',lease=NULL,error=NULL WHERE id=?", (job,))
            store.enqueue_confirmation(job)
        except Exception as exc:
            code = exc.code if isinstance(exc, ReadRejected) else 'FILL_CONFIRMATION_REVIEW'
            retryable = code in ('FILL_ORDER_PENDING','FILL_RATE_LIMITED','FILL_TIMEOUT','FILL_TEMPORARY_UNAVAILABLE')
            with store._connect() as c:
                if retryable:
                    # Bounded request rate, not a bounded order lifetime: valid
                    # GTC/limit orders may fill well after their first observation.
                    backoff = min(120, 5 * 2 ** min(epoch - 1, 5))
                    c.execute("UPDATE vpnl_fill_requests SET state='WAIT_READ',lease=NULL,due=?,error=? WHERE id=? AND state='READING' AND lease=?",(time.time()+backoff,code,job,lease))
                else:
                    c.execute("UPDATE vpnl_fill_requests SET state='REVIEW',lease=NULL,error=? WHERE id=? AND state='READING' AND lease=?", (code, job, lease))
            from .dispatcher import warning
            warning(code)


def send_allowed(store, c, p, binding):
    return validate_stored(store, c, p, binding) and predecessors(c, p.fill_confirmation, p.event_id) is None


def guard_claimed(store,c):
    """Reauthorize the new type at the existing pre-POST restore gate.

    No legacy row/attempt is touched. The original begin_post/_attempt/ACK CAS
    stays byte-identical; a rejected new claim cannot pass its status check.
    """
    from .models import NotificationPayload
    rows=c.execute("SELECT d.id,v.payload,b.binding FROM vpnl_deliveries d JOIN vpnl_revisions v ON v.root_id=d.root_id AND v.revision=d.revision JOIN vpnl_root_bindings b ON b.root_id=d.root_id WHERE d.status='CLAIMED' AND CASE WHEN json_valid(v.payload) THEN json_extract(v.payload,'$.notification_type') END=? LIMIT 40",(CONTRACT,)).fetchall()
    for did,raw,binding in rows:
        try:
            permitted=send_allowed(store,c,NotificationPayload(**strict_object(raw)),strict_object(binding))
        except (ValueError,KeyError,TypeError):
            permitted=False
        if not permitted:
            c.execute("UPDATE vpnl_deliveries SET status='HELD',error='evidence_invalid' WHERE id=? AND status='CLAIMED'",(did,))


def render(payload):
    from exchange.utility.discord_embed import Embed, sanitize_discord_text, enforce_embed_bounds, format_kst_full
    proof = payload.fill_confirmation
    if not validate(proof):
        raise ValueError('fill_confirmation_invalid')
    s = proof['scope']
    clean = lambda value: sanitize_discord_text(str(value), max_len=128)
    money = lambda value: format(Decimal(value), 'f').rstrip('0').rstrip('.') if '.' in format(Decimal(value), 'f') else format(Decimal(value), 'f')
    from decimal import Decimal
    currency = '원' if s['settlement'] == 'KRW' else ' USDT'
    base = s['symbol'].split('/')[0]
    direction = ('매수' if s['side'] == 'buy' else '매도') if s['market_type'] == 'spot' else ((proof['position']+' ('+('매수' if s['side'] == 'buy' else '매도')+')') if proof['position'] != '확인 중' else ('매수' if s['side'] == 'buy' else '매도')+' · 포지션 구분 확인 중')
    label = ('실제 총 매수액' if s['side'] == 'buy' else '실제 총 매도액') if s['market_type'] == 'spot' else '계약 명목가치'
    fees = '확인 중' if proof['fees'] is None else ', '.join(clean(money(v)+' '+k) for k,v in sorted(proof['fees'].items())) or '확인 중'
    lines = ['전략: '+clean(s['strategy']), '실제 평균 체결가: '+money(proof['average'])+currency,
             '실제 체결수량: '+money(proof['filled'])+' '+base, label+': '+money(proof['total'])+currency+' (수수료 제외)',
             '수수료/리베이트: '+fees, '상태: 완료 주문의 전체 체결 확인', '확인 방식: 거래소 인증 체결 조회',
             '금융 손익 검증 영수증이 아닌 조회 기반 확인', '시각: '+format_kst_full(proof['observed_at'])]
    return enforce_embed_bounds(Embed(title='['+s['exchange']+'] '+clean(s['symbol'])+' '+('현물 ' if s['market_type']=='spot' else '선물 ')+direction+' 체결 확인', description='\n'.join(lines), color=0x2563EB))
