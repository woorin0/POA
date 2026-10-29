"""POA-produced notification estimates. No ledger, signer, collector or sender.

Host entry capability authorizes local notification observations, NOT exchange
account truth. Stored snapshots never constitute EXECUTION/FINANCIAL evidence.
"""
import ast,hashlib,inspect,json,marshal,re,time,uuid
from decimal import Decimal,localcontext
from pathlib import Path
from .evidence import canonical,digest,account_reference,payload_valid,payload_fill_valid

_TOKEN=object()
_SAFE=re.compile(r'^[^\x00-\x1f\x7f]{1,128}$')
_SECRET=re.compile(r'(?i)(bearer|api.?key|secret|passphrase|webhook|canary|authorization|https?://|%[0-9a-f]{2})')
SNAP_KEYS={'version','exchange','source_ref','symbol','strategy','order_id','side','quantity','notional','average','fees','fees_known','confirmed','terminal','references','price_source','account_configured'}

def producer_token(entry):
    # Local code capability, not a sandbox against malicious Python/DB operators.
    main=Path(__file__).resolve().parents[2]/'main.py'
    if not callable(entry) or getattr(getattr(entry,'__code__',None),'co_name',None)!='log' or Path(entry.__code__.co_filename).resolve()!=main:raise ValueError('unregistered_notification_producer')
    tree=ast.parse(main.read_text(encoding='utf-8-sig'));node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='log')
    code=compile(ast.Module(body=[node],type_ignores=[]),str(main),'exec')
    expected=next(c for c in code.co_consts if hasattr(c,'co_code') and c.co_name=='log')
    if entry.__code__.co_code!=expected.co_code or entry.__code__.co_consts!=expected.co_consts:raise ValueError('producer_code_mismatch')
    return _TOKEN

def producer_allowed(token):return token is _TOKEN

def number(v):
    if isinstance(v,bool) or not isinstance(v,(str,int,float,Decimal)):raise ValueError('observation_number')
    n=Decimal(str(v))
    if not n.is_finite() or len(n.as_tuple().digits)>80 or abs(n.as_tuple().exponent)>80:raise ValueError('observation_number')
    return n

def text(v):
    if not isinstance(v,str) or not _SAFE.fullmatch(v) or _SECRET.search(v):raise ValueError('observation_reference')
    return v

def norm(n):
    v=n if isinstance(n,Decimal) else number(n)
    if not v.is_finite() or len(v.as_tuple().digits)>1024 or abs(v.as_tuple().exponent)>1024:raise ValueError('calculated_number')
    return json.loads(canonical(v))

def case_key(s):return digest(['poa-notification-event-v1',s['exchange'],s['source_ref'],s['symbol'],s['strategy'],s['order_id']])

def case_for_job(job):
    if job.get('notice_intake'):return case_key(job['notice_snapshot'])
    info=job['info'];ref=job.get('source_account_ref')
    order=job.get('result',{}).get('id')
    if not isinstance(ref,str) or not re.fullmatch('[0-9a-f]{64}',ref) or not order or info.get('is_futures'):return None
    return case_key(dict(exchange=info['exchange'],source_ref=ref,symbol=info['unified_symbol'],strategy=info.get('strategy_id') or info.get('order_name') or 'default',order_id=str(order)))

def notification_scope(s):
    return dict(exchange=s['exchange'],account=s['source_ref'],subaccount='poa-producer-observation',market_type='spot',instrument=s['symbol'],symbol=s['symbol'],settlement=s['symbol'].split('/')[1],contract_size='1',position_side='LONG',position_cycle='tradingview-signal-stream' if s.get('version')==2 else 'notification-observed-stream',book=s['strategy'])

def source_scope(exchange,raw,symbol,order):
    """Compare actual source fields; never fill them from the request."""
    base,quote=symbol.split('/')
    allowed={symbol,base+quote,base+'-'+quote}
    if exchange=='UPBIT':allowed.add(quote+'-'+base)
    found=False
    for obj in (raw,raw.get('info') if isinstance(raw.get('info'),dict) else {}):
        for key in ('symbol','market','instId'):
            value=obj.get(key)
            if value in (None,''):continue
            if not isinstance(value,str) or value.upper() not in allowed:return 'CONFLICT'
            found=True
        if obj.get('exchange') is not None and str(obj['exchange']).upper()!=exchange:return 'CONFLICT'
        if obj.get('category') is not None and str(obj['category']).lower()!='spot':return 'CONFLICT'
        if obj.get('instType') is not None and str(obj['instType']).upper()!='SPOT':return 'CONFLICT'
    for t in raw.get('trades') or []:
        if not isinstance(t,dict):continue
        for key in ('symbol','market','instId'):
            value=t.get(key)
            if value in (None,''):continue
            if not isinstance(value,str) or value.upper() not in allowed:return 'CONFLICT'
            if str(t.get('order') or t.get('order_uuid') or '')==order:found=True
    return 'MATCH' if found else 'MISSING'


def capture(exchange,raw,info,ref,account_configured):
    """Only extracts observed executions; request price/amount never become fills."""
    if info.get('tv_signal') is not None:
        from .tradingview_signal import capture as signal_capture
        return signal_capture(exchange,raw,info,ref,account_configured)
    if not isinstance(raw,dict):raw={}
    side='buy' if info.get('is_buy') else 'sell'
    oid=raw.get('id') or raw.get('uuid') or ''
    s=dict(version=1,exchange=exchange,source_ref=ref,symbol=info['unified_symbol'],strategy=info.get('strategy_id') or info.get('order_name') or 'default',order_id=str(oid),side=side,quantity='0',notional='0',average=None,fees={},fees_known=False,confirmed=False,terminal=False,references=[],price_source='UNAVAILABLE',account_configured=account_configured)
    try:
        if exchange not in ('BINANCE','BYBIT','OKX','UPBIT') or info.get('is_futures') or info.get('is_coinm'):return validate_snapshot(s)
        text(s['order_id']);text(s['strategy']);text(s['symbol'])
        quote=s['symbol'].split('/')[1]
        if quote not in ('USDT','KRW') or (exchange=='UPBIT')!=(quote=='KRW'):return validate_snapshot(s)
        raw_side=str(raw.get('side') or '').lower()
        if raw_side and raw_side not in (('buy','bid') if side=='buy' else ('sell','ask')):return validate_snapshot(s)
        status=str(raw.get('status') or raw.get('state') or '').lower()
        s['terminal']=status in ('closed','filled','done','canceled','cancelled','cancel')
        if status in ('rejected','failed','unknown','pending','expired'):return validate_snapshot(s)
        match=source_scope(exchange,raw,s['symbol'],s['order_id'])
        if match!='MATCH':
            if match=='CONFLICT':s['price_source']='SCOPE_REJECTED'
            return validate_snapshot(s)
        rows=raw.get('trades') or []
        if not isinstance(rows,list) or len(rows)>40:return validate_snapshot(s)
        totals={};known=True;qty=Decimal(0);proceeds=Decimal(0);seen=set()
        def fee(f):
            if not isinstance(f,dict) or f.get('cost') is None or not isinstance(f.get('currency'),str) or not re.fullmatch('[A-Z0-9]{2,12}',f['currency']):raise ValueError('fee_missing')
            return f['currency'],number(f['cost'])
        with localcontext() as ctx:
            ctx.prec=512
            for t in rows:
                if not isinstance(t,dict):raise ValueError('trade_schema')
                tid=str(t.get('id') or t.get('uuid') or '')
                text(tid)
                if tid in seen:raise ValueError('duplicate_trade')
                seen.add(tid)
                parent=t.get('order') or t.get('order_uuid')
                if parent is not None and str(parent)!=s['order_id']:raise ValueError('trade_order')
                if t.get('symbol') is not None and t['symbol']!=s['symbol']:raise ValueError('trade_symbol')
                if t.get('market') is not None and t['market']!=('KRW-'+s['symbol'].split('/')[0]):raise ValueError('trade_symbol')
                p=number(t.get('price'));q=number(t.get('amount',t.get('volume')))
                if p<=0 or q<=0:raise ValueError('trade_value')
                if t.get('funds') is not None and number(t['funds'])!=p*q:raise ValueError('trade_funds')
                qty+=q;proceeds+=q*p
                try:
                    c,v=fee(t.get('fee'));totals[c]=totals.get(c,Decimal(0))+v
                except ValueError:known=False
            summary_q=raw.get('filled',raw.get('executed_volume'))
            if rows:
                if summary_q is not None and number(summary_q)!=qty:raise ValueError('quantity_conflict')
                s.update(quantity=norm(qty),notional=norm(proceeds),average=norm(proceeds/qty),references=sorted(digest(['trade',v]) for v in seen),price_source='ACTUAL_FILLS')
            elif s['terminal']:
                q=number(summary_q);p=number(raw.get('average'))
                if q<=0 or p<=0:raise ValueError('summary_execution_missing')
                s.update(quantity=norm(q),notional=norm(q*p),average=norm(p),references=[digest(['completed_order',s['order_id']])],price_source='COMPLETED_ORDER_AVERAGE')
            else:return validate_snapshot(s)
            summaries=[]
            if raw.get('fee') is not None:
                c,v=fee(raw['fee']);summaries.append({c:v})
            if raw.get('fees') is not None:
                if not isinstance(raw['fees'],list) or not raw['fees']:raise ValueError('fee_schema')
                m={}
                for f in raw['fees']:
                    c,v=fee(f);m[c]=m.get(c,Decimal(0))+v
                summaries.append(m)
            if exchange=='UPBIT' and raw.get('paid_fee') is not None:summaries.append({quote:number(raw['paid_fee'])})
            if summaries and any(v!=summaries[0] for v in summaries):totals={};known=False
            elif exchange=='UPBIT':
                # Entire terminal order's paid_fee; never per-trade duplication.
                totals=summaries[0] if summaries and s['terminal'] else {};known=bool(summaries) and s['terminal']
            elif rows and known:
                if summaries and summaries[0]!=totals:totals={};known=False
            elif not rows or not totals:
                totals=summaries[0] if summaries else {};known=bool(summaries)
            # Some actual trade fees may be known even when other fills omit them.
            s.update(fees={k:norm(v) for k,v in totals.items()},fees_known=known,confirmed=s['terminal'] and number(s['quantity'])>0 and bool(s['references']))
        return validate_snapshot(s)
    except (ValueError,TypeError,KeyError,ArithmeticError):
        s.update(confirmed=False,fees={},fees_known=False)
        return validate_snapshot(s)

def validate_snapshot(s):
    if isinstance(s,dict) and s.get('version')==2:
        from .tradingview_signal import validate
        return validate(s)
    if not isinstance(s,dict) or set(s)!=SNAP_KEYS or s['version']!=1:raise ValueError('snapshot_schema')
    if s['exchange'] not in ('BINANCE','BYBIT','OKX','UPBIT') or not re.fullmatch('[0-9a-f]{64}',s['source_ref']):raise ValueError('snapshot_scope')
    for k in ('symbol','strategy'):
        text(s[k])
    if s['order_id']:text(s['order_id'])
    if s['side'] not in ('buy','sell') or any(type(s[k]) is not bool for k in ('confirmed','terminal','fees_known','account_configured')):raise ValueError('snapshot_flags')
    if s['price_source'] not in ('ACTUAL_FILLS','COMPLETED_ORDER_AVERAGE','UNAVAILABLE','SCOPE_REJECTED'):raise ValueError('snapshot_source')
    if not isinstance(s['references'],list) or len(s['references'])>40 or any(not re.fullmatch('[0-9a-f]{64}',x) for x in s['references']) or len(set(s['references']))!=len(s['references']):raise ValueError('snapshot_references')
    if not isinstance(s['fees'],dict) or len(s['fees'])>8 or any(not re.fullmatch('[A-Z0-9]{2,12}',c) for c in s['fees']):raise ValueError('snapshot_fees')
    out=dict(s,quantity=norm(s['quantity']),notional=norm(s['notional']),average=norm(s['average']) if s['average'] is not None else None,fees={k:norm(v) for k,v in s['fees'].items()})
    if number(out['quantity'])<0 or number(out['notional'])<0:raise ValueError('snapshot_amount')
    if out['confirmed'] and (not out['terminal'] or number(out['quantity'])<=0 or out['average'] is None or number(out['average'])<=0 or not out['references'] or not out['order_id']):raise ValueError('snapshot_confirmation')
    return json.loads(canonical(out))

def install(c):
    for sql in (
      'CREATE TABLE IF NOT EXISTS vpnl_notice_inputs(intake_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,job_id TEXT NOT NULL,sequence INTEGER NOT NULL,snapshot TEXT NOT NULL,snapshot_digest TEXT NOT NULL,scope TEXT NOT NULL,sidecar_id TEXT NOT NULL)',
      'CREATE INDEX IF NOT EXISTS vpnl_notice_case ON vpnl_notice_inputs(case_id,sequence)',
      'CREATE INDEX IF NOT EXISTS vpnl_notice_scope ON vpnl_notice_inputs(scope,sequence)',
      'CREATE TABLE IF NOT EXISTS vpnl_notice_members(delivery_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,mode TEXT NOT NULL)',
      'CREATE TABLE IF NOT EXISTS vpnl_notice_reviews(case_id TEXT PRIMARY KEY,reason TEXT NOT NULL)',
      'CREATE TABLE IF NOT EXISTS vpnl_notice_successors(intake_id TEXT PRIMARY KEY,record TEXT NOT NULL,record_digest TEXT NOT NULL)',
      'CREATE TABLE IF NOT EXISTS vpnl_notice_upbit_observations(intake_id TEXT PRIMARY KEY,record TEXT NOT NULL,record_digest TEXT NOT NULL)'):
        c.execute(sql)
    for table in ('vpnl_notice_inputs','vpnl_notice_members','vpnl_notice_successors','vpnl_notice_upbit_observations'):
        for action in ('UPDATE','DELETE'):c.execute("CREATE TRIGGER IF NOT EXISTS %s_%s_immutable BEFORE %s ON %s BEGIN SELECT RAISE(ABORT,'immutable_notice_origin'); END"%(table,action,action,table))

def notice_input(c,iid,*,effective=True):
    row=c.execute('SELECT case_id,job_id,sequence,snapshot,snapshot_digest,scope,sidecar_id FROM vpnl_notice_inputs WHERE intake_id=?',(iid,)).fetchone()
    if row is None:raise ValueError('notice_input_missing')
    case,job,seq,raw,sd,scope,sid=row;s=validate_snapshot(json.loads(raw))
    if canonical(s)!=raw or digest(s)!=sd or case_key(s)!=case or canonical(notification_scope(s))!=scope:raise ValueError('notice_input_conflict')
    result=dict(intake_id=iid,case=case,job=job,seq=seq,snapshot=s,digest=sd,scope=json.loads(scope),sidecar_id=sid)
    if effective:
        row=c.execute('SELECT record,record_digest FROM vpnl_notice_successors WHERE intake_id=?',(iid,)).fetchone()
        if row:
            record=json.loads(row[0])
            if canonical(record)!=row[0] or digest(record)!=row[1]:raise ValueError('successor_digest')
            snapshot=validate_successor(c,result,record)
            result.update(snapshot=snapshot,digest=digest(snapshot),trusted_scope=record['trusted_scope'])
        else:
            row=c.execute('SELECT record,record_digest FROM vpnl_notice_upbit_observations WHERE intake_id=?',(iid,)).fetchone()
            if row:
                record=json.loads(row[0])
                if canonical(record)!=row[0] or digest(record)!=row[1]:raise ValueError('upbit_observation_digest')
                snapshot=validate_upbit_observation(c,result,record)
                result.update(snapshot=snapshot,digest=digest(snapshot),trusted_scope=record['scope'])
    return result

def job_matches(store,c,job,binding):
    data=store.parse_job(c.execute('SELECT data FROM vpnl_jobs WHERE id=?',(job,)).fetchone()[0])
    n=notice_input(c,data['notice_intake'],effective=False)
    return n['job']==job and canonical(store.order_binding(n['scope'],n['snapshot']['order_id'],data['info']))==canonical(binding)

def bound_root_matches(store,c,binding,data):
    n=notice_input(c,data['notice_intake'],effective=False);s=n['snapshot']
    if binding.get('notice_case')!=n['case'] or binding['order_id']!=s['order_id']:return False
    scope=binding['scope'];mapped=account_reference(s['exchange'],s['source_ref'])
    return scope['exchange']==s['exchange'] and scope['symbol']==s['symbol'] and scope['market_type']=='spot' and scope['account'] in (s['source_ref'],mapped) and binding['producer']=={'kind':'JOB','id':n['job']}

def calculate(c,iid):
    original=notice_input(c,iid,effective=False)
    if original['snapshot'].get('version')==2:
        from .tradingview_signal import calculate as signal_calculate
        return signal_calculate(c,iid)
    n=notice_input(c,iid);s=n['snapshot'];quote=s['symbol'].split('/')[1]
    result=dict(status='UNAVAILABLE',reason='ACQUISITION_UNAVAILABLE',case_id=n['case'],intake_id=iid,snapshot_digest=n['digest'],scope=n['scope'],quantity=s['quantity'],entry_price=None,exit_price=s['average'],amount=None,roi=None,currency=quote,fee_mode='MISSING',ignored_assets=[],basis='POA_OBSERVED_ORDERS')
    if not s['confirmed'] or s['side']!='sell' or not s['account_configured']:return result
    if s['strategy']=='default' or (s['exchange']=='UPBIT' and s['strategy']=='주문'):result['reason']='STRATEGY_UNAVAILABLE';return result
    if c.execute('SELECT 1 FROM vpnl_notice_reviews WHERE case_id=?',(n['case'],)).fetchone():return result
    qty=principal=fees=Decimal(0);complete=True;ignored=set();count=0;trusted_scope=None
    # Projection over existing notification snapshots, never an allocation ledger.
    sql="""SELECT x.snapshot,x.snapshot_digest,x.intake_id FROM vpnl_notice_inputs x WHERE x.scope=? AND x.sequence<=? AND x.sequence=(SELECT max(y.sequence) FROM vpnl_notice_inputs y WHERE y.case_id=x.case_id AND y.sequence<=?) ORDER BY (SELECT min(z.sequence) FROM vpnl_notice_inputs z WHERE z.case_id=x.case_id),x.sequence"""
    with localcontext() as ctx:
        ctx.prec=512
        stream=c.execute(sql,(canonical(n['scope']),n['seq'],n['seq']))
        while True:
            batch=stream.fetchmany(40)
            if not batch:break
            for raw,sd,other_id in batch:
                t=validate_snapshot(json.loads(raw))
                if digest(t)!=sd:raise ValueError('notice_record_digest')
                effective=notice_input(c,other_id);t=effective['snapshot']
                if effective.get('trusted_scope') is not None:
                    if trusted_scope is not None and trusted_scope!=effective['trusted_scope']:
                        result['reason']='EXECUTION_SCOPE_CONFLICT';return result
                    trusted_scope=effective['trusted_scope']
                if (t['exchange'],t['source_ref'],t['symbol'],t['strategy'])!=(s['exchange'],s['source_ref'],s['symbol'],s['strategy']):continue
                count+=1
                if count>4096:result['reason']='NOTIFICATION_HISTORY_LIMIT';return result
                if not t['confirmed']:
                    result['reason']='UNCONFIRMED_HISTORY';return result
                if c.execute('SELECT 1 FROM vpnl_notice_reviews WHERE case_id=?',(case_key(t),)).fetchone():result['reason']='OBSERVATION_CONFLICT';return result
                q=number(t['quantity']);value=number(t['notional']);f=number(t['fees'].get(quote,'0'));full=t['fees_known'] and set(t['fees'])<={quote}
                ignored.update(k for k in t['fees'] if k!=quote)
                if t['side']=='buy':qty+=q;principal+=value;fees+=f;complete=complete and full
                else:
                    if qty<=0 or q>qty:result['reason']='ACQUISITION_QUANTITY_GAP';return result
                    alloc=principal*q/qty;entryfee=fees*q/qty
                    if other_id==iid:
                        all_fees=complete and full;known=entryfee+f
                        mode='COMPLETE' if all_fees else 'PARTIAL' if known else 'MISSING'
                        amount=value-alloc-known;cost=alloc+entryfee
                        if cost<=0:return result
                        result.update(status='ESTIMATED',reason='OBSERVED_MATCH',entry_price=norm(alloc/q),exit_price=norm(value/q),amount=norm(amount),roi=norm(amount/cost*100),fee_mode=mode,ignored_assets=sorted(ignored))
                        return result
                    principal-=alloc;fees-=entryfee;qty-=q
                    if qty==0:principal=fees=Decimal(0);complete=True;ignored=set()
    return result

def attach(p,store,data):
    with store._connect_read() as c:r=calculate(c,data['notice_intake'])
    p.notification_type='ESTIMATED';p.estimated_pnl=r;p.event_type='estimated'
    p.financial_receipt=p.execution_receipt=p.accounting_receipt=p.outcome_receipt=None
    p.net_pnl=p.roi_pct=p.cost_basis=None
    from .models import VerificationStatus
    p.pnl_verification_status=p.roi_verification_status=VerificationStatus.ESTIMATED if r['status']=='ESTIMATED' else VerificationStatus.UNVERIFIED
    return p


def successor_snapshot(origin,fill,scope):
    from .evidence import execution_valid
    from .models import FillStatus
    if not execution_valid(fill,scope):raise ValueError('successor_execution')
    s=origin['snapshot']
    if (fill.exchange,fill.symbol,fill.order_id,fill.order_side)!=(s['exchange'],s['symbol'],s['order_id'],s['side']):raise ValueError('successor_order_scope')
    if s['price_source']=='SCOPE_REJECTED' or scope.get('market_type')!='spot' or scope.get('settlement')!=s['symbol'].split('/')[1]:raise ValueError('successor_market_scope')
    if account_reference(s['exchange'],s['source_ref'])!=scope.get('account') or not scope.get('position_cycle') or not scope.get('book'):raise ValueError('successor_account_scope')
    if fill.status not in (FillStatus.FILLED,FillStatus.CANCELED) or fill.filled_quantity<=0 or fill.average_fill_price is None or fill.average_fill_price<=0:raise ValueError('successor_incomplete')
    raw=dict(id=fill.order_id,symbol=fill.symbol,side=fill.order_side,status='closed' if fill.status==FillStatus.FILLED else 'canceled',filled=norm(fill.filled_quantity),average=norm(fill.average_fill_price),fees=[dict(currency=k,cost=norm(v)) for k,v in fill.fees.items()])
    if not raw['fees']:raw.pop('fees')
    snap=capture(s['exchange'],raw,dict(unified_symbol=s['symbol'],strategy_id=s['strategy'],is_buy=s['side']=='buy'),s['source_ref'],True)
    snap['fees_known']=bool(fill.fees_complete)
    if fill.trades:snap['references']=sorted(digest(['trade',t.id]) for t in fill.trades)
    if not snap['confirmed']:raise ValueError('successor_incomplete')
    return validate_snapshot(snap)


def validate_successor(c,origin,record):
    from .models import VerifiedFill
    from .intake import submission_for
    from .evidence import execution_valid
    if set(record)!={'version','intake_id','origin_digest','job_id','case_id','ack_digest','db_id','cutover_id','producer','trusted_scope','execution','snapshot','facts_digest'} or record['version']!=1:raise ValueError('successor_schema')
    if (record['intake_id'],record['origin_digest'],record['job_id'],record['case_id'],record['producer'])!=(origin['intake_id'],origin['digest'],origin['job'],origin['case'],origin['snapshot']['strategy']):raise ValueError('successor_origin')
    ack=submission_for(c,record['intake_id'])
    if ack is None or (ack['digest'],ack['job_id'],ack['db_id'],ack['cutover_id'])!=(record['ack_digest'],origin['job'],record['db_id'],record['cutover_id']):raise ValueError('successor_lineage')
    if tuple(c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone())!=(record['db_id'],record['cutover_id']):raise ValueError('successor_restore')
    receipt=record['execution'];scope=record['trusted_scope']
    fill=VerifiedFill(**receipt['body']['fill'],execution_receipt=receipt)
    snapshot=successor_snapshot(origin,fill,scope)
    if canonical(receipt['body']['scope'])!=canonical(scope) or canonical(snapshot)!=canonical(record['snapshot']) or record['facts_digest']!=digest([scope,snapshot]):raise ValueError('successor_binding')
    return snapshot



def validate_upbit_observation(c,origin,record):
    from .upbit_spot_read import current_upbit_notification_host
    from .intake import submission_for
    host=current_upbit_notification_host(strategy=origin['snapshot']['strategy'])
    required={'kind','intake_id','origin_digest','job_id','case_id','producer','ack_digest','db_id','cutover_id','host_binding','scope','source_digest','facts','facts_digest','snapshot','fee_source'}
    if type(record) is not dict or set(record)!=required or record['kind']!='UPBIT_ESTIMATED_READ_V1':raise ValueError('upbit_observation_schema')
    if host is None or not host.pinned() or record['host_binding']!=host.binding or canonical(record['scope'])!=canonical(host.scope_facts()):raise ValueError('upbit_observation_host_unavailable')
    s=origin['snapshot']
    if (record['intake_id'],record['origin_digest'],record['job_id'],record['case_id'],record['producer'])!=(origin['intake_id'],origin['digest'],origin['job'],origin['case'],s['strategy']):raise ValueError('upbit_observation_origin')
    if s['source_ref']!=host.source_account_ref or s['strategy']!=host.strategy or s['price_source']=='SCOPE_REJECTED':raise ValueError('upbit_observation_scope')
    ack=submission_for(c,record['intake_id'])
    if ack is None or (ack['digest'],ack['job_id'],ack['db_id'],ack['cutover_id'])!=(record['ack_digest'],origin['job'],record['db_id'],record['cutover_id']):raise ValueError('upbit_observation_ack')
    if tuple(c.execute('SELECT db_id,cutover_id FROM vpnl_submission_lineage WHERE id=1').fetchone())!=(record['db_id'],record['cutover_id']):raise ValueError('upbit_observation_restore')
    facts=record['facts'];snapshot=host.snapshot(facts)
    if (facts['id'],facts['symbol'],facts['side'])!=(s['order_id'],s['symbol'],s['side']):raise ValueError('upbit_observation_order')
    fee_source='ORDER_LIFETIME_PAID_FEE' if facts['paid_fee'] is not None else 'MISSING'
    if digest(facts)!=record['facts_digest'] or canonical(snapshot)!=canonical(record['snapshot']) or record['fee_source']!=fee_source or not re.fullmatch('[0-9a-f]{64}',record['source_digest']):raise ValueError('upbit_observation_facts')
    return snapshot
