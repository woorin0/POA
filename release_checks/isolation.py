"""Independent process: no tests/conftest.py, legacy bridge, or signature mocks."""
import os, sys, types
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
def guard(event,args):
    if event in ('socket.connect','socket.connect_ex','socket.sendto','socket.getaddrinfo'):
        raise RuntimeError('OFFLINE_NETWORK_DISABLED')
    if event=='sqlite3.connect' and str(args[0])!=':memory:':
        import tempfile
        from urllib.parse import urlsplit,unquote
        name=os.fsdecode(args[0])
        if name.startswith('file:'):
            name=unquote(urlsplit(name).path)
            if len(name)>2 and name[0]=='/' and name[2]==':':name=name[1:]
        target=Path(name).resolve()
        if Path(tempfile.gettempdir()).resolve() not in target.parents:
            raise RuntimeError('TEMP_SQLITE_ONLY')
sys.addaudithook(guard)
# Avoid live exchange/__init__.py (PocketBase/settings startup). All validation,
# signing, bootstrap, admission and rendering submodules are loaded from disk.
for name,folder in [('exchange','exchange'),('exchange.utility','exchange/utility')]:
    package=types.ModuleType(name);package.__path__=[str(ROOT/folder)];sys.modules[name]=package
@pytest.fixture(autouse=True)
def isolate_registry():
    from exchange.verified_pnl import evidence as E
    E.configure_provider(None,{})
    yield
    E.configure_provider(None,{})
