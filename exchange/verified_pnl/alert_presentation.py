"""Versioned presentation only. Admission, not a DTO flag, selects fill colour."""
import json
CONFIRMED_FILL=0x2563EB
ERROR=0xDC2626
ESTIMATED=0x8B5CF6
PENDING=0x64748B
PREFIX='POA_PRESENTATION_V4:'
AMOUNT_ONLY_MARKER='POA_EXECUTION_AMOUNT_ONLY_V1:1'


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
    source='\n'.join(line for line in (p.verification_source or '').splitlines() if not line.startswith(PREFIX) and line!=AMOUNT_ONLY_MARKER)
    head,sep,tail=source.partition(origin)
    p.verification_source=head+'\n'+PREFIX+json.dumps({'colour':colour},separators=(',',':'))+'\n'+AMOUNT_ONLY_MARKER+(sep+tail if sep else '')
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


def _verified_executed_amount(payload):
    """Only signed, same-order COMPLETE exchange execution trades; fee-exclusive quote amount.

    This intentionally does not trust payload.notional_amount, order request cost,
    TradingView price, estimated_pnl, or unsigned order acknowledgements.
    """
    from decimal import Decimal, InvalidOperation, localcontext
    from .evidence import payload_fill_valid
    if not payload_fill_valid(payload):
        return None
    try:
        body=payload.execution_receipt['body']
        fill,scope=body['fill'],body['scope']
        if fill.get('trades_complete') is not True or not isinstance(fill.get('trades'),list) or not fill['trades']:
            return None
        if fill.get('market_type') not in ('spot','linear') or scope.get('market_type')!=fill['market_type']:
            return None
        currency=str(scope.get('settlement','')).upper()
        if currency not in ('KRW','USDT','USDC'):
            return None
        if str(fill.get('pnl_currency','')).upper()!=currency:
            return None
        with localcontext() as ctx:
            ctx.prec=1100
            qty=Decimal(str(fill['filled_quantity']))
            avg=Decimal(str(fill['average_fill_price']))
            if not qty.is_finite() or qty<=0 or not avg.is_finite() or avg<=0:
                return None
            total=Decimal(0)
            summed_qty=Decimal(0)
            ids=set()
            for trade in fill['trades']:
                if not isinstance(trade,dict) or not trade.get('id') or str(trade['id']) in ids:
                    return None
                ids.add(str(trade['id']))
                px=Decimal(str(trade['price']))
                amount=Decimal(str(trade['amount']))
                if not px.is_finite() or not amount.is_finite() or px<=0 or amount<=0:
                    return None
                total+=px*amount
                summed_qty+=amount
            if summed_qty!=qty or total/qty!=avg or not total.is_finite() or total<=0:
                return None
            return total,currency,fill['market_type']
    except (KeyError,TypeError,ValueError,InvalidOperation,ArithmeticError,AttributeError):
        return None


def _order_action(side):
    value=str(side or '').strip().lower()
    if any(term in value for term in ('숏 진입','롱 종료','매도','sell')):
        return '매도'
    if any(term in value for term in ('숏 종료','롱 진입','매수','buy')):
        return '매수'
    return '체결'


def apply_amount_only(embed,payload):
    """Notification-only projection for newly admitted messages.

    Frozen historical Outbox wires do not contain the marker, and trade/order/PnL
    calculation, reconciliation, retries, and financial receipts are unchanged.
    """
    if AMOUNT_ONLY_MARKER not in (payload.verification_source or '').splitlines():
        return embed
    from exchange.utility.discord_embed import format_price_display,enforce_embed_bounds
    old=embed.data.get('description','')
    excluded_amount_prefixes=('매수금액:','매도금액:','총 매수액:','총 매도액:','실제 총 매수액:','실제 총 매도액:','실제 총 체결액:')
    lines=[]
    for line in old.splitlines():
        stripped=line.strip()
        low=stripped.lower()
        if any(word in low for word in ('손익','수익률','roi','pnl','차익','펀딩비:','금융 검증')):
            continue
        if stripped.startswith(excluded_amount_prefixes) or stripped.startswith('ℹ️ 매수 원가'):
            continue
        lines.append(line)
    action=_order_action(getattr(payload,'side',None))
    proof=_verified_executed_amount(payload)
    if proof is not None:
        amount,currency,market=proof
        label='실제 총 '+action+'액' if action!='체결' else '실제 총 체결액'
        if market!='spot':
            label+=' (계약 명목가치)'
        value=format_price_display(amount,currency=currency)
        amount_line=label+': '+value+' (수수료 제외)'
    else:
        label='실제 총 '+action+'액' if action!='체결' else '실제 총 체결액'
        amount_line=label+': 확인 중 (실체결 금액 미검증)'
    insert=next((i+1 for i,line in enumerate(lines) if ('체결수량:' in line or '매수수량:' in line or '매도수량:' in line)),None)
    if insert is None:
        insert=next((i+1 for i,line in enumerate(lines) if line.startswith('전략:') or line.startswith('전략/주문명:')),0)
    lines.insert(insert,amount_line)
    if proof is None:
        lines.insert(insert+1,'신호가격·요청금액·추정치를 실제 체결대금으로 표시하지 않습니다')
    embed.description='\n'.join(lines)
    embed.data['description']=embed.description
    return enforce_embed_bounds(embed)
