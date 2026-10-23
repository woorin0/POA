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
    remaining=min(max(int(limit),0),40);count=0
    for table in TABLES:
        progress=con.execute('SELECT cursor,highwater,complete FROM vpnl_migration_progress WHERE source=?',(table,)).fetchone()
        if not progress or progress[2] or not remaining:continue
        cursor,high,_=progress
        q=con.execute(f'SELECT rowid,* FROM {table} WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?',(cursor,high,remaining))
        cols=[x[0] for x in q.description];rows=q.fetchall()
        for row in rows:
            record=dict(zip(cols,row));rid=row[0];ref=f'{table}:{rid}';disposition='LEGACY_UNPROVEN'
            if table=='verified_jobs':
                try:
                    data=strict_object(record['data'])
                    if not isinstance(data.get('info'),dict) or not isinstance(data.get('result'),dict):raise ValueError('job_schema')
                    con.execute("INSERT OR IGNORE INTO vpnl_jobs(id,data,state,due,created) VALUES(?,?,?,?,?)",(record['id'],canonical(data),'PENDING',0,record['created']))
                    disposition='REVERIFY'
                except (ValueError,TypeError,KeyError):disposition='MALFORMED'
            if table=='verified_quarantine':
                scope=str(record.get('scope') or record.get('id') or '')
                con.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(digest([table,rid]),scope,'legacy_quarantine',ref))
            if disposition in ('LEGACY_UNPROVEN','MALFORMED'):
                con.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(digest([table,rid]),None,disposition.lower(),ref))
            con.execute('INSERT OR IGNORE INTO vpnl_legacy_map VALUES(?,?,?,?)',(table,rid,digest(record),disposition))
            cursor=rid;remaining-=1;count+=1
        con.execute('UPDATE vpnl_migration_progress SET cursor=?,complete=? WHERE source=?',(cursor,int(cursor>=high or not rows),table))
    return count
