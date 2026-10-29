"""Independent boundary audit; product and earlier tests are read-only."""
import copy,json,sqlite3,sys
from pathlib import Path
from dataclasses import replace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'estimated_implementation'))
import test_estimated_contracts as T
from test_estimated_contracts import env,history,rig,reset
from test_bundle3_bootstrap import active
from test_bundle4_history import request,store as hs
from exchange.verified_pnl.history import configure_history_provider
H=T.H;OUT=Path(__file__).parent

def evidence(e,name,records):
 s=T.store(e)
 with s._connect_read() as c:
  jobs=[dict(id=x[0],state=x[1],data=json.loads(x[2])) for x in c.execute('SELECT id,state,data FROM vpnl_jobs')]
  payloads=[json.loads(x[0]) for x in c.execute('SELECT payload FROM vpnl_revisions')]
 with sqlite3.connect(s.db_path) as c,sqlite3.connect(OUT/(name+'.sqlite')) as d:c.backup(d)
 result=dict(jobs=jobs,payloads=payloads,wire=records)
 (OUT/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 return result

@pytest.mark.parametrize('venue',T.VENUES)
def test_completed_summary_wrong_symbol_must_not_publish(env,venue):
 T.emit(env,venue,'buy','150')
 # BUY notifications are now intentional. Verify that positive event before
 # asserting the invalid SELL causes no new root/delivery/POST.
 buy=T.send(env);assert len(buy)==1 and T.plans(env)[0]['side']=='매수'
 s=T.store(env)
 with s._connect_read() as c:before=(c.execute('SELECT count(*) FROM vpnl_roots').fetchone()[0],c.execute('SELECT count(*) FROM vpnl_deliveries').fetchone()[0])
 def wrong(r):
  oid=r.get('id') or r.get('uuid');quote='KRW' if venue=='UPBIT' else 'USDT'
  r.clear();r.update(id=oid,status='closed',symbol='ETH/'+quote,side='sell',filled='1',amount='1',remaining='0',average='160',trades=[],fee=dict(cost='1',currency=quote))
 T.emit(env,venue,'sell','160',change=wrong)
 records=T.send(env);proof=evidence(env,'wrong_symbol_'+venue,records)
 assert records==[], 'CROSS_INSTRUMENT_SUMMARY_PUBLISHED: '+str(proof['payloads'][-1]['estimated_pnl']['amount'])
 with s._connect_read() as c:assert before==(c.execute('SELECT count(*) FROM vpnl_roots').fetchone()[0],c.execute('SELECT count(*) FROM vpnl_deliveries').fetchone()[0])
 assert not any(p['side']=='매도' for p in proof['payloads'])


def test_trusted_confirmed_execution_after_create_ack_must_notify(history):
 h=history;r=h.rig;configure_history_provider(None)
 reads=[];old=r.boot.adapter.read_order
 def read(c,oid,symbol):reads.append(oid);return old(c,oid,symbol)
 r.boot.adapter=replace(r.boot.adapter,read_order=read);active(r)
 handler=T.G.live_log(r.st)
 from unittest.mock import patch
 from exchange.verified_pnl.verifier import FillVerifier
 from exchange.verified_pnl import evidence as E
 original=FillVerifier.verify_order_result;verified=[]
 def tracked(self,*args,**kwargs):
  fill=original(self,*args,**kwargs)
  verified.append(dict(order=fill.order_id,status=str(fill.status),quantity=str(fill.filled_quantity),execution_valid=E.execution_valid(fill,fill.scope_data)))
  return fill
 with patch.object(FillVerifier,'verify_order_result',tracked):
  for side in ('buy','sell'):
   result=handler('BINANCE',{'id':side},request(side));assert result['accepted']
   H.dp.verification_tick(r.st,r.boot.client_for)
 from bundle5_wire_observer import FrozenWireObserver
 with FrozenWireObserver(hs(h)) as observer:H.dp.flush_outbox(r.st)
 records=observer.finish()
 e=type('Env',(),{'st':r.st})();proof=evidence(e,'trusted_ack_to_execution',records)
 proof['read_orders']=reads;proof['observed_verifier_results']=verified
 assert any(v['order']=='sell' and v['execution_valid'] and v['quantity']=='1' for v in verified)
 (OUT/'trusted_ack_to_execution.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
 assert 'buy' in reads and 'sell' in reads
 assert r.client.create_order.call_count==0
 sell=[p for p in proof['payloads'] if p['order_id']=='sell']
 assert len(sell)==1,'CONFIRMED_SELL_DROPPED_AFTER_ID_ONLY_ACK'
 assert sell[0]['estimated_pnl']['status']=='ESTIMATED'
 assert T.D(sell[0]['estimated_pnl']['amount'])==8
