"""Untrusted requery intake, independent of the authoritative cutover database.

WAL + synchronous FULL requests durable commit from SQLite/OS. This cannot
promise hardware durability, cross-database atomicity, or capture before record().
No retention/prune API: back up the sidecar including its WAL using SQLite backup.
"""
import json,math,re,sqlite3,time,uuid
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
    info['signal_id']=None
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
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=self.timeout)
        try:
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
        finally:c.close()
    def record(self,data):
        request,review=normalized_request(data);encoded=canonical(request);key=digest(request)
        with self.connect() as c:
            old=c.execute('SELECT request,request_digest FROM notification_intake WHERE id=?',(key,)).fetchone()
            if old:
                if old!=(encoded,digest(request)):raise ValueError('intake_identity_conflict')
            else:c.execute('INSERT INTO notification_intake(id,request,request_digest,status,due,created) VALUES(?,?,?,?,?,?)',(key,encoded,digest(request),'REVIEW' if review else 'STORED_WAIT_CUTOVER',0,time.time()))
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
    def mark(self,key,status,error=None,reference=None):
        if status not in WAITING+('SUBMITTED','REVIEW'):raise ValueError('intake_state')
        with self.connect() as c:
            c.execute("UPDATE notification_intake SET status=?,attempts=attempts+1,error=?,submitted_ref=COALESCE(?,submitted_ref),due=? WHERE id=? AND status NOT IN ('SUBMITTED','REVIEW')",(status,error,reference,time.time()+1 if status=='RETRY' else 0,key))
    @staticmethod
    def parse(encoded,expected):
        if len(encoded)>16384:raise ValueError('intake_schema')
        r=strict_object(encoded)
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
    intake=IntakeStore(settings.VERIFIED_NOTIFICATIONS_DB_PATH);rows=intake.rows(only=only)
    if not rows:return 0
    store=store or ready_store(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
    count=0
    for seq,key,encoded,expected in rows:
        try:
            if key!=expected:raise ValueError('intake_key')
            r=intake.parse(encoded,expected)
        except (ValueError,KeyError,TypeError):intake.mark(key,'REVIEW','INTAKE_SCHEMA');continue
        if store is None:intake.mark(key,'STORED_WAIT_CUTOVER');continue
        info=r['info'];ref=r['source_account_ref'];exchange=info['exchange'];order=r['result']['id']
        try:
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
            store.submit_job(job,old,data,hint)
            # If the process dies here, replay repeats the same durable job submission.
            intake.mark(key,'SUBMITTED',reference=job);count+=1
        except (ValueError,KeyError,TypeError,sqlite3.Error):intake.mark(key,'RETRY','INTAKE_REPLAY_FAILED')
    return count