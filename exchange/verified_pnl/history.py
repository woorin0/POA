"""Bounded spot history generations. Only a registered authenticated read capability
can supply the independent coverage/flat/inventory/ownership observations.
No normalized DTO or completion flag grants history authority.
"""
from dataclasses import dataclass,replace
from decimal import Decimal,localcontext
from types import SimpleNamespace
import hashlib,json
from . import evidence as E
from .bootstrap import current_bootstrap

_PROVIDER=None
POLICY='FLAT_SPOT_QUOTE_MWA_V1'

@dataclass(frozen=True)
class Coverage:
    scope:dict
    start:float
    through:float
    order_count:int
    trade_count:int
    order_digest:str
    reference:str
    source_revision:int
    observed_at:float=0

@dataclass(frozen=True)
class BalanceEvidence:
    scope:dict
    at:float
    quantity:str
    reference:str

@dataclass(frozen=True)
class InventoryEvidence:
    scope:dict
    start:float
    through:float
    opening:str
    closing:str
    external_events:tuple
    owners:tuple
    reference:str

@dataclass(frozen=True)
class HistoryOrder:
    sequence:int
    order_id:str
    side:str
    book:str

@dataclass(frozen=True)
class HistoryPage:
    scope:dict
    after:int
    orders:tuple
    reference:str

@dataclass(frozen=True)
class HistoryReadAdapter:
    contract_id:str
    coverage:object
    balance:object
    inventory:object
    page:object


def install(con):
    for sql in (
        'CREATE TABLE IF NOT EXISTS vpnl_history_candidates(id TEXT PRIMARY KEY,scope TEXT NOT NULL,proof TEXT NOT NULL,cursor INTEGER NOT NULL,status TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS vpnl_history_orders(candidate TEXT NOT NULL,sequence INTEGER NOT NULL,order_id TEXT NOT NULL,receipt TEXT NOT NULL,execution_digest TEXT NOT NULL,fact TEXT NOT NULL,PRIMARY KEY(candidate,sequence),UNIQUE(candidate,order_id))',
        'CREATE TABLE IF NOT EXISTS vpnl_history_trades(candidate TEXT NOT NULL,id TEXT NOT NULL,stamp REAL NOT NULL,order_id TEXT NOT NULL,role TEXT NOT NULL,amount TEXT NOT NULL,price TEXT NOT NULL,fee TEXT NOT NULL,PRIMARY KEY(candidate,id))',
        'CREATE INDEX IF NOT EXISTS vpnl_history_trade_order ON vpnl_history_trades(candidate,stamp,id)',
        'CREATE TABLE IF NOT EXISTS vpnl_history_generations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,previous TEXT,candidate TEXT NOT NULL,body TEXT NOT NULL,receipt TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS vpnl_history_heads(scope TEXT PRIMARY KEY,generation_id TEXT NOT NULL,cursor TEXT NOT NULL,reconciling INTEGER NOT NULL)',
        'CREATE TABLE IF NOT EXISTS vpnl_history_invalidations(generation_id TEXT PRIMARY KEY,successor TEXT NOT NULL,proof TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS vpnl_history_dependencies(id TEXT PRIMARY KEY,scope TEXT NOT NULL,generation_id TEXT NOT NULL,job_id TEXT NOT NULL,financial TEXT NOT NULL)',
        'CREATE INDEX IF NOT EXISTS vpnl_history_dependency_scope ON vpnl_history_dependencies(scope,id)',
        'CREATE TABLE IF NOT EXISTS vpnl_history_watermarks(scope TEXT PRIMARY KEY,revision INTEGER NOT NULL,facts TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS vpnl_history_refresh(id INTEGER PRIMARY KEY CHECK(id=1),cursor TEXT NOT NULL,due REAL NOT NULL)',
        'CREATE TABLE IF NOT EXISTS vpnl_history_revocations(id TEXT PRIMARY KEY,dependency TEXT NOT NULL,receipt TEXT NOT NULL,state TEXT NOT NULL)'):
        con.execute(sql)
    for table in ('vpnl_history_generations','vpnl_history_invalidations','vpnl_history_dependencies'):
        for verb in ('UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{verb}_immutable BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_history'); END")
    for table in ('vpnl_history_orders','vpnl_history_trades'):
        for verb in ('INSERT','UPDATE','DELETE'):
            ref='NEW' if verb=='INSERT' else 'OLD'
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{verb}_sealed BEFORE {verb} ON {table} WHEN EXISTS(SELECT 1 FROM vpnl_history_candidates WHERE id={ref}.candidate AND status='COMPLETE') BEGIN SELECT RAISE(ABORT,'sealed_history'); END")
    con.execute("CREATE TRIGGER IF NOT EXISTS vpnl_history_revocation_sealed BEFORE UPDATE OF id,dependency,receipt ON vpnl_history_revocations BEGIN SELECT RAISE(ABORT,'immutable_history_revocation'); END")
    con.execute("CREATE TRIGGER IF NOT EXISTS vpnl_history_revocation_retain BEFORE DELETE ON vpnl_history_revocations BEGIN SELECT RAISE(ABORT,'retained_history_revocation'); END")
    con.execute("CREATE TRIGGER IF NOT EXISTS vpnl_history_candidate_sealed BEFORE UPDATE ON vpnl_history_candidates WHEN OLD.status='COMPLETE' BEGIN SELECT RAISE(ABORT,'sealed_history'); END")
    con.execute("CREATE TRIGGER IF NOT EXISTS vpnl_history_candidate_retain BEFORE DELETE ON vpnl_history_candidates BEGIN SELECT RAISE(ABORT,'retained_history'); END")
    con.execute("""CREATE TRIGGER IF NOT EXISTS vpnl_history_revision_guard BEFORE INSERT ON vpnl_revisions
      WHEN json_valid(NEW.payload) AND json_extract(NEW.payload,'$.financial_receipt.body.generation_id') IS NOT NULL
      AND (EXISTS(SELECT 1 FROM vpnl_history_invalidations WHERE generation_id=json_extract(NEW.payload,'$.financial_receipt.body.generation_id'))
      OR NOT EXISTS(SELECT 1 FROM vpnl_history_generations g JOIN vpnl_history_heads h ON h.scope=g.scope WHERE g.id=json_extract(NEW.payload,'$.financial_receipt.body.generation_id') AND h.reconciling=0))
      BEGIN SELECT RAISE(ABORT,'history_dependency_unavailable'); END""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS vpnl_history_post_guard BEFORE UPDATE OF status ON vpnl_deliveries WHEN NEW.status='POSTING'
      AND EXISTS(SELECT 1 FROM vpnl_revisions v JOIN vpnl_history_generations g ON g.id=json_extract(v.payload,'$.financial_receipt.body.generation_id')
      JOIN vpnl_history_heads h ON h.scope=g.scope WHERE v.root_id=NEW.root_id AND v.revision=NEW.revision AND (h.reconciling!=0 OR EXISTS(SELECT 1 FROM vpnl_history_invalidations i WHERE i.generation_id=g.id)))
      BEGIN SELECT RAISE(ABORT,'history_dependency_unavailable'); END""")
    install_consumers(con)


def configure_history_provider(provider):
    global _PROVIDER
    if provider is not None and not isinstance(provider,TrustedSpotHistoryProvider):raise ValueError('trusted_history_provider_required')
    _PROVIDER=provider

def history_readiness():return _PROVIDER.state if _PROVIDER else 'HISTORY_NOT_READY'

def order_fact(fill,sequence):
    return dict(sequence=sequence,order_id=fill.order_id,side=fill.order_side,status=fill.status.value,exchange_status=fill.exchange_status,requested=_number(fill.requested_quantity),remaining=_number(fill.remaining_quantity),quantity=_number(fill.filled_quantity),average=_number(fill.average_fill_price),trades=[dict(id=t.id,amount=_number(t.amount),price=_number(t.price),fee=_number(t.fee),currency=t.fee_currency,timestamp=t.timestamp) for t in fill.trades])

def _stream(con,sql,args=()):
    cursor=con.execute(sql,args)
    while True:
        batch=cursor.fetchmany(40)
        if not batch:return
        yield from batch

def _position(con,candidate,target=None):
    qty=cost=fees=allocated=acost=afee=Decimal(0);ended=False
    if con.execute("SELECT 1 FROM vpnl_history_trades WHERE candidate=? GROUP BY stamp HAVING count(DISTINCT role)>1 LIMIT 1",(candidate,)).fetchone():raise ValueError('history_execution_order_ambiguous')
    for tid,stamp,oid,role,n,p,f in _stream(con,'SELECT id,stamp,order_id,role,amount,price,fee FROM vpnl_history_trades WHERE candidate=? ORDER BY stamp,id',(candidate,)):
        if ended:raise ValueError('history_cycle_bounds')
        n,p,f=map(Decimal,(n,p,f))
        if not all(v.is_finite() for v in (n,p,f)) or min(n,p)<=0:raise ValueError('history_trade_value')
        if role=='ENTRY':qty+=n;cost+=n*p;fees+=f
        elif role=='CLOSE':
            if qty<n:raise ValueError('history_inventory_gap')
            pc=cost*n/qty;pf=fees*n/qty
            if oid==target:allocated+=n;acost+=pc;afee+=pf
            qty-=n;cost-=pc;fees-=pf
            if qty==0:ended=True
        else:raise ValueError('history_role')
    return dict(quantity=str(qty),cost=str(cost),entry_fee=str(fees)),(allocated,acost,afee)

class TrustedSpotHistoryProvider:
    def __init__(self,bootstrap,adapter):
        self.bootstrap=bootstrap;self.adapter=adapter;self.state='HISTORY_NOT_READY'
    def _proof(self,context):
        if self.bootstrap is not current_bootstrap() or context.execution_reader is not self.bootstrap or not self.bootstrap._identity_ready():raise ValueError('history_identity')
        a=self.adapter;c=context.client;scope=E.strict_object(context.scope_json)
        if not isinstance(a,HistoryReadAdapter) or not a.contract_id:raise ValueError('history_adapter')
        cover=a.coverage(c,scope);opening=a.balance(c,scope,'opening');closing=a.balance(c,scope,'closing');inventory=a.inventory(c,scope)
        if not isinstance(cover,Coverage) or not isinstance(opening,BalanceEvidence) or not isinstance(closing,BalanceEvidence) or not isinstance(inventory,InventoryEvidence):raise ValueError('history_source')
        if any(E.canonical(x.scope)!=E.canonical(scope) for x in (cover,opening,closing,inventory)):raise ValueError('history_scope')
        if not all(isinstance(x.reference,str) and x.reference for x in (cover,opening,closing,inventory)) or len({x.reference for x in (cover,opening,closing,inventory)})!=4:raise ValueError('independent_history_sources')
        if not all(__import__('math').isfinite(x) for x in (cover.start,cover.through,opening.at,closing.at)) or cover.start>cover.through:raise ValueError('history_window')
        if (opening.at,closing.at,inventory.start,inventory.through)!=(cover.start,cover.through,cover.start,cover.through):raise ValueError('history_window')
        if Decimal(opening.quantity)!=0 or Decimal(inventory.opening)!=0 or Decimal(closing.quantity)!=Decimal(inventory.closing):raise ValueError('flat_anchor')
        if inventory.external_events or inventory.owners!=(scope['book'],):raise ValueError('inventory_ownership')
        if type(cover.source_revision) is not int or cover.source_revision<1:raise ValueError('history_revision_source')
        if type(cover.order_count) is not int or cover.order_count<1 or type(cover.trade_count) is not int or cover.trade_count<1:raise ValueError('coverage_count')
        stable=dict(scope=scope,start=float(cover.start),through=float(cover.through),orders=cover.order_count,trades=cover.trade_count,order_digest=cover.order_digest,closing=_number(closing.quantity),policy=POLICY,contract=a.contract_id)
        refs=dict(coverage_digest=E.digest(cover.reference),flat_digest=E.digest(opening.reference),balance_digest=E.digest(closing.reference),inventory_digest=E.digest(inventory.reference),source_revision=cover.source_revision)
        return stable,refs
    def advance(self,store,fill,info,context,job_id=None):
        with localcontext() as arithmetic:
            arithmetic.prec=512
            self.state='HISTORY_NOT_READY'
            try:return self._advance(store,fill,info,context,job_id)
            except Exception:
                self.state='HISTORY_NOT_READY';return context
    def _advance(self,store,fill,info,context,job_id=None):
        from .verifier import FillVerifier
        from .migrations import require_cutover
        if not E.execution_valid(fill,fill.scope_data):return context
        proof,refs=self._proof(context);cid=E.digest(proof);scope_id=E.digest(proof['scope'])
        with store._connect() as c:
            require_cutover(c)
            watermark=c.execute('SELECT revision,facts FROM vpnl_history_watermarks WHERE scope=?',(scope_id,)).fetchone()
            if watermark and (refs['source_revision']<watermark[0] or (refs['source_revision']==watermark[0] and cid!=watermark[1])):raise ValueError('history_stale_source')
            head=c.execute('SELECT g.body FROM vpnl_history_heads h JOIN vpnl_history_generations g ON g.id=h.generation_id WHERE h.scope=?',(scope_id,)).fetchone()
            actual_head=c.execute('SELECT id FROM vpnl_history_generations WHERE scope=? ORDER BY rowid DESC LIMIT 1',(scope_id,)).fetchone()
            pointer=c.execute('SELECT generation_id FROM vpnl_history_heads WHERE scope=?',(scope_id,)).fetchone()
            if actual_head is not None and (pointer is None or pointer[0]!=actual_head[0]):raise ValueError('history_head_review')
            if head and json.loads(head[0])['coverage']['through']>proof['through']:return context
            c.execute("INSERT OR IGNORE INTO vpnl_history_candidates VALUES(?,?,?,0,'PENDING')",(cid,scope_id,E.canonical(dict(stable=proof,refs=refs))))
            after,status=c.execute('SELECT cursor,status FROM vpnl_history_candidates WHERE id=?',(cid,)).fetchone()
        if status!='COMPLETE':
            page=self.adapter.page(context.client,proof['scope'],after,40)
            if not isinstance(page,HistoryPage) or E.canonical(page.scope)!=E.canonical(proof['scope']) or page.after!=after or not isinstance(page.orders,tuple) or not page.reference or not 0<len(page.orders)<=40:raise ValueError('history_page')
            prepared=[];budget=0
            for index,item in enumerate(page.orders):
                if not isinstance(item,HistoryOrder) or item.sequence!=after+index+1 or item.sequence>proof['orders'] or item.book!=proof['scope']['book'] or item.side not in ('buy','sell'):raise ValueError('history_order')
                observed_info=SimpleNamespace(**vars(info));observed_info.side=item.side;observed_info.is_buy=item.side=='buy';observed_info.is_sell=item.side=='sell';observed_info.is_close=False;observed_info.is_futures=False
                found=FillVerifier().verify_order_result(SimpleNamespace(client=context.client),fill.exchange,fill.symbol,{'id':item.order_id},observed_info,context)
                if not E.execution_valid(found,proof['scope']) or found.fill_timestamp is None or not proof['start']<=found.fill_timestamp<=proof['through']:raise ValueError('history_execution')
                if not 0<len(found.trades)<=39:raise ValueError('history_order_bound')
                if budget+1+len(found.trades)>40:break
                if any(t.timestamp is None or not proof['start']<=t.timestamp<=proof['through'] for t in found.trades):raise ValueError('history_trade_window')
                budget+=1+len(found.trades);prepared.append((item,found))
            if not prepared:raise ValueError('history_page_progress')
            with store._connect() as c:
                if c.execute('SELECT cursor FROM vpnl_history_candidates WHERE id=?',(cid,)).fetchone()[0]!=after:return context
                for item,found in prepared:
                    if not E.execution_valid(found,proof['scope']):raise ValueError('history_key')
                    receipt=found.execution_receipt
                    c.execute('INSERT INTO vpnl_history_orders VALUES(?,?,?,?,?,?)',(cid,item.sequence,item.order_id,E.canonical(receipt),E.digest(receipt['body']),E.canonical(order_fact(found,item.sequence))))
                    for t in found.trades:c.execute('INSERT INTO vpnl_history_trades VALUES(?,?,?,?,?,?,?,?)',(cid,t.id,t.timestamp,item.order_id,found.execution_role,_number(t.amount),_number(t.price),_number(t.fee)))
                after=prepared[-1][0].sequence;c.execute('UPDATE vpnl_history_candidates SET cursor=? WHERE id=?',(after,cid))
            if after<proof['orders']:return context
            # Re-read independent interval evidence after the staged bounded scan.
            fresh,fresh_refs=self._proof(context)
            if E.canonical(fresh)!=E.canonical(proof) or fresh_refs['source_revision']!=refs['source_revision']:raise ValueError('history_changed_during_scan')
            with store._connect() as c:
                current=c.execute('SELECT revision,facts FROM vpnl_history_watermarks WHERE scope=?',(scope_id,)).fetchone()
                if current and (refs['source_revision']<current[0] or (refs['source_revision']==current[0] and cid!=current[1])):raise ValueError('history_stale_source')
                orders=hashlib.sha256();executions=hashlib.sha256()
                for encoded,receipt in _stream(c,'SELECT fact,receipt FROM vpnl_history_orders WHERE candidate=? ORDER BY sequence',(cid,)):
                    r=E.strict_object(receipt)
                    if not E.verify(r,'EXECUTION') or E.canonical(r['body']['scope'])!=E.canonical(proof['scope']):raise ValueError('history_key')
                    from .models import VerifiedFill
                    parsed=VerifiedFill(**r['body']['fill'])
                    if E.canonical(order_fact(parsed,E.strict_object(encoded)['sequence']))!=encoded or not _trade_cache_valid(c,cid,parsed):raise ValueError('history_stage_conflict')
                    orders.update((encoded+'\n').encode());executions.update((E.digest(r['body'])+'\n').encode())
                if orders.hexdigest()!=proof['order_digest'] or c.execute('SELECT count(*) FROM vpnl_history_trades WHERE candidate=?',(cid,)).fetchone()[0]!=proof['trades']:raise ValueError('coverage_digest')
                position,_=_position(c,cid)
                if Decimal(position['quantity'])!=Decimal(proof['closing']):raise ValueError('closing_balance')
                parent=c.execute('SELECT generation_id FROM vpnl_history_heads WHERE scope=?',(scope_id,)).fetchone()
                facts=dict(scope=proof['scope'],coverage=proof,executions_digest=executions.hexdigest(),position=position,policy=POLICY)
                gid=E.digest(facts);body=dict(facts,generation_id=gid,previous=parent[0] if parent else None,candidate=cid,provenance=refs,complete=True,opening_quantity='0',nontrade_inventory_delta='0',through=proof['through'])
                receipt=context.authority._attest('HISTORY',body)
                if not E.verify(receipt,'HISTORY'):raise ValueError('history_key')
                if parent:
                    prior=c.execute('SELECT candidate,body FROM vpnl_history_generations WHERE id=?',(parent[0],)).fetchone()
                    if _conflicts(c,prior[0],cid,json.loads(prior[1])['through']):
                        change=context.authority._attest('HISTORY_CHANGE',dict(scope=proof['scope'],invalidated_generation=parent[0],successor_generation=gid))
                        if not E.verify(change,'HISTORY_CHANGE'):raise ValueError('history_key')
                        c.execute('INSERT OR IGNORE INTO vpnl_history_invalidations VALUES(?,?,?)',(parent[0],gid,E.canonical(change)))
                c.execute('INSERT INTO vpnl_history_generations VALUES(?,?,?,?,?,?)',(gid,scope_id,body['previous'],cid,E.canonical(body),E.canonical(receipt)))
                c.execute("UPDATE vpnl_history_candidates SET status='COMPLETE' WHERE id=?",(cid,))
                c.execute('INSERT INTO vpnl_history_watermarks VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET revision=excluded.revision,facts=excluded.facts',(scope_id,refs['source_revision'],cid))
                c.execute("INSERT INTO vpnl_history_heads VALUES(?,?,'',1) ON CONFLICT(scope) DO UPDATE SET generation_id=excluded.generation_id,cursor='',reconciling=1",(scope_id,gid))
        else:
            with store._connect_read() as c:
                row=c.execute('SELECT id FROM vpnl_history_generations WHERE candidate=?',(cid,)).fetchone()
                if not row:return context
                gid=row[0]
                current=c.execute('SELECT generation_id FROM vpnl_history_heads WHERE scope=?',(scope_id,)).fetchone()
                if current is None or current[0]!=gid:return context
            with store._connect() as c:
                c.execute('INSERT INTO vpnl_history_watermarks VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET revision=excluded.revision,facts=excluded.facts WHERE excluded.revision>=vpnl_history_watermarks.revision',(scope_id,refs['source_revision'],cid))
        reconcile(store,gid,context)
        withdrawal=pending_withdrawal(store,fill.order_id,context,job_id)
        if withdrawal is not None:self.state='HISTORY_DEPENDENCY_REVOKED';return replace(context,history_receipt=withdrawal)
        with store._connect_read() as c:
            blocked=c.execute("SELECT 1 FROM vpnl_consumer_actions r JOIN vpnl_history_consumer_edges d ON d.id=r.edge_id JOIN vpnl_history_proofs p ON p.id=d.proof_id WHERE r.state='KEY_REVIEW' AND d.scope=? AND json_extract(p.financial,'$.body.order_id')=? LIMIT 1",(scope_id,fill.order_id)).fetchone()
        if blocked:self.state='HISTORY_KEY_REVIEW';return context
        if not generation_valid(store,gid,fill):return context
        with store._connect_read() as c:receipt=E.strict_object(c.execute('SELECT receipt FROM vpnl_history_generations WHERE id=?',(gid,)).fetchone()[0])
        self.state='HISTORY_COMPLETE';return replace(context,history_receipt=receipt)


def attach_history(store,fill,info,context,job_id=None):
    if _PROVIDER is None or context is None:return context
    return _PROVIDER.advance(store,fill,info,context,job_id)


def _generation_check(store,gid,fill=None,historical=False):
    try:
        with store._connect_read() as c:
            row=c.execute('SELECT body,receipt,candidate,scope FROM vpnl_history_generations WHERE id=?',(gid,)).fetchone()
            if not row:return False
            body=E.strict_object(row[0]);receipt=E.strict_object(row[1])
            if not E.verify(receipt,'HISTORY',body) or body['generation_id']!=gid or row[3]!=E.digest(body['scope']) or row[2]!=body['candidate']:return False
            if not historical and c.execute('SELECT 1 FROM vpnl_history_invalidations WHERE generation_id=?',(gid,)).fetchone():return False
            h=c.execute('SELECT reconciling,generation_id FROM vpnl_history_heads WHERE scope=?',(row[3],)).fetchone()
            latest_id=c.execute('SELECT id FROM vpnl_history_generations WHERE scope=? ORDER BY rowid DESC LIMIT 1',(row[3],)).fetchone()
            if not h or (h[0] and not historical) or latest_id is None or h[1]!=latest_id[0]:return False
            if not historical and not c.execute('SELECT complete FROM vpnl_consumer_scan WHERE id=1').fetchone()[0]:return False
            facts={k:body[k] for k in ('scope','coverage','executions_digest','position','policy')}
            if E.digest(facts)!=gid:return False
            running=hashlib.sha256();count=0;trade_count=0
            from .models import VerifiedFill
            for sequence,encoded,fact,execution_digest in _stream(c,'SELECT sequence,receipt,fact,execution_digest FROM vpnl_history_orders WHERE candidate=? ORDER BY sequence',(row[2],)):
                r=E.strict_object(encoded);count+=1
                if sequence!=count or execution_digest!=E.digest(r['body']):return False
                parsed=VerifiedFill(**r['body']['fill'])
                if E.canonical(order_fact(parsed,sequence))!=fact:return False
                actual=c.execute('SELECT id,stamp,order_id,role,amount,price,fee FROM vpnl_history_trades WHERE candidate=? AND order_id=? ORDER BY id LIMIT 40',(row[2],parsed.order_id)).fetchall()
                expected=[(t.id,t.timestamp,parsed.order_id,parsed.execution_role,_number(t.amount),_number(t.price),_number(t.fee)) for t in parsed.trades]
                if sorted(expected)!=actual:return False
                trade_count+=len(expected)
                if not E.verify(r,'EXECUTION') or E.canonical(r['body']['scope'])!=E.canonical(body['scope']):return False
                running.update((E.digest(r['body'])+'\n').encode())
            if running.hexdigest()!=body['executions_digest'] or count!=body['coverage']['orders'] or trade_count!=body['coverage']['trades']:return False
            if c.execute('SELECT count(*) FROM vpnl_history_trades WHERE candidate=?',(row[2],)).fetchone()[0]!=trade_count:return False
            latest=c.execute('SELECT g.candidate FROM vpnl_history_heads h JOIN vpnl_history_generations g ON g.id=h.generation_id WHERE h.scope=?',(row[3],)).fetchone()
            if not historical and _conflicts(c,row[2],latest[0],body['through']):return False
            if fill is not None:
                if E.canonical(fill.scope_data)!=E.canonical(body['scope']):return False
                target=c.execute('SELECT execution_digest FROM vpnl_history_orders WHERE candidate=? AND order_id=?',(row[2],fill.order_id)).fetchone()
                if not target or target[0]!=E.digest(fill.execution_receipt['body']):return False
            return True
    except Exception:return False


def generation_valid(store,gid,fill=None):
    return _generation_check(store,gid,fill,False)


def accounting_for_generation(store,fill,context):
    """Stream MWA from sealed, signature-checked executions; never legacy raw rows."""
    if context is None or not E.verify(context.history_receipt,'HISTORY') or context.history_receipt['body'].get('complete') is not True:return None,None
    gid=context.history_receipt['body'].get('generation_id')
    if not gid or not generation_valid(store,gid,fill):return None,None
    with localcontext() as arithmetic:
        arithmetic.prec=512
        with store._connect_read() as c:
            body=E.strict_object(c.execute('SELECT body FROM vpnl_history_generations WHERE id=?',(gid,)).fetchone()[0])
            if E.canonical(body)!=E.canonical(context.history_receipt['body']):return None,None
            _,allocation=_position(c,body['candidate'],fill.order_id)
        n,cost,fee=allocation
        if n!=fill.filled_quantity or n<=0 or cost+fee<=0:return None,None
        plan=dict(scope=fill.scope_data,order_id=fill.order_id,generation_id=gid,history_digest=E.digest(context.history_receipt['body']),execution_digest=E.digest(fill.execution_receipt['body']),executions_digest=body['executions_digest'],entry_price=str(cost/n),entry_cost=str(cost),entry_fee=str(fee),cost_basis=str(cost+fee),currency=fill.pnl_currency)
        receipt=context.authority._attest('ACCOUNTING',plan)
        if not E.verify(receipt,'ACCOUNTING'):return None,None
        fill.accounting_receipt=receipt
        return Decimal(plan['entry_price']),fee


def accounting_valid(store,fill,basis):
    try:
        gid=basis['generation_id']
        if store is None or not generation_valid(store,gid,fill):return False
        with localcontext() as arithmetic:
            arithmetic.prec=512
            with store._connect_read() as c:
                body=E.strict_object(c.execute('SELECT body FROM vpnl_history_generations WHERE id=?',(gid,)).fetchone()[0])
                _,(n,cost,fee)=_position(c,body['candidate'],fill.order_id)
            return basis.get('history_digest')==E.digest(body) and n==fill.filled_quantity and n>0 and Decimal(basis['entry_cost'])==cost and basis['execution_digest']==E.digest(fill.execution_receipt['body']) and basis['executions_digest']==body['executions_digest'] and Decimal(basis['entry_price'])==cost/n and Decimal(basis['entry_fee'])==fee and Decimal(basis['cost_basis'])==cost+fee
    except Exception:return False


def register_dependency(store,fill,result,context,job_id):
    financial=result.financial_receipt
    if financial is None or 'generation_id' not in financial['body']:return
    gid=financial['body']['generation_id']
    if not E.verify(financial,'FINANCIAL') or financial['body']['execution_digest']!=E.digest(fill.execution_receipt['body']) or financial['body']['accounting_digest']!=E.digest(fill.accounting_receipt['body']) or E.canonical(financial['body']['scope'])!=E.canonical(fill.scope_data):raise ValueError('financial_dependency_binding')
    if not generation_valid(store,gid,fill):raise ValueError('stale_history')
    identifier=E.digest([financial['key_id'],financial['body'],job_id]);scope=E.digest(fill.scope_data)
    with store._connect() as c:
        row=c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(job_id,)).fetchone()
        if not row:raise ValueError('history_job_required')
        data=store.parse_job(row[0])
        if data['account']!=fill.scope_data['account'] or str(data['result'].get('id') or data['result'].get('uuid'))!=fill.order_id:raise ValueError('history_job_binding')
        if c.execute('SELECT 1 FROM vpnl_history_invalidations WHERE generation_id=?',(gid,)).fetchone():raise ValueError('stale_history')
        _retain_proof_job(c,financial,scope,gid,job_id)
        # Retain every producer whose published fact can require correction.
        c.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(E.digest(['history-retain',scope]),scope,'history_dependency_retained',gid))


def reconcile(store,gid,context):
    """One bounded consumer batch; publication of one edge never completes a proof."""
    with store._connect() as c:
        new=c.execute('SELECT scope,candidate FROM vpnl_history_generations WHERE id=?',(gid,)).fetchone()
        head=c.execute('SELECT generation_id,cursor,reconciling FROM vpnl_history_heads WHERE scope=?',(new[0],)).fetchone()
        if head[0]!=gid or not head[2]:return
        batch=c.execute('SELECT e.id,e.job_id,e.root_id,p.generation_id,p.financial FROM vpnl_history_consumer_edges e JOIN vpnl_history_proofs p ON p.id=e.proof_id WHERE e.scope=? AND e.id>? ORDER BY e.id LIMIT 40',(new[0],head[1])).fetchall()
        for key,job,root,oldid,encoded in batch:
            if oldid==gid:continue
            old=c.execute('SELECT candidate,body FROM vpnl_history_generations WHERE id=?',(oldid,)).fetchone()
            invalid=c.execute('SELECT 1 FROM vpnl_history_invalidations WHERE generation_id=?',(oldid,)).fetchone()
            if old and not invalid and not _conflicts(c,old[0],new[1],E.strict_object(old[1])['through']):continue
            prior=E.strict_object(encoded);b=prior['body']
            current=c.execute('SELECT r.alias,v.payload FROM vpnl_roots r JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head WHERE r.id=?',(root,)).fetchone()
            if not current: _consumer_review(c,root,new[0]);continue
            present=E.strict_object(current[1]);current_fin=present.get('financial_receipt');revoked=present.get('financial_revocation_receipt')
            from .models import NotificationPayload
            if current_fin and not E.payload_valid(NotificationPayload(**present)):
                _consumer_review(c,root,new[0]);continue
            if current_fin and E.digest(current_fin['body'])!=E.digest(b):continue
            if not current_fin and not E.verify(revoked,'HISTORY'):continue
            proof=context.authority._attest('HISTORY_CHANGE',dict(scope=E.strict_object(context.scope_json),invalidated_generation=oldid,successor_generation=gid))
            if not E.verify(proof,'HISTORY_CHANGE'):raise ValueError('history_signer')
            invalid_row=c.execute('SELECT successor FROM vpnl_history_invalidations WHERE generation_id=?',(oldid,)).fetchone()
            if invalid_row is None:c.execute('INSERT INTO vpnl_history_invalidations VALUES(?,?,?)',(oldid,gid,E.canonical(proof)))
            item=dict(order_id=b['order_id'],financial_receipt_digest=E.digest(b),accounting_digest=b['accounting_digest'],reason='ACCOUNTING_EVIDENCE_REVOKED')
            withdrawal=context.authority._attest('HISTORY',dict(scope=b['scope'],complete=False,generation_id=oldid,successor_generation=gid,consumer_root=root,consumer_alias=current[0],withdrawals=[item]))
            state='PENDING' if E.verify(prior,'FINANCIAL') and prior['key_id']==withdrawal['key_id'] else 'KEY_REVIEW'
            if E.canonical(revoked)==E.canonical(withdrawal):state='PUBLISHED'
            action=E.digest([key,gid])
            row=c.execute('SELECT edge_id,successor,receipt FROM vpnl_consumer_actions WHERE id=?',(action,)).fetchone()
            values=(key,gid,E.canonical(withdrawal))
            if row is None:c.execute('INSERT INTO vpnl_consumer_actions VALUES(?,?,?,?,?)',(action,*values,state))
            elif row!=values:raise ValueError('consumer_action_conflict')
            _mirror_consumer_action(c,action,job,oldid,prior,withdrawal,state)
            if state=='PENDING':c.execute("UPDATE vpnl_jobs SET state='PENDING',due=0,financial_state='WAIT_EVIDENCE',completed=NULL WHERE id=? AND state IN ('DONE','PENDING')",(job,))
        pending=c.execute("SELECT 1 FROM vpnl_consumer_actions a JOIN vpnl_history_consumer_edges e ON e.id=a.edge_id WHERE e.scope=? AND a.successor=? AND a.state IN ('PENDING','KEY_REVIEW') LIMIT 1",(new[0],gid)).fetchone()
        review=c.execute('SELECT 1 FROM vpnl_consumer_reviews WHERE resolved=0 AND (scope=? OR scope IS NULL) LIMIT 1',(new[0],)).fetchone()
        scan=c.execute('SELECT complete FROM vpnl_consumer_scan WHERE id=1').fetchone()[0]
        more=len(batch)==40 or pending is not None or review is not None or not scan
        c.execute('UPDATE vpnl_history_heads SET cursor=?,reconciling=? WHERE scope=?',(batch[-1][0] if batch else head[1],int(more),new[0]))


def pending_withdrawal(store,order_id,context,job_id=None):
    if job_id is None:return None
    with store._connect() as c:
        rows=c.execute("SELECT a.id,a.receipt,e.root_id FROM vpnl_consumer_actions a JOIN vpnl_history_consumer_edges e ON e.id=a.edge_id WHERE a.state='PENDING' AND e.scope=? AND e.job_id=? ORDER BY a.id LIMIT 40",(E.digest(E.strict_object(context.scope_json)),job_id)).fetchall()
        for key,encoded,root in rows:
            receipt=E.strict_object(encoded)
            if not E.verify(receipt,'HISTORY') or receipt['body']['withdrawals'][0]['order_id']!=order_id:continue
            row=c.execute('SELECT v.payload FROM vpnl_roots r JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head WHERE r.id=?',(root,)).fetchone()
            current=E.strict_object(row[0]) if row else {};prior=current.get('financial_receipt')
            if E.verify(prior,'FINANCIAL') and E.digest(prior['body'])==receipt['body']['withdrawals'][0]['financial_receipt_digest']:return receipt
            if E.canonical(current.get('financial_revocation_receipt'))==E.canonical(receipt):
                c.execute("UPDATE vpnl_consumer_actions SET state='PUBLISHED' WHERE id=?",(key,));continue
            c.execute("UPDATE vpnl_consumer_actions SET state='NOT_CURRENT' WHERE id=?",(key,))
        return None


def correction_alias(store,payload,job_id):
    if not E.payload_valid(payload):return None
    gid=payload.financial_receipt['body'].get('generation_id')
    with store._connect_read() as c:
        rows=c.execute("SELECT a.receipt,r.alias,v.payload FROM vpnl_consumer_actions a JOIN vpnl_history_consumer_edges e ON e.id=a.edge_id JOIN vpnl_roots r ON r.id=e.root_id JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head WHERE a.state='PUBLISHED' AND a.successor=? AND e.job_id=? ORDER BY a.id LIMIT 40",(gid,job_id)).fetchall()
        for receipt,alias,encoded in rows:
            if E.canonical(E.strict_object(encoded).get('financial_revocation_receipt'))==receipt:return alias
    return None


def record_published_withdrawal(store,payload):
    with store._connect() as c:
        current=c.execute('SELECT r.id,v.payload FROM vpnl_roots r JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head WHERE r.alias=?',(payload.event_id,)).fetchone()
        if not current:return
        saved=E.strict_object(current[1])
        if payload.event_type=='revoked' and E.verify(payload.financial_revocation_receipt,'HISTORY'):
            if E.canonical(saved.get('financial_revocation_receipt'))!=E.canonical(payload.financial_revocation_receipt):return
            c.execute("UPDATE vpnl_consumer_actions SET state='PUBLISHED' WHERE receipt=? AND state='PENDING'",(E.canonical(payload.financial_revocation_receipt),))
            c.execute("UPDATE vpnl_history_revocations SET state='PUBLISHED' WHERE id IN (SELECT id FROM vpnl_consumer_actions WHERE receipt=? AND state='PUBLISHED')",(E.canonical(payload.financial_revocation_receipt),))
        elif E.payload_valid(payload):
            if E.canonical(saved.get('financial_receipt'))!=E.canonical(payload.financial_receipt):return
            gid=payload.financial_receipt['body'].get('generation_id')
            c.execute("UPDATE vpnl_consumer_actions SET state='CORRECTED' WHERE successor=? AND state='PUBLISHED' AND edge_id IN (SELECT id FROM vpnl_history_consumer_edges WHERE root_id=?)",(gid,current[0]))


def consumer_job_pending(store,job_id):
    with store._connect_read() as c:return c.execute("SELECT 1 FROM vpnl_consumer_actions a JOIN vpnl_history_consumer_edges e ON e.id=a.edge_id WHERE e.job_id=? AND a.state IN ('PENDING','PUBLISHED','KEY_REVIEW') LIMIT 1",(job_id,)).fetchone() is not None


def _number(value):return json.loads(E.canonical(Decimal(str(value))))

def _conflicts(c,old,new,through):
    changed=c.execute("SELECT 1 FROM vpnl_history_trades o LEFT JOIN vpnl_history_trades n ON n.candidate=? AND n.id=o.id WHERE o.candidate=? AND (n.id IS NULL OR n.stamp!=o.stamp OR n.order_id!=o.order_id OR n.role!=o.role OR n.amount!=o.amount OR n.price!=o.price OR n.fee!=o.fee) LIMIT 1",(new,old)).fetchone()
    added=c.execute('SELECT 1 FROM vpnl_history_trades n LEFT JOIN vpnl_history_trades o ON o.candidate=? AND o.id=n.id WHERE n.candidate=? AND n.stamp<=? AND o.id IS NULL LIMIT 1',(old,new,through)).fetchone()
    return bool(changed or added)


def refresh_dependencies(store,client_factory):
    """One persisted dependency per refresh, including completed producer jobs.

    Temporary read failure neither invalidates a generation nor requests a resend.
    """
    if _PROVIDER is None:return
    import time
    now=time.time()
    with store._connect() as c:
        schedule=c.execute('SELECT cursor,due FROM vpnl_history_refresh WHERE id=1').fetchone()
        if schedule and schedule[1]>now:return
        after=schedule[0] if schedule else ''
        row=c.execute('SELECT id,job_id FROM vpnl_history_dependencies WHERE id>? ORDER BY id LIMIT 1',(after,)).fetchone()
        if row is None:row=c.execute('SELECT id,job_id FROM vpnl_history_dependencies ORDER BY id LIMIT 1').fetchone()
        if row is None:return
        c.execute('INSERT INTO vpnl_history_refresh VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor,due=excluded.due',(row[0],now+60))
        job=c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(row[1],)).fetchone()
    if not job:_PROVIDER.state='HISTORY_REVIEW_MISSING_PRODUCER';return
    try:
        data=store.parse_job(job[0]);info=SimpleNamespace(**data['info']);client=client_factory(info.exchange,info)
        context=E.context_for(info.exchange,data['account'],info,client)
        if context is None:return
        from .verifier import FillVerifier
        fill=FillVerifier().verify_order_result(SimpleNamespace(client=client),info.exchange,info.unified_symbol,data['result'],info,context)
        _PROVIDER.advance(store,fill,info,context,row[1])
    except Exception:_PROVIDER.state='HISTORY_NOT_READY'


def _trade_cache_valid(c,candidate,fill):
    actual=c.execute('SELECT id,stamp,order_id,role,amount,price,fee FROM vpnl_history_trades WHERE candidate=? AND order_id=? ORDER BY id LIMIT 40',(candidate,fill.order_id)).fetchall()
    expected=[(t.id,t.timestamp,fill.order_id,fill.execution_role,_number(t.amount),_number(t.price),_number(t.fee)) for t in fill.trades]
    return sorted(expected)==actual


def install_consumers(c):
    for sql in (
        'CREATE TABLE IF NOT EXISTS vpnl_history_proofs(id TEXT PRIMARY KEY,scope TEXT NOT NULL,generation_id TEXT NOT NULL,financial TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS vpnl_history_consumer_edges(id TEXT PRIMARY KEY,proof_id TEXT NOT NULL,scope TEXT NOT NULL,job_id TEXT NOT NULL,root_id TEXT NOT NULL,revision INTEGER NOT NULL,UNIQUE(proof_id,job_id,root_id,revision))',
        'CREATE INDEX IF NOT EXISTS vpnl_consumer_scope ON vpnl_history_consumer_edges(scope,id)',
        'CREATE TABLE IF NOT EXISTS vpnl_consumer_actions(id TEXT PRIMARY KEY,edge_id TEXT NOT NULL,successor TEXT NOT NULL,receipt TEXT NOT NULL,state TEXT NOT NULL,UNIQUE(edge_id,successor))',
        'CREATE TABLE IF NOT EXISTS vpnl_consumer_reviews(root_id TEXT PRIMARY KEY,scope TEXT,reason TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0)',
        'CREATE TABLE IF NOT EXISTS vpnl_consumer_scan(id INTEGER PRIMARY KEY CHECK(id=1),cursor INTEGER NOT NULL,highwater INTEGER NOT NULL,complete INTEGER NOT NULL)'):
        c.execute(sql)
    if c.execute('SELECT 1 FROM vpnl_consumer_scan WHERE id=1').fetchone() is None:
        highwater=c.execute('SELECT coalesce(max(rowid),0) FROM vpnl_revisions').fetchone()[0]
        c.execute('INSERT INTO vpnl_consumer_scan VALUES(1,0,?,?)',(highwater,int(highwater==0)))
    for table in ('vpnl_history_proofs','vpnl_history_consumer_edges'):
        for verb in ('UPDATE','DELETE'):c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_consumer_evidence'); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS vpnl_consumer_action_immutable BEFORE UPDATE OF id,edge_id,successor,receipt ON vpnl_consumer_actions BEGIN SELECT RAISE(ABORT,'immutable_consumer_action'); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS vpnl_consumer_action_retain BEFORE DELETE ON vpnl_consumer_actions BEGIN SELECT RAISE(ABORT,'retained_consumer_action'); END")
    c.execute('CREATE INDEX IF NOT EXISTS vpnl_consumer_root_revision ON vpnl_history_consumer_edges(root_id,revision)')
    c.execute("CREATE TRIGGER IF NOT EXISTS vpnl_consumer_edge_post_guard BEFORE UPDATE OF status ON vpnl_deliveries WHEN NEW.status='POSTING' AND EXISTS(SELECT 1 FROM vpnl_revisions v WHERE v.root_id=NEW.root_id AND v.revision=NEW.revision AND json_extract(v.payload,'$.financial_receipt.body.generation_id') IS NOT NULL) AND NOT EXISTS(SELECT 1 FROM vpnl_history_consumer_edges e WHERE e.root_id=NEW.root_id AND e.revision=NEW.revision) BEGIN SELECT RAISE(ABORT,'verified_consumer_required'); END")

    c.execute("CREATE TRIGGER IF NOT EXISTS vpnl_consumer_revision_guard BEFORE INSERT ON vpnl_revisions WHEN json_extract(NEW.payload,'$.financial_receipt.body.generation_id') IS NOT NULL AND ((SELECT complete FROM vpnl_consumer_scan WHERE id=1)=0 OR EXISTS(SELECT 1 FROM vpnl_consumer_reviews WHERE resolved=0 AND (scope IS NULL OR scope=(SELECT scope FROM vpnl_history_generations WHERE id=json_extract(NEW.payload,'$.financial_receipt.body.generation_id'))))) BEGIN SELECT RAISE(ABORT,'consumer_recovery_pending'); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS vpnl_consumer_post_guard BEFORE UPDATE OF status ON vpnl_deliveries WHEN NEW.status='POSTING' AND EXISTS(SELECT 1 FROM vpnl_revisions v WHERE v.root_id=NEW.root_id AND v.revision=NEW.revision AND json_extract(v.payload,'$.financial_receipt.body.generation_id') IS NOT NULL) AND ((SELECT complete FROM vpnl_consumer_scan WHERE id=1)=0 OR EXISTS(SELECT 1 FROM vpnl_consumer_reviews WHERE resolved=0 AND scope IS NULL)) BEGIN SELECT RAISE(ABORT,'consumer_recovery_pending'); END")


def _put(c,table,columns,values):
    row=c.execute('SELECT '+','.join(columns)+' FROM '+table+' WHERE '+columns[0]+'=?',(values[0],)).fetchone()
    if row is None:c.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in values)+')',values);return True
    if row!=tuple(values):raise ValueError('consumer_identity_conflict')
    return False


def _consumer_review(c,root,scope):
    row=c.execute('SELECT scope FROM vpnl_consumer_reviews WHERE root_id=?',(root,)).fetchone()
    if row is None:c.execute("INSERT INTO vpnl_consumer_reviews VALUES(?,?,'consumer_binding_review',0)",(root,scope))
    c.execute('UPDATE vpnl_history_heads SET reconciling=1 WHERE scope=?',(scope,))
    c.execute("INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)",(E.digest(['consumer-review',root]),scope,'consumer_binding_review',E.digest(root)))


def record_consumer(store,c,p,root,revision,binding):
    if p.financial_receipt is None or 'generation_id' not in p.financial_receipt['body']:return
    from .models import NotificationPayload
    from .ledger import identity
    if not E.payload_valid(p):raise ValueError('consumer_financial')
    fin=p.financial_receipt;b=fin['body'];gid=b['generation_id'];scope=E.digest(b['scope'])
    if not _generation_check(store,gid,historical=True):raise ValueError('consumer_generation')
    if E.canonical(binding.get('scope'))!=E.canonical(b['scope']) or binding.get('producer',{}).get('kind')!='JOB':raise ValueError('consumer_producer')
    job=binding['producer']['id'];row=c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(job,)).fetchone()
    if row is None:raise ValueError('consumer_job_missing')
    data=store.parse_job(row[0]);order=str(data['result'].get('id') or data['result'].get('uuid') or '')
    if data['account']!=b['scope']['account'] or (data['info']['exchange'],data['info']['unified_symbol'],order)!=(p.exchange,p.symbol,p.order_id):raise ValueError('consumer_job_binding')
    aliases={identity(job,'final',[],2),identity(job,'partial',sorted(t['id'] for t in p.execution_receipt['body']['fill']['trades']),2)}
    if p.event_id not in aliases and not c.execute('SELECT 1 FROM vpnl_history_consumer_edges WHERE root_id=? AND job_id=?',(root,job)).fetchone():raise ValueError('consumer_alias_review')
    actual=c.execute('SELECT alias FROM vpnl_roots WHERE id=?',(root,)).fetchone()
    if actual is None or actual[0]!=p.event_id:raise ValueError('consumer_root')
    proof=E.digest([fin['key_id'],b]);_put(c,'vpnl_history_proofs',('id','scope','generation_id','financial'),(proof,scope,gid,E.canonical(fin)))
    edge=E.digest([proof,job,root,revision])
    created=_put(c,'vpnl_history_consumer_edges',('id','proof_id','scope','job_id','root_id','revision'),(edge,proof,scope,job,root,revision))
    business=store.order_binding(b['scope'],order,data['info']);store._register_order_identity(c,business,p.execution_receipt,job)
    _retain_proof_job(c,fin,scope,gid,job)
    c.execute('INSERT OR IGNORE INTO vpnl_quarantine VALUES(?,?,?,?,0)',(E.digest(['history-retain',scope]),scope,'history_dependency_retained',gid))
    if created and c.execute('SELECT 1 FROM vpnl_history_invalidations WHERE generation_id=?',(gid,)).fetchone():
        c.execute("UPDATE vpnl_history_heads SET cursor='',reconciling=1 WHERE scope=?",(scope,))


def recover_consumers(store):
    """Bounded, restartable verification of immutable legacy v4 published facts.

    Old dependency rows are retained. Aliases are never treated as authority.
    """
    from .models import NotificationPayload
    with store._connect() as c:
        cursor,highwater,complete=c.execute('SELECT cursor,highwater,complete FROM vpnl_consumer_scan WHERE id=1').fetchone()
        if complete:return 0
        batch=c.execute('SELECT rowid,root_id,revision,payload,financial_digest FROM vpnl_revisions WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 40',(cursor,highwater)).fetchall()
        for seq,root,rev,encoded,fd in batch:
            scope=None
            try:
                p=NotificationPayload(**E.strict_object(encoded))
                if not p.financial_receipt or 'generation_id' not in p.financial_receipt['body']:continue
                scope=E.digest(p.financial_receipt['body']['scope'])
                bound=c.execute('SELECT binding,binding_digest FROM vpnl_root_bindings WHERE root_id=?',(root,)).fetchone()
                if not bound or E.digest(E.strict_object(bound[0]))!=bound[1]:raise ValueError('consumer_binding')
                if E.digest(store._facts(p))!=fd:raise ValueError('consumer_digest')
                delivery=c.execute('SELECT wire,wire_digest,status FROM vpnl_deliveries WHERE root_id=? AND revision=?',(root,rev)).fetchone()
                if delivery is None or hashlib.sha256(delivery[0]).hexdigest()!=delivery[1] or store._wire(p)!=delivery[0]:raise ValueError('consumer_wire')
                if delivery[2]=='SENT' and not c.execute("SELECT 1 FROM vpnl_delivery_attempts WHERE delivery_id=(SELECT id FROM vpnl_deliveries WHERE root_id=? AND revision=?) AND revision=? AND wire_digest=? AND phase='SENT' LIMIT 1",(root,rev,rev,delivery[1])).fetchone():raise ValueError('consumer_delivery_receipt')
                c.execute('SAVEPOINT consumer_row')
                try:record_consumer(store,c,p,root,rev,E.strict_object(bound[0]))
                except Exception:c.execute('ROLLBACK TO consumer_row');c.execute('RELEASE consumer_row');raise
                c.execute('RELEASE consumer_row')
            except (ValueError,KeyError,TypeError):_consumer_review(c,root,scope)
        last=batch[-1][0] if batch else highwater
        c.execute('UPDATE vpnl_consumer_scan SET cursor=?,complete=? WHERE id=1',(last,int(last>=highwater)))
        return len(batch)


def _retain_proof_job(c,financial,scope,gid,job):
    encoded=E.canonical(financial)
    row=c.execute('SELECT id FROM vpnl_history_dependencies WHERE job_id=? AND generation_id=? AND financial=?',(job,gid,encoded)).fetchone()
    if row:return row[0]
    key=E.digest([financial['key_id'],financial['body'],job])
    _put(c,'vpnl_history_dependencies',('id','scope','generation_id','job_id','financial'),(key,scope,gid,job,encoded))
    return key


def _mirror_consumer_action(c,key,job,gid,financial,withdrawal,state):
    # Additive compatibility projection only. Consumer edges/actions remain the
    # authority; no old row or historical receipt is repurposed.
    dep=_retain_proof_job(c,financial,E.digest(financial['body']['scope']),gid,job)
    row=c.execute('SELECT dependency,receipt FROM vpnl_history_revocations WHERE id=?',(key,)).fetchone()
    expected=(dep,E.canonical(withdrawal))
    if row is None:c.execute('INSERT INTO vpnl_history_revocations VALUES(?,?,?,?)',(key,*expected,state))
    elif row!=expected:raise ValueError('legacy_action_projection_conflict')


def recover_consumer_actions(store):
    """Repair commit/acknowledgement gaps from immutable root heads, <=40 edges.

    This acknowledges local publication, never Discord delivery or another issuer.
    """
    from .models import NotificationPayload
    with store._connect() as c:
        row=c.execute("SELECT cursor FROM vpnl_recovery_cursor WHERE lane='consumer-actions'").fetchone();after=row[0] if row else ''
        sql="SELECT a.id,a.edge_id,a.successor,a.receipt,a.state,e.root_id,p.financial,e.job_id FROM vpnl_consumer_actions a JOIN vpnl_history_consumer_edges e ON e.id=a.edge_id JOIN vpnl_history_proofs p ON p.id=e.proof_id WHERE a.state IN ('PENDING','PUBLISHED') AND a.id>? ORDER BY a.id LIMIT 40"
        batch=c.execute(sql,(after,)).fetchall()
        if not batch:batch=c.execute(sql,('',)).fetchall()
        for key,edge,gid,encoded,state,root,prior,job in batch:
            try:
                receipt=E.strict_object(encoded);old=E.strict_object(prior)
                if not E.verify(receipt,'HISTORY') or receipt['key_id']!=old['key_id']:continue
                current=c.execute('SELECT v.payload FROM vpnl_roots r JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head WHERE r.id=?',(root,)).fetchone()
                if not current:continue
                p=NotificationPayload(**E.strict_object(current[0]));next_state=None
                if E.canonical(p.financial_revocation_receipt)==encoded:next_state='PUBLISHED'
                elif E.payload_valid(p):
                    b=p.financial_receipt['body']
                    if p.financial_receipt['key_id']==old['key_id'] and b.get('generation_id')==gid and b['order_id']==old['body']['order_id'] and E.canonical(b['scope'])==E.canonical(old['body']['scope']):next_state='CORRECTED'
                if next_state is None and state=='PENDING':
                    c.execute("UPDATE vpnl_jobs SET state='PENDING',due=0,financial_state='WAIT_EVIDENCE',completed=NULL WHERE id=? AND state='DONE'",(job,))
                if next_state is not None:
                    c.execute('UPDATE vpnl_consumer_actions SET state=? WHERE id=?',(next_state,key))
                    if next_state=='PUBLISHED':c.execute("UPDATE vpnl_history_revocations SET state='PUBLISHED' WHERE id=?",(key,))
            except (ValueError,KeyError,TypeError):
                _consumer_review(c,root,None)
        c.execute("INSERT INTO vpnl_recovery_cursor VALUES('consumer-actions',?) ON CONFLICT(lane) DO UPDATE SET cursor=excluded.cursor",(batch[-1][0] if batch else '',))
        return len(batch)
