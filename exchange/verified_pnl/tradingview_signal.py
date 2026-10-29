"""Authenticated-webhook signal estimates, never exchange fills or financial proof.
Only notification snapshots are projected; no account balances or financial ledger.
"""
import json
from decimal import Decimal, localcontext

def parse(value):
    from .estimated_pnl import number as raw_number, norm, text
    def number(v):
        try:return raw_number(v)
        except (ArithmeticError,TypeError,ValueError):raise ValueError('tv_number') from None
    if isinstance(value,str):
        if len(value)>2048:raise ValueError('tv_schema')
        def pairs(items):
            out={}
            for k,v in items:
                if k in out:raise ValueError('tv_duplicate_field')
                out[k]=v
            return out
        value=json.loads(value,object_pairs_hook=pairs)
    required={'kind','price','quantity','action','order_id','signal_id','strategy_id'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'entry_price','entry_source'}:raise ValueError('tv_schema')
    if value['kind'] not in ('strategy_order_fill','alert') or value['action'] not in ('buy','sell'):raise ValueError('tv_source')
    out={k:text(value[k]) for k in ('kind','action','order_id','signal_id','strategy_id')}
    for k in ('price','quantity'):
        n=number(value[k])
        if n<=0 or n>Decimal('1e24') or isinstance(value[k],float):raise ValueError('tv_number_use_decimal_string')
        out[k]=norm(n)
    if any('{{' in v or '}}' in v for v in out.values()):raise ValueError('tv_unresolved_placeholder')
    if 'entry_price' in value or 'entry_source' in value:
        if value.get('entry_source')!='strategy.position_avg_price' or value['action']!='sell':raise ValueError('tv_entry_source')
        n=number(value.get('entry_price'))
        if n<=0 or n>Decimal('1e24') or isinstance(value['entry_price'],float):raise ValueError('tv_entry_price')
        out.update(entry_price=norm(n),entry_source=value['entry_source'])
    return out

def capture(exchange,raw,info,ref,configured):
    from .estimated_pnl import norm,number,validate_snapshot
    s=parse(info['tv_signal'])
    if info.get('is_futures') or info.get('is_coinm') or s['strategy_id']!=info.get('strategy_id') or s['action']!=('buy' if info.get('is_buy') else 'sell'):raise ValueError('tv_request_binding')
    with localcontext() as ctx:
        ctx.prec=512;value=norm(number(s['price'])*number(s['quantity']))
    return validate_snapshot(dict(version=2,signal=s,exchange=exchange,source_ref=ref,symbol=info['unified_symbol'],strategy=s['strategy_id'],order_id=str(raw.get('id') or raw.get('uuid') or ''),side=s['action'],quantity=s['quantity'],notional=value,average=s['price'],fees={},fees_known=False,confirmed=False,terminal=False,references=[],price_source='UNAVAILABLE',account_configured=configured))

def validate(snapshot):
    from .estimated_pnl import validate_snapshot,canonical,number
    signal=parse(snapshot['signal']);base=dict(snapshot);base.pop('signal');base['version']=1
    base=validate_snapshot(base)
    with localcontext() as ctx:
        ctx.prec=512
        if base['side']!=signal['action'] or base['strategy']!=signal['strategy_id'] or number(base['quantity'])!=number(signal['quantity']) or number(base['average'])!=number(signal['price']) or number(base['notional'])!=number(signal['quantity'])*number(signal['price']):raise ValueError('tv_snapshot_binding')
    if base['confirmed'] or base['terminal'] or base['fees'] or base['fees_known'] or base['references'] or base['price_source']!='UNAVAILABLE':raise ValueError('tv_not_execution')
    if base['symbol'].count('/')!=1 or base['symbol'].split('/')[1] not in ('KRW','USDT') or not base['order_id']:raise ValueError('tv_spot_scope')
    return json.loads(canonical(dict(base,version=2,signal=signal)))

def calculate(c,iid):
    from .estimated_pnl import notice_input,canonical,digest,number,norm,case_key
    n=notice_input(c,iid,effective=False);s=validate(n['snapshot']);sig=s['signal']
    r=dict(status='UNAVAILABLE',reason='TV_ACQUISITION_UNAVAILABLE',case_id=n['case'],intake_id=iid,snapshot_digest=n['digest'],scope=n['scope'],quantity=s['quantity'],entry_price=None,exit_price=s['average'],amount=None,roi=None,currency=s['symbol'].split('/')[1],fee_mode='MISSING',ignored_assets=[],basis='TRADINGVIEW_SIGNAL',strategy=s['strategy'],source='TradingView 신호 기록',unmatched_quantity='0')
    if s['side']!='sell' or not s['account_configured']:return r
    qty=principal=Decimal(0);seen=set();had_buy=False;count=0
    sql='SELECT intake_id FROM vpnl_notice_inputs WHERE scope=? AND sequence<=? ORDER BY sequence'
    with localcontext() as ctx:
        ctx.prec=512;cur=c.execute(sql,(canonical(n['scope']),n['seq']))
        while True:
            batch=cur.fetchmany(40)
            if not batch:break
            for (other,) in batch:
                count+=1
                if count>4096:r['reason']='TV_HISTORY_LIMIT';return r
                item=notice_input(c,other,effective=False);t=validate(item['snapshot'])
                if digest(t)!=item['digest']:raise ValueError('tv_record_digest')
                if c.execute('SELECT 1 FROM vpnl_notice_reviews WHERE case_id=?',(case_key(t),)).fetchone():r['reason']='TV_OBSERVATION_CONFLICT';return r
                event=t['signal']['signal_id']
                if event in seen:raise ValueError('tv_duplicate_stream')
                seen.add(event);q=number(t['quantity']);price=number(t['average'])
                if t['side']=='buy':qty+=q;principal+=q*price;had_buy=True;continue
                matched=min(q,qty);entry=principal/qty if qty else None
                if other==iid:
                    if not had_buy and qty==0 and sig.get('entry_price'):
                        matched=q;entry=number(sig['entry_price']);r['source']='명시적 전략 평균 진입가격·TradingView 매도 신호'
                    if matched<=0 or entry is None:return r
                    amount=(price-entry)*matched;cost=entry*matched
                    r.update(status='ESTIMATED',reason='TV_SIGNAL_MATCH',quantity=norm(matched),unmatched_quantity=norm(q-matched),entry_price=norm(entry),amount=norm(amount),roi=norm(amount/cost*100))
                    return r
                if matched:principal-=entry*matched;qty-=matched
                if not qty:principal=Decimal(0)
    return r

def process(store,key,lease,data,settings):
    from .dispatcher import make_payload
    from .models import VerifiedFill,PnlCalculationResult
    from .estimated_pnl import attach
    from .ledger import identity
    from types import SimpleNamespace
    s=validate(data['notice_snapshot'])
    if s['side']=='buy':
        with store._connect() as c:
            if c.execute("UPDATE vpnl_jobs SET state='OBSERVED_RECORDED',financial_state='SIGNAL_ONLY',job_lease=NULL WHERE id=? AND state='VERIFYING' AND job_lease=?",(key,lease)).rowcount!=1:raise ValueError('tv_lease')
        return
    fill=VerifiedFill(exchange=s['exchange'],symbol=s['symbol'],order_id=s['order_id'],source='TRADINGVIEW_SIGNAL_NOT_EXECUTION')
    p=make_payload(identity(key,'estimated',[],2),fill,SimpleNamespace(**data['info']),PnlCalculationResult(),None,settings)
    p=attach(p,store,data)
    ok=store.enqueue(p,max_attempts=settings.DISCORD_RETRY_MAX_ATTEMPTS if settings.DISCORD_RETRY_ENABLED else 1,job_claim=(key,lease))
    with store._connect() as c:
        state='OBSERVED_NOTIFIED' if ok or store.notice_decided(key) else 'OBSERVED_REVIEW'
        if c.execute("UPDATE vpnl_jobs SET state=?,financial_state='SIGNAL_ONLY',job_lease=NULL WHERE id=? AND state='VERIFYING' AND job_lease=?",(state,key,lease)).rowcount!=1:raise ValueError('tv_lease')


def render(payload):
    from exchange.utility.discord_embed import (format_price_display,format_quantity_display,format_roi_display,sanitize_discord_text,enforce_embed_bounds,Embed,COLOR_ORDER_AMBIGUOUS)
    e=payload.estimated_pnl;ccy=e['currency'];base=payload.symbol.split('/')[0]
    money=lambda n,signed=False:format_price_display(n,currency=ccy,signed=signed) if n is not None else '미확인'
    lines=['전략: '+sanitize_discord_text(e.get('strategy',''),max_len=64),'거래소: '+payload.exchange,'종목: '+sanitize_discord_text(payload.symbol,max_len=32),'매수 신호 평균가: '+money(e.get('entry_price')),'매도 신호가격: '+money(e.get('exit_price')),'매칭 수량: '+format_quantity_display(e.get('quantity'),base)]
    if e.get('status')=='ESTIMATED':
        lines+=['신호 기준 예상 손익: '+money(e['amount'],True),'예상 ROI: '+format_roi_display(e['roi'])]
    else:lines+=['신호 기준 예상 손익: 계산 불가','예상 ROI: 미확정']
    if Decimal(e.get('unmatched_quantity','0'))>0:lines+=['원가 미확인 수량: '+format_quantity_display(e['unmatched_quantity'],base)]
    lines+=['가격 출처: '+e['source'],'수수료: 미반영','안내: TradingView 신호 기준 예상치 · 실제 체결·정산손익 아님']
    return enforce_embed_bounds(Embed(title='TradingView 신호 기준 예상 손익',description='\n'.join(lines),color=COLOR_ORDER_AMBIGUOUS))
