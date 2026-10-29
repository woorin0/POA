"""Read-only aggregate diagnostics. No settings, secrets, clients or migrations."""
import argparse,json,sqlite3,hashlib,re,importlib.util,subprocess
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

def inspect_events(path,limit,kind):
    """Bounded selectors and safe reasons. Never print raw payloads/identifiers."""
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1);c.execute('PRAGMA query_only=ON')
    out=[]
    try:
        table='vpnl_jobs' if kind=='outbox' else 'notification_intake'
        column='data' if kind=='outbox' else 'request';sort='created DESC,id' if kind=='outbox' else 'seq DESC'
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():return []
        cols={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
        if column not in cols:return []
        status_column='state' if kind=='outbox' else 'status'
        error_column='error' if 'error' in cols else 'NULL'
        for row_id,encoded,state,error in c.execute('SELECT id,substr('+column+',1,32769),'+status_column+','+error_column+' FROM '+table+' ORDER BY '+sort+' LIMIT ?',(limit,)):
            item={'path':'LEGACY','state':state if state in STATES else 'OTHER','execution_state':'UNVERIFIED','delivery_counts':{},'missing_codes':[],'order_fingerprint':None,'signal_fingerprint':None}
            if error=='TV_SIGNAL_ORDER_CONFLICT':item['missing_codes'].append('TV_SIGNAL_ORDER_CONFLICT')
            try:
                if len(encoded)>32768:raise ValueError()
                data=json.loads(encoded);info=data.get('info') or {};snap=data.get('notice_snapshot') or data.get('observation') or {};signal=snap.get('signal') or {}
                item['execution_state']='OBSERVED_NOT_FINANCIALLY_VERIFIED' if snap.get('confirmed') is True else 'UNCONFIRMED'
                if snap.get('version')==2 and signal:item['path']='TV_SIGNAL'
                elif snap or data.get('notice_intake'):item['path']='ORDER_OBSERVATION'
                if item['path']!='TV_SIGNAL':item['missing_codes'].append('TV_SIGNAL_ABSENT')
                if not info.get('strategy_id'):item['missing_codes'].append('STRATEGY_ID_ABSENT_ORDER_NAME_FALLBACK')
                if item['path']!='TV_SIGNAL' and not snap.get('confirmed'):item['missing_codes'].append('FILL_NOT_CONFIRMED')
                ref=data.get('source_account_ref');exchange=info.get('exchange');oid=snap.get('order_id') or (data.get('result') or {}).get('id')
                def fp(domain,*values):return hashlib.sha256(json.dumps([domain,*values],ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
                if isinstance(ref,str) and re.fullmatch('[0-9a-f]{64}',ref):
                    if isinstance(oid,str):item['order_fingerprint']=fp('poa-notification-order-v1',ref,exchange,oid)
                    sid=signal.get('signal_id')
                    if isinstance(sid,str):item['signal_fingerprint']=fp('poa-notification-signal-v1',ref,exchange,snap.get('strategy'),snap.get('symbol'),sid)
                if not isinstance(ref,str) or not re.fullmatch('[0-9a-f]{64}',ref):item['missing_codes'].append('ACCOUNT_REFERENCE_UNAVAILABLE')
                iid=data.get('notice_intake') if kind=='outbox' else row_id
                if iid and c.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('vpnl_notice_inputs','vpnl_notice_members','vpnl_deliveries')").fetchone()[0]==3:
                    marks=','.join('?' for _ in STATES)
                    item['delivery_counts']=dict(c.execute("SELECT CASE WHEN d.status IN ("+marks+") THEN d.status ELSE 'OTHER' END,count(*) FROM vpnl_notice_members m JOIN vpnl_deliveries d ON d.id=m.delivery_id WHERE m.case_id=(SELECT case_id FROM vpnl_notice_inputs WHERE intake_id=?) GROUP BY 1",(*STATES,iid)).fetchall())
                if not oid:item['missing_codes'].append('ORDER_ID_MISSING')
                if not signal.get('signal_id'):item['missing_codes'].append('SIGNAL_ID_NOT_STORED')
            except Exception:item['missing_codes']=['ROW_SCHEMA_UNVERIFIED']
            out.append(item)
        return out
    finally:c.close()

def main(argv=None):
    parser=argparse.ArgumentParser(description='Aggregate notification status only; no IDs, payloads or credentials.')
    parser.add_argument('--db',required=True,help='Notification SQLite path; never a trading/operational account DB.')
    parser.add_argument('--events',type=int,default=20,help='Bounded recent events (1..40), hashed selectors only.')
    parser.add_argument('--server-alias',default=None,help='Optional non-secret node alias, not identity evidence.')
    args=parser.parse_args(argv);report={'mode':'READ_ONLY','worker_liveness':'CHECK_SYSTEMD_AND_WORKER_LOG_CODES','real_delivery_confirmation':'UNVERIFIED'}
    origin_path=Path(__file__).resolve().parents[1]/'exchange/verified_pnl/notification_origin.py'
    spec=importlib.util.spec_from_file_location('poa_origin_diagnostic',origin_path);origin=importlib.util.module_from_spec(spec);spec.loader.exec_module(origin)
    report['deployment']=origin.deployment()
    alias=args.server_alias
    report['server_alias']=alias if isinstance(alias,str) and re.fullmatch('[A-Za-z0-9_-]{1,24}',alias) and not re.search('(?i)secret|token|bearer|apikey|password',alias) else None
    try:
        state=subprocess.run(['systemctl','is-active','poa'],capture_output=True,text=True,timeout=2).stdout.strip()
        report['service_state']=state if state in ('active','inactive','failed','activating','deactivating') else 'UNVERIFIED'
    except Exception:report['service_state']='UNVERIFIED'
    report['worker_state']='UNVERIFIED_CHECK_RECENT_WORKER_LOG_CODES'
    failed=False
    for label,path in (('outbox',args.db),('intake',args.db+'.intake.sqlite3')):
        try:
            report[label]=inspect_db(path)
            if not report[label]:report[label]={'error':'NAMESPACE_NOT_FOUND'};failed=True
            elif any(v.get('schema')=='UNSUPPORTED' for v in report[label].values()):failed=True
            report[label]['recent_events']=inspect_events(path,max(1,min(40,args.events)),label)
        except (sqlite3.Error,OSError,ValueError):report[label]={'error':'READ_FAILED'};failed=True
    print(json.dumps(report,ensure_ascii=True,sort_keys=True))
    return 2 if failed else 0

if __name__=='__main__':raise SystemExit(main())
