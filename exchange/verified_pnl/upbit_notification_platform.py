"""Platform startup for notification-only Upbit reads; never a finance provider.

Linux opt-in explicitly reuses the existing POA trading key for GET only. This
restricts this client, not the actual API key's other exchange permissions.
"""
import hashlib,ipaddress,json,os,stat,sys
from pathlib import Path
from . import upbit_notification_bootstrap as B
from .upbit_notification_get import ReadCredentials
from .ledger import identity

class LinuxReadConfigurationError(ValueError):
    def __init__(self):super().__init__('UPBIT_LINUX_READ_CONFIGURATION_REJECTED')


def _settings_environment(settings):
    result={}
    for key,default in [('POA_UPBIT_ESTIMATED_READ_ENABLED',False),('POA_UPBIT_READ_BINDING_SHA256','')]:
        # Explicit process metadata wins; otherwise use Settings-resolved .env.
        value=os.environ[key] if key in os.environ else getattr(settings,key,default)
        if key=='POA_UPBIT_ESTIMATED_READ_ENABLED' and type(value) is str:
            text=value.strip().lower()
            if text in ('true','1','yes','on'):value=True
            elif text in ('false','0','no','off'):value=False
        result[key]=('true' if value else 'false') if type(value) is bool else value
    return result


def _check_binding_stat(info):
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in (0,os.geteuid()) or info.st_nlink!=1 or not 0<info.st_size<=8192:raise LinuxReadConfigurationError()


def _secure_read(path):
    """No symlink following, shared permissions, unbounded reads or secret files."""
    if not sys.platform.startswith('linux') or not Path(path).is_absolute():raise LinuxReadConfigurationError()
    fd=None
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        first=os.fstat(fd);_check_binding_stat(first)
        chunks=[];size=0
        while size<=8192:
            chunk=os.read(fd,8193-size)
            if not chunk:break
            size+=len(chunk);chunks.append(chunk)
        last=os.fstat(fd)
        if size!=first.st_size or (first.st_ino,first.st_dev,first.st_size,first.st_mtime_ns)!=(last.st_ino,last.st_dev,last.st_size,last.st_mtime_ns):raise LinuxReadConfigurationError()
        return b''.join(chunks)
    except Exception:raise LinuxReadConfigurationError() from None
    finally:
        if fd is not None:os.close(fd)


def _json(raw):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result:raise LinuxReadConfigurationError()
            result[k]=v
        return result
    return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(LinuxReadConfigurationError()))


def _linux_sources(settings,env):
    if getattr(settings,'POA_UPBIT_READ_CREDENTIAL_MODE','disabled')!='poa_settings':raise LinuxReadConfigurationError()
    path=getattr(settings,'POA_UPBIT_READ_BINDING_FILE','/etc/poa/upbit-notification-binding.json')
    pin=env['POA_UPBIT_READ_BINDING_SHA256']
    if type(pin) is not str or len(pin)!=64 or any(c not in '0123456789abcdef' for c in pin):raise LinuxReadConfigurationError()
    def load():
        raw=_secure_read(path)
        if hashlib.sha256(raw).hexdigest()!=pin:raise LinuxReadConfigurationError()
        d=_json(raw)
        if type(d) is not dict or set(d)!={'format','config','evidence','permissions','key_usage','allowed_ips'} or d['format']!='POA_UPBIT_LINUX_BINDING_V1' or d['key_usage']!='POA_SETTINGS_SHARED_TRADING_KEY':raise LinuxReadConfigurationError()
        if type(d['permissions']) is not list or 'order_query' not in d['permissions'] or len(set(d['permissions']))!=len(d['permissions']) or not set(d['permissions']).issubset({'order_query','order'}):raise LinuxReadConfigurationError()
        if type(d['allowed_ips']) is not list or not 1<=len(d['allowed_ips'])<=8:raise LinuxReadConfigurationError()
        for ip in d['allowed_ips']:ipaddress.ip_address(ip)
        cfg=B.UpbitReadConfig(**d['config']);cfg.validate()
        e=d['evidence']
        if type(e) is not dict or set(e)!={'source','reference','digest','expires_at'}:raise LinuxReadConfigurationError()
        cred=ReadCredentials(getattr(settings,'UPBIT_KEY',None),getattr(settings,'UPBIT_SECRET',None))
        if cred.fingerprint()!=cfg.read_key_fingerprint or identity('UPBIT',cred.access_key)!=cfg.source_account_ref:raise LinuxReadConfigurationError()
        proof=B.ExternalAccountBinding(cfg.account_uuid,cfg.pocket_uuid,cfg.source_account_ref,cfg.strategy,cfg.read_key_fingerprint,cfg.producer,frozenset({'order_query'}),e['source'],e['reference'],e['digest'],e['expires_at'])
        return cfg,proof,cred
    cfg,proof,unused=load()
    def resolve(current):
        fresh,p,unused=load()
        if fresh!=cfg or current!=cfg:raise LinuxReadConfigurationError()
        return p
    def credentials():
        fresh,p,cred=load()
        if fresh!=cfg:raise LinuxReadConfigurationError()
        return cred
    sources=B.UpbitReadSources(cfg,credentials,resolve)
    B._binding(sources)
    return sources


def prepare_platform_upbit_host(settings):
    """Settings already resolves .env/process-env precedence; no dotenv discovery.

    Windows vault is preserved. Linux uses its own protected binding file. A
    single configured strategy is admitted; other strategies remain blocked.
    """
    env=_settings_environment(settings)
    if env['POA_UPBIT_ESTIMATED_READ_ENABLED']=='false':return B.HostPreparation(False,'DISABLED')
    try:
        if env['POA_UPBIT_ESTIMATED_READ_ENABLED']!='true':raise LinuxReadConfigurationError()
        if sys.platform=='win32':
            from .upbit_notification_local import configure_local_upbit_sources
            status=configure_local_upbit_sources(environment=env)
            if status=='LOCAL_SOURCES_UNAVAILABLE':raise LinuxReadConfigurationError()
        elif sys.platform.startswith('linux'):
            if not B.configure_upbit_read_sources(_linux_sources(settings,env)):raise LinuxReadConfigurationError()
        else:raise LinuxReadConfigurationError()
        return B.prepare_upbit_notification_host(environment=env)
    except Exception:
        B.configure_upbit_read_sources(None)
        from .upbit_spot_read import configure_upbit_notification_host
        configure_upbit_notification_host(None)
        return B.HostPreparation(True,'PLATFORM_READ_CONFIGURATION_REJECTED')
