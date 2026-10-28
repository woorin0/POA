"""Validation-only observer. Production must still pass frozen data bytes."""
import base64,hashlib,json
from unittest.mock import Mock,patch

class FrozenWireObserver:
 def __init__(self,store,hook=None,error=None):
  self.store=store;self.hook=hook;self.error=error;self.records=[]
 def post(self,*args,**kwargs):
  assert 'json' not in kwargs and isinstance(kwargs.get('data'),bytes)
  raw=kwargs['data'];digest=hashlib.sha256(raw).hexdigest()
  with self.store._connect_read() as c:
   rows=c.execute("SELECT d.id,d.root_id,d.revision,d.wire,d.wire_digest,a.id,a.epoch,a.token_hash,a.revision,a.wire_digest,a.phase FROM vpnl_deliveries d JOIN vpnl_delivery_attempts a ON a.delivery_id=d.id WHERE d.status='POSTING' AND a.phase='POSTING' AND d.wire_digest=?",(digest,)).fetchall()
  assert len(rows)==1
  d,root,rev,wire,wd,attempt,epoch,token,ar,ad,phase=rows[0]
  assert raw==wire and digest==wd==ad and rev==ar and len(token)==64
  record=dict(raw_b64=base64.b64encode(raw).decode(),json=json.loads(raw),digest=digest,headers=dict(kwargs.get('headers',{})),delivery=d,root=root,revision=rev,attempt=attempt,epoch=epoch,claim_token_hash=token,claim_phase=phase)
  assert record['headers']['Content-Type']=='application/json'
  self.records.append(record)
  if self.hook:self.hook(record)
  if self.error:raise self.error
  return Mock(status_code=200,json=Mock(return_value={'id':str(123000+len(self.records))}))
 def __enter__(self):self.patch=patch('requests.post',side_effect=self.post);self.patch.start();return self
 def __exit__(self,*exc):self.patch.stop()
 def finish(self):
  for r in self.records:
   with self.store._connect_read() as c:
    row=c.execute('SELECT d.status,d.revision,d.wire_digest,d.message_id,a.phase,a.revision,a.wire_digest,a.message_id,a.outcome FROM vpnl_deliveries d JOIN vpnl_delivery_attempts a ON a.delivery_id=d.id WHERE a.id=?',(r['attempt'],)).fetchone()
   status,rev,digest,message,phase,ar,ad,am,outcome=row
   assert rev==ar==r['revision'] and digest==ad==r['digest']
   if status=='SENT':assert phase=='SENT' and message==am and message is not None
   r['ack']=dict(status=status,phase=phase,revision=ar,digest=ad,message_id=am,outcome=outcome)
  return self.records
