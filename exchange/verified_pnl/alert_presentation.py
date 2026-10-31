"""Versioned presentation only. Admission, not a DTO flag, selects fill colour."""
import json
CONFIRMED_FILL=0x2563EB
ERROR=0xDC2626
ESTIMATED=0x8B5CF6
PENDING=0x64748B
PREFIX='POA_PRESENTATION_V4:'


def admit(c,p,job):
    from .evidence import payload_fill_valid,outcome_rejected
    from .notification_origin import PREFIX as origin
    colour=PENDING
    if payload_fill_valid(p):colour=CONFIRMED_FILL
    elif outcome_rejected(p):colour=ERROR
    elif p.notification_type=='ESTIMATED':
        r=p.estimated_pnl or {}
        if r.get('status')=='ESTIMATED':colour=ESTIMATED
        if job and r.get('basis') in ('POA_BUY_OBSERVATION','POA_OBSERVED_ORDERS'):
            from .estimated_pnl import notice_input
            n=notice_input(c,r['intake_id'])
            if n['job']==job.get('_presentation_job') and n['snapshot']['confirmed']:colour=CONFIRMED_FILL
    source='\n'.join(line for line in (p.verification_source or '').splitlines() if not line.startswith(PREFIX))
    head,sep,tail=source.partition(origin)
    p.verification_source=head+'\n'+PREFIX+json.dumps({'colour':colour},separators=(',',':'))+(sep+tail if sep else '')
    return p


def colour(p):
    for line in (p.verification_source or '').splitlines():
        if line.startswith(PREFIX):
            try:
                v=json.loads(line[len(PREFIX):])
                if set(v)=={'colour'} and type(v['colour']) is int and v['colour'] in (CONFIRMED_FILL,ERROR,ESTIMATED,PENDING):return v['colour']
            except (ValueError,TypeError):pass
    return None


def apply(embed,p):
    value=colour(p)
    if value is None:return embed
    embed.color=value
    embed.data['color']=value
    if value==CONFIRMED_FILL and p.notification_type=='ESTIMATED':
        description=embed.data.get('description','')
        description=description.replace('체결 기록 관측 · 검증 미확정','체결 확인 · 금융 검증 미확정')
        if '체결 확인 · 금융 검증 미확정' not in description:description+='\n체결 확인 · 금융 검증 미확정'
        embed.description=description
        embed.data['description']=description
    return embed



def legacy(embed,exchange,result,info,settings):
    """Only new legacy spot calls; no historical Outbox payload is passed here."""
    if exchange not in ('UPBIT','BINANCE','BYBIT','OKX') or getattr(info,'is_futures',False):return embed
    value=PENDING
    try:
        from .estimated_pnl import capture
        from .ledger import identity
        key=getattr(settings,exchange+'_KEY',None)
        fields={k:getattr(info,k,None) for k in ('unified_symbol','is_buy','is_futures','is_coinm','strategy_id','order_name')}
        snapshot=capture(exchange,result if isinstance(result,dict) else {},fields,identity(exchange,key or 'unconfigured'),bool(key))
        if snapshot['confirmed']:value=CONFIRMED_FILL
    except Exception:pass
    embed.color=value;embed.data['color']=value
    return embed
