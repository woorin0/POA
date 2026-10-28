"""Round 6 independent runtime harness. Loads only this checkout's product modules."""
import importlib.util,sys,json,hashlib
from pathlib import Path
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];ART=ROOT/'audit_artifacts'
spec=importlib.util.spec_from_file_location('r6_bootstrap',ROOT/'tests/test_independent_audit.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
t=h.t;de=h.de;dp=h.dp;ob=sys.modules['exchange.verified_pnl.outbox']
from exchange.verified_pnl.models import *
from exchange.verified_pnl.verifier import FillVerifier
from exchange.verified_pnl.ledger import allocate,identity

def settings(db):
    st=h.settings(db);st.NET_REALIZED_PNL_ENABLED=True;st.VERIFIED_COST_BASIS_COMPLETE=True;st.PARTIAL_FILL_ALERTS=True
    return st

def native(oid,price,qty,status='closed',ts=1700000000000):
    return dict(id=oid,status=status,amount=str(qty),filled=str(qty),remaining='0',average=str(price),fee={'cost':str(qty),'currency':'USDT'},trades=[dict(id='t-'+oid,order=oid,symbol='BTC/USDT',price=str(price),amount=str(qty),fee={'cost':str(qty),'currency':'USDT'},timestamp=ts)])

def incomplete():
    d=native('buy',150,2);d['trades'][0].update(price='100',amount='1',fee={'cost':'1','currency':'USDT'})
    return d

def full_entry():
    d=native('buy',150,2);d['trades'][0].update(price='100',amount='1',fee={'cost':'1','currency':'USDT'})
    second=dict(d['trades'][0]);second.update(id='t-buy-2',price='200',timestamp=1700000000001);d['trades'].append(second)
    return d

def origins():
    rows=[]
    for n in ['models','verifier','ledger','pnl_calculator','dispatcher','outbox']:
        m=sys.modules['exchange.verified_pnl.'+n];p=Path(m.__file__).resolve()
        rows.append(dict(module=m.__name__,file=str(p),sha256=hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()))
    p=Path(de.__file__).resolve();rows.append(dict(module='exchange.utility.discord_embed',file=str(p),sha256=hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()))
    return rows

def wire(p,label):
    with patch('requests.post',return_value=Mock(status_code=200,json=Mock(return_value={'id':'mock'}))) as post:
        ob.DiscordSender('https://discord.com/api/webhooks/1/MOCK').send_payload(p)
    result=post.call_args.kwargs['json']['embeds'][0]
    with (ART/'round6_wire.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(dict(label=label,payload=p.dict(),embed=result,details=de.build_notification_details(p)),ensure_ascii=False,default=str)+'\n')
    return result

def run(db,orders,actions,history=None):
    st=settings(db);client=Mock();client.market.return_value={'contractSize':1}
    def fetch(oid,*a):
        d=orders[oid]
        if d.get('__raise__'):raise TimeoutError('MOCK_QUERY_TIMEOUT')
        return d
    client.fetch_order.side_effect=fetch
    if isinstance(history,dict) and history.get('__raise__'):client.fetch_my_trades.side_effect=TimeoutError('MOCK_TRADE_QUERY_TIMEOUT')
    else:client.fetch_my_trades.return_value=history or []
    factory=lambda *a:client
    made=[];real=dp.make_payload
    def record(*a,**k):
        p=real(*a,**k);made.append(p.dict());return p
    with patch.object(dp,'make_payload',side_effect=record),patch('requests.post',return_value=Mock(status_code=200,json=Mock(return_value={'id':'mock'}))) as post:
        for a in actions:
            if a['op']=='dispatch':
                if 'account' in a:st.BINANCE_KEY=a['account']
                info=t.OrderRequest(exchange='BINANCE',base='BTC',quote='USDT',side=a['side'],amount=float(a.get('qty',1)),order_name=a.get('name','production-alpha'),strategy_id=a.get('strategy','alpha-v1'))
                dp.dispatch_verified_alert('BINANCE',{'id':a['id']},info,st)
                dp.verification_tick(st,factory)
            elif a['op']=='tick':dp.verification_tick(st,factory)
            elif a['op']=='flush':dp.flush_outbox(st)
        sends=post.call_count
    store=ob.NotificationOutboxStore(str(db))
    with store._connect_read() as c:
        jobs=[dict(id=x[0],data=json.loads(x[1]),state=x[2],due=x[3]) for x in c.execute('select id,data,state,due from verified_jobs order by created')]
        trades=[dict(id=x[0],scope=x[1],data=json.loads(x[2])) for x in c.execute('select id,scope,data from verified_trades')]
        alerts=[dict(id=x[0],status=x[1],payload=json.loads(x[2])) for x in c.execute('select event_id,status,payload from verified_outbox')]
    out=dict(jobs=jobs,trades=trades,alerts=alerts,made=made,sends=sends,fetches=[str(x.args[0]) for x in client.fetch_order.call_args_list],trade_fetches=client.fetch_my_trades.call_count,creates=client.create_order.call_count,origins=origins())
    return out

if __name__=='__main__':
    fixture=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
    out=run(fixture['db'],fixture['orders'],fixture['actions'],fixture.get('history'))
    Path(sys.argv[2]).write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str),encoding='utf8')
