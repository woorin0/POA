"""Read-only aggregate diagnostics. No settings, secrets, clients or migrations."""
import argparse,json,sqlite3
from pathlib import Path

STATES=('PENDING','VERIFYING','DONE','HELD','REVIEW','MANUAL_REVIEW','OBSERVED_RECORDED','OBSERVED_NOTIFIED','OBSERVED_REVIEW','OBSERVED_WAIT_PROVIDER','STORED_WAIT_CUTOVER','WAIT_PROVIDER','WAIT_IDENTITY','RETRY','READY_FOR_REVALIDATION','SUBMITTED','SENT','UNKNOWN','POSTING','CLAIMED','FAILED','SUPERSEDED_UNSENT','CUTOVER','PREFLIGHT','WRITER_QUIESCED','BACKUP_VERIFIED','APPROVED','IMPORTING','VALIDATION','MATCHED','SCANNING','BLOCKED')
TABLES=(('vpnl_jobs','state'),('vpnl_deliveries','status'),('vpnl_delivery_attempts','phase'),('vpnl_cutover','phase'),('notification_intake','status'),('intake_restore_scan','state'))

def inspect_db(path):
    result={};c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    try:
        c.execute('PRAGMA query_only=ON');c.execute('PRAGMA busy_timeout=1000')
        for table,column in TABLES:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
            cols={x[1] for x in c.execute('PRAGMA table_info('+table+')')}
            if column not in cols:result[table]={'schema':'UNSUPPORTED'};continue
            marks=','.join('?' for _ in STATES)
            result[table]=dict(c.execute("SELECT CASE WHEN "+column+" IN ("+marks+") THEN "+column+" ELSE 'OTHER' END,count(*) FROM "+table+" GROUP BY 1",STATES).fetchall())
        for table in ('vpnl_roots','vpnl_revisions','vpnl_post_lineage','intake_post_lineage','vpnl_restore_reviews','vpnl_notice_reviews','intake_restore_reviews','intake_restore_dirty'):
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():result[table]={'count':c.execute('SELECT count(*) FROM '+table).fetchone()[0]}
        return result
    finally:c.close()

def main(argv=None):
    parser=argparse.ArgumentParser(description='Aggregate notification status only; no IDs, payloads or credentials.')
    parser.add_argument('--db',required=True,help='Notification SQLite path; never a trading/operational account DB.')
    args=parser.parse_args(argv);report={'mode':'READ_ONLY','worker_liveness':'CHECK_SYSTEMD_AND_WORKER_LOG_CODES','real_delivery_confirmation':'UNVERIFIED'}
    failed=False
    for label,path in (('outbox',args.db),('intake',args.db+'.intake.sqlite3')):
        try:
            report[label]=inspect_db(path)
            if not report[label]:report[label]={'error':'NAMESPACE_NOT_FOUND'};failed=True
            elif any(v.get('schema')=='UNSUPPORTED' for v in report[label].values()):failed=True
        except (sqlite3.Error,OSError,ValueError):report[label]={'error':'READ_FAILED'};failed=True
    print(json.dumps(report,ensure_ascii=True,sort_keys=True))
    return 2 if failed else 0

if __name__=='__main__':raise SystemExit(main())
