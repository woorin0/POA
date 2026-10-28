import os, sys, pathlib
from urllib.parse import unquote,urlsplit
AUDIT_ROOT=pathlib.Path(__file__).resolve().parents[1]
def guard(event,args):
    if event=='socket.connect':
        f=sys._getframe(1)
        for _ in range(10):
            if f is None:break
            if f.f_code.co_name=='socketpair' and pathlib.Path(f.f_code.co_filename)==pathlib.Path(__import__('socket').__file__):return
            f=f.f_back
    if event in ('socket.connect','socket.connect_ex','socket.sendto','socket.getaddrinfo'):raise RuntimeError('AUDIT_NETWORK_DISABLED')
    if event=='sqlite3.connect':
        name=os.fsdecode(args[0])
        if name==':memory:':return
        if name.startswith('file:'):
            name=unquote(urlsplit(name).path)
            if len(name)>2 and name[0]=='/' and name[2]==':':name=name[1:]
        p=pathlib.Path(name).resolve()
        roots=[AUDIT_ROOT,pathlib.Path(os.environ.get('TEMP','/tmp')).resolve(),pathlib.Path('C:/Users/Public/Documents/ESTsoft/CreatorTemp')]
        if p.name in ('data.db','store.db','logs.db') or not any(r in p.parents for r in roots):raise RuntimeError('AUDIT_PRODUCTION_DB_DISABLED')
sys.addaudithook(guard)
os.environ['PYTHONDONTWRITEBYTECODE']='1'
# Product modules must come from the independently checked-out target, even when a test hardcodes a path.
import importlib.abc, importlib.machinery
class AuditOriginGate(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname == 'exchange' or fullname.startswith('exchange.'):
            spec=importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and spec.origin and spec.origin not in ('built-in','frozen'):
                resolved=pathlib.Path(spec.origin).resolve()
                if AUDIT_ROOT not in resolved.parents:raise ImportError('AUDIT_PRODUCT_ORIGIN_OUTSIDE_INDEPENDENT_CHECKOUT: '+str(resolved))
            return spec
        return None
sys.meta_path.insert(0,AuditOriginGate())
