"""Untrusted requery intake, independent of the authoritative cutover database.

WAL + synchronous FULL requests durable commit from SQLite/OS. This cannot
promise hardware durability, cross-database atomicity, or capture before record().
No retention/prune API: back up the sidecar including its WAL using SQLite backup.
"""
import json,math,re,sqlite3,time,uuid,hashlib
from pathlib import Path
from contextlib import contextmanager
from .evidence import canonical,digest,strict_object

WAITING=('STORED_WAIT_CUTOVER','WAIT_PROVIDER','WAIT_IDENTITY','READY_FOR_REVALIDATION','RETRY')
_SECRET=re.compile(r'(?i)(bearer|api.?key|secret|passphrase|webhook|https?:|canary|authorization|%[0-9a-f]{2})')
_TEXT=re.compile(r'^[^\x00-\x1f\x7f]{1,128}$')

def safe_failure(exc):
    # Do not store/log exception strings, paths, identifiers or SQL parameters.
    n=getattr(exc,'sqlite_errorcode',None)
    if n is None and isinstance(exc,sqlite3.Error) and str(exc).lower() in {'database is locked','database table is locked','database schema is locked'}:n=5
    return {13:'INTAKE_DISK_FULL',5:'INTAKE_LOCKED',6:'INTAKE_LOCKED',8:'INTAKE_READONLY',11:'INTAKE_CORRUPT',26:'INTAKE_CORRUPT',14:'INTAKE_UNAVAILABLE'}.get(n & 255 if isinstance(n,int) else None,'INTAKE_PERSISTENCE_FAILED')

def normalized_request(data):
    info={};review=False
    # Only typed order metadata. Never settings, HTTP, receipts, or order-result raw.
    for k in ('exchange','base','quote','unified_symbol','side','is_close','is_entry','is_buy','is_sell','is_futures','is_spot','is_coinm','contract_size','order_name','exit_type','leverage','strategy_id'):
        value=data['info'].get(k)
        if isinstance(value,str):
            if not value.isprintable() or not _TEXT.fullmatch(value) or _SECRET.search(value):value='ref-'+digest(value);review=True
        elif value is not None and (type(value) not in (int,float,bool) or (isinstance(value,float) and not math.isfinite(value))):value=None;review=True
        info[k]=value
    if data['info'].get('tv_signal') is not None:
        from .tradingview_signal import parse
        signal=parse(data['info']['tv_signal'])
        if signal['strategy_id']!=info.get('strategy_id') or signal['action']!=('buy' if info.get('is_buy') else 'sell') or info.get('is_futures'):raise ValueError('tv_request_binding')
        info['tv_signal']=canonical(signal)
    signal_id=data['info'].get('signal_id')
    if signal_id is not None and (not isinstance(signal_id,str) or not signal_id.isprintable() or not _TEXT.fullmatch(signal_id) or _SECRET.search(signal_id)):raise ValueError('tv_signal_reference')
    info['signal_id']=signal_id
    if data['info'].get('signal_time') is not None:
        value=data['info']['signal_time']
        if type(value) not in (int,float) or not math.isfinite(value) or not 0<value<4102444800:raise ValueError('tv_signal_time')
        info['signal_time']=value
    result={}
    for k in ('id','uuid','clientOrderId'):
        value=data['result'].get(k)
        if value is not None:
            value=str(value)
            if not value.isprintable() or not _TEXT.fullmatch(value) or _SECRET.search(value):review=True;continue
            result[k]=value
    if not result.get('id'):review=True
    ref=data['source_account_ref']
    if not isinstance(ref,str) or not re.fullmatch('[0-9a-f]{64}',ref):raise ValueError('intake_reference')
    if info.get('exchange') not in {'BINANCE','BYBIT','OKX','UPBIT'} or not isinstance(info.get('unified_symbol'),str) or any(type(info.get(k)) is not bool for k in ('is_buy','is_sell','is_close','is_futures')):review=True
    scope_hint=data.get('host_binding_digest')
    if scope_hint is not None and (not isinstance(scope_hint,str) or not re.fullmatch('[0-9a-f]{64}',scope_hint)):raise ValueError('intake_scope_hint')
    request=dict(version=1,info=info,result=result,source_account_ref=ref,event_kind='REQUERY',host_binding_digest=scope_hint)
    if not result.get('id'):request['unresolved_ref']=uuid.uuid4().hex
    return request,review

class IntakeStore:
    def __init__(self,target,*,timeout=5):
        target=Path(target).resolve()
        if target.name in {'store.db','data.db','logs.db','safe_orders.sqlite3'}:raise ValueError('notification_path_required')
        self.path=str(target)+'.intake.sqlite3';self.timeout=timeout
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS notification_intake(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,request TEXT NOT NULL,request_digest TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL,created REAL NOT NULL,error TEXT,submitted_ref TEXT)')
            c.execute('CREATE INDEX IF NOT EXISTS intake_ready ON notification_intake(status,seq)')
            c.execute('CREATE TABLE IF NOT EXISTS intake_checkpoint(id INTEGER PRIMARY KEY CHECK(id=1),cursor INTEGER NOT NULL)')
            c.execute('INSERT OR IGNORE INTO intake_checkpoint VALUES(1,0)')
            install_restore_intake(c)
            c.execute('CREATE TABLE IF NOT EXISTS notification_tv_dedup(id TEXT PRIMARY KEY,facts TEXT NOT NULL,intake_id TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):c.execute("CREATE TRIGGER IF NOT EXISTS tv_dedup_%s BEFORE %s ON notification_tv_dedup BEGIN SELECT RAISE(ABORT,'immutable_tv_event'); END"%(action,action))
            c.execute('CREATE TABLE IF NOT EXISTS notification_observation_admissions(request_id TEXT PRIMARY KEY,observation_digest TEXT NOT NULL,scope_digest TEXT NOT NULL,sidecar_id TEXT NOT NULL,producer TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):c.execute("CREATE TRIGGER IF NOT EXISTS notice_admission_%s BEFORE %s ON notification_observation_admissions BEGIN SELECT RAISE(ABORT,'immutable_producer_admission'); END"%(action,action))
    @contextmanager
    def connect(self):
        from .delivery_store import worker_connection
        reused=worker_connection(self.path);c=reused if reused is not None else sqlite3.connect(self.path,timeout=self.timeout)
        if reused is not None and c.in_transaction:raise ValueError('intake_nested_transaction')
        try:
            if reused is None:
                c.execute('PRAGMA busy_timeout='+str(max(0,int(self.timeout*1000))))
                # Changing journal mode races on a fresh sidecar. Only retry the
                # pre-transaction WAL transition; never replay an uncertain commit.
                deadline=time.monotonic()+self.timeout
                while True:
                    try:
                        if c.execute('PRAGMA journal_mode').fetchone()[0].lower()!='wal':c.execute('PRAGMA journal_mode=WAL')
                        break
                    except sqlite3.OperationalError as exc:
                        if safe_failure(exc)!='INTAKE_LOCKED' or time.monotonic()>=deadline:raise
                        time.sleep(.02)
                c.execute('PRAGMA synchronous=FULL')
            c.execute('BEGIN IMMEDIATE');yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:
            if reused is None:c.close()
    def record(self,data,*,observation=None,producer=None):
        request,review=normalized_request(data)
        if observation is not None:
            from .estimated_pnl import producer_allowed,validate_snapshot
            if not producer_allowed(producer):raise ValueError('unregistered_notification_producer')
            observation=validate_snapshot(observation)
            if observation['source_ref']!=request['source_account_ref'] or observation['exchange']!=request['info']['exchange'] or observation['symbol']!=request['info']['unified_symbol'] or observation['order_id']!=request['result'].get('id') or observation['strategy']!=(request['info'].get('strategy_id') or request['info'].get('order_name') or 'default'):raise ValueError('observation_request_binding')
            request.update(version=2,event_kind='POA_OBSERVATION',observation=observation)
        encoded=canonical(request);key=digest(request)
        with self.connect() as c:
            tv=None;tv_order_conflict=False
            if observation is not None and observation.get('version') in (2,3):
                tv=digest([observation['exchange'],observation['source_ref'],observation['symbol'],observation['strategy'],observation['signal']['signal_id']])
                facts=canonical(observation['signal'])
                prior=c.execute('SELECT facts,intake_id FROM notification_tv_dedup WHERE id=?',(tv,)).fetchone()
                if prior:
                    if prior[0]!=facts:raise ValueError('tv_signal_conflict')
                    saved=c.execute('SELECT request,request_digest FROM notification_intake WHERE id=?',(prior[1],)).fetchone()
                    if saved is None or saved[1]!=prior[1]:raise ValueError('tv_dedup_reference')
                    original=self.parse(*saved)['observation']
                    if any(original[k]!=observation[k] for k in ('exchange','source_ref','symbol','strategy','signal')):raise ValueError('tv_dedup_binding')
                    if original['order_id']==observation['order_id']:return prior[1]
                    # A second real order is not the same durable request. Keep
                    # it for review; do not allocate its cost or auto-send it.
                    review=True;tv_order_conflict=True
                else:c.execute('INSERT INTO notification_tv_dedup VALUES(?,?,?)',(tv,facts,key))
            old=c.execute('SELECT request,request_digest FROM notification_intake WHERE id=?',(key,)).fetchone()
            if old:
                if old!=(encoded,digest(request)):raise ValueError('intake_identity_conflict')
            else:c.execute('INSERT INTO notification_intake(id,request,request_digest,status,due,created) VALUES(?,?,?,?,?,?)',(key,encoded,digest(request),'REVIEW' if review else 'STORED_WAIT_CUTOVER',0,time.time()))
            if tv_order_conflict:c.execute("UPDATE notification_intake SET error='TV_SIGNAL_ORDER_CONFLICT' WHERE id=? AND status='REVIEW'",(key,))
            if observation is not None:
                row=(digest(observation),digest([observation['exchange'],observation['source_ref'],observation['symbol'],observation['strategy']]),c.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone()[0],'POA_MAIN_LOG_V1')
                old=c.execute('SELECT observation_digest,scope_digest,sidecar_id,producer FROM notification_observation_admissions WHERE request_id=?',(key,)).fetchone()
                if old is None:c.execute('INSERT INTO notification_observation_admissions VALUES(?,?,?,?,?)',(key,*row))
                elif old!=row:raise ValueError('producer_admission_conflict')
        return key
    def rows(self,limit=40,only=None):
        with self.connect() as c:
            if only is not None:return c.execute('SELECT seq,id,request,request_digest FROM notification_intake WHERE id=? AND status NOT IN (\'SUBMITTED\',\'REVIEW\')',(only,)).fetchall()
            cursor=c.execute('SELECT cursor FROM intake_checkpoint WHERE id=1').fetchone()[0]
            sql="SELECT seq,id,request,request_digest FROM notification_intake WHERE status IN ('STORED_WAIT_CUTOVER','WAIT_PROVIDER','WAIT_IDENTITY','READY_FOR_REVALIDATION','RETRY') AND due<=? AND seq>? ORDER BY seq LIMIT ?"
            rows=c.execute(sql,(time.time(),cursor,min(40,max(1,limit)))).fetchall()
            if not rows:rows=c.execute(sql,(time.time(),0,min(40,max(1,limit)))).fetchall()
            if rows:c.execute('UPDATE intake_checkpoint SET cursor=? WHERE id=1',(rows[-1][0],))
            return rows
    def mark(self,key,status,error=None,reference=None,ack=None):
        if status not in WAITING+('SUBMITTED','REVIEW'):raise ValueError('intake_state')
        with self.connect() as c:
            if ack is not None:
                row=c.execute('SELECT request_digest FROM notification_intake WHERE id=?',(key,)).fetchone()
                if row is None or ack['intake_id']!=key or ack['request_digest']!=row[0]:raise ValueError('submission_ack_conflict')
            c.execute("UPDATE notification_intake SET status=?,attempts=attempts+1,error=?,submitted_ref=COALESCE(?,submitted_ref),submission_ack=COALESCE(?,submission_ack),due=? WHERE id=? AND status NOT IN ('SUBMITTED','REVIEW')",(status,error,reference,canonical(ack) if ack is not None else None,time.time()+1 if status=='RETRY' else 0,key))
    def sidecar_id(self):
        with self.connect() as c:return c.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone()[0]
    @staticmethod
    def parse(encoded,expected):
        if len(encoded)>16384:raise ValueError('intake_schema')
        r=strict_object(encoded)
        if r.get('version')==2:
            from .estimated_pnl import validate_snapshot
            if digest(r)!=expected or r.get('event_kind')!='POA_OBSERVATION':raise ValueError('intake_schema')
            safe,review=normalized_request(r);obs=validate_snapshot(r.get('observation'))
            safe.update(version=2,event_kind='POA_OBSERVATION',observation=obs)
            if review or canonical(safe)!=canonical(r) or obs['source_ref']!=r['source_account_ref'] or obs['order_id']!=r['result'].get('id') or obs['symbol']!=r['info']['unified_symbol'] or obs['strategy']!=(r['info'].get('strategy_id') or r['info'].get('order_name') or 'default'):raise ValueError('observation_schema')
            return r
        if digest(r)!=expected or r.get('version')!=1 or r.get('event_kind')!='REQUERY' or not isinstance(r.get('info'),dict) or not isinstance(r.get('result'),dict):raise ValueError('intake_schema')
        safe,review=normalized_request(r)
        if review or canonical(safe)!=canonical(r):raise ValueError('intake_schema')
        return r

def ready_store(target):
    """Observe incomplete legacy cutover read-only; never install/approve it."""
    from .outbox import NotificationOutboxStore
    p=Path(target).resolve()
    if p.exists():
        c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=1)
        try:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vpnl_cutover'").fetchone():
                row=c.execute('SELECT phase FROM vpnl_cutover WHERE id=1').fetchone()
                if not row or row[0]!='CUTOVER':return None
            else:
                for table in ('verified_jobs','verified_outbox','verified_trades','verified_quarantine','verified_revisions'):
                    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() and c.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():return None
        finally:c.close()
    store=NotificationOutboxStore(target)
    return store if store.cutover_ready() else None

def replay(settings,*,store=None,only=None):
    """At most 40 requery requests. Submit-before-ACK is intentionally retryable.

    Idempotency belongs to submit_job/canonical identity, not a distributed commit.
    This function never calls REST/order APIs/sender or issues trusted receipts.
    """
    from .dispatcher import _host_identity_hint
    from .bootstrap import current_bootstrap
    from .evidence import account_reference,_PROVIDER
    from .ledger import identity
    intake=IntakeStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
    store=store or ready_store(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
    check=restore_preflight(store) if store is not None else {'blocked':False,'recovered':0}
    if check['blocked']:return 0
    rows=intake.rows(only=only)
    count=check['recovered']
    if not rows:return count
    for seq,key,encoded,expected in rows:
        try:
            if key!=expected:raise ValueError('intake_key')
            r=intake.parse(encoded,expected)
        except (ValueError,KeyError,TypeError):intake.mark(key,'REVIEW','INTAKE_SCHEMA');continue
        if store is None:intake.mark(key,'STORED_WAIT_CUTOVER');continue
        info=r['info'];ref=r['source_account_ref'];exchange=info['exchange'];order=r['result']['id']
        try:
            if r['version']==2:
                with intake.connect() as sc:validate_observed_admission(sc,r,key)
                ack=store.submit_observed(intake,key,expected,r,seq)
                intake.mark(key,'SUBMITTED',reference=ack['job_id'],ack=ack);count+=1
                continue
            boot=current_bootstrap();account=account_reference(exchange,ref)
            if _PROVIDER is None or (boot is not None and not boot._key_ready()):intake.mark(key,'WAIT_PROVIDER');continue
            hint=_host_identity_hint(info,ref,order)
            market='linear' if info.get('is_futures') else 'spot';symbol=info['unified_symbol']
            old=identity(exchange,ref,market,symbol,order)
            job=identity('notification-input-v2',ref,exchange,market,symbol,order,store.producer_identity(info))
            data=dict(info=info,result=r['result'],account=account or ref,source_account_ref=ref)
            if r.get('host_binding_digest') is not None and (hint is None or digest(hint)!=r['host_binding_digest']):
                intake.mark(key,'WAIT_IDENTITY');continue
            if account is None or (boot is not None and hint is None):
                # Existing F02 API can retain an untrusted HELD job after cutover.
                # Keep intake pending: this is not a successful ready submission.
                store.submit_job(job,old,data,None)
                intake.mark(key,'WAIT_IDENTITY');continue
            sidecar_id=intake.sidecar_id()
            ack=store.submit_intake(job,old,data,hint,key,expected,sidecar_id)
            # v4 is committed before the sidecar ACK. Retry retrieves that same ACK.
            if ack is None:intake.mark(key,'WAIT_IDENTITY');continue
            intake.mark(key,'SUBMITTED',reference=ack['job_id'],ack=ack);count+=1
        except (ValueError,KeyError,TypeError,sqlite3.Error):intake.mark(key,'RETRY','INTAKE_REPLAY_FAILED')
    return count

_BOOT_CHECKED=set()
_ACK_COLUMNS='serial,intake_id,request_digest,sidecar_id,job_id,binding,nonce,db_id,cutover_id,status,digest'

def install_restore_intake(c):
    if 'submission_ack' not in [r[1] for r in c.execute('PRAGMA table_info(notification_intake)')]:c.execute('ALTER TABLE notification_intake ADD COLUMN submission_ack TEXT')
    c.execute('CREATE TABLE IF NOT EXISTS intake_lineage(id INTEGER PRIMARY KEY CHECK(id=1),sidecar_id TEXT NOT NULL)')
    c.execute('INSERT OR IGNORE INTO intake_lineage VALUES(1,?)',(uuid.uuid4().hex,))
    c.execute("CREATE TRIGGER IF NOT EXISTS intake_lineage_immutable BEFORE UPDATE ON intake_lineage BEGIN SELECT RAISE(ABORT,'immutable_intake_lineage'); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS intake_lineage_retain BEFORE DELETE ON intake_lineage BEGIN SELECT RAISE(ABORT,'retained_intake_lineage'); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS intake_rows_retain BEFORE DELETE ON notification_intake BEGIN SELECT RAISE(ABORT,'retained_intake'); END")
    c.execute('CREATE TABLE IF NOT EXISTS intake_restore_dirty(intake_id TEXT PRIMARY KEY)')
    c.execute('CREATE TABLE IF NOT EXISTS intake_restore_scan(id INTEGER PRIMARY KEY CHECK(id=1),side_cursor INTEGER NOT NULL,side_high INTEGER NOT NULL,v4_cursor INTEGER NOT NULL,v4_high INTEGER NOT NULL,marker TEXT,state TEXT NOT NULL)')
    c.execute("INSERT OR IGNORE INTO intake_restore_scan(id,side_cursor,side_high,v4_cursor,v4_high,marker,state) VALUES(1,0,0,0,0,NULL,'SCANNING')")
    c.execute('CREATE TABLE IF NOT EXISTS intake_post_lineage(serial INTEGER PRIMARY KEY,record TEXT NOT NULL)')
    for name in ('post_cursor','post_reverse_cursor'):
        if name not in [r[1] for r in c.execute('PRAGMA table_info(intake_restore_scan)')]:c.execute('ALTER TABLE intake_restore_scan ADD COLUMN '+name+' INTEGER NOT NULL DEFAULT 0')
    for verb in ('UPDATE','DELETE'):c.execute(f"CREATE TRIGGER IF NOT EXISTS intake_post_lineage_{verb} BEFORE {verb} ON intake_post_lineage BEGIN SELECT RAISE(ABORT,'retained_post_lineage'); END")
    c.execute('CREATE TABLE IF NOT EXISTS intake_restore_reviews(id TEXT PRIMARY KEY,intake_id TEXT,reason TEXT NOT NULL,expected_digest TEXT,actual_digest TEXT,created REAL NOT NULL)')
    c.execute("CREATE TRIGGER IF NOT EXISTS intake_restore_insert AFTER INSERT ON notification_intake BEGIN INSERT OR IGNORE INTO intake_restore_dirty VALUES(NEW.id); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS intake_restore_update AFTER UPDATE OF id,request,request_digest,submission_ack,status ON notification_intake WHEN NEW.id IS NOT OLD.id OR NEW.request IS NOT OLD.request OR NEW.request_digest IS NOT OLD.request_digest OR NEW.submission_ack IS NOT OLD.submission_ack OR (NEW.status='SUBMITTED' AND OLD.status!='SUBMITTED') BEGIN INSERT OR IGNORE INTO intake_restore_dirty VALUES(OLD.id); INSERT OR IGNORE INTO intake_restore_dirty VALUES(NEW.id); END")
    for verb in ('UPDATE','DELETE'):c.execute(f"CREATE TRIGGER IF NOT EXISTS intake_restore_reviews_{verb} BEFORE {verb} ON intake_restore_reviews BEGIN SELECT RAISE(ABORT,'retained_restore_review'); END")

def submission_for(c,iid):
    row=c.execute('SELECT '+_ACK_COLUMNS+' FROM vpnl_intake_submissions WHERE intake_id=?',(iid,)).fetchone()
    if row is None:return None
    d=dict(zip(_ACK_COLUMNS.split(','),row));d['binding']=strict_object(d['binding']);d['version']=1
    return d

def _job_matches(store,c,job,binding):
    row=c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(job,)).fetchone()
    if row is None:return False
    data=store.parse_job(row[0])
    if data.get('notice_intake'):
        from .estimated_pnl import job_matches
        return job_matches(store,c,job,binding)
    scope=binding['scope'];order=str(data['result'].get('id') or data['result'].get('uuid') or '')
    return data['account']==scope['account'] and data['info']['exchange']==scope['exchange'] and data['info']['unified_symbol']==scope['symbol'] and bool(data['info']['is_futures'])==(scope['market_type']!='spot') and canonical(store.order_binding(scope,order,data['info']))==canonical(binding)

def make_submission(store,c,iid,rd,sid,job,binding):
    if iid!=rd or not _job_matches(store,c,job,binding):raise ValueError('submission_job_binding')
    encoded=canonical(binding);bd=digest(binding);memo=c.execute('SELECT binding,binding_digest FROM vpnl_submission_bindings WHERE job_id=?',(job,)).fetchone()
    if memo is None:c.execute('INSERT INTO vpnl_submission_bindings VALUES(?,?,?)',(job,encoded,bd))
    elif memo!=(encoded,bd):raise ValueError('submission_binding_conflict')
    existing=submission_for(c,iid)
    if existing is not None:
        if existing['request_digest']!=rd or existing['sidecar_id']!=sid or existing['job_id']!=job or canonical(existing['binding'])!=encoded or not validate_submission(store,c,existing):raise ValueError('submission_conflict')
        return existing
    dbid,cutid=c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone()
    if cutid!=digest([dbid,c.execute('SELECT manifest FROM vpnl_cutover WHERE id=1').fetchone()[0]]):raise ValueError('submission_cutover_lineage')
    serial=c.execute('SELECT coalesce(max(serial),0)+1 FROM vpnl_intake_submissions').fetchone()[0]
    ack=dict(version=1,serial=serial,intake_id=iid,request_digest=rd,sidecar_id=sid,job_id=job,binding=binding,nonce=uuid.uuid4().hex,db_id=dbid,cutover_id=cutid,status='JOB_SUBMITTED')
    ack['digest']=digest(ack)
    c.execute('INSERT INTO vpnl_intake_submissions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(serial,iid,rd,sid,job,encoded,ack['nonce'],dbid,cutid,ack['status'],ack['digest']))
    return ack

def validate_submission(store,c,ack):
    try:
        if ack['version']!=1 or ack['status']!='JOB_SUBMITTED' or type(ack['serial']) is not int or ack['serial']<1 or not re.fullmatch('[0-9a-f]{32}',ack['nonce']):return False
        if digest({k:v for k,v in ack.items() if k!='digest'})!=ack['digest']:return False
        if tuple(c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone())!=(ack['db_id'],ack['cutover_id']):return False
        saved=submission_for(c,ack['intake_id'])
        if saved is None or canonical(saved)!=canonical(ack):return False
        memo=c.execute('SELECT binding,binding_digest FROM vpnl_submission_bindings WHERE job_id=?',(ack['job_id'],)).fetchone()
        if memo!=(canonical(ack['binding']),digest(ack['binding'])) or not _job_matches(store,c,ack['job_id'],ack['binding']):return False
        roots=c.execute("SELECT binding,binding_digest FROM vpnl_root_bindings WHERE CASE WHEN json_valid(binding) THEN json_extract(binding,'$.producer.id') END=? LIMIT 41",(ack['job_id'],)).fetchall()
        if len(roots)>40:return False
        for encoded,bd in roots:
            b=strict_object(encoded)
            if digest(b)!=bd or b.get('producer')!={'kind':'JOB','id':ack['job_id']} or b.get('order_id')!=ack['binding']['order_id']:return False
            data=store.parse_job(c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(ack['job_id'],)).fetchone()[0])
            if data.get('notice_intake'):
                from .estimated_pnl import bound_root_matches
                if not bound_root_matches(store,c,b,data):return False
            elif canonical(store.order_binding(b['scope'],b['order_id'],data['info']))!=canonical(ack['binding']):return False
        return True
    except (ValueError,KeyError,TypeError):return False

def _review(c,sc,iid,reason,expected=None,actual=None):
    # Only digests of potentially malformed/untrusted metadata enter warning history.
    ed=digest(expected);ad=digest(actual);key=digest([iid,reason,ed,ad])
    values=(key,iid,reason,ed,ad,time.time())
    for conn,table in ((c,'vpnl_restore_reviews'),(sc,'intake_restore_reviews')):
        if not conn.execute('SELECT 1 FROM '+table+' WHERE id=?',(key,)).fetchone():conn.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?,?)',values)
    if iid is not None:sc.execute("UPDATE notification_intake SET status='REVIEW',error=? WHERE id=?",(reason,iid))

def _check_intake(store,c,sc,iid):
    row=sc.execute('SELECT request,request_digest,status,submission_ack FROM notification_intake WHERE id=?',(iid,)).fetchone()
    try:ack=submission_for(c,iid)
    except (ValueError,KeyError,TypeError):_review(c,sc,iid,'RESTORE_ACK_INVALID');return 0
    if row is None:
        if ack is not None:_review(c,sc,iid,'RESTORE_INTAKE_MISSING',ack['digest'])
        return 0
    raw,rd,status,encoded=row
    try:
        request=IntakeStore.parse(raw,rd)
        if iid!=rd:raise ValueError('intake_identity')
        if request.get('version')==2:validate_observed_admission(sc,request,iid)
    except (ValueError,KeyError,TypeError):
        if status=='SUBMITTED' or encoded is not None or ack is not None:_review(c,sc,iid,'RESTORE_REQUEST_INVALID',rd,digest(raw))
        else:sc.execute("UPDATE notification_intake SET status='REVIEW',error='INTAKE_SCHEMA' WHERE id=?",(iid,))
        return 0
    if status=='REVIEW':
        if ack is not None or encoded is not None:_review(c,sc,iid,'RESTORE_REVIEW_REQUIRED')
        return 0
    if status=='SUBMITTED' and encoded is None:_review(c,sc,iid,'RESTORE_LEGACY_UNBOUND',rd);return 0
    if ack is None:
        pending=c.execute('SELECT request_digest,sidecar_id,job_id FROM vpnl_submission_pending WHERE intake_id=?',(iid,)).fetchone()
        if pending and (pending[0]!=rd or pending[1]!=sc.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone()[0] or not c.execute('SELECT 1 FROM vpnl_jobs WHERE id=?',(pending[2],)).fetchone()):
            _review(c,sc,iid,'RESTORE_PENDING_JOB_MISSING',pending)
        elif encoded is not None or status=='SUBMITTED':_review(c,sc,iid,'RESTORE_ACK_MISSING',digest(encoded))
        return 0
    try:
        sid=sc.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone()[0]
        binding=ack['binding'];scope=binding['scope']
        matches=ack['intake_id']==iid and ack['request_digest']==rd and ack['sidecar_id']==sid and binding['order_id']==request['result']['id'] and binding['producer']==store.producer_identity(request['info']) and scope['exchange']==request['info']['exchange'] and scope['symbol']==request['info']['unified_symbol'] and (request.get('host_binding_digest') is None or request['host_binding_digest']==digest(binding))
        if not matches or not validate_submission(store,c,ack):raise ValueError('submission_relation')
        if encoded is not None and canonical(strict_object(encoded))!=canonical(ack):raise ValueError('sidecar_ack')
    except (ValueError,KeyError,TypeError):_review(c,sc,iid,'RESTORE_SUBMISSION_MISMATCH',digest(encoded),ack.get('digest'));return 0
    if status!='SUBMITTED':
        # Only a committed v4 ACK may complete an interrupted sidecar handoff.
        sc.execute("UPDATE notification_intake SET status='SUBMITTED',submission_ack=?,submitted_ref=?,error=NULL WHERE id=?",(canonical(ack),ack['job_id'],iid));return 1
    return 0

def restore_preflight(store):
    with store._connect() as c:return restore_preflight_tx(store,c)

def restore_preflight_tx(store,c):
    """Lock order is v4 IMMEDIATE -> sidecar IMMEDIATE. Max40 validated rows.

    Gate checkpoints are not send receipts. Every startup rechecks both directions;
    immutable nonce/serial prefixes detect older/forked snapshots with the same UUID.
    Dirty queues protect concurrent producer and normal job identity mutations.
    """
    gate=c.execute('SELECT phase FROM vpnl_cutover WHERE id=1').fetchone()
    if not gate or gate[0]!='CUTOVER':return dict(ready=False,blocked=False,scanned=0,recovered=0,state='WAIT_CUTOVER')
    intake=IntakeStore(store.db_path)
    with intake.connect() as sc:
        row=sc.execute('SELECT side_cursor,side_high,v4_cursor,v4_high,marker,state FROM intake_restore_scan WHERE id=1').fetchone();cursor,high,vcursor,vhigh,encoded,state=row
        old_post_cursors=sc.execute('SELECT post_cursor,post_reverse_cursor FROM intake_restore_scan WHERE id=1').fetchone();pcursor,prcursor=old_post_cursors
        marker=strict_object(encoded) if encoded else None
        dbid,cutid=c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone();last=c.execute('SELECT serial,digest FROM vpnl_intake_submissions ORDER BY serial DESC LIMIT 1').fetchone() or (0,None)
        post=c.execute('SELECT serial,record FROM vpnl_post_lineage ORDER BY serial DESC LIMIT 1').fetchone() or (0,None)
        post_high=sc.execute('SELECT coalesce(max(serial),0) FROM intake_post_lineage').fetchone()[0]
        current=dict(db_id=dbid,cutover_id=cutid,serial=last[0],digest=last[1],post_serial=post[0],post_digest=digest(post[1]) if post[1] else None)
        side_high=sc.execute('SELECT coalesce(max(seq),0) FROM notification_intake').fetchone()[0]
        manifest=c.execute('SELECT manifest FROM vpnl_cutover WHERE id=1').fetchone()[0]
        mismatch=cutid!=digest([dbid,manifest])
        if marker:
            prior=c.execute('SELECT digest FROM vpnl_intake_submissions WHERE serial=?',(marker['serial'],)).fetchone() if marker['serial'] else None
            mismatch=mismatch or (marker['db_id'],marker['cutover_id'])!=(dbid,cutid) or marker['serial']>last[0] or (marker['serial'] and (prior is None or prior[0]!=marker['digest'])) or side_high<high
        if marker and marker.get('post_serial',0):
            previous_post=c.execute('SELECT record FROM vpnl_post_lineage WHERE serial=?',(marker['post_serial'],)).fetchone()
            mismatch=mismatch or marker['post_serial']>post[0] or previous_post is None or digest(previous_post[0])!=marker.get('post_digest')
        if mismatch:
            _review(c,sc,None,'RESTORE_LINEAGE_MISMATCH',marker,current);cursor=vcursor=pcursor=prcursor=0
        if store.db_path not in _BOOT_CHECKED:
            if state=='READY':cursor=vcursor=pcursor=prcursor=0
            _BOOT_CHECKED.add(store.db_path)
        high=max(side_high,high if not mismatch else 0);vhigh=max(last[0],vhigh if not mismatch else 0)
        budget=40;scanned=0;recovered=0
        def check(iid):
            nonlocal budget,scanned,recovered
            recovered+=_check_intake(store,c,sc,iid);budget-=1;scanned+=1
            sc.execute('DELETE FROM intake_restore_dirty WHERE intake_id=?',(iid,));c.execute('DELETE FROM vpnl_submission_dirty WHERE intake_id=?',(iid,))
        batch=sc.execute('SELECT seq,id FROM notification_intake WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?',(cursor,high,budget)).fetchall()
        for seq,iid in batch:check(iid);cursor=seq
        if not batch and cursor<high:cursor=high
        if budget:
            batch=c.execute('SELECT serial,intake_id FROM vpnl_intake_submissions WHERE serial>? AND serial<=? ORDER BY serial LIMIT ?',(vcursor,vhigh,budget)).fetchall()
            for serial,iid in batch:check(iid);vcursor=serial
            if not batch and vcursor<vhigh:vcursor=vhigh
        if budget:
            for (iid,) in sc.execute('SELECT intake_id FROM intake_restore_dirty ORDER BY intake_id LIMIT ?',(budget,)).fetchall():check(iid)
        if budget:
            for (iid,) in c.execute('SELECT intake_id FROM vpnl_submission_dirty ORDER BY intake_id LIMIT ?',(budget,)).fetchall():check(iid)
        while budget:
            dirty=c.execute('SELECT job_id,cursor FROM vpnl_submission_job_dirty ORDER BY job_id LIMIT 1').fetchone()
            if dirty is None:break
            job,after=dirty;batch=c.execute('SELECT serial,intake_id FROM vpnl_intake_submissions WHERE job_id=? AND serial>? ORDER BY serial LIMIT ?',(job,after,budget)).fetchall()
            if not batch:c.execute('DELETE FROM vpnl_submission_job_dirty WHERE job_id=?',(job,));budget-=1;scanned+=1;continue
            for serial,iid in batch:check(iid);after=serial
            c.execute('UPDATE vpnl_submission_job_dirty SET cursor=? WHERE job_id=?',(after,job))
        if budget:
            for serial in [r[0] for r in sc.execute('SELECT serial FROM intake_post_lineage WHERE serial>? ORDER BY serial LIMIT ?',(pcursor,budget)).fetchall()]:
                _check_post_lineage(store,c,sc,serial);pcursor=serial;budget-=1;scanned+=1
        if budget:
            for serial in [r[0] for r in c.execute('SELECT serial FROM vpnl_post_lineage WHERE serial>? ORDER BY serial LIMIT ?',(prcursor,budget)).fetchall()]:
                _check_post_lineage(store,c,sc,serial);prcursor=serial;budget-=1;scanned+=1
        blocked=bool(sc.execute('SELECT 1 FROM intake_restore_reviews LIMIT 1').fetchone() or c.execute('SELECT 1 FROM vpnl_restore_reviews LIMIT 1').fetchone())
        dirty=bool(sc.execute('SELECT 1 FROM intake_restore_dirty LIMIT 1').fetchone() or c.execute('SELECT 1 FROM vpnl_submission_dirty LIMIT 1').fetchone() or c.execute('SELECT 1 FROM vpnl_submission_job_dirty LIMIT 1').fetchone())
        complete=cursor>=high and vcursor>=vhigh and pcursor>=post_high and prcursor>=post[0] and not dirty
        new_state='READY' if complete else 'SCANNING';new=(cursor,high,vcursor,vhigh,canonical(current),new_state)
        if tuple(row)!=new:sc.execute('UPDATE intake_restore_scan SET side_cursor=?,side_high=?,v4_cursor=?,v4_high=?,marker=?,state=? WHERE id=1',new)
        if old_post_cursors!=(pcursor,prcursor):sc.execute('UPDATE intake_restore_scan SET post_cursor=?,post_reverse_cursor=? WHERE id=1',(pcursor,prcursor))
        return dict(ready=complete and not blocked,blocked=blocked,scanned=scanned,recovered=recovered,state='REVIEW' if blocked else new_state,cursor=cursor,v4_cursor=vcursor)

def restore_diagnostics(target,limit=40,after=''):
    """Read-only digest references; local delivery absence never proves non-delivery."""
    if not isinstance(after,str) or (after and not re.fullmatch('[0-9a-f]{64}',after)):raise ValueError('restore_diagnostic_cursor')
    path=Path(target).resolve();side=Path(str(path)+'.intake.sqlite3');output=[]
    known={'RESTORE_ACK_INVALID','RESTORE_INTAKE_MISSING','RESTORE_REQUEST_INVALID','RESTORE_REVIEW_REQUIRED','RESTORE_LEGACY_UNBOUND','RESTORE_PENDING_JOB_MISSING','RESTORE_ACK_MISSING','RESTORE_SUBMISSION_MISMATCH','RESTORE_LINEAGE_MISMATCH','RESTORE_PENDING_INPUT_INVALID','RESTORE_POST_LINEAGE_MISMATCH'}
    safe=lambda value:value if isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) else digest(value) if value is not None else None
    c=sc=None
    try:
        c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);sc=sqlite3.connect(side.as_uri()+'?mode=ro',uri=True)
        for review_id,iid,reason,expected,actual in sc.execute('SELECT id,intake_id,reason,expected_digest,actual_digest FROM intake_restore_reviews WHERE id>? ORDER BY id LIMIT ?',(after,max(1,min(limit,40)))):
            row=sc.execute('SELECT submission_ack FROM notification_intake WHERE id=?',(iid,)).fetchone();ack=None
            try:ack=strict_object(row[0]) if row and row[0] else None
            except (ValueError,TypeError):pass
            present=c.execute('SELECT job_id FROM vpnl_intake_submissions WHERE intake_id=?',(iid,)).fetchone();job=present[0] if present else ack.get('job_id') if isinstance(ack,dict) else None
            post_body=None
            if reason=='RESTORE_POST_LINEAGE_MISMATCH' and isinstance(iid,str) and re.fullmatch('post:[0-9]+',iid):
                witness=sc.execute('SELECT record FROM intake_post_lineage WHERE serial=?',(int(iid[5:]),)).fetchone() or c.execute('SELECT record FROM vpnl_post_lineage WHERE serial=?',(int(iid[5:]),)).fetchone()
                if witness:
                    try:post_body=strict_object(witness[0])['body']
                    except (ValueError,KeyError,TypeError):pass
            exists=bool(job and c.execute('SELECT 1 FROM vpnl_jobs WHERE id=?',(job,)).fetchone())
            deliveries=c.execute("SELECT d.id,d.root_id,d.status FROM vpnl_deliveries d JOIN vpnl_root_bindings b ON b.root_id=d.root_id WHERE CASE WHEN json_valid(b.binding) THEN json_extract(b.binding,'$.producer.id') END=? ORDER BY d.id LIMIT 40",(job,)).fetchall() if job else []
            if isinstance(post_body,dict):
                actual=c.execute('SELECT status FROM vpnl_deliveries WHERE id=?',(post_body.get('delivery_id'),)).fetchone()
                deliveries=[(post_body.get('delivery_id'),post_body.get('root_id'),actual[0] if actual else 'MISSING')]
            output.append(dict(review_ref=safe(review_id),intake_ref=digest(iid),reason=reason if reason in known else 'RESTORE_REVIEW_REQUIRED',expected_digest=safe(expected),actual_digest=safe(actual),submission_present=present is not None,job_ref=digest(job) if job else None,job_present=exists,expected_lineage_ref=digest([ack.get('db_id'),ack.get('cutover_id'),ack.get('nonce')]) if isinstance(ack,dict) else None,local_deliveries=[dict(delivery_ref=digest(d),root_ref=digest(r),state=st if st in ('SENT','UNKNOWN','HELD','PENDING','CLAIMED','POSTING','FAILED','BLOCKED_PREDECESSOR','SUPERSEDED_UNSENT','MISSING') else 'UNKNOWN') for d,r,st in deliveries],delivery_evidence='EXTERNAL_CONFIRMATION_REQUIRED',automatic_replay=False))
        return output
    except Exception:raise ValueError('restore_diagnostics_unavailable') from None
    finally:
        if sc is not None:sc.close()
        if c is not None:c.close()

def snapshot_restore_evidence(target,destination):
    """Explicit read-only source backup. Each DB snapshot is independent, not atomic.

    Preserve originals/restored copies before manual reconciliation. This function
    neither resolves REVIEW nor changes source state or authorizes delivery.
    """
    import hashlib
    folder=Path(destination).resolve();path=Path(target).resolve();manifest=dict(snapshot_ref=uuid.uuid4().hex,paired_snapshot_atomic=False,created=time.time(),databases=[])
    try:
        folder.mkdir(exist_ok=False)
        for name,source in (('v4.sqlite',path),('sidecar.sqlite',Path(str(path)+'.intake.sqlite3'))):
            src=dst=None
            try:
                src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True);dst=sqlite3.connect(folder/name)
                src.backup(dst,pages=40)
                if dst.execute('PRAGMA integrity_check').fetchone()!=('ok',):raise ValueError('snapshot_integrity')
            finally:
                if dst is not None:dst.close()
                if src is not None:src.close()
            checksum=hashlib.sha256()
            with (folder/name).open('rb') as data:
                for chunk in iter(lambda:data.read(65536),b''):checksum.update(chunk)
            manifest['databases'].append(dict(file=name,sha256=checksum.hexdigest()))
        (folder/'manifest.json').write_text(canonical(manifest),encoding='utf8');return manifest
    except Exception:raise ValueError('restore_snapshot_incomplete') from None


def record_post_lineage(store,c,claim):
    """Persist a pre-POST intent in both DBs, never a SENT receipt.

    Sidecar commits first. If the v4 commit fails, its unmatched witness requires
    REVIEW; no HTTP occurs. There is no cross-database atomic commit guarantee.
    """
    dbid,cutid=c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone()
    serial=c.execute('SELECT coalesce(max(serial),0)+1 FROM vpnl_post_lineage').fetchone()[0]
    body=dict(version=1,serial=serial,db_id=dbid,cutover_id=cutid,nonce=uuid.uuid4().hex,attempt_id=claim['attempt_id'],delivery_id=claim['delivery_id'],root_id=claim['root_id'],revision=claim['revision'],wire_digest=claim['wire_digest'],epoch=claim['epoch'],token_hash=hashlib.sha256(claim['token'].encode()).hexdigest())
    record=dict(body=body,digest=digest(body));encoded=canonical(record)
    c.execute('INSERT INTO vpnl_post_lineage VALUES(?,?)',(serial,encoded))
    intake=IntakeStore(store.db_path)
    with intake.connect() as sc:
        row=sc.execute('SELECT record FROM intake_post_lineage WHERE serial=?',(serial,)).fetchone()
        if row is None:sc.execute('INSERT INTO intake_post_lineage VALUES(?,?)',(serial,encoded))
        elif row[0]!=encoded:raise ValueError('restore_post_lineage_conflict')
        # Advance only the exact immutable pair just appended by the validated
        # begin_post transaction. Startup still revalidates the entire prefix.
        # Sidecar commit before a failed v4 commit produces a detectable mismatch.
        state,marker_raw,left,right=sc.execute('SELECT state,marker,post_cursor,post_reverse_cursor FROM intake_restore_scan WHERE id=1').fetchone()
        marker=strict_object(marker_raw) if marker_raw else None
        prior=c.execute('SELECT record FROM vpnl_post_lineage WHERE serial=?',(serial-1,)).fetchone()
        prefix_digest=digest(prior[0]) if prior else None
        if (state=='READY' and marker is not None and (left,right)==(serial-1,serial-1)
                and (marker.get('db_id'),marker.get('cutover_id'))==(dbid,cutid)
                and marker.get('post_serial',0)==serial-1 and marker.get('post_digest')==prefix_digest):
            marker['post_serial']=serial;marker['post_digest']=digest(encoded)
            sc.execute('UPDATE intake_restore_scan SET post_cursor=?,post_reverse_cursor=?,marker=? WHERE id=1',(serial,serial,canonical(marker)))



def _check_post_lineage(store,c,sc,serial):
    left=c.execute('SELECT record FROM vpnl_post_lineage WHERE serial=?',(serial,)).fetchone()
    right=sc.execute('SELECT record FROM intake_post_lineage WHERE serial=?',(serial,)).fetchone()
    try:
        if left is None or right is None or left!=right:raise ValueError('post_lineage_pair')
        record=strict_object(left[0]);body=record['body']
        if record['digest']!=digest(body) or body['version']!=1 or body['serial']!=serial:raise ValueError('post_lineage_digest')
        if (body['db_id'],body['cutover_id'])!=c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone():raise ValueError('post_lineage_scope')
        row=c.execute('SELECT a.delivery_id,a.revision,a.wire_digest,a.epoch,a.token_hash,d.root_id,d.wire_digest,d.wire FROM vpnl_delivery_attempts a JOIN vpnl_deliveries d ON d.id=a.delivery_id WHERE a.id=?',(body['attempt_id'],)).fetchone()
        expected=(body['delivery_id'],body['revision'],body['wire_digest'],body['epoch'],body['token_hash'],body['root_id'],body['wire_digest'])
        if row is None or row[:7]!=expected or hashlib.sha256(row[7]).hexdigest()!=body['wire_digest']:raise ValueError('post_lineage_delivery')
    except (ValueError,KeyError,TypeError):
        _review(c,sc,'post:'+str(serial),'RESTORE_POST_LINEAGE_MISMATCH',right[0] if right else None,left[0] if left else None)


def validate_observed_admission(sc,request,key):
    obs=request['observation']
    expected=(digest(obs),digest([obs['exchange'],obs['source_ref'],obs['symbol'],obs['strategy']]),sc.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone()[0],'POA_MAIN_LOG_V1')
    if sc.execute('SELECT observation_digest,scope_digest,sidecar_id,producer FROM notification_observation_admissions WHERE request_id=?',(key,)).fetchone()!=expected:raise ValueError('producer_admission_missing')
    return True
