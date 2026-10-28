"""Signed execution ledger. Legacy rows never acquire trust by adding metadata."""
import json,hashlib
from decimal import Decimal,localcontext
from .evidence import canonical,digest,verify,execution_valid

def identity(*parts):
    return hashlib.sha256(json.dumps(parts,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()

def ensure_quarantine_schema(con):
    # Legacy quarantine is left intact, including the older scope/reason schema.
    con.execute('CREATE TABLE IF NOT EXISTS vpnl_quarantine(id TEXT PRIMARY KEY,scope TEXT,reason TEXT,source_ref TEXT,resolved INTEGER NOT NULL DEFAULT 0)')

def allocation_plan(records,fill,history):
    with localcontext() as arithmetic:
        arithmetic.prec=512
        return _allocation_plan(records,fill,history)

def _allocation_plan(records,fill,history):
    scope=fill.scope_data
    # Futures require separate proven contract/margin/funding allocation adapters.
    if scope.get('market_type')!='spot' or Decimal(str(scope.get('contract_size',0)))!=1:return None
    if not verify(history,'HISTORY') or canonical(history['body'].get('scope'))!=canonical(scope): return None
    h=history['body']
    if h.get('opening_quantity')!='0' or h.get('complete') is not True or h.get('nontrade_inventory_delta')!='0':return None
    if not isinstance(h.get('orders'),list) or not isinstance(h.get('through'),(int,float)) or fill.fill_timestamp is None or h['through']<fill.fill_timestamp:return None
    try:
        expected={x['order_id']:set(x['trade_ids']) for x in h['orders'] if x['last_timestamp']<=fill.fill_timestamp}
        if len({x['order_id'] for x in h['orders']})!=len(h['orders']):return None
        records=[x for x in records if max(t['timestamp'] for t in x['fill']['trades'])<=fill.fill_timestamp]
        actual={x['fill']['order_id']:{t['id'] for t in x['fill']['trades']} for x in records}
        if actual!=expected:return None
    except (KeyError,TypeError,ValueError):return None
    qty=cost=fees=allocated=allocated_cost=allocated_fee=Decimal(0)
    rows=[];seen=set();books=set()
    for r in records:
        if not verify(r['receipt'],'EXECUTION'): return None
        b=r['receipt']['body'];f=b['fill']
        if canonical(b['scope'])!=canonical(scope) or canonical(r['fill'])!=canonical(f): return None
        books.add(b['scope']['book'])
        for t in f['trades']:
            key=t['id']
            if key in seen: return None
            seen.add(key)
            if t.get('timestamp') is None or t.get('fee') is None or t.get('fee_currency')!=fill.pnl_currency: return None
            rows.append((t['timestamp'],t['id'],f,t))
    if len(books)!=1:return None
    for _,_,f,t in sorted(rows,key=lambda x:(x[0],x[1])):
        n,p,fee=Decimal(str(t['amount'])),Decimal(str(t['price'])),Decimal(str(t['fee']))
        if min(n,p)<=0 or not all(x.is_finite() for x in (n,p,fee)): return None
        close=f.get('execution_role')=='CLOSE'
        if f.get('execution_role') not in ('CLOSE','ENTRY'):return None
        if not close:qty+=n;cost+=n*p;fees+=fee
        else:
            if qty<n:return None
            share=n/qty;pc=cost*share;pf=fees*share
            if f['order_id']==fill.order_id:allocated+=n;allocated_cost+=pc;allocated_fee+=pf
            qty-=n;cost-=pc;fees-=pf
    if allocated!=fill.filled_quantity or allocated<=0:return None
    basis=allocated_cost+allocated_fee
    if basis<=0:return None
    return {'scope':scope,'order_id':fill.order_id,'executions':sorted(digest(x['receipt']['body']) for x in records),'history_digest':digest(history['body']),'entry_price':str(allocated_cost/allocated),'entry_fee':str(allocated_fee),'cost_basis':str(basis),'currency':fill.pnl_currency}

def allocate(store,fill,info,account,history_complete=False,evidence_context=None):
    if evidence_context is not None and isinstance(evidence_context.history_receipt,dict) and evidence_context.history_receipt.get('body',{}).get('generation_id') is not None:
        from .history import accounting_for_generation
        if history_complete is not True:return None,None
        return accounting_for_generation(store,fill,evidence_context)
    scope=fill.scope_data
    if not scope or scope.get('account')!=account or not execution_valid(fill,scope):return None,None
    scope_id=digest(scope);close=bool(getattr(info,'is_close',False) or (not getattr(info,'is_futures',False) and getattr(info,'is_sell',False)))
    record={'fill':fill.execution_receipt['body']['fill'],'receipt':fill.execution_receipt,'close':close}
    with store._connect() as con:
        ensure_quarantine_schema(con)
        con.execute('CREATE TABLE IF NOT EXISTS vpnl_ledger_entries(id TEXT PRIMARY KEY,scope TEXT NOT NULL,order_id TEXT NOT NULL,data TEXT NOT NULL)')
        if con.execute("SELECT 1 FROM vpnl_quarantine WHERE scope=? AND resolved=0 AND reason!='legacy_unproven' LIMIT 1",(scope_id,)).fetchone():return None,None
        # Existing unproved history must not silently enter the trusted ledger.
        old_scope=identity(fill.exchange,account,fill.market_type,fill.symbol,fill.position_side)
        legacy=con.execute('SELECT id FROM verified_trades WHERE scope=? LIMIT 1',(old_scope,)).fetchone()
        if legacy:
            con.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(identity('legacy',old_scope),scope_id,'legacy_unproven',legacy[0]))
        key=digest([scope_id,fill.order_id,fill.execution_receipt['body']]);encoded=canonical(record)
        previous=con.execute('SELECT data FROM vpnl_ledger_entries WHERE scope=? AND order_id=? ORDER BY rowid DESC LIMIT 1',(scope_id,fill.order_id)).fetchone()
        compatible=True
        if previous:
            prior=json.loads(previous[0])['fill'];old_trades={x['id']:x for x in prior['trades']};new_trades={x['id']:x for x in record['fill']['trades']}
            if Decimal(str(record['fill']['filled_quantity']))<Decimal(str(prior['filled_quantity'])):return None,None
            compatible=all(k in new_trades and canonical(new_trades[k])==canonical(v) for k,v in old_trades.items())
        if not compatible:
            con.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(identity('conflict',key),scope_id,'execution_conflict',key));return None,None
        con.execute('INSERT OR IGNORE INTO vpnl_ledger_entries VALUES(?,?,?,?)',(key,scope_id,fill.order_id,encoded))
    if not close or history_complete is not True or evidence_context is None or not verify(evidence_context.history_receipt,'HISTORY'):return None,None
    records=[];cursor=0
    with store._connect_read() as con:
        while True:
            batch=con.execute('SELECT rowid,data FROM vpnl_ledger_entries WHERE scope=? AND rowid>? ORDER BY rowid LIMIT 40',(scope_id,cursor)).fetchall()
            if not batch:break
            records.extend(json.loads(x[1]) for x in batch);cursor=batch[-1][0]
    latest={}
    for candidate in records:
        oid=candidate['fill']['order_id'];old=latest.get(oid)
        if old is None or Decimal(str(candidate['fill']['filled_quantity']))>=Decimal(str(old['fill']['filled_quantity'])):latest[oid]=candidate
    records=list(latest.values())
    legacy_sources=[]
    if legacy:
        cursor=''
        with store._connect_read() as con:
            while True:
                old_rows=con.execute('SELECT id,data,rowid FROM verified_trades WHERE scope=? AND id>? ORDER BY id LIMIT 40',(old_scope,cursor)).fetchall()
                if not old_rows:break
                for old_id,raw,source_row in old_rows:
                    legacy_sources.append(source_row)
                    try:oid=json.loads(raw)['order']
                    except (ValueError,TypeError,KeyError):return None,None
                    if oid not in latest:return None,None
                cursor=old_rows[-1][0]
    plan=allocation_plan(records,fill,evidence_context.history_receipt)
    if plan is None:return None,None
    from .verifier import FillVerifier
    receipt=FillVerifier().certify_allocation(fill,evidence_context,dict(plan,records=records))
    if receipt and legacy:
        with store._connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS vpnl_quarantine_resolutions(id TEXT PRIMARY KEY,receipt_digest TEXT NOT NULL)')
            qid=identity('legacy',old_scope)
            con.execute('INSERT OR IGNORE INTO vpnl_quarantine_resolutions VALUES(?,?)',(qid,digest(receipt)))
            con.execute("UPDATE vpnl_quarantine SET resolved=1 WHERE id=? AND reason='legacy_unproven'",(qid,))
            for source_row in legacy_sources:
                source_id=digest(['verified_trades',source_row])
                con.execute('INSERT OR IGNORE INTO vpnl_quarantine_resolutions VALUES(?,?)',(source_id,digest(receipt)))
                con.execute("UPDATE vpnl_quarantine SET resolved=1 WHERE id=? AND reason='legacy_unproven'",(source_id,))
    return (Decimal(plan['entry_price']),Decimal(plan['entry_fee'])) if receipt else (None,None)
