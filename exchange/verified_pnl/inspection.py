"""Read-only v4 administration views. No receipts, payloads, wire bytes or raw errors."""
import sqlite3,json,hashlib
from pathlib import Path
from .migrations import preflight,display_state


def inspect(path,*,exchange=None,account=None,order=None,root=None,revision=None,delivery=None,limit=40,after=''):
    """Bounded delivery keyset page; never initializes or migrates a database."""
    if type(limit) is not int or not 1<=limit<=40:raise ValueError('inspection_limit')
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.execute('BEGIN')
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='vpnl_cutover'").fetchone():
            return {'phase':'LEGACY_PREFLIGHT_REQUIRED','legacy':preflight(path)}
        phase=c.execute('SELECT phase FROM vpnl_cutover WHERE id=1').fetchone()[0]
        where=['d.id>?'];args=[after]
        for name,value,column in [('exchange',exchange,"json_extract(b.binding,'$.exchange')"),('account',account,"json_extract(b.binding,'$.scope.account')"),('order',order,"json_extract(b.binding,'$.order_id')"),('root',root,'d.root_id'),('revision',revision,'d.revision'),('delivery',delivery,'d.id')]:
            if value is not None:where.append(column+'=?');args.append(value)
        args.append(limit)
        rows=c.execute('SELECT d.id,d.root_id,d.revision,d.status,d.wire_digest,d.message_id FROM vpnl_deliveries d LEFT JOIN vpnl_root_bindings b ON b.root_id=d.root_id WHERE '+' AND '.join(where)+' ORDER BY d.id LIMIT ?',args).fetchall()
        deliveries=[dict(zip(('delivery','root','revision','status','wire_digest','message_id'),r)) for r in rows]
        legacy=c.execute('SELECT source,source_row,legacy_state,disposition,reason FROM vpnl_legacy_archive ORDER BY source,source_row LIMIT ?',(limit,)).fetchall()
        quarantine=c.execute('SELECT id,reason,source_ref,resolved FROM vpnl_quarantine ORDER BY id LIMIT ?',(limit,)).fetchall()
        return {'phase':phase,'deliveries':deliveries,'next':rows[-1][0] if rows else None,
                'counts':dict(c.execute('SELECT status,count(*) FROM vpnl_deliveries GROUP BY status')),
                'legacy': [dict(zip(('source','source_row','state','disposition','reason'),(r[0],r[1],display_state(r[2]),r[3],r[4]))) for r in legacy],
                'legacy_count':c.execute('SELECT count(*) FROM vpnl_legacy_archive').fetchone()[0],
                'quarantine_count':c.execute('SELECT count(*) FROM vpnl_quarantine WHERE resolved=0').fetchone()[0],
                'quarantine':[{'reference':hashlib.sha256(str(r[0]).encode()).hexdigest(),'reason_code':r[1] if r[1] in {'legacy_evidence_unproven','legacy_schema_invalid','legacy_quarantine_retained','root_binding_review','schema_invalid'} else 'review_required','source_reference_digest':hashlib.sha256(str(r[2]).encode()).hexdigest(),'resolved':bool(r[3])} for r in quarantine]}


def inspect_alias(path,alias):
    """Diagnostic mapping only. Legacy references cannot authorize a v4 manual action."""
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        roots=c.execute('SELECT id FROM vpnl_roots WHERE alias=?',(alias,)).fetchall()
        old=c.execute('SELECT source,source_row FROM vpnl_legacy_aliases WHERE alias=? LIMIT 41',(alias,)).fetchall()
        if len(roots)+len(old)!=1:raise ValueError('ambiguous_or_missing_alias')
        if old: return {'review_required':True,'source':old[0][0],'source_row':old[0][1]}
        return {'root':roots[0][0],'review_required':False}


def inspect_legacy(path,source,source_row):
    """Exact original reference; retained evidence stays untrusted, never a delivery ID."""
    from .migrations import TABLES
    if source not in TABLES or type(source_row) is not int:raise ValueError('legacy_reference_required')
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        row=c.execute('SELECT source_digest,record,legacy_state,disposition,reason FROM vpnl_legacy_archive WHERE source=? AND source_row=?',(source,source_row)).fetchone()
        if row is None:raise ValueError('legacy_reference_not_found')
        record=json.loads(row[1]);aliases=c.execute('SELECT alias FROM vpnl_legacy_aliases WHERE source=? AND source_row=?',(source,source_row)).fetchall()
        return {'source':source,'source_row':source_row,'source_digest':row[0],'state':display_state(row[2]),'disposition':row[3],'reason':row[4],
                'original_reason_digest':hashlib.sha256(str(record.get('reason','')).encode()).hexdigest(),
                'original_reason_preserved':'reason' in record,'scope_unproven':True,
                'alias_digests':[hashlib.sha256(str(a[0]).encode()).hexdigest() for a in aliases],
                'delivery':None,'wire_receipt_available':False}
