"""Immutable financial revisions and leased delivery attempts; no HTTP under a DB lock."""
import hashlib,json,sqlite3,time,uuid,secrets,threading
from pathlib import Path
from contextlib import contextmanager
from .evidence import canonical,digest,strict_object,payload_valid,payload_fill_valid,safe_code,verify
from .models import NotificationPayload,VerificationStatus
from .migrations import install,migrate_batch,require_cutover
from loguru import logger

_WORKER_HANDLES=threading.local()

def worker_connection(path,role='write'):
    return getattr(_WORKER_HANDLES,'paths',{}).get(str(path),{}).get(role)

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
        reused=worker_connection(self.db_path);c=reused if reused is not None else sqlite3.connect(self.db_path,timeout=timeout)
        if reused is not None and c.in_transaction:raise ValueError('notification_nested_transaction')
        try:
            if reused is None:c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=5000')
            c.execute('BEGIN IMMEDIATE');yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:
            if reused is None:c.close()
    @contextmanager
    def _connect_read(self,timeout=5):
        reused=worker_connection(self.db_path,'read')
        if reused is not None and reused.in_transaction:reused=None
        c=reused if reused is not None else sqlite3.connect('file:'+Path(self.db_path).as_posix()+'?mode=ro',uri=True,timeout=timeout)
        try:
            if reused is None:c.execute('PRAGMA foreign_keys=ON')
            yield c
        finally:
            if reused is None:c.close()
            elif c.in_transaction:c.rollback()
    @contextmanager
    def worker_session(self):
        """Three worker-thread handles; no cached trust or pinned read transaction.

        Writer transactions still BEGIN IMMEDIATE and COMMIT with FULL. The read
        handle is mode=ro. All actual restore/receipt checks still run. Close on
        worker exit; offline restore must stop the worker before replacing files.
        """
        from .intake import IntakeStore
        paths=getattr(_WORKER_HANDLES,'paths',{})
        if self.db_path in paths:raise ValueError('worker_session_active')
        side=IntakeStore(self.db_path);handles=[]
        try:
            for path in (self.db_path,side.path):
                c=sqlite3.connect(path,isolation_level=None);handles.append(c)
                c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=5000')
                c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=FULL')
            reader=sqlite3.connect('file:'+Path(self.db_path).as_posix()+'?mode=ro',uri=True,isolation_level=None);handles.append(reader)
            reader.execute('PRAGMA foreign_keys=ON')
            _WORKER_HANDLES.paths={**paths,self.db_path:{'write':handles[0],'read':reader},side.path:{'write':handles[1]}}
            yield
        finally:
            _WORKER_HANDLES.paths=paths
            for c in reversed(handles):c.close()

    def publication_reference(self,c,payload,job_claim):
        """A durable delivery reference, not proof of Discord receipt."""
        row=c.execute("SELECT data FROM vpnl_jobs WHERE id=? AND job_lease=? AND state='VERIFYING'",job_claim).fetchone()
        if row is None or c.execute('SELECT 1 FROM vpnl_identity_reviews WHERE job_id=?',(job_claim[0],)).fetchone():return None
        p=payload.copy(deep=True)
        if not payload_valid(p):
            p.net_pnl=p.roi_pct=p.cost_basis=None;p.pnl_verification_status=p.roi_verification_status=VerificationStatus.UNVERIFIED;p.financial_receipt=None
        if not payload_fill_valid(p):p.fill_verification_status=p.verification_status=VerificationStatus.UNVERIFIED
        try:
            binding=self._binding(p,job_claim,self.parse_job(row[0]))
            found=c.execute('SELECT r.id,r.head,b.binding,b.binding_digest,v.payload,v.financial_digest,d.id,d.wire,d.wire_digest,d.status FROM vpnl_roots r JOIN vpnl_root_bindings b ON b.root_id=r.id JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head JOIN vpnl_deliveries d ON d.root_id=r.id AND d.revision=r.head WHERE r.alias=?',(p.event_id,)).fetchone()
            if found is None:return None
            rid,rev,encoded,bd,raw,fd,did,wire,wd,state=found
            if canonical(binding)!=encoded or digest(binding)!=bd or state=='QUARANTINED':return None
            stored=NotificationPayload(**strict_object(raw))
            if fd!=digest(self._facts(p)) or fd!=digest(self._facts(stored)) or hashlib.sha256(wire).hexdigest()!=wd or self._wire(stored)!=wire:return None
            if stored.net_pnl is not None and not payload_valid(stored):return None
            return dict(root_id=rid,revision=rev,delivery_id=did,wire_digest=wd,financial_digest=fd,alias=p.event_id)
        except (ValueError,KeyError,TypeError):return None

    @staticmethod
    def _facts(p):
        d=p.dict();excluded={'event_type','event_id','event_version','event_timestamp','order_name','strategy_id','signal_id','verification_source','financial_receipt','accounting_receipt','execution_receipt','outcome_receipt','financial_revocation_receipt'}
        facts={k:v for k,v in d.items() if k not in excluded}
        from .evidence import outcome_rejected
        financial=None
        if payload_valid(p):
            body=p.financial_receipt['body']
            financial={k:body[k] for k in ('scope','order_id','fill')}
            financial['result']={k:v for k,v in body['result'].items() if k not in {'source','notes'}}
        facts["evidence_semantics"]={"financial":financial,"execution":p.execution_receipt["body"] if payload_fill_valid(p) else None,"outcome":p.outcome_receipt["body"] if outcome_rejected(p) else None}
        if p.financial_revocation_receipt is not None:
            item=NotificationOutboxStore._withdrawal_item(p)
            facts['withdrawal']=item
        return facts
    @staticmethod
    def _scope(p):
        if getattr(p,'notification_type',None)=='ESTIMATED':
            value=p.estimated_pnl
            return value.get('scope') if isinstance(value,dict) and value.get('status') in ('ESTIMATED','UNAVAILABLE') else None
        if not payload_fill_valid(p):return None
        scope=p.execution_receipt['body']['scope']
        required={'exchange','account','market_type','instrument','symbol','settlement','contract_size','position_side','position_cycle','book'}
        if not required<=set(scope) or any(scope[k] in (None,'') for k in required):return None
        if payload_valid(p) and canonical(scope)!=canonical(p.financial_receipt['body']['scope']):return None
        return scope
    @staticmethod
    def _withdrawal_item(p):
        """Only an explicit HISTORY-signed invalidation, never missing data or a timeout."""
        receipt=p.financial_revocation_receipt
        if not verify(receipt,'HISTORY') or receipt['body'].get('complete') is not False:return None
        scope=NotificationOutboxStore._scope(p)
        if scope is None or canonical(receipt['body'].get('scope'))!=canonical(scope):return None
        values=receipt['body'].get('withdrawals')
        if not isinstance(values,list):return None
        matches=[v for v in values if isinstance(v,dict) and v.get('order_id')==p.order_id]
        if len(matches)!=1:return None
        item=matches[0]
        if set(item)!={'order_id','financial_receipt_digest','accounting_digest','reason'} or item['reason']!='ACCOUNTING_EVIDENCE_REVOKED':return None
        return item
    @staticmethod
    def _withdrawal_valid(p,old):
        if not payload_valid(old) or payload_valid(p):return False
        item=NotificationOutboxStore._withdrawal_item(p)
        if item is None:return False
        target=p.financial_revocation_receipt['body'].get('consumer_alias')
        if target is not None and target!=p.event_id:return False
        prior=old.financial_receipt
        return (canonical(NotificationOutboxStore._scope(p))==canonical(prior['body']['scope'])
                and p.financial_revocation_receipt['key_id']==prior['key_id']
                and item['financial_receipt_digest']==digest(prior['body'])
                and item['accounting_digest']==prior['body']['accounting_digest'])
    @staticmethod
    def _binding(p,job_claim,job):
        """Renewable leases authorize a stable producer; flags and message kinds do not."""
        scope=NotificationOutboxStore._scope(p)
        producer={'kind':'JOB','id':job_claim[0]} if job_claim is not None else ({'kind':'VERIFIER','id':p.execution_receipt['key_id']} if scope is not None else {'kind':'DIAGNOSTIC'})
        if job is not None:
            info=job['info'];result=job['result']
            order=str(result.get('id') or result.get('uuid') or '')
            if not order or (p.exchange,p.symbol,p.order_id)!=(info['exchange'],info['unified_symbol'],order):raise ValueError('producer_order_binding')
            account=job.get('source_account_ref') if getattr(p,'notification_type',None)=='ESTIMATED' else job['account']
            if scope is not None and scope['account']!=account:raise ValueError('producer_account_binding')
        binding={'version':1,'scope':scope,'exchange':p.exchange,'symbol':p.symbol,'order_id':p.order_id,'producer':producer}
        if job is not None and job.get('notice_intake'):
            from .estimated_pnl import case_key
            binding['notice_case']=case_key(job['notice_snapshot'])
        return binding
    @staticmethod
    def _review_root(c,rid):
        c.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(digest(['root_binding',rid]),None,'root_binding_review',rid))
        c.execute("UPDATE vpnl_deliveries SET status='HELD' WHERE root_id=? AND status IN ('PENDING','CLAIMED')",(rid,))
    @staticmethod
    def _wire(p):
        from .outbox import build_verified_embed
        data=build_verified_embed(p).data
        if p.event_type=='revoked' and NotificationOutboxStore._withdrawal_item(p) is not None:
            data=dict(data)
            prefix='⚠️ 이전 순손익·ROI 검증이 철회되었습니다. 현재 손익은 미확정입니다.\n'
            other=len(data.get('title',''))+len(data.get('footer',{}).get('text',''))+len(data.get('author',{}).get('name',''))+sum(len(f.get('name',''))+len(f.get('value','')) for f in data.get('fields',[]))
            data['description']=(prefix+data.get('description',''))[:max(0,min(4096,6000-other))]
        return canonical({'embeds':[data],'allowed_mentions':{'parse':[]}}).encode()
    def enqueue(self,payload,max_attempts=5,*,job_claim=None):
        p=payload.copy(deep=True);valid=payload_valid(p)
        if getattr(p,'notification_type',None) not in (None,'ESTIMATED'):return False
        if p.notification_type=='ESTIMATED' and job_claim is None:return False
        if not valid:
            p.net_pnl=p.roi_pct=p.cost_basis=None;p.pnl_verification_status=p.roi_verification_status=VerificationStatus.UNVERIFIED;p.financial_receipt=None
        if not payload_fill_valid(p):
            p.fill_verification_status=p.verification_status=VerificationStatus.UNVERIFIED
        if job_claim is not None and not self._prepare_submission(payload,job_claim):return False
        now=time.time()
        with self._connect() as c:
            require_cutover(c)
            if not self._restore_ready(c):return False
            job=None
            if job_claim is not None:
                owned=c.execute("SELECT data FROM vpnl_jobs WHERE id=? AND job_lease=? AND state='VERIFYING' AND due>?",(*job_claim,time.time())).fetchone()
                if not owned:raise ValueError('job_lease_lost')
                job=self.parse_job(owned[0])
            if p.notification_type=='ESTIMATED':
                if not self._estimated_authority(c,p,job_claim[0],job):return False
            elif job_claim is not None and not self._authorize_order_identity(c,p,job_claim,job):return False
            if job is not None and not self._notice_gate(c,p,job):return False
            from .alert_presentation import admit
            p=admit(c,p,dict(job,_presentation_job=job_claim[0]) if job is not None else None)
            try:binding=self._binding(p,job_claim,job)
            except ValueError:return False
            if valid and binding['scope'] is None:return False
            topic=digest(['execution-root-v1',p.event_id,binding])
            root=c.execute('SELECT id,head,topic FROM vpnl_roots WHERE alias=? OR topic=?',(p.event_id,topic)).fetchone()
            if root:
                rid,head,existing_topic=root
                saved=c.execute('SELECT binding,binding_digest FROM vpnl_root_bindings WHERE root_id=?',(rid,)).fetchone()
                try:
                    existing_binding=strict_object(saved[0]) if saved else None
                    intact=existing_binding is not None and digest(existing_binding)==saved[1]
                except (ValueError,TypeError,KeyError):intact=False
                if not intact:
                    self._review_root(c,rid);return False
                if existing_topic!=topic or canonical(binding)!=saved[0]:
                    if existing_binding.get('scope') is None and binding['scope'] is not None:self._review_root(c,rid)
                    return False
                old=c.execute('SELECT payload,financial_digest FROM vpnl_revisions WHERE root_id=? AND revision=?',(rid,head)).fetchone()
                old_payload=NotificationPayload(**strict_object(old[0]))
                withdrawing=self._withdrawal_valid(p,old_payload)
                if payload_valid(old_payload) and not valid and not withdrawing:return False
                if p.event_type=='revoked' and not withdrawing:
                    if old_payload.event_type!='revoked' or self._withdrawal_item(p) is None:return False
                    if canonical(self._withdrawal_item(p))!=canonical(self._withdrawal_item(old_payload)) or p.financial_revocation_receipt['key_id']!=old_payload.financial_revocation_receipt['key_id']:return False
                if p.financial_revocation_receipt is not None and p.event_type!='revoked':return False
                if old_payload.event_type=='revoked' and not valid and p.event_type!='revoked':return False
                if old_payload.event_type=='revoked' and valid:
                    item=self._withdrawal_item(old_payload)
                    if item and (item['financial_receipt_digest']==digest(p.financial_receipt['body']) or item['accounting_digest']==p.financial_receipt['body']['accounting_digest']):return False
                facts=self._facts(p);fd=digest(facts);wire=self._wire(p);wd=hashlib.sha256(wire).hexdigest()
                try:old_integrity=digest(self._facts(NotificationPayload(**strict_object(old[0]))))==old[1]
                except (ValueError,TypeError,KeyError):old_integrity=False
                if old[1]==fd and old_integrity:
                    c.execute('INSERT INTO vpnl_observations(root_id,revision,receipt,observed) VALUES(?,?,?,?)',(rid,head,canonical(p.financial_receipt),now));return False
                if p.event_version<=head:return False
                revision=p.event_version
            else:
                if p.event_type=='revoked' or p.financial_revocation_receipt is not None:return False
                rid=uuid.uuid4().hex;revision=max(1,p.event_version);c.execute('INSERT INTO vpnl_roots VALUES(?,?,?,0)',(rid,p.event_id,topic))
                c.execute('INSERT INTO vpnl_root_bindings VALUES(?,?,?)',(rid,canonical(binding),digest(binding)))
                facts=self._facts(p);fd=digest(facts);wire=self._wire(p);wd=hashlib.sha256(wire).hexdigest()
            c.execute('INSERT INTO vpnl_revisions VALUES(?,?,?,?,?,?)',(rid,revision,p.json(),fd,('ACCOUNTING_EVIDENCE_REVOKED' if p.event_type=='revoked' else 'FINANCIAL_CHANGE') if root else 'INITIAL',now))
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
            c.execute('INSERT INTO vpnl_deliveries(id,root_id,revision,kind,parent,wire,wire_digest,status,due,updated,max_attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,rid,revision,('REVOKE' if p.event_type=='revoked' else 'CORRECTION') if previous else 'INITIAL',parent,wire,wd,status,now,now,max_attempts))
            from .estimated_pnl import case_for_job
            notice_case=case_for_job(job) if job is not None else None
            if notice_case is not None and c.execute('SELECT 1 FROM vpnl_notice_inputs WHERE case_id=? LIMIT 1',(notice_case,)).fetchone():
                did=c.execute('SELECT id FROM vpnl_deliveries WHERE root_id=? AND revision=?',(rid,revision)).fetchone()[0]
                c.execute('INSERT INTO vpnl_notice_members VALUES(?,?,?)',(did,notice_case,'ESTIMATED' if p.notification_type=='ESTIMATED' else 'FINANCIAL'))
            from .history import record_consumer
            record_consumer(self,c,p,rid,revision,binding)
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
            require_cutover(c)
            if not self._restore_ready(c):return None
            c.execute("UPDATE vpnl_deliveries SET status='FAILED',error='send_failed' WHERE id IN (SELECT id FROM vpnl_deliveries WHERE status='PENDING' AND attempts>=max_attempts ORDER BY due,id LIMIT 40)")
            row=c.execute("SELECT d.id,d.root_id,d.revision,d.wire,d.wire_digest,d.attempts,r.payload,r.financial_digest FROM vpnl_deliveries d JOIN vpnl_revisions r ON r.root_id=d.root_id AND r.revision=d.revision WHERE d.status='PENDING' AND d.due<=? AND CASE WHEN json_valid(r.payload)=0 THEN 1 WHEN json_extract(r.payload,'$.financial_receipt.body.generation_id') IS NULL THEN 1 ELSE EXISTS(SELECT 1 FROM vpnl_history_consumer_edges e WHERE e.root_id=d.root_id AND e.revision=d.revision) AND (SELECT complete FROM vpnl_consumer_scan WHERE id=1)=1 AND NOT EXISTS(SELECT 1 FROM vpnl_history_invalidations i WHERE i.generation_id=json_extract(r.payload,'$.financial_receipt.body.generation_id')) AND NOT EXISTS(SELECT 1 FROM vpnl_history_heads h JOIN vpnl_history_generations g ON g.scope=h.scope WHERE g.id=json_extract(r.payload,'$.financial_receipt.body.generation_id') AND h.reconciling=1) AND NOT EXISTS(SELECT 1 FROM vpnl_consumer_reviews q WHERE q.resolved=0 AND (q.scope IS NULL OR q.scope=(SELECT scope FROM vpnl_history_generations WHERE id=json_extract(r.payload,'$.financial_receipt.body.generation_id')))) END ORDER BY d.due,d.id LIMIT 1",(time.time(),)).fetchone()
            if not row:return None
            did,rid,rev,wire,wd,epoch,encoded,fd=row
            if not c.execute('SELECT 1 FROM vpnl_root_bindings WHERE root_id=?',(rid,)).fetchone():
                self._review_root(c,rid);return False
            try:
                p=NotificationPayload(**strict_object(encoded))
                saved=c.execute('SELECT binding,binding_digest FROM vpnl_root_bindings WHERE root_id=?',(rid,)).fetchone()
                bound=strict_object(saved[0])
                if digest(bound)!=saved[1] or canonical(bound.get('scope'))!=canonical(self._scope(p)):raise ValueError('root_binding')
                if (p.exchange,p.symbol,p.order_id)!=(bound['exchange'],bound['symbol'],bound['order_id']):raise ValueError('root_binding')
                if p.event_type=='revoked':
                    prior=c.execute('SELECT payload FROM vpnl_revisions WHERE root_id=? AND revision<? ORDER BY revision DESC LIMIT 1',(rid,rev)).fetchone()
                    if prior is None or not self._withdrawal_valid(p,NotificationPayload(**strict_object(prior[0]))):raise ValueError('withdrawal_binding')
                if digest(self._facts(p))!=fd or self._wire(p)!=wire or hashlib.sha256(wire).hexdigest()!=wd:raise ValueError('wire_binding')
                if p.net_pnl is not None and not payload_valid(p):raise ValueError('financial_binding')
                if p.notification_type=='ESTIMATED':
                    from .upbit_spot_read import current_upbit_notification_host
                    iid=(p.estimated_pnl or {}).get('intake_id')
                    if iid and c.execute('SELECT 1 FROM vpnl_notice_upbit_observations WHERE intake_id=?',(iid,)).fetchone() and current_upbit_notification_host() is None:
                        # Missing host configuration is not corrupt evidence. Keep durable PENDING.
                        c.execute("UPDATE vpnl_deliveries SET due=? WHERE id=?",(time.time()+5,did));return False
                    owner=bound['producer']['id'];job=self.parse_job(c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(owner,)).fetchone()[0])
                    if not self._estimated_authority(c,p,owner,job):raise ValueError('estimated_admission')
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
            require_cutover(c)
            if not self._restore_ready(c):return False
            if not self._attempt(c,claim,'CLAIMED'):return False
            row=c.execute('SELECT head FROM vpnl_roots WHERE id=?',(claim['root_id'],)).fetchone()
            expiry=c.execute('SELECT expires FROM vpnl_delivery_attempts WHERE id=?',(claim['attempt_id'],)).fetchone()[0]
            if row[0]!=claim['revision'] or expiry<time.time():return False
            c.execute("UPDATE vpnl_deliveries SET status='POSTING',updated=? WHERE id=?",(time.time(),claim['delivery_id']))
            c.execute("UPDATE vpnl_delivery_attempts SET phase='POSTING' WHERE id=?",(claim['attempt_id'],))
            from .intake import record_post_lineage
            record_post_lineage(self,c,claim)
            return True
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
    def requeue_unknown(self,event_id=None,*,confirm_duplicate_risk=False,allow_bulk=False,actor=None,expected_revision=None,expected_digest=None):
        """Individual delivery only. Explicit resend may duplicate an already delivered webhook.

        DB state alone cannot establish whether Discord received the message.
        Stop the old sender. You MUST inspect the existing message before approving a resend.
        Exactly-Once delivery cannot be guaranteed: manual resend can duplicate it.
        Approval must be exact bool True for one individual delivery with actor and CAS.
        No bulk approval is accepted; UNKNOWN/HELD never cause an automatic POST.
        """
        if confirm_duplicate_risk is not True:raise ValueError('duplicate_risk_confirmation_required')
        if event_id is None or allow_bulk is not False:raise ValueError('individual_delivery_required')
        with self._connect_read() as c:
            if c.execute('SELECT 1 FROM vpnl_roots WHERE alias=?',(str(event_id),)).fetchone() or c.execute('SELECT 1 FROM vpnl_legacy_aliases WHERE alias=? LIMIT 1',(str(event_id),)).fetchone():raise ValueError('alias_is_not_delivery_uuid')
        return self.manual_delivery('requeue',event_id,approved=confirm_duplicate_risk,actor=actor,expected_revision=expected_revision,expected_digest=expected_digest)
    def manual_delivery(self,action,delivery_id,*,approved=False,actor=None,expected_revision=None,expected_digest=None):
        """CAS admin action; never invokes HTTP. Actor is recorded as a digest only."""
        if approved is not True or not isinstance(actor,str) or not actor.strip():raise ValueError('manual_approval_required')
        if action not in ('hold','requeue') or type(expected_revision) is not int or not isinstance(expected_digest,str):raise ValueError('delivery_cas_required')
        try:did=uuid.UUID(str(delivery_id)).hex
        except (ValueError,TypeError,AttributeError):raise ValueError('delivery_uuid_required') from None
        with self._connect() as c:
            require_cutover(c)
            row=c.execute('SELECT d.status,d.revision,d.wire_digest,d.root_id,r.head,d.parent FROM vpnl_deliveries d JOIN vpnl_roots r ON r.id=d.root_id WHERE d.id=?',(did,)).fetchone()
            if row is None:raise ValueError('delivery_not_found')
            state,rev,wd,root,head,parent=row
            if (rev,wd)!=(expected_revision,expected_digest) or head!=rev:raise ValueError('delivery_cas_conflict')
            if c.execute("SELECT 1 FROM vpnl_delivery_attempts WHERE delivery_id=? AND phase IN ('CLAIMED','POSTING')",(did,)).fetchone():raise ValueError('active_lease')
            allowed=('UNKNOWN','HELD') if action=='requeue' else ('PENDING',)
            if state not in allowed:raise ValueError('delivery_state_conflict')
            if action=='requeue':
                if c.execute("SELECT 1 FROM vpnl_deliveries WHERE root_id=? AND status IN ('CLAIMED','POSTING')",(root,)).fetchone():raise ValueError('active_predecessor')
                if parent and not c.execute('SELECT 1 FROM vpnl_deliveries WHERE id=? AND root_id=?',(parent,root)).fetchone():raise ValueError('predecessor_unresolved')
                # Explicit per-delivery duplicate-risk approval may release a correction
                # after UNKNOWN/HELD. Never mark its uncertain predecessor SENT.
            status='PENDING' if action=='requeue' else 'HELD';now=time.time()
            n=c.execute('UPDATE vpnl_deliveries SET status=?,due=?,updated=?,max_attempts=attempts+5 WHERE id=? AND status=? AND revision=? AND wire_digest=?',(status,now,now,did,state,rev,wd)).rowcount
            if n!=1:raise ValueError('delivery_cas_conflict')
            c.execute('INSERT INTO vpnl_manual_actions VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),did,rev,action,digest(actor),now))
        logger.warning('verified_alerts: manual_delivery_action count={}',n)
        return n
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
            if data.get('publication_pending') or digest(data)!=validation_digest:return False
            receipt=data.get('completion_receipt')
            if not isinstance(receipt,dict) or receipt.get('kind') not in ('EXECUTION','FINANCIAL','OUTCOME'):return False
            if not verify(receipt,receipt['kind']):return False
            b=receipt['body'];info=data['info'];scope=b['scope'];oid=str(data['result'].get('id') or data['result'].get('uuid') or '')
            actual=b.get('order_id') if receipt['kind'] in ('FINANCIAL','OUTCOME') else b['fill']['order_id']
            return actual==oid and scope['account']==data['account'] and scope['exchange']==info['exchange'] and scope['symbol']==info['unified_symbol']
        except (KeyError,TypeError,ValueError):return False
    def completion_publication(self,c,key,data):
        """Resolve an existing delivery by bound identity, never a loose alias."""
        try:
            from .ledger import identity
            ref=data.get('publication_committed')
            if ref is not None and not isinstance(ref,dict):return None
            where='r.id=?' if ref else 'r.alias=?'
            target=ref['root_id'] if ref else identity(key,'final',[],2)
            row=c.execute('SELECT r.id,r.alias,r.head,b.binding,b.binding_digest,v.payload,v.financial_digest FROM vpnl_roots r JOIN vpnl_root_bindings b ON b.root_id=r.id JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head WHERE '+where,(target,)).fetchone()
            if row is None:return None
            rid,alias,head,raw,bd,payload,fd=row;binding=strict_object(raw)
            oid=str(data['result'].get('id') or data['result'].get('uuid') or '')
            receipt=data.get('completion_receipt');scope=receipt['body']['scope'] if isinstance(receipt,dict) else None
            if digest(binding)!=bd or binding.get('producer')!={'kind':'JOB','id':key} or binding.get('order_id')!=oid:return None
            if scope is None:
                stored=NotificationPayload(**strict_object(payload))
                proof=stored.execution_receipt if payload_fill_valid(stored) else stored.outcome_receipt
                if not isinstance(proof,dict) or not verify(proof,proof['kind']):return None
                scope=proof['body']['scope']
            if binding['scope']['account']!=data['account'] or binding['scope']['exchange']!=data['info']['exchange'] or binding['scope']['symbol']!=data['info']['unified_symbol']:return None
            if canonical(self.order_binding(binding['scope'],oid,data['info']))!=canonical(self.order_binding(scope,oid,data['info'])):return None
            if ref:
                old=c.execute('SELECT v.financial_digest,d.wire_digest FROM vpnl_revisions v JOIN vpnl_deliveries d ON d.root_id=v.root_id AND d.revision=v.revision WHERE v.root_id=? AND v.revision=? AND d.id=?',(rid,ref['revision'],ref['delivery_id'])).fetchone()
                if old!=(ref['financial_digest'],ref['wire_digest']) or alias!=ref['alias']:return None
            elif not c.execute('SELECT 1 FROM vpnl_deliveries WHERE root_id=? AND revision=? LIMIT 1',(rid,head)).fetchone():return None
            return payload,fd
        except (ValueError,KeyError,TypeError,AttributeError,sqlite3.ProgrammingError,sqlite3.InterfaceError):return None

    def ready_jobs(self,limit=20):
        limit=min(limit,20);now=time.time()
        with self._connect_read() as c:
            require_cutover(c)
            recovery=c.execute("SELECT id,data,created FROM vpnl_jobs WHERE state='PENDING' AND due<=? AND financial_state!='NEW' ORDER BY due,id LIMIT ?",(now,limit//2)).fetchall()
            fresh=c.execute("SELECT id,data,created FROM vpnl_jobs WHERE state='PENDING' AND due<=? AND financial_state='NEW' ORDER BY due,id LIMIT ?",(now,limit-len(recovery))).fetchall()
            left=limit-len(recovery)-len(fresh)
            if left:
                picked=[x[0] for x in recovery+fresh];placeholders=','.join('?' for _ in picked) or "''"
                more=c.execute(f"SELECT id,data,created FROM vpnl_jobs WHERE state='PENDING' AND due<=? AND id NOT IN ({placeholders}) ORDER BY due,id LIMIT ?",[now]+picked+[left]).fetchall()
            else:more=[]
            return recovery+fresh+more
    def import_legacy_batch(self,limit=40):
        with self._connect() as c:return migrate_batch(c,limit)
    def cutover_ready(self):
        with self._connect_read() as c:
            return c.execute('SELECT phase FROM vpnl_cutover WHERE id=1').fetchone()[0]=='CUTOVER'
    def recover(self):
        now=time.time()
        with self._connect() as c:
            migrate_batch(c,40)
            if c.execute('SELECT phase FROM vpnl_cutover WHERE id=1').fetchone()[0]!='CUTOVER':return
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
                finance=self.completion_publication(c,key,data)
                if finance:
                    try:
                        published=NotificationPayload(**strict_object(finance[0]))
                        consistent=consistent and digest(self._facts(published))==finance[1] and (payload_valid(published) if published.net_pnl is not None else (payload_fill_valid(published) or __import__(__package__+'.evidence',fromlist=['outcome_rejected']).outcome_rejected(published)))
                    except (ValueError,TypeError,KeyError):consistent=False
                if (consistent or data.get('final_alert_sent') or data.get('publication_committed') or data.get('completion_receipt')) and finance is None:
                    # A prior verified completion without delivery history cannot
                    # prove non-delivery. Never replay it as a fresh notification.
                    c.execute("UPDATE vpnl_jobs SET state='HELD',financial_state='PUBLICATION_REVIEW',completed=NULL WHERE id=?",(key,))
                    c.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(digest(['publication',key]),None,'publication_history_missing',digest(key)))
                    c.execute("UPDATE vpnl_deliveries SET status='HELD' WHERE status IN ('PENDING','CLAIMED') AND root_id IN (SELECT root_id FROM vpnl_root_bindings WHERE CASE WHEN json_valid(binding) THEN json_extract(binding,'$.producer.id') END=?)",(key,))
                    continue
                if not consistent:
                    c.execute("UPDATE vpnl_jobs SET state='PENDING',due=0,financial_state='WAIT_EVIDENCE',completed=NULL WHERE id=?",(key,))
            c.execute("INSERT INTO vpnl_recovery_cursor VALUES('integrity',?) ON CONFLICT(lane) DO UPDATE SET cursor=excluded.cursor",(batch[-1][0] if batch else '',))
    def prune_completed(self,max_age_seconds=7*86400):
        cutoff=time.time()-max_age_seconds;count=0
        with self._connect() as c:
            if not self._restore_ready(c):return 0
            if not c.execute('SELECT complete FROM vpnl_consumer_scan WHERE id=1').fetchone()[0]:return 0
            c.execute('CREATE TABLE IF NOT EXISTS vpnl_job_tombstones(id TEXT PRIMARY KEY,proof_digest TEXT NOT NULL)')
            batch=c.execute("SELECT id,data,validation_digest FROM vpnl_jobs WHERE state='DONE' AND financial_state='VERIFIED_COMPLETE' AND id NOT IN (SELECT job_id FROM vpnl_intake_submissions) AND id NOT IN (SELECT job_id FROM vpnl_submission_pending) AND completed<? ORDER BY completed,id LIMIT 40",(cutoff,)).fetchall()
            for key,encoded,vd in batch:
                try:data=self.parse_job(encoded)
                except (ValueError,TypeError):continue
                if not self.completion_valid(data,vd) or self.completion_publication(c,key,data) is None:continue
                alias=__import__(__package__+'.ledger',fromlist=['identity']).identity(key,'final',[],2)
                if not c.execute('SELECT 1 FROM vpnl_deliveries d JOIN vpnl_roots r ON r.id=d.root_id WHERE r.alias=? LIMIT 1',(alias,)).fetchone():continue
                scope=digest(data['completion_receipt']['body']['scope'])
                if c.execute('SELECT 1 FROM vpnl_quarantine WHERE scope=? AND resolved=0 LIMIT 1',(scope,)).fetchone():continue
                alias=__import__(__package__+'.ledger',fromlist=['identity']).identity(key,'final',[],2)
                if c.execute("SELECT 1 FROM vpnl_deliveries d JOIN vpnl_roots r ON r.id=d.root_id WHERE r.alias=? AND d.status NOT IN ('SENT','FAILED','SUPERSEDED_UNSENT') LIMIT 1",(alias,)).fetchone():continue
                c.execute('INSERT OR IGNORE INTO vpnl_job_tombstones VALUES(?,?)',(key,vd));c.execute('DELETE FROM vpnl_jobs WHERE id=?',(key,));count+=1
        return count

    @staticmethod
    def producer_identity(info):
        return str(info.get('strategy_id') or info.get('order_name') or 'default')
    @staticmethod
    def order_binding(scope,order,info):
        keys=('exchange','account','subaccount','market_type','instrument','symbol','settlement','contract_size','position_side','position_cycle','book')
        # Historical EXECUTIONs may predate explicit subaccounts; absence is a
        # distinct scope, never an alias for a configured subaccount.
        stable={k:scope.get(k) for k in keys}
        if not order or any(stable[k] in (None,'') for k in keys if k!='subaccount'):raise ValueError('identity_scope_incomplete')
        return dict(version=1,scope=stable,order_id=str(order),producer=NotificationOutboxStore.producer_identity(info))
    @staticmethod
    def _identity_receipt_matches(binding,receipt):
        try:
            if not verify(receipt,receipt['kind']) or receipt['kind'] not in ('EXECUTION','OUTCOME'):return False
            b=receipt['body'];order=b['fill']['order_id'] if receipt['kind']=='EXECUTION' else b['order_id']
            expected=NotificationOutboxStore.order_binding(b['scope'],order,{'strategy_id':binding['producer']})
            return canonical(expected)==canonical(binding)
        except (ValueError,KeyError,TypeError):return False
    @staticmethod
    def _register_order_identity(c,binding,receipt,job_id):
        if not NotificationOutboxStore._identity_receipt_matches(binding,receipt):raise ValueError('identity_evidence')
        key=digest(binding);encoded=canonical(binding)
        row=c.execute('SELECT binding,job_id,receipt FROM vpnl_order_identities WHERE id=?',(key,)).fetchone()
        if row:
            if row[0]!=encoded or not NotificationOutboxStore._identity_receipt_matches(binding,strict_object(row[2])):raise ValueError('identity_review')
            return row[1]
        c.execute('INSERT INTO vpnl_order_identities VALUES(?,?,?,?)',(key,encoded,job_id,canonical(receipt)))
        return job_id
    def _authorize_order_identity(self,c,p,claim,job):
        from .evidence import outcome_rejected
        receipt=p.execution_receipt if payload_fill_valid(p) else p.outcome_receipt if outcome_rejected(p) else None
        if receipt is None:
            # Transient read failure/zero-fill is not an account contradiction.
            # Keep polling; it cannot authorize a new execution root yet.
            return False
        try:
            scope=receipt['body']['scope'];binding=self.order_binding(scope,p.order_id,job['info'])
            if job['account']!=scope['account']:raise ValueError('identity_account')
            owner=self._register_order_identity(c,binding,receipt,claim[0])
            if owner!=claim[0]:
                # Existing published duplicate consumers retain their producer and
                # immutable roots so every one can receive a legitimate correction.
                prior=c.execute('SELECT binding,binding_digest FROM vpnl_root_bindings b JOIN vpnl_roots r ON r.id=b.root_id WHERE r.alias=?',(p.event_id,)).fetchone()
                if not prior or digest(strict_object(prior[0]))!=prior[1] or strict_object(prior[0]).get('producer')!={'kind':'JOB','id':claim[0]}:raise ValueError('duplicate_order_identity')
            c.execute('DELETE FROM vpnl_identity_reviews WHERE job_id=?',(claim[0],))
            return True
        except (ValueError,KeyError,TypeError):
            c.execute('INSERT INTO vpnl_identity_reviews VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET reason=excluded.reason',(claim[0],'identity_review'))
            return False
    def submit_job(self,key,legacy_key,data,binding_hint=None):
        with self._connect() as c:
            require_cutover(c)
            return self._submit_job_tx(c,key,legacy_key,data,binding_hint)[0]
    def _submit_job_tx(self,c,key,legacy_key,data,binding_hint=None):
        if binding_hint is not None:
            row=c.execute('SELECT binding,job_id,receipt FROM vpnl_order_identities WHERE id=?',(digest(binding_hint),)).fetchone()
            if row and row[0]==canonical(binding_hint) and self._identity_receipt_matches(binding_hint,strict_object(row[2])):
                return False,row[1]
        # Provisional admission keys are not financial authority. They stay
        # stable before/after bootstrap and include the producer, not its PnL.
        scoped_key=digest(['notification-bound-input-v1',binding_hint]) if binding_hint is not None else key
        for candidate in dict.fromkeys((key,legacy_key,scoped_key)):
            row=c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(candidate,)).fetchone()
            if row:
                try:
                    saved=c.execute('SELECT binding,receipt FROM vpnl_order_identities WHERE job_id=?',(candidate,)).fetchone()
                    if saved and binding_hint is not None and (saved[0]!=canonical(binding_hint) or not self._identity_receipt_matches(binding_hint,strict_object(saved[1]))):continue
                    old=self.parse_job(row[0]);new=self.parse_job(canonical(data))
                    fields=('exchange','unified_symbol','is_futures')
                    if old['account']==new['account'] and old['result']==new['result'] and self.producer_identity(old['info'])==self.producer_identity(new['info']) and all(old['info'].get(f)==new['info'].get(f) for f in fields):return False,candidate
                except (ValueError,KeyError,TypeError):pass
        if c.execute('SELECT 1 FROM vpnl_job_tombstones WHERE id IN (?,?,?)',(key,legacy_key,scoped_key)).fetchone():return False,None
        key=scoped_key
        from .evidence import account_reference
        mapped=account_reference(data['info']['exchange'],data.get('source_account_ref',data['account']))
        from .bootstrap import current_bootstrap
        known=binding_hint is not None or (current_bootstrap() is None and mapped is not None and mapped==data['account'])
        now=time.time()
        c.execute("INSERT INTO vpnl_jobs(id,data,state,due,created,financial_state) VALUES(?,?,?,?,?,?)",(key,canonical(data),'PENDING' if known else 'HELD',now,now,'NEW' if known else 'WAIT_IDENTITY'))
        return True,key
    def identity_review(self,key):
        with self._connect_read() as c:return c.execute('SELECT 1 FROM vpnl_identity_reviews WHERE job_id=?',(key,)).fetchone() is not None

    def _restore_ready(self,c):
        from .intake import restore_preflight_tx
        return restore_preflight_tx(self,c)['ready']
    def submit_intake(self,key,legacy_key,data,binding_hint,intake_id,request_digest,sidecar_id):
        from .intake import IntakeStore,submission_for,make_submission,validate_submission
        with self._connect() as c:
            require_cutover(c)
            intake=IntakeStore(self.db_path)
            with intake.connect() as sc:
                row=sc.execute('SELECT request,request_digest,status,submission_ack FROM notification_intake WHERE id=?',(intake_id,)).fetchone()
                if row is None or row[1]!=request_digest or intake_id!=request_digest:raise ValueError('submission_request_missing')
                intake.parse(row[0],request_digest)
                if sc.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone()[0]!=sidecar_id:raise ValueError('submission_sidecar')
                if c.execute('SELECT 1 FROM vpnl_restore_reviews LIMIT 1').fetchone() or sc.execute('SELECT 1 FROM intake_restore_reviews LIMIT 1').fetchone():raise ValueError('restore_review_required')
                if row[2] in ('SUBMITTED','REVIEW') and row[3] is None:raise ValueError('unbound_submission')
                existing=submission_for(c,intake_id)
                if existing:
                    if existing['request_digest']!=request_digest or existing['sidecar_id']!=sidecar_id or (binding_hint is not None and canonical(existing['binding'])!=canonical(binding_hint)) or not validate_submission(self,c,existing):raise ValueError('submission_conflict')
                    return existing
                inserted,job=self._submit_job_tx(c,key,legacy_key,data,binding_hint)
                if job is None:raise ValueError('submission_job_unavailable')
                if binding_hint is None:
                    values=(request_digest,sidecar_id,job);old=c.execute('SELECT request_digest,sidecar_id,job_id FROM vpnl_submission_pending WHERE intake_id=?',(intake_id,)).fetchone()
                    if old is None:c.execute('INSERT INTO vpnl_submission_pending VALUES(?,?,?,?)',(intake_id,*values))
                    elif old!=values:raise ValueError('submission_pending_conflict')
                    return None
                return make_submission(self,c,intake_id,request_digest,sidecar_id,job,binding_hint)
    def _prepare_submission(self,p,claim):
        from .intake import IntakeStore,make_submission,_review
        from .evidence import outcome_rejected
        with self._connect() as c:
            if not c.execute('SELECT 1 FROM vpnl_submission_pending WHERE job_id=? LIMIT 1',(claim[0],)).fetchone():return True
            if not self._restore_ready(c):return False
            row=c.execute("SELECT data FROM vpnl_jobs WHERE id=? AND job_lease=? AND state='VERIFYING' AND due>?",(*claim,time.time())).fetchone()
            if row is None:raise ValueError('job_lease_lost')
            data=self.parse_job(row[0])
            if not self._authorize_order_identity(c,p,claim,data):return False
            receipt=p.execution_receipt if payload_fill_valid(p) else p.outcome_receipt if outcome_rejected(p) else None
            if receipt is None:return False
            binding=self.order_binding(receipt['body']['scope'],p.order_id,data['info'])
            # Pending refs are created by the intake transaction. No old SUBMITTED
            # row is retrospectively given an acknowledgement here.
            pending=c.execute('SELECT intake_id,request_digest,sidecar_id FROM vpnl_submission_pending WHERE job_id=? AND intake_id NOT IN (SELECT intake_id FROM vpnl_intake_submissions) ORDER BY intake_id LIMIT 40',(claim[0],)).fetchall()
            intake=IntakeStore(self.db_path)
            with intake.connect() as sc:
                for iid,rd,sid in pending:
                    raw=sc.execute('SELECT request,request_digest,status FROM notification_intake WHERE id=?',(iid,)).fetchone()
                    if raw is None or raw[1]!=rd or raw[2] in ('SUBMITTED','REVIEW'):
                        _review(c,sc,iid,'RESTORE_PENDING_INPUT_INVALID',rd);return False
                    intake.parse(raw[0],rd)
                    make_submission(self,c,iid,rd,sid,claim[0],binding)
            return not c.execute('SELECT 1 FROM vpnl_submission_pending WHERE job_id=? AND intake_id NOT IN (SELECT intake_id FROM vpnl_intake_submissions) LIMIT 1',(claim[0],)).fetchone()
    def intake_job_pending(self,job):
        with self._connect_read() as c:return c.execute('SELECT 1 FROM vpnl_submission_pending WHERE job_id=? AND intake_id NOT IN (SELECT intake_id FROM vpnl_intake_submissions) LIMIT 1',(job,)).fetchone() is not None


    def submit_observed(self,intake,iid,rd,request,sequence):
        from .estimated_pnl import validate_snapshot,case_key,notification_scope,notice_input
        from .intake import validate_observed_admission,submission_for,make_submission,validate_submission
        snap=validate_snapshot(request['observation']);case=case_key(snap);key=digest(['poa-observed-job-v1',case])
        binding=self.order_binding(notification_scope(snap),snap['order_id'],request['info'])
        with self._connect() as c:
            require_cutover(c)
            with intake.connect() as sc:
                row=sc.execute('SELECT request,request_digest FROM notification_intake WHERE id=?',(iid,)).fetchone()
                if row!=(canonical(request),rd) or iid!=rd:raise ValueError('observed_request')
                intake.parse(row[0],rd);validate_observed_admission(sc,request,iid)
                sid=sc.execute('SELECT sidecar_id FROM intake_lineage WHERE id=1').fetchone()[0]
            oldack=submission_for(c,iid)
            if oldack:
                if not validate_submission(self,c,oldack):raise ValueError('observed_ack')
                return oldack
            # A pre-upgrade producer may already have published this event. Do not
            # remap its root or infer delivery success: preserve the new input for REVIEW.
            from .ledger import identity
            oldkeys={identity(snap['exchange'],snap['source_ref'],'spot',snap['symbol'],snap['order_id']),identity('notification-input-v2',snap['source_ref'],snap['exchange'],'spot',snap['symbol'],snap['order_id'],self.producer_identity(request['info']))}
            from .dispatcher import _host_identity_hint
            hint=_host_identity_hint(request['info'],snap['source_ref'],snap['order_id'])
            if hint is not None:
                prior_identity=c.execute('SELECT job_id FROM vpnl_order_identities WHERE id=? AND binding=?',(digest(hint),canonical(hint))).fetchone()
                if prior_identity:oldkeys.add(prior_identity[0])
                oldkeys.add(digest(['notification-bound-input-v1',hint]))
            for prior_job in oldkeys:
                if c.execute("SELECT 1 FROM vpnl_root_bindings WHERE json_extract(binding,'$.producer.kind')='JOB' AND json_extract(binding,'$.producer.id')=? LIMIT 1",(prior_job,)).fetchone():
                    c.execute('INSERT OR IGNORE INTO vpnl_notice_reviews VALUES(?,?)',(case,'existing_notification_review'))
            prior=c.execute('SELECT snapshot FROM vpnl_notice_inputs WHERE case_id=? ORDER BY sequence DESC LIMIT 1',(case,)).fetchone()
            if prior:
                before=validate_snapshot(strict_object(prior[0]))
                if (before['confirmed'] and snap['confirmed'] or before.get('version') in (2,3) or snap.get('version') in (2,3)) and canonical(before)!=canonical(snap):
                    c.execute('INSERT OR IGNORE INTO vpnl_notice_reviews VALUES(?,?)',(case,'observation_conflict'))
            c.execute('INSERT INTO vpnl_notice_inputs VALUES(?,?,?,?,?,?,?,?)',(iid,case,key,sequence,canonical(snap),digest(snap),canonical(notification_scope(snap)),sid))
            data=dict(info=request['info'],result=request['result'],account=request['source_account_ref'],source_account_ref=request['source_account_ref'],notice_intake=iid,notice_snapshot=snap)
            old=c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(key,)).fetchone()
            if old is None:
                c.execute("INSERT INTO vpnl_jobs(id,data,state,due,created,financial_state) VALUES(?,?,'PENDING',?,?,'OBSERVATION_NEW')",(key,canonical(data),time.time(),time.time()))
            else:
                held=c.execute("SELECT 1 FROM vpnl_notice_members m JOIN vpnl_deliveries d ON d.id=m.delivery_id WHERE m.case_id=? AND d.status IN ('SENT','UNKNOWN','HELD','POSTING','CLAIMED') LIMIT 1",(case,)).fetchone()
                if not held and not c.execute('SELECT 1 FROM vpnl_notice_reviews WHERE case_id=?',(case,)).fetchone():
                    c.execute("UPDATE vpnl_jobs SET data=?,state='PENDING',due=?,financial_state='OBSERVATION_NEW' WHERE id=? AND state!='VERIFYING'",(canonical(data),time.time(),key))
            return make_submission(self,c,iid,rd,sid,key,binding)

    def _estimated_authority(self,c,p,key,job):
        from .estimated_pnl import notice_input,calculate,bound_root_matches
        from .intake import submission_for,validate_submission
        try:
            if p.notification_type!='ESTIMATED' or p.financial_receipt is not None or p.execution_receipt is not None or p.accounting_receipt is not None or p.outcome_receipt is not None or p.financial_revocation_receipt is not None:return False
            if any(getattr(p,n) is not None for n in ('net_pnl','cost_basis','roi_pct')) or p.event_type!='estimated':return False
            result=p.estimated_pnl
            if not isinstance(result,dict):return False
            n=notice_input(c,result['intake_id']);snap=n['snapshot']
            if snap.get('version')==3 and p.signal_id!=snap['signal']['signal_id']:return False
            if snap['side']=='buy':return self._buy_notification_authority(c,p,key,job,n)
            if n['job']!=key or snap['side']!='sell' or (not snap['confirmed'] and snap.get('version') not in (2,3)) or not snap['account_configured']:return False
            if snap.get('version') in (2,3) and (result.get('basis')!='TRADINGVIEW_SIGNAL' or p.fill_status.value!='UNVERIFIED' or p.fill_verification_status.value!='UNVERIFIED'):return False
            if c.execute('SELECT 1 FROM vpnl_notice_reviews WHERE case_id=?',(n['case'],)).fetchone():return False
            if (p.exchange,p.symbol,p.order_id)!=(snap['exchange'],snap['symbol'],snap['order_id']):return False
            if p.side!='매도' or (p.strategy_id or p.order_name or 'default')!=snap['strategy']:return False
            if job.get('notice_intake')!=result['intake_id']:return False
            if canonical(calculate(c,result['intake_id']))!=canonical(result):return False
            ack=submission_for(c,result['intake_id'])
            return ack is not None and validate_submission(self,c,ack)
        except (ValueError,TypeError,KeyError,AttributeError):return False

    def _buy_notification_authority(self,c,p,key,job,n):
        """Separate buy notification admission; never grants execution/financial truth.

        Called only after shared no-receipt/no-PnL checks, inside the existing
        enqueue/claim lease transaction. Sell admission remains unchanged.
        """
        from .estimated_pnl import calculate,canonical
        from .intake import submission_for,validate_submission
        s=n['snapshot'];r=p.estimated_pnl
        if n['job']!=key or not s['account_configured'] or s['side']!='buy':return False
        pending=r.get('basis')=='POA_BUY_ACK'
        if pending:
            from .buy_ack import result
            if p.event_version!=1 or r!=result(c,r['intake_id']):return False
        elif s.get('version') in (2,3):
            if r.get('basis')!='TRADINGVIEW_BUY_SIGNAL' or s['confirmed']:return False
        elif not s['confirmed'] or r.get('basis')!='POA_BUY_OBSERVATION':return False
        if p.side!='매수' or (p.exchange,p.symbol,p.order_id)!=(s['exchange'],s['symbol'],s['order_id']):return False
        if (p.strategy_id or p.order_name or 'default')!=s['strategy'] or job.get('notice_intake')!=r['intake_id']:return False
        if p.fill_status.value!='UNVERIFIED' or p.fill_verification_status.value!='UNVERIFIED':return False
        if any(getattr(p,k).value!='UNVERIFIED' for k in ('verification_status','pnl_verification_status','roi_verification_status')):return False
        if p.filled_qty not in (None,'0'):return False
        if r.get('amount') is not None or r.get('roi') is not None:return False
        if c.execute('SELECT 1 FROM vpnl_notice_reviews WHERE case_id=?',(n['case'],)).fetchone():return False
        if not pending and canonical(calculate(c,r['intake_id']))!=canonical(r):return False
        ack=submission_for(c,r['intake_id'])
        return ack is not None and validate_submission(self,c,ack)

    def _notice_gate(self,c,p,job):
        from .estimated_pnl import case_for_job
        case=case_for_job(job)
        if case is None:return True
        rows=c.execute('SELECT d.status,m.mode FROM vpnl_notice_members m JOIN vpnl_deliveries d ON d.id=m.delivery_id WHERE m.case_id=? LIMIT 40',(case,)).fetchall()
        if p.notification_type=='ESTIMATED':
            if not any(mode=='FINANCIAL' or status in ('SENT','UNKNOWN','HELD','POSTING') for status,mode in rows):return True
            if any(mode=='FINANCIAL' for status,mode in rows):return False
            from .buy_ack import transition
            return transition(c,p,job)
        if payload_valid(p) or payload_fill_valid(p):
            if any(mode=='ESTIMATED' and state in ('SENT','UNKNOWN','HELD','POSTING') for state,mode in rows):return False
            c.execute("UPDATE vpnl_deliveries SET status='SUPERSEDED_UNSENT',updated=? WHERE id IN (SELECT delivery_id FROM vpnl_notice_members WHERE case_id=? AND mode='ESTIMATED') AND status IN ('PENDING','CLAIMED')",(time.time(),case))
        return True

    def notice_decided(self,key):
        with self._connect_read() as c:
            row=c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(key,)).fetchone()
            if not row:return False
            data=self.parse_job(row[0])
            if not data.get('notice_intake'):return False
            from .estimated_pnl import case_key
            return bool(c.execute("SELECT 1 FROM vpnl_notice_members m JOIN vpnl_deliveries d ON d.id=m.delivery_id WHERE m.case_id=? AND d.status IN ('SENT','UNKNOWN','HELD','POSTING') LIMIT 1",(case_key(data['notice_snapshot']),)).fetchone())


    def record_notice_successor(self,key,lease,data,fill,context):
        """Append only a trusted execution-derived notification observation.

        Shares the v4 transaction; the original sidecar request and ACK stay immutable.
        It is not a FINANCIAL receipt and cannot authorize an HTTP attempt.
        """
        from .estimated_pnl import notice_input,successor_snapshot,validate_successor
        from .intake import submission_for,validate_submission
        iid=data['notice_intake']
        with self._connect() as c:
            if not c.execute("SELECT 1 FROM vpnl_jobs WHERE id=? AND job_lease=? AND state='VERIFYING' AND due>?",(key,lease,time.time())).fetchone():raise ValueError('job_lease_lost')
            origin=notice_input(c,iid,effective=False)
            old=c.execute('SELECT record,record_digest FROM vpnl_notice_successors WHERE intake_id=?',(iid,)).fetchone()
            if origin['snapshot']['confirmed'] and old is None:return True
            if context is None:return False
            scope=strict_object(context.scope_json)
            try:snapshot=successor_snapshot(origin,fill,scope)
            except (ValueError,KeyError,TypeError):return False
            ack=submission_for(c,iid)
            if ack is None or not validate_submission(self,c,ack) or origin['job']!=key:raise ValueError('successor_submission')
            facts=digest([scope,snapshot])
            if old:
                prior=strict_object(old[0])
                if digest(prior)!=old[1]:raise ValueError('successor_digest')
                validate_successor(c,origin,prior)
                if prior['facts_digest']!=facts:
                    c.execute('INSERT OR IGNORE INTO vpnl_notice_reviews VALUES(?,?)',(origin['case'],'successor_conflict'));return False
                return True
            rec=dict(version=1,intake_id=iid,origin_digest=origin['digest'],job_id=key,case_id=origin['case'],ack_digest=ack['digest'],db_id=ack['db_id'],cutover_id=ack['cutover_id'],producer=origin['snapshot']['strategy'],trusted_scope=scope,execution=fill.execution_receipt,snapshot=snapshot,facts_digest=facts)
            validate_successor(c,origin,rec)
            c.execute('INSERT INTO vpnl_notice_successors VALUES(?,?,?)',(iid,canonical(rec),digest(rec)))
            return True


    def record_upbit_observation(self,key,lease,data,observation):
        from .upbit_spot_read import current_upbit_notification_host
        from .estimated_pnl import notice_input,validate_upbit_observation
        from .intake import submission_for,validate_submission
        host=current_upbit_notification_host(binding=observation.binding_digest)
        if host is None or not host.owns(observation):return False
        with self._connect() as c:
            owned=c.execute("SELECT data FROM vpnl_jobs WHERE id=? AND job_lease=? AND state='VERIFYING' AND due>?",(key,lease,time.time())).fetchone()
            if owned is None:raise ValueError('job_lease_lost')
            saved=self.parse_job(owned[0]);iid=saved.get('notice_intake')
            if iid!=data.get('notice_intake') or iid!=observation.intake_id:return False
            origin=notice_input(c,iid,effective=False)
            if origin['job']!=key or c.execute('SELECT 1 FROM vpnl_notice_successors WHERE intake_id=?',(iid,)).fetchone():return False
            ack=submission_for(c,iid)
            if ack is None or not validate_submission(self,c,ack):return False
            snapshot=host.snapshot(observation.facts)
            rec=dict(kind='UPBIT_ESTIMATED_READ_V1',intake_id=iid,origin_digest=origin['digest'],job_id=key,case_id=origin['case'],producer=origin['snapshot']['strategy'],ack_digest=ack['digest'],db_id=ack['db_id'],cutover_id=ack['cutover_id'],host_binding=host.binding,scope=host.scope_facts(),source_digest=observation.source_digest,facts=observation.facts,facts_digest=digest(observation.facts),snapshot=snapshot,fee_source='ORDER_LIFETIME_PAID_FEE' if observation.facts['paid_fee'] is not None else 'MISSING')
            validate_upbit_observation(c,origin,rec)
            # Persistent trade ownership survives a new reader process/cache.
            trade_ids=[t['id'] for t in observation.facts['trades']]
            params=(host.reader.scope.account,host.reader.scope.subaccount,observation.facts['id'],*trade_ids)
            conflict=c.execute("SELECT 1 FROM vpnl_notice_upbit_observations r JOIN json_each(r.record,'$.facts.trades') t WHERE json_extract(r.record,'$.scope.account')=? AND json_extract(r.record,'$.scope.subaccount')=? AND json_extract(r.record,'$.facts.id')!=? AND json_extract(t.value,'$.id') IN ("+','.join('?' for _ in trade_ids)+") LIMIT 1",params).fetchone()
            owner_conflict=c.execute("SELECT 1 FROM vpnl_notice_upbit_observations WHERE json_extract(record,'$.scope.account')=? AND json_extract(record,'$.scope.subaccount')=? AND json_extract(record,'$.facts.id')=? AND (json_extract(record,'$.producer')!=? OR json_extract(record,'$.case_id')!=?) LIMIT 1",(host.reader.scope.account,host.reader.scope.subaccount,observation.facts['id'],host.strategy,origin['case'])).fetchone()
            if conflict or owner_conflict:
                c.execute('INSERT OR IGNORE INTO vpnl_notice_reviews VALUES(?,?)',(origin['case'],'upbit_trade_owner_conflict'));return False
            old=c.execute('SELECT record,record_digest FROM vpnl_notice_upbit_observations WHERE intake_id=?',(iid,)).fetchone()
            if old:
                prior=strict_object(old[0])
                if digest(prior)!=old[1]:raise ValueError('upbit_observation_digest')
                validate_upbit_observation(c,origin,prior)
                if (prior['facts_digest'],prior['host_binding'])!=(rec['facts_digest'],rec['host_binding']):
                    c.execute('INSERT OR IGNORE INTO vpnl_notice_reviews VALUES(?,?)',(origin['case'],'upbit_observation_conflict'));return False
                return True
            c.execute('INSERT INTO vpnl_notice_upbit_observations VALUES(?,?,?)',(iid,canonical(rec),digest(rec)));return True
