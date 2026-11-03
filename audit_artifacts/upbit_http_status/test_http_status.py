"""Actual HTTPS transport -> GET policy/JWT -> notification error mapper.
Only http.client.HTTPSConnection is replaced. No sockets/credentials/Discord.
"""
import copy,hashlib,json,ssl,sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'upbit_get_host'))
import test_get_host as G
from exchange.verified_pnl import upbit_notification_get as M,buy_ack as A

CANARY='SYNTHETIC_RESPONSE_SECRET_CANARY'
class Clock:
 def __init__(self):self.t=0
 def now(self):return self.t
 def sleep(self,n):self.t+=n

def order():
 raw=G.raw_order('http-status-xrp','bid',['2000'],G.STAMP)
 raw['market']='KRW-XRP'
 for fill in raw['trades']:fill['market']='KRW-XRP'
 return raw

class FakeHTTP:
 def __init__(self,status=200,body=None,headers=None,fault=None):
  self.expected_uuid=order()['uuid'];self.status=status;self.body=json.dumps(order() if body is None else body).encode() if not isinstance(body,bytes) else body
  self.headers={'Content-Type':'application/json','Content-Length':str(len(self.body)),**(headers or {})};self.fault=fault;self.calls=[];self.reads=0;self.closed=0
 def connection(self,host,port,timeout,context):
  assert host=='api.upbit.com' and port==443 and timeout==3.05
  assert context.check_hostname is True and context.verify_mode==ssl.CERT_REQUIRED
  outer=self
  class Conn:
   def __init__(self):self.sock=NS(settimeout=lambda n:None);self.offset=0
   def set_debuglevel(self,n):assert n==0
   def connect(self):
    if outer.fault=='tls':raise ssl.SSLError(CANARY)
   def request(self,method,target,headers):
    assert method=='GET' and target=='/v1/order?uuid='+outer.expected_uuid
    assert headers['Accept-Encoding']=='identity' and headers['Authorization'].startswith('Bearer ')
    outer.calls.append(dict(method=method,target=target,authorization=headers['Authorization']))
   def getresponse(self):
    if outer.fault=='timeout':raise TimeoutError(CANARY)
    return self
   @property
   def status(self):return outer.status
   def getheaders(self):return list(outer.headers.items())
   def read1(self,n):
    outer.reads+=1;part=outer.body[self.offset:self.offset+n];self.offset+=len(part);return part
   def close(self):outer.closed+=1
  return Conn()
 def client(self):
  clock=Clock();credential=M.ReadCredentials('SYNTHETIC_ACCESS_CANARY','SYNTHETIC_SECRET_CANARY')
  return M.UpbitNotificationGET(lambda:credential,lambda fingerprint:None,transport=M.HTTPSOrderGETTransport(),expected_market='KRW-XRP',monotonic=clock.now,sleep=clock.sleep)

AUTH_CASES=[(401,'jwt_verification','UPBIT_READ_AUTH_REJECTED'),(401,'no_authorization_ip','UPBIT_READ_IP_REJECTED'),(401,'out_of_scope','UPBIT_READ_PERMISSION_REJECTED'),(403,'out_of_scope','UPBIT_READ_PERMISSION_REJECTED'),(403,'unknown_permission','UPBIT_READ_PERMISSION_REJECTED'),(403,'no_authorization_ip','UPBIT_READ_IP_REJECTED')]
@pytest.mark.parametrize('status,name,expected',AUTH_CASES)
def test_auth_real_transport_preserves_status_and_classification(status,name,expected,capsys):
 fake=FakeHTTP(status,{'error':{'name':name,'message':CANARY}});client=fake.client()
 with patch.object(M.http.client,'HTTPSConnection',side_effect=fake.connection):
  with pytest.raises(M.UpbitGETError) as caught:client('/v1/order',{'uuid':order()['uuid']})
  assert A.error_code(caught.value)==expected
  assert len(fake.calls)==fake.closed==1
  with pytest.raises(M.UpbitGETError) as disabled:client('/v1/order',{'uuid':order()['uuid']})
  assert disabled.value.code=='AUTHENTICATION_DISABLED' and len(fake.calls)==1
 out=capsys.readouterr();assert CANARY not in out.out+out.err+str(caught.value)

@pytest.mark.parametrize('status',[200,401,403,302,429,500])
def test_transport_retains_actual_http_status(status):
 fake=FakeHTTP(status);request=M.OrderGETRequest('GET',M.ORIGIN+M.PATH+'?uuid='+order()['uuid'],{'Authorization':'Bearer synthetic','Accept':'application/json','Accept-Encoding':'identity'})
 with patch.object(M.http.client,'HTTPSConnection',side_effect=fake.connection):response=M.HTTPSOrderGETTransport()(request)
 assert response.status==status and fake.closed==1
 assert response.body==(fake.body if status in (200,401,403) else b'')
 assert bool(fake.reads)==(status in (200,401,403))

@pytest.mark.parametrize('status,body,expected',[(401,b'{invalid','UPBIT_READ_AUTH_REJECTED'),(403,{'error':{'message':'no_authorization_ip '+CANARY}},'UPBIT_READ_PERMISSION_REJECTED'),(401,{'error':{'name':CANARY}},'UPBIT_READ_AUTH_REJECTED'),(401,{'error':{'name':{'verified':True}}},'UPBIT_READ_AUTH_REJECTED')])
def test_auth_allowlist_never_parses_message_or_untrusted_name(status,body,expected):
 fake=FakeHTTP(status,body)
 with patch.object(M.http.client,'HTTPSConnection',side_effect=fake.connection):
  with pytest.raises(M.UpbitGETError) as caught:fake.client()('/v1/order',{'uuid':order()['uuid']})
 assert A.error_code(caught.value)==expected and CANARY not in str(caught.value) and len(fake.calls)==1

@pytest.mark.parametrize('body',[b'{bad',b'[]',b'{"error":{"name":"jwt_verification"}}',b'{"uuid":1,"uuid":2}',b'{"value":NaN}',b'\xff'])
def test_200_schema_errors_remain_review_not_auth_or_success(body):
 fake=FakeHTTP(200,body)
 with patch.object(M.http.client,'HTTPSConnection',side_effect=fake.connection):
  with pytest.raises(M.UpbitGETError) as caught:fake.client()('/v1/order',{'uuid':order()['uuid']})
 assert A.error_code(caught.value)=='UPBIT_READ_SCHEMA_INVALID' and len(fake.calls)==1

@pytest.mark.parametrize('field,value,expected',[('uuid',G.U('wrong-order'),'UPBIT_READ_ORDER_UUID'),('market','KRW-LINK','UPBIT_READ_SCOPE_MISMATCH'),('side','buy','UPBIT_READ_SCOPE_MISMATCH')])
def test_200_identity_mismatch_remains_rejected(field,value,expected):
 raw=order();raw[field]=value;fake=FakeHTTP(200,raw)
 with patch.object(M.http.client,'HTTPSConnection',side_effect=fake.connection):
  with pytest.raises(M.UpbitGETError) as caught:fake.client()('/v1/order',{'uuid':order()['uuid']})
 assert A.error_code(caught.value)==expected

def test_200_native_order_is_returned_exactly_without_financial_issuance():
 raw=order();fake=FakeHTTP(200,raw)
 with patch.object(M.http.client,'HTTPSConnection',side_effect=fake.connection):result=fake.client()('/v1/order',{'uuid':raw['uuid']})
 assert result==raw and result['market']=='KRW-XRP' and 'financial_receipt' not in result

@pytest.mark.parametrize('status,fault,expected,attempts',[(302,None,'UPBIT_READ_UNAVAILABLE',1),(429,None,'UPBIT_READ_RATE_LIMITED',2),(500,None,'UPBIT_READ_SERVER_ERROR',2),(200,'timeout','UPBIT_READ_TIMEOUT',2),(200,'tls','UPBIT_READ_UNAVAILABLE',0)])
def test_retry_redirect_timeout_tls_existing_policy(status,fault,expected,attempts):
 fake=FakeHTTP(status,fault=fault,headers={'Retry-After':'1'})
 with patch.object(M.http.client,'HTTPSConnection',side_effect=fake.connection):
  with pytest.raises(M.UpbitGETError) as caught:fake.client()('/v1/order',{'uuid':order()['uuid']})
 assert A.error_code(caught.value)==expected and len(fake.calls)==attempts and CANARY not in str(caught.value)
 if attempts==2:assert fake.calls[0]['authorization']!=fake.calls[1]['authorization']

@pytest.mark.parametrize('status,headers',[(200,{'Content-Length':'1048577'}),(401,{'Content-Length':'1048577'}),(403,{'Content-Encoding':'gzip'})])
def test_auth_body_keeps_size_and_encoding_bounds(status,headers):
 fake=FakeHTTP(status,headers=headers)
 with patch.object(M.http.client,'HTTPSConnection',side_effect=fake.connection):
  with pytest.raises(M.UpbitGETError) as caught:fake.client()('/v1/order',{'uuid':order()['uuid']})
 assert caught.value.code in ('RESPONSE_TOO_LARGE','RESPONSE_ENCODING_REJECTED') and fake.reads==0


def test_only_transport_status_return_changed_from_base():
 import subprocess
 path='exchange/verified_pnl/upbit_notification_get.py'
 old=subprocess.check_output(['git','show','15f8417ddcd9c9e68950b75e19fc58d0d9bf3117:'+path])
 expected=old.replace(b"return OrderGETResponse(200,b''.join(chunks),headers)",b"return OrderGETResponse(response.status,b''.join(chunks),headers)")
 assert Path(path).read_bytes()==expected


setup=G.setup
@pytest.mark.parametrize('fault,expected',[(None,None),('ord_type','UPBIT_READ_SCHEMA_INVALID'),('paid_fee','UPBIT_READ_SCHEMA_INVALID'),('created_at','UPBIT_READ_SCHEMA_INVALID')])
def test_actual_200_native_normalizer_keeps_review_without_faking_fill(setup,fault,expected):
 from dataclasses import replace
 r=setup;raw=copy.deepcopy(r.state['orders'][r.buy])
 if fault:raw[fault]={'ord_type':'not-a-supported-type','paid_fee':'NaN','created_at':'not-a-time'}[fault]
 fake=FakeHTTP(200,raw);fake.expected_uuid=r.buy
 r.sources=replace(r.sources,transport=M.HTTPSOrderGETTransport())
 assert G.configure_upbit_read_sources(r.sources)
 G.prepared(r)
 with patch.object(M.http.client,'HTTPSConnection',side_effect=fake.connection):G.feed(r,'buy');G.tick(r)
 with G.store(r)._connect_read() as c:
  state,code=c.execute('SELECT state,financial_state FROM vpnl_jobs LIMIT 1').fetchone()
  count=c.execute('SELECT count(*) FROM vpnl_notice_upbit_observations').fetchone()[0]
  plans=[json.loads(row[0]) for row in c.execute('SELECT payload FROM vpnl_revisions')]
 if fault:
  assert state=='OBSERVED_REVIEW' and code==expected and count==0
  assert all(p['fill_status']=='UNVERIFIED' and p['filled_qty'] in (None,'0') for p in plans)
 else:assert state=='OBSERVED_NOTIFIED' and count==1
 assert all(p['financial_receipt'] is None and p['execution_receipt'] is None for p in plans)


def test_buy_and_sell_restart_preserves_two_sent_events(setup):
 from exchange.verified_pnl import intake as I
 r=setup;G.prepared(r)
 for side in ('buy','sell'):G.feed(r,side);G.tick(r)
 store=G.store(r)
 with store._connect_read() as c:observations=c.execute('SELECT intake_id,record,record_digest FROM vpnl_notice_upbit_observations ORDER BY intake_id').fetchall()
 first=G.send(r);assert len(first)==2 and all(x['ack']['status']=='SENT' for x in first)
 with store._connect_read() as c:before=c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries ORDER BY id').fetchall()
 I._BOOT_CHECKED.discard(store.db_path);I.replay(r.st,store=G.store(r))
 for side in ('buy','sell'):G.feed(r,side)
 G.tick(r);assert G.send(r)==[]
 with store._connect_read() as c:
  assert c.execute('SELECT intake_id,record,record_digest FROM vpnl_notice_upbit_observations ORDER BY intake_id').fetchall()==observations
  assert c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries ORDER BY id').fetchall()==before


@pytest.mark.parametrize('status',['SENT','UNKNOWN','HELD','POSTING'])
def test_exact_sell_delivery_cas_and_no_unauthorized_repost(setup,status):
 r=setup;G.prepared(r);G.feed(r,'buy');G.tick(r);assert len(G.send(r))==1
 G.feed(r,'sell');G.tick(r);store=G.store(r);claim=None
 with store._connect_read() as c:delivery=c.execute("SELECT d.id FROM vpnl_deliveries d JOIN vpnl_revisions v ON v.root_id=d.root_id AND v.revision=d.revision WHERE json_extract(v.payload,'$.order_id')=?",(r.sell,)).fetchone()[0]
 if status=='SENT':assert len(G.send(r))==1
 elif status=='POSTING':
  claim=store.claim_next();assert claim['delivery_id']==delivery;bad=dict(claim,token='wrong-token')
  assert not store.begin_post(bad) and store.begin_post(claim) and not store.ack(bad,True,message_id='123')
 else:
  with store._connect() as c:c.execute('UPDATE vpnl_deliveries SET status=? WHERE id=?',(status,delivery))
 with store._connect_read() as c:before=c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries ORDER BY id').fetchall()
 G.feed(r,'sell');G.tick(r);assert G.send(r)==[]
 with store._connect_read() as c:assert c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries ORDER BY id').fetchall()==before
 if claim:
  with G.FrozenWireObserver(store) as observer:observer.post(data=claim['wire'],headers={'Content-Type':'application/json'})
  assert store.ack(claim,True,message_id='123000') and observer.finish()[0]['ack']['status']=='SENT'


def test_source_reference_change_never_reposts_same_physical_orders(setup):
 from dataclasses import replace
 from exchange.verified_pnl.upbit_spot_read import UpbitNotificationHost,configure_upbit_notification_host
 r=setup;host=G.prepared(r)
 for side in ('buy','sell'):G.feed(r,side);G.tick(r)
 assert len(G.send(r))==2
 store=G.store(r)
 with store._connect_read() as c:before=c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries ORDER BY id').fetchall()
 r.st.UPBIT_KEY='mock-host-reconfigured';reader=host.reader
 reader.scope=replace(reader.scope,legacy_account_reference=G.H.identity('UPBIT',r.st.UPBIT_KEY))
 other=UpbitNotificationHost(reader,source_account_ref=reader.scope.legacy_account_reference,strategy='strategy-A')
 assert configure_upbit_notification_host(other)
 for side in ('buy','sell'):G.feed(r,side);G.tick(r)
 assert G.send(r)==[]
 with store._connect_read() as c:
  assert c.execute('SELECT id,wire,wire_digest,status,message_id FROM vpnl_deliveries ORDER BY id').fetchall()==before
  assert c.execute('SELECT count(*) FROM vpnl_notice_reviews').fetchone()[0]==2
