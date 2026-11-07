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
    from .order_fill_read import CONTRACT
    if getattr(p,'notification_type',None)==CONTRACT:
        from .order_fill_confirmation import render
        return render(p)
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
    """Fee-exclusive quote sum from signed, complete normalized execution trades.

    Trade quantities already include the verifier's contract-size conversion.
    Reconcile VWAP using that verifier's existing 512-digit arithmetic policy;
    no exchange-price tolerance or inferred execution is introduced here.
    """
    from decimal import Decimal, localcontext
    from .evidence import payload_fill_valid,canonical
    if not payload_fill_valid(payload):return None
    try:
        body=payload.execution_receipt['body'];fill,scope=body['fill'],body['scope']
        trades=fill.get('trades')
        if fill.get('trades_complete') is not True or not isinstance(trades,list) or not 1<=len(trades)<=4000:return None
        market=fill.get('market_type')
        if market not in ('spot','linear') or scope.get('market_type')!=market:return None
        if not scope.get('account') or not scope.get('symbol') or not scope.get('instrument'):return None
        if (fill.get('exchange'),fill.get('symbol'))!=(scope.get('exchange'),scope['symbol']):return None
        if fill.get('scope_data') is not None and canonical(fill['scope_data'])!=canonical(scope):return None
        currency=scope.get('settlement')
        if currency not in ('KRW','USDT','USDC') or fill.get('pnl_currency')!=currency:return None
        if scope['symbol'].split('/')[-1].split(':')[0]!=currency:return None
        def number(value):
            n=Decimal(str(value))
            if not n.is_finite() or len(n.as_tuple().digits)>1100 or abs(n.as_tuple().exponent)>1100:raise ValueError('amount_bounds')
            return n
        with localcontext() as ctx:
            ctx.prec=1100
            qty=number(fill['filled_quantity']);avg=number(fill['average_fill_price']);factor=number(scope['contract_size'])
            if min(qty,avg,factor)<=0 or number(fill['contract_size'])!=factor or (market=='spot' and factor!=1):return None
            total=Decimal(0);summed=Decimal(0);ids=set()
            for t in trades:
                if not isinstance(t,dict) or not isinstance(t.get('id'),str) or not t['id'] or t['id'] in ids:return None
                ids.add(t['id'])
                refs=[t[k] for k in ('order','order_id') if t.get(k) is not None]
                if not refs or any(ref!=fill['order_id'] for ref in refs):return None
                px,amount=number(t['price']),number(t['amount'])
                if min(px,amount)<=0:return None
                value=px*amount
                if t.get('cost') is not None and number(t['cost'])!=value:return None
                total+=value;summed+=amount
            if summed!=qty or not total.is_finite() or total<=0:return None
            with localcontext() as approved:
                approved.prec=512
                if total/qty!=avg:return None
            return total,currency,market
    except (KeyError,TypeError,ValueError,ArithmeticError,AttributeError):return None


def _order_action(side):
    value=str(side or '').strip().lower()
    if any(term in value for term in ('숏 진입','롱 종료','매도','sell')):
        return '매도'
    if any(term in value for term in ('숏 종료','롱 진입','매수','buy')):
        return '매수'
    return '체결'


def apply_amount_only(embed,payload):
    """Fresh, marked notification projection; no historical wire is rewritten."""
    if AMOUNT_ONLY_MARKER not in (payload.verification_source or '').splitlines():return embed
    from exchange.utility.discord_embed import (Embed,format_price_display,format_quantity_display,
        enforce_embed_bounds,sanitize_discord_text,format_kst_full,normalize_display_symbol,extract_base_asset)
    from .evidence import outcome_rejected
    clean=lambda value:sanitize_discord_text(str(value or ''),max_len=128)
    old=embed.data;original=old.get('description','');action=_order_action(getattr(payload,'side',None))
    lines=[]
    strategy=getattr(payload,'strategy_id',None) or getattr(payload,'order_name',None)
    if strategy:lines.append('전략: '+clean(strategy))
    else:
        previous=next((line for line in original.splitlines() if line.startswith(('전략:','전략/주문명:'))),None)
        if previous:lines.append(clean(previous))
    # Explicit request labels are context only. Signal/reference prices, virtual
    # inventory, old PnL fields, footer and arbitrary fields are never projected.
    # Preserve only bounded, nonfinancial context from the existing renderer.
    context_prefixes=('요청수량:','매수수량:','매도수량:','미체결수량:',
        '원가 미확인 수량:','매칭 수량:','잔여수량:','상태:','실제 체결:',
        '사유:','오류 코드:','코드:','신호 시각:','서버:','배포:')
    for line in original.splitlines():
        if line.startswith(context_prefixes) and not any(word in line.lower() for word in ('손익','roi','수익률','차익')):
            lines.append(clean(line))
    estimate=getattr(payload,'estimated_pnl',None) or {}
    if estimate.get('basis') in ('TRADINGVIEW_SIGNAL','TRADINGVIEW_BUY_SIGNAL'):
        lines.append('수량 기준: TradingView 신호 (실체결 수량 아님)')
    if estimate.get('status')=='UNAVAILABLE':
        lines.append('원가 확인: 계산 불가')
    observed_confirmation='체결 확인 · 금융 검증 미확정' in original and colour(payload)==CONFIRMED_FILL
    proof=_verified_executed_amount(payload)
    label='실제 총 '+action+'액' if action!='체결' else '실제 총 체결액'
    if proof is not None:
        amount,currency,market=proof;fill=payload.execution_receipt['body']['fill'];base=extract_base_asset(normalize_display_symbol(fill['symbol']))
        lines+=['실제 평균 체결가: '+format_price_display(fill['average_fill_price'],currency=currency),
                '실제 체결수량: '+format_quantity_display(fill['filled_quantity'],base_asset=base)+(' (기초자산 환산)' if market=='linear' else ''),
                label+(' (계약 명목가치)' if market=='linear' else '')+': '+format_price_display(amount,currency=currency)+' (수수료 제외)']
        fees=fill.get('fees')
        if fill.get('fees_complete') is True and isinstance(fees,dict) and 0<len(fees)<=16:
            lines.append('수수료/리베이트: '+', '.join(format_price_display(value,currency=clean(ccy)) for ccy,value in sorted(fees.items())))
        else:lines.append('수수료: 확인 중')
        lines.append('체결 상태: '+('부분체결 확인 (확인된 누적 체결만)' if fill.get('status')=='PARTIALLY_FILLED' else '체결 확인'))
    else:
        fee_note=('일부 미확인 · 일부 수수료 미반영' if estimate.get('fee_mode')=='PARTIAL' else '미확인 · 수수료 미반영' if estimate.get('fee_mode')=='MISSING' or '수수료 미반영' in original.splitlines() else '확인 중')
        lines+=['실제 평균 체결가: 확인 중','실제 체결수량: 확인 중',label+': 확인 중 (실체결 금액 미검증)',
                '수수료: '+fee_note,'체결 상태: '+('거래소 거절 확인' if outcome_rejected(payload) else '체결 확인 · 금융 검증 미확정' if observed_confirmation else '확인 중'),
                '신호가격·요청금액·추정치를 실제 체결대금으로 표시하지 않습니다']
    reason=getattr(payload,'exit_reason',None);reason=getattr(reason,'value',reason)
    if action=='매도' and reason:lines.append('청산 사유: '+clean(reason))
    ts=getattr(payload,'fill_timestamp',None)
    if ts is None:ts=getattr(payload,'event_timestamp',None)
    when=next((clean(line) for line in original.splitlines() if line.startswith(('시각:','🕒 ')) and 'KST' in line),None) if proof is None or ts is None else None
    if when:lines.append(when)
    elif ts is not None:lines.append('시각: '+format_kst_full(ts))
    title=old.get('title','')
    if any(word in title.lower() for word in ('손익','roi','pnl','수익률','차익')):title=' · '.join(clean(getattr(payload,k,'')) for k in ('exchange','symbol','side'))
    return enforce_embed_bounds(Embed(title=title,description='\n'.join(lines),color=old.get('color',PENDING)))


def new_unverified_amount_embed(embed,exchange,symbol,side,strategy=None,timestamp=None):
    """Fresh legacy/error calls only; never used by stored Outbox rendering."""
    if exchange not in ('UPBIT','BINANCE','BYBIT','OKX','BITGET'):return embed
    from types import SimpleNamespace
    p=SimpleNamespace(exchange=exchange,symbol=symbol,side=side,order_name=strategy,
        verification_source=AMOUNT_ONLY_MARKER,execution_receipt=None,outcome_receipt=None,event_timestamp=timestamp)
    return apply_amount_only(embed,p)
