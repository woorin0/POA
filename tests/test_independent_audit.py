import asyncio, concurrent.futures, importlib.util, json, socket, time
from pathlib import Path
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import pytest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('audit_pr_bootstrap',ROOT/'tests/test_discord_embed_v2.py')
t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)
from exchange.verified_pnl.models import *
from exchange.verified_pnl import dispatcher as dp
from exchange.verified_pnl.outbox import NotificationOutboxStore, DiscordSender
from exchange.utility import discord_embed as de
from exchange.verified_pnl.ledger import allocate

def payload(**kw):
    vals=dict(event_id='audit',exchange='BINANCE',symbol='BTC/USDT',side='매도',order_name='audit',exit_reason=ExitReason.TP,pnl_result=PnlResult.UNKNOWN,fill_status=FillStatus.FILLED,event_timestamp=0,filled_qty='1',entry_price='100',exit_price='110')
    vals.update(kw);return NotificationPayload(**vals)

def settings(db):
    return t.Settings(_env_file=None,PASSWORD='test-only',VERIFIED_DISCORD_ALERTS=True,VERIFIED_FILL_PRICE=True,DISCORD_OUTBOX_ENABLED=True,EXIT_REASON_CLASSIFICATION_V2=True,DISCORD_RETRY_ENABLED=True,VERIFIED_NOTIFICATIONS_DB_PATH=str(db),DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/123456/mock_token')

def load_log():
    with patch('loguru.logger.remove'),patch('loguru.logger.add'),patch('dhooks.Webhook'):
        lm=t._load_module('audit_runtime_log',ROOT/'exchange/utility/LogMaker.py')
    lm.log_message=Mock();lm.logger=Mock()
    return lm

@pytest.mark.parametrize('err',[None,'ORDER_PROCESSING_FAILED','unknown exception','\n'.join('ORDER_PROCESSING_FAILED')])
def test_unknown_result_must_not_be_confirmed_rejection(err):
    info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1)
    e=de.build_error_embed(err,order_info=info).data
    assert e['color']==de.COLOR_ORDER_AMBIGUOUS,e

@pytest.mark.parametrize('message',['HTTP read timeout','ccxt RequestTimeout','insufficient balance'])
def test_known_error_categories(message):
    info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1)
    e=de.build_error_embed(message,order_info=info).data
    assert e['color']==(de.COLOR_ORDER_FAILED if message=='insufficient balance' else de.COLOR_ORDER_AMBIGUOUS)

@pytest.mark.parametrize('exc',[TypeError('after-submit'),RuntimeError('response lost'),t.legacy.ccxt.RequestTimeout('timed out')])
def test_actual_main_exception_path_conservative(exc):
    lm=load_log();main=t._load_module('audit_runtime_main',ROOT/'main.py')
    main.settings=t.Settings(_env_file=None,PASSWORD='test-only',VERIFIED_DISCORD_ALERTS=False)
    main.log_order_error_message=lm.log_order_error_message
    info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1)
    bot=Mock(order_info=info);bot.market_buy.side_effect=exc;main.get_bot=Mock(return_value=bot)
    tasks=main.BackgroundTasks();asyncio.run(main.order(info,tasks));asyncio.run(tasks())
    assert bot.market_buy.call_count==1
    e=lm.log_message.call_args.kwargs['embed'].data
    assert e['color']==de.COLOR_ORDER_AMBIGUOUS,e

@pytest.mark.parametrize('text,marker',[
    ('Authorization: Bearer AUDIT_AUTH_CANARY','AUDIT_AUTH_CANARY'),
    ('{"apiKey": "AUDIT_KEY_CANARY"}','AUDIT_KEY_CANARY'),
    ('api%5Fkey%3DAUDIT_ENCODED_CANARY','AUDIT_ENCODED_CANARY'),
    ('account_number=123-AUDIT-ACCOUNT','123-AUDIT-ACCOUNT'),
    ('response_body={"access_token": "AUDIT_HTTP_CANARY"}','AUDIT_HTTP_CANARY'),
    ('https://discord.com/api/webhooks/1/AUDIT_WEBHOOK_CANARY','AUDIT_WEBHOOK_CANARY'),
    ('api_key=AUDIT_PLAIN_CANARY','AUDIT_PLAIN_CANARY')])
def test_raw_errors_must_not_reach_discord(text,marker):
    lm=load_log();lm.log_error_message(RuntimeError(text),'DB auth error')
    e=lm.log_message.call_args.kwargs['embed'].data
    assert marker not in json.dumps(e),e
    assert marker not in str(lm.logger.mock_calls)

def test_raw_order_error_key_must_not_reach_discord():
    lm=load_log();info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1)
    lm.log_order_error_message(RuntimeError('{"apiKey": "AUDIT_ORDER_KEY"}'),info)
    assert 'AUDIT_ORDER_KEY' not in json.dumps(lm.log_message.call_args.kwargs['embed'].data)

def test_fill_verified_does_not_verify_unverified_pnl(tmp_path):
    fill=VerifiedFill(exchange='BINANCE',symbol='BTC/USDT',status=FillStatus.FILLED,verification_status=VerificationStatus.VERIFIED,average_fill_price=D(110),filled_quantity=D(1))
    pnl=PnlCalculationResult(net_realized_pnl=D(9),verification_status=VerificationStatus.UNVERIFIED)
    info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='sell',amount=1)
    p=dp.make_payload('old',fill,info,pnl,D(100),settings(tmp_path/'pnl.sqlite'))
    assert '확정손익: +9 ' not in de.build_mobile_verified_embed(p).data['description']

def test_legacy_outbox_estimated_roi_must_stay_estimated(tmp_path):
    p=payload(verification_status=VerificationStatus.VERIFIED,verification_source='REST / PnL: UNVERIFIED',net_pnl='10 USDT',roi_pct='20',roi_label='추정 ROI (레버리지 기반)')
    s=NotificationOutboxStore(str(tmp_path/'legacy.sqlite'));s.enqueue(p)
    restored=NotificationPayload(**s.get_pending()[0]['payload'])
    assert '손익 검증 완료' not in de.build_mobile_verified_embed(restored).data['description']

def test_unverified_values_not_shown_in_visible_fields():
    p=payload(net_pnl='9 USDT',roi_pct='10',verification_status=VerificationStatus.UNVERIFIED)
    e=de.build_mobile_verified_embed(p).data
    assert not any(f['name']=='순실현손익' and f['value']=='9 USDT' for f in e['fields']),e

def test_fee_currencies_must_not_be_added_as_usdt():
    e=de.build_mobile_verified_embed(payload(entry_fee_allocated='0.1 BTC',exit_fee='1 USDT',pnl_currency='USDT')).data
    assert '총수수료: 1.1 USDT' not in e['description'],e

def test_mobile_discord_wire_avoids_duplicate_fields():
    p=payload(net_pnl='10 USDT',roi_pct='10',verification_status=VerificationStatus.VERIFIED,order_id='audit-order',signal_id='audit-signal')
    e=de.build_mobile_verified_embed(p).data
    with patch('requests.post') as post:
        post.return_value=Mock(status_code=200,json=Mock(return_value={'id':'mock'}))
        DiscordSender('https://discord.com/api/webhooks/123456/mock_token').send_payload(p)
    fields=post.call_args.kwargs['json']['embeds'][0]['fields']
    assert not any(f['name'] in ('거래소','심볼','순실현손익','주문 ID','신호 ID') for f in fields),fields

@pytest.mark.parametrize('id_kind',['id','clientOrderId'])
def test_distinct_orders_same_signal_keep_manual_alerts(tmp_path,id_kind):
    s=NotificationOutboxStore(str(tmp_path/'manual.sqlite'));st=settings(tmp_path/'manual.sqlite')
    info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1,signal_id='shared',strategy_id='strategy')
    for i in range(2):
        data={'account':'test','info':info.dict(),'result':{id_kind:str(i)}}
        p=dp.make_manual_review_payload('job'+str(i),data,'long_term_recovery_disabled',st)
        s.enqueue(p)
    assert len(s.get_pending())==2

def test_distinct_order_jobs_same_signal_are_preserved(tmp_path):
    st=settings(tmp_path/'jobs.sqlite');info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1,signal_id='shared')
    for i in range(2): dp.dispatch_verified_alert('BINANCE',{'id':str(i)},info,st)
    with NotificationOutboxStore(st.VERIFIED_NOTIFICATIONS_DB_PATH)._connect_read() as c:
        assert c.execute('select count(*) from verified_jobs').fetchone()[0]==2

def test_atomic_enqueue_and_concurrent_dedup(tmp_path):
    s=NotificationOutboxStore(str(tmp_path/'dedup.sqlite'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(lambda _:s.enqueue(payload()),range(10)))==1
    with pytest.raises(RuntimeError):
        with s._connect() as c:
            c.execute("delete from verified_outbox");raise RuntimeError('rollback')
    assert len(s.get_pending())==1

def test_discord_unknown_send_is_held_across_restart(tmp_path):
    import requests
    st=settings(tmp_path/'send.sqlite');s=NotificationOutboxStore(st.VERIFIED_NOTIFICATIONS_DB_PATH);s.enqueue(payload())
    with patch('requests.post',side_effect=requests.exceptions.ReadTimeout('AUDIT_TOKEN')) as post:
        dp.flush_outbox(st);dp.flush_outbox(st);assert post.call_count==1
    with s._connect_read() as c: assert c.execute('select status from verified_outbox').fetchone()[0]=='UNKNOWN'

def test_abandoned_send_recovery(tmp_path):
    s=NotificationOutboxStore(str(tmp_path/'restart.sqlite'));s.enqueue(payload());assert s.claim('audit')
    with s._connect() as c:c.execute('update verified_outbox set updated_at=?',(time.time()-31,))
    s.recover();assert not s.get_pending()
    with s._connect_read() as c:assert c.execute('select status from verified_outbox').fetchone()[0]=='UNKNOWN'

def test_old_payload_without_new_optional_fields():
    data=payload().dict()
    for k in ['strategy_id','pnl_currency','notional_amount']:data.pop(k,None)
    assert de.build_mobile_verified_embed(NotificationPayload(**data)).data['title']

def test_split_buy_sell_ledger_and_fee_inclusive_roi(tmp_path):
    s=NotificationOutboxStore(str(tmp_path/'ledger.sqlite'))
    def event(oid,side,qty,price,fee,stamp,strategy='s'):
        info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side=side,amount=float(qty),strategy_id=strategy)
        f=VerifiedFill(exchange='BINANCE',symbol='BTC/USDT',order_id=oid,market_type='spot',position_side='LONG',filled_quantity=D(qty),average_fill_price=D(price),status=FillStatus.FILLED,verification_status=VerificationStatus.VERIFIED,source='ORDER_RESULT',trades_complete=True,pnl_currency='USDT',fees={'USDT':D(fee)},fees_complete=True,trades=[VerifiedTrade(id=oid,price=D(price),amount=D(qty),fee=D(fee),fee_currency='USDT',timestamp=stamp)])
        return f,info
    for oid,price,stamp in [('b1','100',1),('b2','200',2)]:
        f,i=event(oid,'buy','1',price,'1',stamp);allocate(s,f,i,'acct',True)
    for oid,qty,fee,stamp in [('s1','0.5','0.5',3),('s2','1.5','1.5',4)]:
        f,i=event(oid,'sell',qty,'220',fee,stamp);entry,ef=allocate(s,f,i,'acct',True)
        assert entry==D(150);assert ef==D(qty)
        p=t.PnlCalculator().calculate_pnl(f,i,entry_price=entry,allocated_entry_fee=ef)
        assert p.net_realized_pnl==D(qty)*D(68)
        assert p.net_roi_pct==D(68)/D(151)*100
    f,i=event('s2','sell','1.5','220','1.5',4)
    assert allocate(s,f,i,'acct',False)==(None,None)
    f,i=event('b3','buy','1','100','1',5,'other')
    assert allocate(s,f,i,'acct',True)==(None,None)

def test_third_currency_fee_prevents_verified_net_and_leverage_roi():
    f=VerifiedFill(exchange='BINANCE',symbol='BTC/USDT',market_type='spot',filled_quantity=D(1),average_fill_price=D(110),verification_status=VerificationStatus.VERIFIED,pnl_currency='USDT',fees={'BNB':D('.001')},fees_complete=True)
    i=NS(is_close=False,is_futures=False,is_sell=True,leverage=20)
    p=t.PnlCalculator().calculate_pnl(f,i,entry_price=D(100),allocated_entry_fee=D(1))
    assert p.net_realized_pnl is None and p.net_roi_pct is None

def test_string_join_is_not_actual_main_get_error_contract():
    main=t._load_module('audit_contract_main',ROOT/'main.py')
    assert main.get_error(RuntimeError('hidden'))==['ORDER_PROCESSING_FAILED']
    assert '\n'.join(main.get_error(None))=='ORDER_PROCESSING_FAILED'
    assert load_log().get_error(None)=='ORDER_PROCESSING_FAILED'

def test_http500_after_delivery_must_not_repeat_unknown_outcome(tmp_path):
    st=settings(tmp_path/'http500.sqlite');s=NotificationOutboxStore(st.VERIFIED_NOTIFICATIONS_DB_PATH);s.enqueue(payload())
    # Simulate Discord accepting the first message and an intermediary returning 500.
    responses=[Mock(status_code=500),Mock(status_code=200,json=Mock(return_value={'id':'second'}))]
    with patch('requests.post',side_effect=responses) as post:
        dp.flush_outbox(st)
        with s._connect() as c:c.execute('update verified_outbox set next_retry_at=0')
        dp.flush_outbox(st)
    assert post.call_count==1,'Unknown post-delivery 500 was retried and can create a second message'

def test_local_log_sinks_receive_only_constant_error_codes():
    lm=load_log();info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side='buy',amount=1)
    lm.log_error_message('Authorization: Bearer AUDIT_LOCAL_LOG_CANARY','test')
    lm.log_order_error_message('{"apiKey":"AUDIT_LOCAL_LOG_CANARY"}',info)
    assert 'AUDIT_LOCAL_LOG_CANARY' not in str(lm.logger.mock_calls)
    assert 'REQUEST_PROCESSING_FAILED' in str(lm.logger.mock_calls)
    assert 'ORDER_PROCESSING_FAILED' in str(lm.logger.mock_calls)

def test_audit_network_and_production_db_guard_is_active():
    import sys
    with pytest.raises(RuntimeError,match='AUDIT_NETWORK_DISABLED'):
        sys.audit('socket.connect',None,('203.0.113.1',443))
    with pytest.raises(RuntimeError,match='AUDIT_PRODUCTION_DB_DISABLED'):
        sys.audit('sqlite3.connect',str(ROOT/'pb_data/data.db'))
