"""Immutable financial revisions and leased delivery attempts; no HTTP under a DB lock."""
import hashlib,json,sqlite3,time,uuid,secrets
from pathlib import Path
from contextlib import contextmanager
from .evidence import canonical,digest,strict_object,payload_valid,payload_fill_valid,safe_code
from .models import NotificationPayload,VerificationStatus
from .migrations import install,migrate_batch
from loguru import logger

class NotificationOutboxStore:
    _last_done_recovery_by_db={}
    def __init__(self,db_path='notifications.sqlite3',*,migration_mode=False):
        self.db_path=str(Path(db_path).resolve())
        if Path(self.db_path).name in {'store.db','data.db','logs.db','safe_orders.sqlite3'}:raise ValueError('separate_notification_database_required')
        with self._connect() as c:
            active=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vpnl_schema'").fetchone()
            if not active and migration_mode is not True:
                for table in ('verified_jobs','verified_trades','verified_quarantine','verified_revisions','verified_outbox'):
                    exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
                    if exists and c.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():
                        raise ValueError('notification_migration_approval_required')
            c.execute('CREATE TABLE IF NOT EXISTS verified_jobs(id TEXT PRIMARY KEY,data TEXT,state TEXT,due REAL,created REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS verified_trades(id TEXT PRIMARY KEY,scope TEXT,data TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS verified_quarantine(id TEXT PRIMARY KEY,reason TEXT,payload TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS verified_revisions(id TEXT PRIMARY KEY,event_id TEXT,revision INTEGER,state TEXT,payload TEXT,created_at REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS verified_outbox(event_id TEXT PRIMARY KEY,exchange TEXT,symbol TEXT,order_id TEXT,client_order_id TEXT,event_type TEXT,version INTEGER,payload TEXT,status TEXT,attempts INTEGER,max_attempts INTEGER,next_retry_at REAL,last_error TEXT,created_at REAL,updated_at REAL,discord_message_id TEXT)')
            install(c)
    @contextmanager
    def _connect(self,timeout=5):
        c=sqlite3.connect(self.db_path,timeout=timeout)
        try:
            c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=5000');c.execute('BEGIN IMMEDIATE');yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:c.close()
    @contextmanager
    def _connect_read(self,timeout=5):
        c=sqlite3.connect('file:'+Path(self.db_path).as_posix()+'?mode=ro',uri=True,timeout=timeout)
        try:c.execute('PRAGMA foreign_keys=ON');yield c
        finally:c.close()
    @staticmethod
    def _facts(p):
        d=p.dict();excluded={'event_id','event_version','event_timestamp','order_name','strategy_id','signal_id','verification_source','financial_receipt','execution_receipt','outcome_receipt'}
        facts={k:v for k,v in d.items() if k not in excluded}
        from .evidence import outcome_rejected
        financial=None
        if payload_valid(p):
            body=p.financial_receipt['body']
            financial={k:body[k] for k in ('scope','order_id','fill')}
            financial['result']={k:v for k,v in body['result'].items() if k not in {'source','notes'}}
        facts["evidence_semantics"]={"financial":financial,"execution":p.execution_receipt["body"] if payload_fill_valid(p) else None,"outcome":p.outcome_receipt["body"] if outcome_rejected(p) else None}
        return facts
    @staticmethod
    def _wire(p):
        from .outbox import build_verified_embed
        return canonical({'embeds':[build_verified_embed(p).data],'allowed_mentions':{'parse':[]}}).encode()
    def enqueue(self,payload,max_attempts=5,*,job_claim=None):
        p=payload.copy(deep=True);valid=payload_valid(p)
        if not valid:
            p.net_pnl=p.roi_pct=p.cost_basis=None;p.pnl_verification_status=p.roi_verification_status=VerificationStatus.UNVERIFIED;p.financial_receipt=None
        if not payload_fill_valid(p):
            p.fill_verification_status=p.verification_status=VerificationStatus.UNVERIFIED
        facts=self._facts(p);fd=digest(facts);wire=self._wire(p);wd=hashlib.sha256(wire).hexdigest();now=time.time()
        topic=digest([p.event_id,p.financial_receipt['body']['scope'],p.order_id,p.event_type]) if valid else digest(['diagnostic',p.event_id,p.exchange,p.symbol,p.order_id,p.event_type])
        with self._connect() as c:
            if job_claim is not None and not c.execute("SELECT 1 FROM vpnl_jobs WHERE id=? AND job_lease=? AND state='VERIFYING' AND due>?",(*job_claim,time.time())).fetchone():raise ValueError('job_lease_lost')
            root=c.execute('SELECT id,head,topic FROM vpnl_roots WHERE alias=? OR topic=?',(p.event_id,topic)).fetchone()
            if root:
                rid,head,existing_topic=root
                if existing_topic!=topic:return False
                old=c.execute('SELECT payload,financial_digest FROM vpnl_revisions WHERE root_id=? AND revision=?',(rid,head)).fetchone()
                try:old_integrity=digest(self._facts(NotificationPayload(**strict_object(old[0]))))==old[1]
                except (ValueError,TypeError,KeyError):old_integrity=False
                if old[1]==fd and old_integrity:
                    c.execute('INSERT INTO vpnl_observations(root_id,revision,receipt,observed) VALUES(?,?,?,?)',(rid,head,canonical(p.financial_receipt),now));return False
                if p.event_version<=head:return False
                revision=p.event_version
            else:
                rid=uuid.uuid4().hex;revision=max(1,p.event_version);c.execute('INSERT INTO vpnl_roots VALUES(?,?,?,0)',(rid,p.event_id,topic))
            c.execute('INSERT INTO vpnl_revisions VALUES(?,?,?,?,?,?)',(rid,revision,p.json(),fd,'FINANCIAL_CHANGE' if root else 'INITIAL',now))
            c.execute('UPDATE vpnl_roots SET head=? WHERE id=?',(revision,rid))
            previous=c.execute('SELECT id,status FROM vpnl_deliveries WHERE root_id=? ORDER BY revision DESC LIMIT 1',(rid,)).fetchone()
            status='PENDING';parent=previous[0] if previous else None
            if previous:
                if previous[1] in ('PENDING','CLAIMED'):
                    c.execute("UPDATE vpnl_deliveries SET status='SUPERSEDED_UNSENT',updated=? WHERE id=?",(now,parent))
                elif previous[1]=='POSTING':status='BLOCKED_PREDECESSOR'
                elif previous[1] in ('UNKNOWN','HELD','BLOCKED_PREDECESSOR'):status='HELD'
            legacy=c.execute('SELECT status FROM verified_outbox WHERE event_id=?',(p.event_id,)).fetchone()
            if legacy and legacy[0] in ('SENT','SENDING','UNKNOWN','HELD'):status='HELD'
            c.execute('INSERT INTO vpnl_deliveries(id,root_id,revision,kind,parent,wire,wire_digest,status,due,updated,max_attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,rid,revision,'CORRECTION' if previous else 'INITIAL',parent,wire,wd,status,now,now,max_attempts))
        logger.info('verified_alerts: financial_revision_recorded');return True
    def get_pending(self,limit=10):
        with self._connect_read() as c:
            return [{'event_id':x[0],'payload':strict_object(x[1]),'attempts':x[2],'max_attempts':x[3]} for x in c.execute("SELECT d.id,r.payload,d.attempts,d.max_attempts FROM vpnl_deliveries d JOIN vpnl_revisions r ON r.root_id=d.root_id AND r.revision=d.revision WHERE d.status='PENDING' AND d.due<=? ORDER BY d.due,d.id LIMIT ?",(time.time(),min(limit,40)))]
    def claim_next(self):
        # Quarantine at most 40 invalid rows per call without permanently blocking the queue.
        for _ in range(40):
            claim=self._claim_next()
            if claim is not False:return claim
        return None
    def _claim_next(self):
        with self._connect() as c:
            c.execute("UPDATE vpnl_deliveries SET status='FAILED',error='send_failed' WHERE id IN (SELECT id FROM vpnl_deliveries WHERE status='PENDING' AND attempts>=max_attempts ORDER BY due,id LIMIT 40)")
            row=c.execute("SELECT d.id,d.root_id,d.revision,d.wire,d.wire_digest,d.attempts,r.payload,r.financial_digest FROM vpnl_deliveries d JOIN vpnl_revisions r ON r.root_id=d.root_id AND r.revision=d.revision WHERE d.status='PENDING' AND d.due<=? ORDER BY d.due,d.id LIMIT 1",(time.time(),)).fetchone()
            if not row:return None
            did,rid,rev,wire,wd,epoch,encoded,fd=row
            try:
                p=NotificationPayload(**strict_object(encoded))
                if digest(self._facts(p))!=fd or self._wire(p)!=wire or hashlib.sha256(wire).hexdigest()!=wd:raise ValueError('wire_binding')
                if p.net_pnl is not None and not payload_valid(p):raise ValueError('financial_binding')
            except (ValueError,TypeError,KeyError):
                c.execute("UPDATE vpnl_deliveries SET status='QUARANTINED',error='evidence_invalid' WHERE id=?",(did,));return False
            token=secrets.token_hex(32);aid=uuid.uuid4().hex;epoch+=1
            c.execute("UPDATE vpnl_deliveries SET status='CLAIMED',attempts=?,updated=? WHERE id=? AND status='PENDING'",(epoch,time.time(),did))
            c.execute('INSERT INTO vpnl_delivery_attempts VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',(aid,did,epoch,hashlib.sha256(token.encode()).hexdigest(),rev,wd,'CLAIMED',time.time()+30))
            return dict(delivery_id=did,attempt_id=aid,root_id=rid,revision=rev,wire=wire,wire_digest=wd,epoch=epoch,token=token)
    def _attempt(self,c,claim,phase,*,allow_expired=False):
        if hashlib.sha256(claim['wire']).hexdigest()!=claim['wire_digest']:return None
        return c.execute('SELECT a.id FROM vpnl_delivery_attempts a JOIN vpnl_deliveries d ON d.id=a.delivery_id WHERE a.id=? AND a.delivery_id=? AND a.epoch=? AND a.token_hash=? AND a.revision=? AND a.wire_digest=? AND a.phase=? AND d.status=? AND d.root_id=? AND a.expires>?',(claim['attempt_id'],claim['delivery_id'],claim['epoch'],hashlib.sha256(claim['token'].encode()).hexdigest(),claim['revision'],claim['wire_digest'],phase,phase,claim['root_id'],0 if allow_expired else time.time())).fetchone()
    def begin_post(self,claim):
        with self._connect() as c:
            if not self._attempt(c,claim,'CLAIMED'):return False
            row=c.execute('SELECT head FROM vpnl_roots WHERE id=?',(claim['root_id'],)).fetchone()
            expiry=c.execute('SELECT expires FROM vpnl_delivery_attempts WHERE id=?',(claim['attempt_id'],)).fetchone()[0]
            if row[0]!=claim['revision'] or expiry<time.time():return False
            c.execute("UPDATE vpnl_deliveries SET status='POSTING',updated=? WHERE id=?",(time.time(),claim['delivery_id']))
            c.execute("UPDATE vpnl_delivery_attempts SET phase='POSTING' WHERE id=?",(claim['attempt_id'],));return True
    def ack(self,claim,success,message_id=None,error=None,delay=None):
        with self._connect() as c:
            if not self._attempt(c,claim,'POSTING'):
                bound=None
                if hashlib.sha256(claim['wire']).hexdigest()==claim['wire_digest']:
                    bound=c.execute("SELECT a.id FROM vpnl_delivery_attempts a JOIN vpnl_deliveries d ON d.id=a.delivery_id WHERE a.id=? AND a.delivery_id=? AND a.epoch=? AND a.token_hash=? AND a.revision=? AND a.wire_digest=? AND a.phase IN ('POSTING','UNKNOWN') AND d.root_id=?",(claim['attempt_id'],claim['delivery_id'],claim['epoch'],hashlib.sha256(claim['token'].encode()).hexdigest(),claim['revision'],claim['wire_digest'],claim['root_id'])).fetchone()
                if bound:
                    c.execute('CREATE TABLE IF NOT EXISTS vpnl_late_outcomes(attempt_id TEXT PRIMARY KEY,wire_digest TEXT NOT NULL,success INTEGER NOT NULL,received REAL NOT NULL)')
                    c.execute('INSERT OR IGNORE INTO vpnl_late_outcomes VALUES(?,?,?,?)',(claim['attempt_id'],claim['wire_digest'],int(success is True),time.time()))
                    c.execute("UPDATE vpnl_deliveries SET status='UNKNOWN',updated=? WHERE id=? AND status='POSTING' AND attempts=?",(time.time(),claim['delivery_id'],claim['epoch']))
                    c.execute("UPDATE vpnl_delivery_attempts SET phase='UNKNOWN' WHERE id=? AND phase='POSTING'",(claim['attempt_id'],))
                return False
            message_id=str(message_id) if message_id is not None and str(message_id).isdigit() and len(str(message_id))<=32 else None
            state='SENT' if success else ('UNKNOWN' if error=='delivery_unknown' else ('PENDING' if delay is not None else 'FAILED'))
            c.execute('UPDATE vpnl_deliveries SET status=?,message_id=?,error=?,due=?,updated=? WHERE id=?',(state,message_id,safe_code(error) if error else None,time.time()+(delay or 0),time.time(),claim['delivery_id']))
            c.execute('UPDATE vpnl_delivery_attempts SET phase=?,message_id=?,outcome=? WHERE id=?',(state,message_id,safe_code(error) if error else None,claim['attempt_id']))
            c.execute("UPDATE vpnl_deliveries SET status=?,updated=? WHERE parent=? AND status='BLOCKED_PREDECESSOR'",('PENDING' if state=='SENT' else 'HELD',time.time(),claim['delivery_id']))
            return True
    def claim(self,event_id):
        raise ValueError('lease_claim_required')
    def mark_sent(self,event_id,message_id=None):
        raise ValueError('lease_ack_required')
    def hold_unknown(self,event_id):
        raise ValueError('lease_ack_required')
    def mark_failed(self,event_id,error_msg,terminal=False,delay=5):
        # Legacy diagnostics cannot mutate an active leased attempt.
        with self._connect() as c:
            c.execute("UPDATE vpnl_deliveries SET error=? WHERE id=? AND status IN ('PENDING','HELD','UNKNOWN','FAILED')",(safe_code(error_msg),event_id))
    def requeue_unknown(self,event_id=None,*,confirm_duplicate_risk=False,allow_bulk=False):
        if confirm_duplicate_risk is not True:raise ValueError('duplicate_risk_confirmation_required')
        if event_id is None and allow_bulk is not True:raise ValueError('individual_delivery_required')
        with self._connect() as c:
            if event_id is None:
                n=c.execute("UPDATE vpnl_deliveries SET status='PENDING',due=?,updated=?,max_attempts=attempts+5 WHERE status IN ('UNKNOWN','HELD')",(time.time(),time.time())).rowcount
            else:
                n=c.execute("UPDATE vpnl_deliveries SET status='PENDING',due=?,updated=?,max_attempts=attempts+5 WHERE id=? AND status IN ('UNKNOWN','HELD')",(time.time(),time.time(),str(event_id))).rowcount
        logger.warning('verified_alerts: manual_resend_authorized count={}',n);return n
    def _quarantine_job(self,c,key,encoded):
        c.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(digest(['job',key]),None,'schema_invalid',digest(encoded)))
        c.execute("UPDATE vpnl_jobs SET state='QUARANTINED',financial_state='QUARANTINED' WHERE id=?",(key,))
    @staticmethod
    def parse_job(encoded):
        data=strict_object(encoded)
        if len(encoded)>262144:raise ValueError('job_size')
        info=data.get('info');result=data.get('result')
        if not isinstance(info,dict) or not isinstance(result,dict) or not isinstance(data.get('account'),str):raise ValueError('job_schema')
        if info.get('exchange') not in {'BINANCE','BYBIT','OKX','UPBIT'} or not isinstance(info.get('unified_symbol'),str):raise ValueError('job_schema')
        if any(type(info.get(k)) is not bool for k in ('is_buy','is_sell','is_futures','is_close')):raise ValueError('job_schema')
        from .evidence import account_reference
        mapped=account_reference(info['exchange'],data['account'])
        if mapped is not None:data['account']=mapped
        if type(data.get('attempts',0)) is not int or data.get('attempts',0)<0:raise ValueError('job_schema')
        return data
    @staticmethod
    def completion_valid(data,validation_digest):
        from .evidence import verify
        try:
            if digest(data)!=validation_digest:return False
            receipt=data.get('completion_receipt')
            if not isinstance(receipt,dict) or receipt.get('kind') not in ('EXECUTION','FINANCIAL','OUTCOME'):return False
            if not verify(receipt,receipt['kind']):return False
            b=receipt['body'];info=data['info'];scope=b['scope'];oid=str(data['result'].get('id') or data['result'].get('uuid') or '')
            actual=b.get('order_id') if receipt['kind'] in ('FINANCIAL','OUTCOME') else b['fill']['order_id']
            return actual==oid and scope['account']==data['account'] and scope['exchange']==info['exchange'] and scope['symbol']==info['unified_symbol']
        except (KeyError,TypeError,ValueError):return False
    def ready_jobs(self,limit=20):
        limit=min(limit,20);now=time.time()
        with self._connect_read() as c:
            recovery=c.execute("SELECT id,data,created FROM vpnl_jobs WHERE state='PENDING' AND due<=? AND financial_state!='NEW' ORDER BY due,id LIMIT ?",(now,limit//2)).fetchall()
            fresh=c.execute("SELECT id,data,created FROM vpnl_jobs WHERE state='PENDING' AND due<=? AND financial_state='NEW' ORDER BY due,id LIMIT ?",(now,limit-len(recovery))).fetchall()
            left=limit-len(recovery)-len(fresh)
            if left:
                picked=[x[0] for x in recovery+fresh];placeholders=','.join('?' for _ in picked) or "''"
                more=c.execute(f"SELECT id,data,created FROM vpnl_jobs WHERE state='PENDING' AND due<=? AND id NOT IN ({placeholders}) ORDER BY due,id LIMIT ?",[now]+picked+[left]).fetchall()
            else:more=[]
            return recovery+fresh+more
    def recover(self):
        now=time.time()
        with self._connect() as c:
            migrate_batch(c,40)
            expired=c.execute("SELECT a.id,a.delivery_id,a.phase FROM vpnl_delivery_attempts a JOIN vpnl_deliveries d ON d.id=a.delivery_id WHERE a.phase IN ('POSTING','CLAIMED') AND a.expires<? AND d.status=a.phase ORDER BY a.expires,a.id LIMIT 40",(now,)).fetchall()
            for aid,did,phase in expired:
                state='UNKNOWN' if phase=='POSTING' else 'PENDING'
                c.execute('UPDATE vpnl_deliveries SET status=?,error=?,updated=? WHERE id=?',(state,'abandoned_send',now,did))
                c.execute('UPDATE vpnl_delivery_attempts SET phase=?,outcome=? WHERE id=?',('UNKNOWN' if phase=='POSTING' else 'EXPIRED','abandoned_send',aid))
                c.execute("UPDATE vpnl_deliveries SET status='HELD' WHERE parent=? AND status='BLOCKED_PREDECESSOR'",(did,))
            c.execute("UPDATE vpnl_jobs SET state='PENDING' WHERE id IN (SELECT id FROM vpnl_jobs WHERE state='VERIFYING' AND due<? ORDER BY due,id LIMIT 40)",(now,))
            cursor=c.execute("SELECT cursor FROM vpnl_recovery_cursor WHERE lane='integrity'").fetchone()
            after=cursor[0] if cursor else ''
            batch=c.execute("SELECT id,data,validation_digest FROM vpnl_jobs WHERE state='DONE' AND id>? ORDER BY id LIMIT 40",(after,)).fetchall()
            for key,encoded,vd in batch:
                try:data=self.parse_job(encoded)
                except (ValueError,TypeError,KeyError):self._quarantine_job(c,key,encoded);continue
                consistent=self.completion_valid(data,vd)
                from .ledger import identity
                alias=identity(key,'final',[],2)
                finance=c.execute('SELECT v.payload,v.financial_digest FROM vpnl_revisions v JOIN vpnl_roots r ON r.id=v.root_id AND r.head=v.revision WHERE r.alias=?',(alias,)).fetchone()
                if finance:
                    try:
                        published=NotificationPayload(**strict_object(finance[0]))
                        consistent=consistent and digest(self._facts(published))==finance[1] and (payload_valid(published) if published.net_pnl is not None else (payload_fill_valid(published) or __import__(__package__+'.evidence',fromlist=['outcome_rejected']).outcome_rejected(published)))
                    except (ValueError,TypeError,KeyError):consistent=False
                if not consistent:
                    c.execute("UPDATE vpnl_jobs SET state='PENDING',due=0,financial_state='WAIT_EVIDENCE',completed=NULL WHERE id=?",(key,))
            c.execute("INSERT INTO vpnl_recovery_cursor VALUES('integrity',?) ON CONFLICT(lane) DO UPDATE SET cursor=excluded.cursor",(batch[-1][0] if batch else '',))
    def prune_completed(self,max_age_seconds=7*86400):
        cutoff=time.time()-max_age_seconds;count=0
        with self._connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS vpnl_job_tombstones(id TEXT PRIMARY KEY,proof_digest TEXT NOT NULL)')
            batch=c.execute("SELECT id,data,validation_digest FROM vpnl_jobs WHERE state='DONE' AND financial_state='VERIFIED_COMPLETE' AND completed<? ORDER BY completed,id LIMIT 40",(cutoff,)).fetchall()
            for key,encoded,vd in batch:
                try:data=self.parse_job(encoded)
                except (ValueError,TypeError):continue
                if not self.completion_valid(data,vd):continue
                scope=digest(data['completion_receipt']['body']['scope'])
                if c.execute('SELECT 1 FROM vpnl_quarantine WHERE scope=? AND resolved=0 LIMIT 1',(scope,)).fetchone():continue
                alias=__import__(__package__+'.ledger',fromlist=['identity']).identity(key,'final',[],2)
                if c.execute("SELECT 1 FROM vpnl_deliveries d JOIN vpnl_roots r ON r.id=d.root_id WHERE r.alias=? AND d.status NOT IN ('SENT','FAILED','SUPERSEDED_UNSENT') LIMIT 1",(alias,)).fetchone():continue
                c.execute('INSERT OR IGNORE INTO vpnl_job_tombstones VALUES(?,?)',(key,vd));c.execute('DELETE FROM vpnl_jobs WHERE id=?',(key,));count+=1
        return count
