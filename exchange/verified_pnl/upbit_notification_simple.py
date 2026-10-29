"""POA-origin-only ESTIMATED read routing; never a financial identity provider."""
import threading,time,uuid,weakref
from collections import OrderedDict
from .bootstrap import SpotScope,AuthenticatedAccount
from .ledger import identity
from .upbit_notification_get import ReadCredentials,UpbitNotificationGET
from .upbit_spot_read import UpbitSpotReads,UpbitNotificationHost,canonical_uuid

class SimpleUpbitNotificationHosts:
    """One existing key, separate per-strategy notification readers.

    Names are internal POA namespaces, NOT official Upbit account/pocket UUIDs.
    select_job checks the durable origin and lease before minting a one-use read
    permit; observation admission checks the lease again after the query.
    """
    def __init__(self,settings):
        self.settings=settings
        cred=ReadCredentials(getattr(settings,'UPBIT_KEY',None),getattr(settings,'UPBIT_SECRET',None))
        self.fingerprint=cred.fingerprint();self.source_account_ref=identity('UPBIT',cred.access_key)
        self.account='poa-estimated-upbit:'+self.source_account_ref
        self.pocket_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL,self.account)) # internal, never exchange-certified
        self._children=OrderedDict();self._known_hosts=weakref.WeakValueDictionary();self._lock=threading.RLock();self._read_authorization=threading.local()
        def credentials():
            if not getattr(self._read_authorization,'oid',None):raise ValueError('simple_poa_job_required')
            if not self.pinned():raise ValueError('simple_key_binding_changed')
            return ReadCredentials(self.settings.UPBIT_KEY,self.settings.UPBIT_SECRET)
        def guard(fp):
            if fp!=self.fingerprint or not self.pinned():raise ValueError('simple_key_binding_changed')
        self._get=UpbitNotificationGET(credentials,guard)
    def pinned(self):
        try:
            cred=ReadCredentials(self.settings.UPBIT_KEY,self.settings.UPBIT_SECRET)
            return cred.fingerprint()==self.fingerprint and identity('UPBIT',cred.access_key)==self.source_account_ref
        except Exception:return False
    def for_strategy(self,strategy):
        from .estimated_pnl import text
        text(strategy)
        if not self.pinned():return None
        with self._lock:
            host=self._children.pop(strategy,None)
            if host is None:host=next((h for h in self._known_hosts.values() if h.strategy==strategy),None)
            if host is None:
                scope=SpotScope('UPBIT',self.account,'pocket:'+self.pocket_uuid,self.source_account_ref,'BTC/KRW','KRW-BTC','KRW','poa-estimated-internal-stream',strategy)
                def read_identity(client):
                    if not self.pinned():raise ValueError('simple_key_binding_changed')
                    return AuthenticatedAccount('UPBIT',scope.account,scope.subaccount,frozenset({'read'}))
                reader=UpbitSpotReads(scope,self._get,read_identity,pocket_uuid=self.pocket_uuid,as_of_ms=int(time.time()*1000))
                original_get=reader._get
                def fresh_get(path,params):
                    if path!='/v1/order' or params!={'uuid':getattr(self._read_authorization,'oid',None)}:raise ValueError('simple_poa_job_required')
                    reader.as_of_ms=int(time.time()*1000)
                    try:return original_get(path,params)
                    finally:reader.as_of_ms=int(time.time()*1000)
                reader._get=fresh_get
                host=UpbitNotificationHost(reader,source_account_ref=self.source_account_ref,strategy=strategy,allow_unattributed=True)
                host._poa_read_permits=set()
                original_observe=host.observe
                def leased_observe(iid,info,ref,oid,*,_observe=original_observe,_host=host):
                    with _host._issued_lock:
                        permit=(iid,ref,oid)
                        if permit not in _host._poa_read_permits:raise ValueError('simple_poa_job_required')
                        _host._poa_read_permits.remove(permit)
                    self._read_authorization.oid=oid
                    try:return _observe(iid,info,ref,oid)
                    finally:self._read_authorization.oid=None
                host.observe=leased_observe
                self._known_hosts[host.binding]=host
            self._children[strategy]=host
            while len(self._children)>32:self._children.popitem(last=False)
            return host
    def by_binding(self,binding):
        with self._lock:
            return self._known_hosts.get(binding) if self.pinned() else None
    def select_job(self,store,key,lease,data):
        from .estimated_pnl import notice_input
        from .intake import submission_for,validate_submission
        with store._connect_read() as c:
            row=c.execute("SELECT data FROM vpnl_jobs WHERE id=? AND state='VERIFYING' AND job_lease=? AND due>?",(key,lease,time.time())).fetchone()
            if row is None:raise ValueError('simple_job_lease_required')
            saved=store.parse_job(row[0]);iid=saved.get('notice_intake')
            if not iid or iid!=data.get('notice_intake'):raise ValueError('simple_intake_required')
            origin=notice_input(c,iid,effective=False);s=origin['snapshot'];ack=submission_for(c,iid)
            if origin['job']!=key or ack is None or ack['job_id']!=key or not validate_submission(store,c,ack):raise ValueError('simple_submission_required')
            info=saved['info'];oid=canonical_uuid(str(saved['result']['id']))
            strategy=info.get('strategy_id') or info.get('order_name') or 'default'
            if (s['exchange'],s['source_ref'],s['symbol'],s['order_id'],s['strategy'])!=('UPBIT',self.source_account_ref,'BTC/KRW',oid,strategy) or info.get('is_futures') or info.get('is_coinm'):raise ValueError('simple_origin_scope')
            if (data['source_account_ref'],str(data['result']['id']),data['info'])!=(self.source_account_ref,oid,info):raise ValueError('simple_job_scope')
        host=self.for_strategy(strategy)
        if host is None:raise ValueError('simple_key_binding_changed')
        with host._issued_lock:host._poa_read_permits.add((iid,self.source_account_ref,oid))
        return host
