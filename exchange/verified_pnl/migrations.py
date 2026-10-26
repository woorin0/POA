"""Additive notification migration. Original tables and rows are never rewritten."""
import json,time
from .evidence import strict_object,canonical,digest

TABLES=('verified_jobs','verified_outbox','verified_revisions','verified_trades','verified_quarantine')

def install(con):
    statements=[
    'CREATE TABLE IF NOT EXISTS vpnl_schema(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS vpnl_migration_progress(source TEXT PRIMARY KEY,cursor INTEGER NOT NULL,highwater INTEGER NOT NULL,complete INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS vpnl_legacy_map(source TEXT NOT NULL,source_row INTEGER NOT NULL,source_digest TEXT NOT NULL,disposition TEXT NOT NULL,PRIMARY KEY(source,source_row))',
    'CREATE TABLE IF NOT EXISTS vpnl_quarantine(id TEXT PRIMARY KEY,scope TEXT,reason TEXT,source_ref TEXT,resolved INTEGER NOT NULL DEFAULT 0)',
    'CREATE TABLE IF NOT EXISTS vpnl_roots(id TEXT PRIMARY KEY,alias TEXT UNIQUE NOT NULL,topic TEXT UNIQUE NOT NULL,head INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS vpnl_revisions(root_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),payload TEXT NOT NULL,financial_digest TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(root_id,revision),FOREIGN KEY(root_id) REFERENCES vpnl_roots(id))',
    'CREATE TABLE IF NOT EXISTS vpnl_observations(id INTEGER PRIMARY KEY,root_id TEXT NOT NULL,revision INTEGER NOT NULL,receipt TEXT,observed REAL NOT NULL,FOREIGN KEY(root_id,revision) REFERENCES vpnl_revisions(root_id,revision))',
    'CREATE TABLE IF NOT EXISTS vpnl_deliveries(id TEXT PRIMARY KEY,root_id TEXT NOT NULL,revision INTEGER NOT NULL,kind TEXT NOT NULL,parent TEXT,wire BLOB NOT NULL,wire_digest TEXT NOT NULL,status TEXT NOT NULL,due REAL NOT NULL,updated REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL,error TEXT,message_id TEXT,UNIQUE(root_id,revision,kind),FOREIGN KEY(root_id,revision) REFERENCES vpnl_revisions(root_id,revision),FOREIGN KEY(parent) REFERENCES vpnl_deliveries(id))',
    'CREATE TABLE IF NOT EXISTS vpnl_delivery_attempts(id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL,epoch INTEGER NOT NULL,token_hash TEXT NOT NULL,revision INTEGER NOT NULL,wire_digest TEXT NOT NULL,phase TEXT NOT NULL,expires REAL NOT NULL,message_id TEXT,outcome TEXT,UNIQUE(delivery_id,epoch),FOREIGN KEY(delivery_id) REFERENCES vpnl_deliveries(id))',
    "CREATE TABLE IF NOT EXISTS vpnl_jobs(id TEXT PRIMARY KEY,data TEXT NOT NULL,state TEXT NOT NULL,due REAL NOT NULL,created REAL NOT NULL,financial_state TEXT NOT NULL DEFAULT 'UNASSESSED',completed REAL,validation_digest TEXT)",
    'CREATE TABLE IF NOT EXISTS vpnl_recovery_cursor(lane TEXT PRIMARY KEY,cursor TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS vpnl_jobs_ready ON vpnl_jobs(state,due,id)',
    'CREATE INDEX IF NOT EXISTS vpnl_delivery_ready ON vpnl_deliveries(status,due,id)',
    'CREATE INDEX IF NOT EXISTS vpnl_jobs_financial ON vpnl_jobs(financial_state,id)']
    for statement in statements:con.execute(statement)
    con.execute('CREATE TABLE IF NOT EXISTS vpnl_root_bindings(root_id TEXT PRIMARY KEY,binding TEXT NOT NULL,binding_digest TEXT NOT NULL,FOREIGN KEY(root_id) REFERENCES vpnl_roots(id))')
    con.execute("CREATE TRIGGER IF NOT EXISTS vpnl_root_binding_immutable BEFORE UPDATE ON vpnl_root_bindings BEGIN SELECT RAISE(ABORT,'immutable_root_binding'); END")
    con.execute("CREATE TRIGGER IF NOT EXISTS vpnl_root_binding_retain BEFORE DELETE ON vpnl_root_bindings BEGIN SELECT RAISE(ABORT,'retained_root_binding'); END")
    con.execute("CREATE TRIGGER IF NOT EXISTS vpnl_revision_binding_required BEFORE INSERT ON vpnl_revisions WHEN NOT EXISTS(SELECT 1 FROM vpnl_root_bindings WHERE root_id=NEW.root_id) BEGIN SELECT RAISE(ABORT,'root_binding_required'); END")
    if 'job_lease' not in [x[1] for x in con.execute('PRAGMA table_info(vpnl_jobs)')]:
        con.execute('ALTER TABLE vpnl_jobs ADD COLUMN job_lease TEXT')
    con.execute('CREATE TABLE IF NOT EXISTS vpnl_job_tombstones(id TEXT PRIMARY KEY,proof_digest TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS vpnl_order_identities(id TEXT PRIMARY KEY,binding TEXT NOT NULL,job_id TEXT NOT NULL,receipt TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS vpnl_identity_reviews(job_id TEXT PRIMARY KEY,reason TEXT NOT NULL)')
    for verb in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS vpnl_order_identity_{verb.lower()} BEFORE {verb} ON vpnl_order_identities BEGIN SELECT RAISE(ABORT,'immutable_order_identity'); END")

    from .history import install as install_history
    install_history(con)
    install_cutover(con)
    install_submission_lineage(con)
    con.execute('INSERT OR IGNORE INTO vpnl_schema VALUES(1,4)')
    if con.execute('SELECT version FROM vpnl_schema WHERE id=1').fetchone()[0]!=4:raise ValueError('unsupported_notification_schema')
    for table,columns in [('vpnl_revisions','payload,financial_digest,root_id,revision,reason'),('vpnl_deliveries','wire,wire_digest,root_id,revision,kind,parent')]:
        con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable BEFORE UPDATE OF {columns} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_notification'); END")
        con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_retain BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'retained_notification'); END")
    for table in TABLES:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
        high=con.execute(f'SELECT coalesce(max(rowid),0) FROM {table}').fetchone()[0]
        con.execute('INSERT OR IGNORE INTO vpnl_migration_progress VALUES(?,0,?,0)',(table,high))
        # Fail closed for an old binary sharing this database after v4 starts.
        for verb in ('INSERT','UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS vpnl_freeze_{table}_{verb} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'legacy_notification_read_only'); END")

def migrate_batch(con,limit=40):
    """Explicitly approved, archival-only import. No legacy row grants send authority."""
    gate=con.execute('SELECT phase FROM vpnl_cutover WHERE id=1').fetchone()
    if not gate or gate[0] not in ('APPROVED','IMPORTING'):return 0
    con.execute("UPDATE vpnl_cutover SET phase='IMPORTING' WHERE id=1")
    remaining=min(max(int(limit),0),40);count=0
    for table in TABLES:
        progress=con.execute('SELECT cursor,highwater,complete FROM vpnl_migration_progress WHERE source=?',(table,)).fetchone()
        if not progress or progress[2] or not remaining:continue
        cursor,high,_=progress
        q=con.execute(f'SELECT rowid AS __source_rowid__,* FROM {table} WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?',(cursor,high,remaining))
        cols=[x[0] for x in q.description];rows=q.fetchall()
        for row in rows:
            record=dict(zip(cols,row));rid=row[0];ref=f'{table}:{rid}'
            encoded=canonical(_jsonable(record));state=str(record.get('status') or record.get('state') or 'REVIEW')
            reason='legacy_evidence_unproven'
            for key in ('data','payload'):
                if record.get(key) is not None:
                    try:strict_object(record[key])
                    except (ValueError,TypeError):reason='legacy_schema_invalid'
            if table=='verified_quarantine':reason='legacy_quarantine_retained'
            alias=record.get('event_id') or (record.get('id') if table=='verified_outbox' else None)
            con.execute('INSERT INTO vpnl_legacy_archive VALUES(?,?,?,?,?,?,?)',
                (table,rid,digest(_jsonable(record)),encoded,state,'REVIEW',reason))
            if table=='verified_jobs' and record.get('id') is not None:
                con.execute("UPDATE vpnl_jobs SET state='HELD',financial_state='LEGACY_REVIEW' WHERE id=?",(str(record['id']),))
            if alias is not None:
                con.execute('INSERT INTO vpnl_legacy_aliases VALUES(?,?,?)',(str(alias),table,rid))
            con.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(digest([table,rid]),str(record.get('scope') or '') or None,reason,ref))
            con.execute('INSERT OR IGNORE INTO vpnl_legacy_map VALUES(?,?,?,?)',(table,rid,digest(_jsonable(record)),'REVIEW'))
            cursor=rid;remaining-=1;count+=1
        con.execute('UPDATE vpnl_migration_progress SET cursor=?,complete=? WHERE source=?',(cursor,int(cursor>=high or not rows),table))
    if not con.execute('SELECT 1 FROM vpnl_migration_progress WHERE complete=0').fetchone():
        con.execute("UPDATE vpnl_cutover SET phase='VALIDATION' WHERE id=1")
    return count


def _jsonable(value):
    if isinstance(value,bytes):return {'sqlite_blob_hex':value.hex()}
    if isinstance(value,dict):return {k:_jsonable(v) for k,v in value.items()}
    return value

def _manifest(con):
    import hashlib
    report={}
    for table in TABLES:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
        schema=[list(r) for r in con.execute('PRAGMA table_info('+table+')')]
        h=hashlib.sha256();count=0;states={};invalid=[]
        q=con.execute('SELECT rowid AS __source_rowid__,* FROM '+table+' ORDER BY rowid');cols=[d[0] for d in q.description]
        while True:
            batch=q.fetchmany(40)
            if not batch:break
            for row in batch:
                d=dict(zip(cols,row));h.update((canonical(_jsonable(d))+'\n').encode());count+=1
                state=display_state(d.get('status') or d.get('state') or 'NONE');states[state]=states.get(state,0)+1
                for key in ('payload','data'):
                    if d.get(key) is not None:
                        try:strict_object(d[key])
                        except (ValueError,TypeError):
                            if len(invalid)<40:invalid.append(row[0])
        report[table]={'schema':schema,'count':count,'states':states,'digest':h.hexdigest(),'invalid_sample':invalid}
    return report

def preflight(path):
    """Read-only snapshot; no initialization, journal change or credential loading."""
    import sqlite3
    from pathlib import Path
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as con:
        con.execute('BEGIN')
        result={'integrity':con.execute('PRAGMA integrity_check').fetchone()[0],'tables':_manifest(con)}
        result['fingerprint']=digest(result['tables'])
        result['unproven']={table:{'count':info['count'],'sample_source_rows':[r[0] for r in con.execute('SELECT rowid FROM '+table+' ORDER BY rowid LIMIT 40')]} for table,info in result['tables'].items()}
        parts=[]
        for table in TABLES:
            if table not in result['tables']:continue
            columns={r[1] for r in con.execute('PRAGMA table_info('+table+')')}
            column='event_id' if 'event_id' in columns else ('alias' if 'alias' in columns else None)
            if column:parts.append('SELECT '+column+' AS alias FROM '+table+' WHERE '+column+' IS NOT NULL')
        result['multiple_alias_references']=[]
        if parts:
            for alias,n in con.execute('SELECT alias,count(*) FROM ('+' UNION ALL '.join(parts)+') GROUP BY alias HAVING count(*)>1 ORDER BY alias LIMIT 40'):
                result['multiple_alias_references'].append({'alias_digest':digest(str(alias)),'references':n,'review_required':True})
        return result

def install_cutover(con):
    con.execute('CREATE TABLE IF NOT EXISTS vpnl_cutover(id INTEGER PRIMARY KEY CHECK(id=1),phase TEXT NOT NULL,manifest TEXT NOT NULL,backup_path TEXT,backup_digest TEXT,actor_digest TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS vpnl_legacy_archive(source TEXT NOT NULL,source_row INTEGER NOT NULL,source_digest TEXT NOT NULL,record TEXT NOT NULL,legacy_state TEXT NOT NULL,disposition TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(source,source_row))')
    con.execute('CREATE TABLE IF NOT EXISTS vpnl_legacy_aliases(alias TEXT NOT NULL,source TEXT NOT NULL,source_row INTEGER NOT NULL,PRIMARY KEY(alias,source,source_row))')
    con.execute('CREATE TABLE IF NOT EXISTS vpnl_manual_actions(id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL,revision INTEGER NOT NULL,action TEXT NOT NULL,actor_digest TEXT NOT NULL,created REAL NOT NULL)')
    for table in ('vpnl_legacy_archive','vpnl_legacy_aliases','vpnl_manual_actions'):
        for verb in ('UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{verb}_retain BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'retained_cutover_history'); END")
    if not con.execute('SELECT 1 FROM vpnl_cutover').fetchone():
        manifest=_manifest(con);nonempty=any(t['count'] for t in manifest.values())
        # Existing v4 data are preserved, but previously bypassed legacy migrations need explicit review.
        con.execute('INSERT INTO vpnl_cutover(id,phase,manifest) VALUES(1,?,?)',('QUIESCED' if nonempty else 'CUTOVER',canonical(manifest)))
        if nonempty:
            con.execute('UPDATE vpnl_migration_progress SET cursor=0,complete=0')

def require_cutover(con):
    row=con.execute('SELECT phase FROM vpnl_cutover WHERE id=1').fetchone()
    if not row or row[0]!='CUTOVER':raise ValueError('notification_cutover_required')

def verify_backup(store,destination,*,writer_quiesced=False):
    """Caller must stop all old writers/senders first; triggers enforce subsequent writes."""
    import sqlite3,hashlib,os
    from pathlib import Path
    if writer_quiesced is not True:raise ValueError('writer_quiescence_required')
    dest=Path(destination).resolve()
    if dest==Path(store.db_path).resolve():raise ValueError('distinct_backup_required')
    with store._connect() as c:
        phase=c.execute('SELECT phase FROM vpnl_cutover WHERE id=1').fetchone()[0]
        if phase not in ('QUIESCED','BACKUP_VERIFIED'):raise ValueError('cutover_phase')
        expected=c.execute('SELECT manifest FROM vpnl_cutover WHERE id=1').fetchone()[0]
        if canonical(_manifest(c))!=expected:raise ValueError('legacy_snapshot_changed')
    fd=os.open(dest,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd)
    # An interrupted backup is never accepted implicitly; operator uses a new destination.
    with sqlite3.connect(Path(store.db_path).resolve().as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(dest) as dst:src.backup(dst)
    check=preflight(dest)
    if check['integrity']!='ok' or canonical(check['tables'])!=expected:raise ValueError('backup_verification_failed')
    checksum=_file_digest(dest)
    with store._connect() as c:
        c.execute("UPDATE vpnl_cutover SET phase='BACKUP_VERIFIED',backup_path=?,backup_digest=? WHERE id=1 AND phase IN ('QUIESCED','BACKUP_VERIFIED')",(str(dest),checksum))
    return {'integrity':'ok','backup_digest':checksum}

def approve_cutover(store,*,approved=False,actor=None):
    import hashlib
    from pathlib import Path
    if approved is not True or not isinstance(actor,str) or not actor.strip():raise ValueError('explicit_cutover_approval_required')
    with store._connect() as c:
        row=c.execute('SELECT phase,backup_path,backup_digest,manifest FROM vpnl_cutover WHERE id=1').fetchone()
        if row[0]!='BACKUP_VERIFIED':raise ValueError('backup_verification_required')
        if _file_digest(Path(row[1]))!=row[2]:raise ValueError('backup_changed')
        if canonical(_manifest(c))!=row[3]:raise ValueError('legacy_snapshot_changed')
        c.execute("UPDATE vpnl_cutover SET phase='APPROVED',actor_digest=? WHERE id=1",(digest(actor),))

def finish_cutover(store):
    with store._connect() as c:
        phase,manifest=c.execute('SELECT phase,manifest FROM vpnl_cutover WHERE id=1').fetchone()
        if phase=='CUTOVER':return False
        if phase!='VALIDATION':raise ValueError('import_incomplete')
        backup=c.execute('SELECT backup_path,backup_digest FROM vpnl_cutover WHERE id=1').fetchone()
        if not backup[0] or _file_digest(backup[0])!=backup[1]:raise ValueError('backup_changed')
        if canonical(_manifest(c))!=manifest:raise ValueError('legacy_snapshot_changed')
        for table,info in json.loads(manifest).items():
            if c.execute('SELECT count(*) FROM vpnl_legacy_archive WHERE source=?',(table,)).fetchone()[0]!=info['count']:raise ValueError('archive_count_mismatch')
        if c.execute('PRAGMA foreign_key_check').fetchone() or c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('cutover_integrity_failed')
        c.execute("UPDATE vpnl_cutover SET phase='CUTOVER' WHERE id=1")
        return True


def _file_digest(path):
    import hashlib
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda:stream.read(65536),b''):h.update(chunk)
    return h.hexdigest()


def display_state(value):
    allowed={'NONE','PENDING','SENDING','SENT','UNKNOWN','HELD','FAILED','DONE','VERIFYING','REVIEW','QUARANTINED','POSTING','CLAIMED','CANCELED','SUPERSEDED_UNSENT','BLOCKED_PREDECESSOR'}
    return value if isinstance(value,str) and value in allowed else 'UNRECOGNIZED'


def install_submission_lineage(c):
    """Local submission provenance, never financial verification or send approval."""
    import uuid
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_submission_lineage(id INTEGER PRIMARY KEY CHECK(id=1),db_id TEXT NOT NULL,cutover_id TEXT NOT NULL)')
    dbid=uuid.uuid4().hex;manifest=c.execute('SELECT manifest FROM vpnl_cutover WHERE id=1').fetchone()[0]
    c.execute('INSERT OR IGNORE INTO vpnl_submission_lineage VALUES(1,?,?)',(dbid,digest([dbid,manifest])))
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_post_lineage(serial INTEGER PRIMARY KEY,record TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_submission_bindings(job_id TEXT PRIMARY KEY,binding TEXT NOT NULL,binding_digest TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_intake_submissions(serial INTEGER PRIMARY KEY,intake_id TEXT UNIQUE NOT NULL,request_digest TEXT NOT NULL,sidecar_id TEXT NOT NULL,job_id TEXT NOT NULL,binding TEXT NOT NULL,nonce TEXT UNIQUE NOT NULL,db_id TEXT NOT NULL,cutover_id TEXT NOT NULL,status TEXT NOT NULL,digest TEXT NOT NULL)')
    c.execute('CREATE INDEX IF NOT EXISTS vpnl_submission_job ON vpnl_intake_submissions(job_id,serial)')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_submission_pending(intake_id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,sidecar_id TEXT NOT NULL,job_id TEXT NOT NULL)')
    c.execute('CREATE INDEX IF NOT EXISTS vpnl_submission_pending_job ON vpnl_submission_pending(job_id)')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_submission_dirty(intake_id TEXT PRIMARY KEY)')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_submission_job_dirty(job_id TEXT PRIMARY KEY,cursor INTEGER NOT NULL DEFAULT 0)')
    c.execute('CREATE TABLE IF NOT EXISTS vpnl_restore_reviews(id TEXT PRIMARY KEY,intake_id TEXT,reason TEXT NOT NULL,expected_digest TEXT,actual_digest TEXT,created REAL NOT NULL)')
    for table in ('vpnl_post_lineage','vpnl_submission_lineage','vpnl_submission_bindings','vpnl_intake_submissions','vpnl_submission_pending','vpnl_restore_reviews'):
        for verb in ('UPDATE','DELETE'):
            c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{verb} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_submission'); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS vpnl_ack_dirty_insert AFTER INSERT ON vpnl_intake_submissions BEGIN INSERT OR IGNORE INTO vpnl_submission_dirty VALUES(NEW.intake_id); END")
    for verb in ('UPDATE','DELETE'):
        name='vpnl_job_submission_dirty_'+verb
        existing=c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()
        if existing and 'WHEN EXISTS' not in existing[0]:c.execute('DROP TRIGGER '+name)
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {verb} ON vpnl_jobs WHEN EXISTS(SELECT 1 FROM vpnl_intake_submissions WHERE job_id=OLD.id) OR EXISTS(SELECT 1 FROM vpnl_submission_pending WHERE job_id=OLD.id) BEGIN INSERT OR IGNORE INTO vpnl_submission_job_dirty(job_id) VALUES(OLD.id); END")
