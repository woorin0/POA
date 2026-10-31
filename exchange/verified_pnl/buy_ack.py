"""POA order-acceptance notifications; no execution or financial authority."""
import json,sqlite3


def result(c,iid):
    from .estimated_pnl import notice_input
    n=notice_input(c,iid,effective=False);s=n['snapshot']
    if s['version']!=1 or s['side']!='buy' or s['confirmed'] or s['terminal'] or s['price_source']!='ORDER_ACK_ONLY' or not s['account_configured']:raise ValueError('buy_ack_origin')
    if s['exchange']=='UPBIT':
        from .upbit_spot_read import canonical_uuid
        canonical_uuid(s['order_id'])
    return dict(status='UNAVAILABLE',reason='BUY_ACCEPTED_PENDING',case_id=n['case'],intake_id=iid,snapshot_digest=n['digest'],scope=n['scope'],quantity='0',entry_price=None,exit_price=None,amount=None,roi=None,currency=s['symbol'].split('/')[1],fee_mode='MISSING',ignored_assets=[],basis='POA_BUY_ACK',strategy=s['strategy'],signal_price=None,virtual_quantity=None,virtual_average=None)


def emit(store,key,lease,data,settings):
    from .dispatcher import make_payload,warning
    from .models import VerifiedFill,PnlCalculationResult
    from .ledger import identity
    from .estimated_pnl import attach
    from types import SimpleNamespace
    try:
        with store._connect_read() as c:result(c,data['notice_intake'])
    except (ValueError,KeyError,TypeError):return False
    fill=VerifiedFill(exchange=data['info']['exchange'],symbol=data['info']['unified_symbol'],order_id=data['result']['id'],source='POA_ORDER_ACK_NOT_EXECUTION')
    p=make_payload(identity(key,'estimated',[],2),fill,SimpleNamespace(**data['info']),PnlCalculationResult(),None,settings)
    p=attach(p,store,data,settings,pending_buy=True);p.event_version=1
    try:
        ok=store.enqueue(p,max_attempts=settings.DISCORD_RETRY_MAX_ATTEMPTS if settings.DISCORD_RETRY_ENABLED else 1,job_claim=(key,lease))
        with store._connect_read() as c:known=store.publication_reference(c,p,(key,lease)) is not None
        if ok or known:
            from .operational_alerts import resolve
            resolve(settings,phase='OUTBOX',info=data['info'],order_id=data['result']['id'],codes=('BUY_ACK_ADMISSION_FAILED',))
        if not ok and not known and not store.notice_decided(key):
            from .operational_alerts import report
            report(settings,'BUY_ACK_ADMISSION_FAILED',phase='OUTBOX',info=data['info'],order_id=data['result']['id'])
        return ok
    except Exception:
        warning('BUY_ACK_ADMISSION_FAILED')
        from .operational_alerts import report
        report(settings,'BUY_ACK_ADMISSION_FAILED',phase='OUTBOX',info=data['info'],order_id=data['result']['id'])
        return False


def prepare_confirmation(store,p):
    if (p.estimated_pnl or {}).get('basis')!='POA_BUY_OBSERVATION':return p
    with store._connect_read() as c:
        row=c.execute('SELECT v.payload FROM vpnl_roots r JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head WHERE r.alias=?',(p.event_id,)).fetchone()
    if row and json.loads(row[0]).get('estimated_pnl',{}).get('basis')=='POA_BUY_ACK':
        from .notification_origin import PREFIX
        p.event_version=2
        head,sep,tail=(p.verification_source or '').partition(PREFIX)
        p.verification_source=head+'\nPOA_BUY_ACK_CONFIRMED'+(sep+tail if sep else '')
    return p


def transition(c,p,job):
    """Only one same-root acceptance->observed-fill revision, never a requeue."""
    from .estimated_pnl import case_for_job,notice_input,canonical,calculate
    r=p.estimated_pnl or {}
    if p.event_version!=2 or p.side!='매수' or r.get('basis')!='POA_BUY_OBSERVATION' or r.get('amount') is not None or r.get('roi') is not None:return False
    n=notice_input(c,r['intake_id'])
    if not n['snapshot']['confirmed'] or n['snapshot']['side']!='buy' or canonical(calculate(c,r['intake_id']))!=canonical(r):return False
    case=case_for_job(job)
    rows=c.execute('SELECT d.root_id,d.status,v.payload FROM vpnl_notice_members m JOIN vpnl_deliveries d ON d.id=m.delivery_id JOIN vpnl_revisions v ON v.root_id=d.root_id AND v.revision=d.revision WHERE m.case_id=? LIMIT 40',(case,)).fetchall()
    root=c.execute('SELECT id FROM vpnl_roots WHERE alias=?',(p.event_id,)).fetchone()
    if not root or not rows or len(rows)>2:return False
    accepted=False
    for rid,state,raw in rows:
        old=json.loads(raw);basis=old.get('estimated_pnl',{}).get('basis')
        if (old.get('exchange'),old.get('symbol'),old.get('order_id'),old.get('side'))!=(p.exchange,p.symbol,p.order_id,'매수'):return False
        if any(old.get(k) is not None for k in ('financial_receipt','execution_receipt','accounting_receipt','outcome_receipt','financial_revocation_receipt')):return False
        if rid!=root[0] or old.get('notification_type')!='ESTIMATED' or state in ('UNKNOWN','HELD','BLOCKED_PREDECESSOR'):return False
        if basis=='POA_BUY_ACK':
            if old.get('event_version')!=1 or old['estimated_pnl']!=result(c,r['intake_id']):return False
            accepted=True
        elif basis!='POA_BUY_OBSERVATION' or state in ('SENT','POSTING'):return False
    return accepted


def confirmation_blocked(store,key):
    with store._connect_read() as c:
        row=c.execute("SELECT v.payload FROM vpnl_root_bindings b JOIN vpnl_roots r ON r.id=b.root_id JOIN vpnl_revisions v ON v.root_id=r.id AND v.revision=r.head WHERE json_extract(b.binding,'$.producer.id')=? LIMIT 1",(key,)).fetchone()
        return bool(row and json.loads(row[0]).get('estimated_pnl',{}).get('basis')=='POA_BUY_ACK')


def error_code(exc,stage='READ'):
    from .upbit_notification_get import UpbitGETError
    from .upbit_spot_read import UpbitReadIncomplete
    if stage=='ADMISSION':return 'UPBIT_OBSERVATION_DB_FAILED' if isinstance(exc,sqlite3.Error) else 'UPBIT_OBSERVATION_ADMISSION_FAILED'
    if isinstance(exc,UpbitGETError):
        if exc.code=='ORDER_RESPONSE_INVALID':return {'UUID':'UPBIT_READ_ORDER_UUID','MARKET':'UPBIT_READ_SCOPE_MISMATCH','SIDE':'UPBIT_READ_SCOPE_MISMATCH','SCHEMA':'UPBIT_READ_SCHEMA_INVALID'}.get(exc.reason,'UPBIT_READ_RESPONSE_SCOPE')
        return {'AUTHENTICATION_REJECTED':'UPBIT_READ_AUTH_REJECTED','AUTHENTICATION_DISABLED':'UPBIT_READ_AUTH_REJECTED','PERMISSION_REJECTED':'UPBIT_READ_PERMISSION_REJECTED','IP_REJECTED':'UPBIT_READ_IP_REJECTED','RATE_LIMITED':'UPBIT_READ_RATE_LIMITED','READ_BLOCKED':'UPBIT_READ_RATE_LIMITED','READ_TIMEOUT':'UPBIT_READ_TIMEOUT','UPBIT_SERVER_UNAVAILABLE':'UPBIT_READ_SERVER_ERROR','ORDER_RESPONSE_INVALID':'UPBIT_READ_RESPONSE_SCOPE','ORDER_UUID_REQUIRED':'UPBIT_READ_ORDER_UUID','READ_CONNECTION_FAILED':'UPBIT_READ_CONNECTION_FAILED'}.get(exc.code,'UPBIT_READ_UNAVAILABLE')
    if isinstance(exc,UpbitReadIncomplete):
        code=exc.args[0] if exc.args else None
        if code=='UPBIT_ORDER_TIME':return 'UPBIT_READ_CLOCK_RANGE'
        if code in ('UPBIT_IDENTITY_UNAVAILABLE','UPBIT_ACCOUNT_POCKET_UNVERIFIED'):return 'UPBIT_READ_BINDING_FAILED'
        if code in ('UPBIT_ORDER_UUID_MISMATCH','UPBIT_UUID'):return 'UPBIT_READ_ORDER_UUID'
        if code in ('UPBIT_NOTIFICATION_SCOPE','UPBIT_SYMBOL','UPBIT_TRADE_SCOPE_OR_UUID','UPBIT_NOTIFICATION_ORDER_SIDE'):return 'UPBIT_READ_SCOPE_MISMATCH'
        if code in ('UPBIT_EXECUTED_VOLUME_CONFLICT','UPBIT_QUANTITY_SUMMARY','UPBIT_DONE_QUANTITY_CONFLICT','UPBIT_TRADE_FUNDS_CONFLICT'):return 'UPBIT_READ_QUANTITY_CONFLICT'
        if code in ('UPBIT_TRADE_LIST_INCOMPLETE','UPBIT_NOTIFICATION_TERMINAL_REQUIRED','UPBIT_NOTIFICATION_VWAP'):return 'UPBIT_READ_FILL_INCOMPLETE'
        return 'UPBIT_READ_SCHEMA_INVALID'
    return 'UPBIT_READ_BINDING_FAILED' if stage=='BINDING' else 'UPBIT_READ_UNEXPECTED_FAILURE'


def render(p):
    from dhooks import Embed
    from exchange.utility.discord_embed import enforce_embed_bounds,sanitize_discord_text
    from datetime import datetime,timezone,timedelta
    clean=lambda v:sanitize_discord_text(v,max_len=128)
    when=datetime.fromtimestamp(p.event_timestamp,timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S KST')
    return enforce_embed_bounds(Embed(title=clean(p.exchange)+' · '+clean(p.symbol)+' · 매수 주문 접수',description='전략/주문명: '+clean(p.strategy_id or p.order_name)+'\n상태: 체결 확인 대기\n주문 접수 응답 기준 · 체결가격·수량 미확인\n손익/ROI 산출 없음\n시각: '+when,color=0x64748B))


def render_confirmation(p):
    from .tradingview_signal import render
    from exchange.utility.discord_embed import Embed,enforce_embed_bounds
    old=render(p).data
    return enforce_embed_bounds(Embed(title=old['title'].replace('매수 기록','매수 체결 확인'),description=old['description'].replace('체결 기록 관측 · 검증 미확정','매수 체결 확인 · 조회된 체결 기록 (금융 검증 미확정)'),color=old['color']))
