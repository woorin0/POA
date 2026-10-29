import ast,asyncio,copy,hashlib,importlib.util,json,os,stat,sys,time,threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest,yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'upbit_get_host'))
import test_get_host as T
from exchange.verified_pnl import upbit_notification_platform as L,upbit_notification_bootstrap as B,upbit_notification_get as G
ROOT=Path.cwd()
spec=importlib.util.spec_from_file_location('ubuntu_installer',ROOT/'deployment/install_once.py');I=importlib.util.module_from_spec(spec);spec.loader.exec_module(I)

@pytest.fixture
def local(tmp_path,monkeypatch):
 f=T.setup.__wrapped__(tmp_path);r=next(f)
 r.st.UPBIT_KEY=r.credentials.access_key;r.st.UPBIT_SECRET=r.credentials.secret_key
 cfg=replace(r.cfg,source_account_ref=T.H.identity('UPBIT',r.credentials.access_key));r.cfg=cfg
 config=dict(enabled=True,credential_mode='poa_settings',account_uuid=cfg.account_uuid,pocket_uuid=cfg.pocket_uuid,strategy=cfg.strategy,permissions=['order_query','order'],allowed_ips=['192.0.2.10'],evidence=dict(source='operator-reviewed',reference='approval-record',digest='a'*64,expires_at=time.time()+600))
 data={'upbit_estimated_read':config};values={'UPBIT_KEY':r.st.UPBIT_KEY,'UPBIT_SECRET':r.st.UPBIT_SECRET}
 env,content=I.upbit_read_configuration(data,values)
 for k,v in env.items():setattr(r.st,k,v)
 r.linux=dict(content=content,reads=0,http=[],status=200,config=config)
 def read(path):r.linux['reads']+=1;return r.linux['content'].encode()
 def tx(req):
  r.linux['http'].append(req);oid=req.url.split('uuid=')[1]
  return T.response(r.state['orders'][oid],r.linux['status'])
 monkeypatch.setattr(L,'sys',NS(platform='linux'));monkeypatch.setattr(L,'_secure_read',read);monkeypatch.setattr(G,'HTTPSOrderGETTransport',lambda:tx)
 r.prepare=lambda:L.prepare_platform_upbit_host(r.st)
 yield r
 try:next(f)
 except StopIteration:pass


def test_linux_startup_actual_log_worker_get_to_estimated(local):
 r=local;node=next(n for n in ast.parse((ROOT/'main.py').read_text(encoding='utf-8')).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='startup');node.decorator_list=[]
 ns=dict(settings=r.st,app=NS(state=NS()),log_message=lambda *a:None,VERSION='mock')
 exec(compile(ast.Module(body=[node],type_ignores=[]),str(ROOT/'main.py'),'exec'),ns)
 T.feed(r,'buy');stop=None
 import ctypes
 with patch.object(ctypes,'WinDLL',side_effect=AssertionError('WINDOWS_VAULT_FORBIDDEN')),T.FrozenWireObserver(T.store(r)) as observer:
  try:
   asyncio.run(ns['startup']());stop=ns['app'].state.verified_notifications_stop;assert stop is not None
   deadline=time.time()+10;n=0
   while time.time()<deadline:
    with T.store(r)._connect_read() as c:n=c.execute('SELECT count(*) FROM vpnl_notice_upbit_observations').fetchone()[0]
    if n==1:break
    time.sleep(.02)
   assert n==1;T.feed(r,'sell');deadline=time.time()+10
   while not observer.records and time.time()<deadline:time.sleep(.02)
  finally:
   if stop is not None:stop.set()
   deadline=time.time()+5
   while any(t.name=='verified-notifications' for t in threading.enumerate()) and time.time()<deadline:time.sleep(.02)
  wires=observer.finish()
 assert len(wires)==1 and len(r.linux['http'])==2
 p=T.payload(r);assert T.T.D(p['estimated_pnl']['amount'])==9895000 and p['financial_receipt'] is None
 assert T.send(r)==[]
 (Path(__file__).parent/'worker_wire.json').write_text(json.dumps(wires),encoding='utf-8')


def test_default_disabled_reads_no_keys_or_binding(local):
 r=local;r.st.POA_UPBIT_ESTIMATED_READ_ENABLED=False;r.st.UPBIT_KEY=None;r.st.UPBIT_SECRET=None
 assert r.prepare().status=='DISABLED' and r.linux['reads']==0 and not r.linux['http']

@pytest.mark.parametrize('fault',['missing_key','missing_secret','pin','mode','scope_ref','fingerprint','expiry','permissions','account','pocket','strategy','evidence','json'])
def test_config_failures_no_get(local,fault):
 r=local;d=json.loads(r.linux['content'])
 if fault=='missing_key':r.st.UPBIT_KEY=None
 elif fault=='missing_secret':r.st.UPBIT_SECRET=None
 elif fault=='pin':r.st.POA_UPBIT_READ_BINDING_SHA256='f'*64
 elif fault=='mode':r.st.POA_UPBIT_READ_CREDENTIAL_MODE='disabled'
 else:
  if fault=='scope_ref':d['config']['source_account_ref']='f'*64
  elif fault=='fingerprint':d['config']['read_key_fingerprint']='f'*64
  elif fault=='expiry':d['evidence']['expires_at']=time.time()-1
  elif fault=='permissions':d['permissions']=['order']
  elif fault in ('account','pocket'):d['config'][fault+'_uuid']='not-an-id'
  elif fault=='strategy':d['config']['strategy']='default'
  elif fault=='evidence':d['evidence']['digest']=''
  r.linux['content']=json.dumps(d) if fault!='json' else '[]'
  r.st.POA_UPBIT_READ_BINDING_SHA256=hashlib.sha256(r.linux['content'].encode()).hexdigest()
 assert r.prepare().host is None and not r.linux['http']

@pytest.mark.parametrize('status',[401,403])
def test_no_query_permission_safe_no_post(local,status,capsys):
 r=local;r.linux['status']=status;p=r.prepare();assert p.host and T.configure_upbit_notification_host(p.host)
 T.feed(r,'sell');T.tick(r);assert len(r.linux['http'])==1 and T.send(r)==[]
 assert 'CANARY' not in ''.join(capsys.readouterr())

@pytest.mark.parametrize('fault',['account','strategy','market','uuid','side','unfilled'])
def test_scope_and_unfilled_no_post(local,fault):
 r=local;p=r.prepare();assert p.host and T.configure_upbit_notification_host(p.host)
 if fault=='account':r.st.UPBIT_KEY='different-account'
 info=T.T.info('UPBIT','sell')
 if fault=='strategy':info.strategy_id='other-strategy'
 if fault=='market':r.state['orders'][r.sell]['market']='KRW-ETH'
 if fault=='uuid':r.state['orders'][r.sell]['uuid']=r.buy
 if fault=='side':r.state['orders'][r.sell]['side']='bid'
 if fault=='unfilled':r.state['orders'][r.sell].update(state='wait',executed_volume='0',remaining_volume='1',trades_count=0,trades=[])
 r.log('UPBIT',{'id':r.sell},info);T.tick(r);assert T.send(r)==[]

@pytest.mark.parametrize('missing',['buy','fee'])
def test_missing_buy_or_fee(local,missing):
 r=local
 if missing=='fee':
  for x in r.state['orders'].values():x.pop('paid_fee')
 p=r.prepare();assert p.host and T.configure_upbit_notification_host(p.host)
 if missing!='buy':T.feed(r,'buy');T.tick(r)
 T.feed(r,'sell');T.tick(r);v=T.payload(r)['estimated_pnl']
 if missing=='buy':assert v['status']=='UNAVAILABLE'
 else:assert v['fee_mode']=='MISSING' and T.T.D(v['amount'])==10000000
 assert len(T.send(r))==1


def test_settings_dotenv_and_process_precedence(tmp_path,monkeypatch):
 for k in ('POA_UPBIT_ESTIMATED_READ_ENABLED','POA_UPBIT_READ_CREDENTIAL_MODE','POA_UPBIT_READ_BINDING_SHA256'):monkeypatch.delenv(k,raising=False)
 p=tmp_path/'settings.env';p.write_text('PASSWORD=test-only\nPOA_UPBIT_ESTIMATED_READ_ENABLED=true\nPOA_UPBIT_READ_CREDENTIAL_MODE=poa_settings\nPOA_UPBIT_READ_BINDING_SHA256='+'a'*64+'\n')
 s=T.H.t.Settings(_env_file=p);assert s.POA_UPBIT_ESTIMATED_READ_ENABLED is True and L._settings_environment(s)['POA_UPBIT_ESTIMATED_READ_ENABLED']=='true'
 monkeypatch.setenv('POA_UPBIT_ESTIMATED_READ_ENABLED','false');assert not T.H.t.Settings(_env_file=p).POA_UPBIT_ESTIMATED_READ_ENABLED


def test_windows_provider_preserved(local,monkeypatch):
 r=local;monkeypatch.setattr(L,'sys',NS(platform='win32'))
 from exchange.verified_pnl import upbit_notification_local as W
 with patch.object(W,'configure_local_upbit_sources',return_value='LOCAL_SOURCES_UNAVAILABLE') as configure:
  assert r.prepare().host is None and configure.call_count==1 and r.linux['reads']==0


def test_cloud_init_installer_and_service_settings_contract(local):
 d=yaml.safe_load((ROOT/'deployment/cloud-config-ubuntu2204.yaml').read_text(encoding='utf-8'))
 files={x['path']:x for x in d['write_files']};embedded=files['/usr/local/lib/poa/install_once.py']['content']
 assert embedded.rstrip()==(ROOT/'deployment/install_once.py').read_text(encoding='utf-8').rstrip();compile(embedded,'cloud-installer','exec')
 inp=json.loads(files['/root/poa-install-input.json']['content']);assert 'upbit_estimated_read' not in inp and inp['repo_ref']=='main'
 assert files['/root/poa-install-input.json']['permissions']=='0600'
 service=files['/etc/systemd/system/poa.service']['content'];assert 'WorkingDirectory=/root/POA' in service and 'User=root' in service
 assert 'UPBIT_SECRET' not in service and 'EnvironmentFile=' not in service # Pydantic reads .env; no shell JSON quoting ambiguity.
 env,raw=I.upbit_read_configuration({'upbit_estimated_read':local.linux['config']},{'UPBIT_KEY':local.st.UPBIT_KEY,'UPBIT_SECRET':local.st.UPBIT_SECRET})
 assert env['POA_UPBIT_ESTIMATED_READ_ENABLED'] and env['POA_UPBIT_READ_BINDING_SHA256']==hashlib.sha256(raw.encode()).hexdigest()
 assert local.st.UPBIT_SECRET not in raw and local.st.UPBIT_KEY not in raw
 assert json.loads(raw)['key_usage']=='POA_SETTINGS_SHARED_TRADING_KEY'
 assert I.upbit_read_configuration({}, {})[0]['POA_UPBIT_ESTIMATED_READ_ENABLED'] is False

@pytest.mark.parametrize('fault',['shared','symlink','owner','hardlink','empty','large'])
def test_posix_binding_stat_policy(local,monkeypatch,fault):
 info=NS(st_mode=stat.S_IFREG|0o600,st_uid=1000,st_nlink=1,st_size=100)
 monkeypatch.setattr(L.os,'geteuid',lambda:1000,raising=False)
 if fault=='shared':info.st_mode|=0o040
 if fault=='symlink':info.st_mode=stat.S_IFLNK|0o600
 if fault=='owner':info.st_uid=2000
 if fault=='hardlink':info.st_nlink=2
 if fault=='empty':info.st_size=0
 if fault=='large':info.st_size=8193
 with pytest.raises(L.LinuxReadConfigurationError):L._check_binding_stat(info)

@pytest.mark.parametrize('fault',['missing_uuid','strategy','permission','expired','missing_key'])
def test_installer_rejects_unsafe_optin(local,fault):
 r=local;cfg=copy.deepcopy(r.linux['config']);values={'UPBIT_KEY':r.st.UPBIT_KEY,'UPBIT_SECRET':r.st.UPBIT_SECRET}
 if fault=='missing_uuid':cfg['account_uuid']=''
 if fault=='strategy':cfg['strategy']='default'
 if fault=='permission':cfg['permissions']=['order']
 if fault=='expired':cfg['evidence']['expires_at']=0
 if fault=='missing_key':values['UPBIT_KEY']=''
 with pytest.raises(I.InstallError,match='UPBIT_READ_BINDING_REQUIRED_OR_INVALID'):I.upbit_read_configuration({'upbit_estimated_read':cfg},values)


def test_secure_read_posix_syscalls_bounded_no_follow(monkeypatch,tmp_path):
 # POSIX syscall contract simulation only: this is not native Ubuntu execution.
 info=NS(st_mode=stat.S_IFREG|0o600,st_uid=1000,st_nlink=1,st_size=2,st_ino=8,st_dev=1,st_mtime_ns=42)
 seen={};chunks=[b'{}',b'']
 def opened(path,flags):seen['flags']=flags;return 77
 fake=NS(O_RDONLY=0,O_NOFOLLOW=0x100,O_CLOEXEC=0x200,open=opened,fstat=lambda fd:info,read=lambda fd,n:chunks.pop(0),close=lambda fd:seen.update(closed=fd),geteuid=lambda:1000)
 monkeypatch.setattr(L,'os',fake);monkeypatch.setattr(L,'sys',NS(platform='linux'))
 assert L._secure_read(str(tmp_path/'binding.json'))==b'{}'
 assert seen['flags']==0x300 and seen['closed']==77


def test_linux_canary_not_logged_persisted_or_wire(local,capsys):
 from loguru import logger
 r=local;logs=[];sink=logger.add(lambda m:logs.append(str(m)))
 try:
  p=r.prepare();assert p.host and T.configure_upbit_notification_host(p.host)
  for side in ('buy','sell'):T.feed(r,side);T.tick(r)
  wires=T.send(r)
 finally:logger.remove(sink)
 with T.store(r)._connect_read() as c:dump='\n'.join(c.iterdump())
 capture=''.join(capsys.readouterr());text=dump+json.dumps(wires)+''.join(logs)+capture
 assert all(x not in text for x in ('FAKE_ACCESS_KEY_CANARY','FAKE_SECRET_KEY_CANARY','Authorization'))


def test_linux_registry_change_after_host_construction_rejects(local):
 r=local;p=r.prepare();assert p.host
 d=json.loads(r.linux['content']);d['config']['strategy']='different-strategy';r.linux['content']=json.dumps(d)
 T.configure_upbit_notification_host(p.host);T.feed(r,'sell');T.tick(r);assert not r.linux['http'] and T.send(r)==[]


def test_explicit_process_flag_overrides_settings_snapshot(local,monkeypatch):
 r=local;r.st.POA_UPBIT_ESTIMATED_READ_ENABLED=False;monkeypatch.setenv('POA_UPBIT_ESTIMATED_READ_ENABLED','true')
 assert r.prepare().host is not None
 monkeypatch.setenv('POA_UPBIT_ESTIMATED_READ_ENABLED','false');reads=r.linux['reads']
 assert r.prepare().status=='DISABLED' and r.linux['reads']==reads


def test_installer_env_roundtrip_through_real_settings(local,tmp_path,monkeypatch):
 r=local
 for k in ('POA_UPBIT_ESTIMATED_READ_ENABLED','POA_UPBIT_READ_BINDING_SHA256'):monkeypatch.delenv(k,raising=False)
 env,raw=I.upbit_read_configuration({'upbit_estimated_read':r.linux['config']},{'UPBIT_KEY':r.st.UPBIT_KEY,'UPBIT_SECRET':r.st.UPBIT_SECRET})
 env.update(PASSWORD='test-only',UPBIT_KEY=r.st.UPBIT_KEY,UPBIT_SECRET=r.st.UPBIT_SECRET)
 text=''.join(k+'='+json.dumps(v,ensure_ascii=False)+'\n' for k,v in env.items())
 p=tmp_path/'installed.env';p.write_bytes(text.encode('utf-8'))
 settings=T.H.t.Settings(_env_file=p)
 assert settings.POA_UPBIT_ESTIMATED_READ_ENABLED and settings.POA_UPBIT_READ_CREDENTIAL_MODE=='poa_settings'
 sources=L._linux_sources(settings,L._settings_environment(settings));assert sources.config.source_account_ref==T.H.identity('UPBIT',r.st.UPBIT_KEY)


@pytest.mark.parametrize('value,expected',[('TRUE','ready'),('True','ready'),('1','ready'),('on','ready'),('false','disabled'),('0','disabled'),('no','disabled'),('invalid','rejected')])
def test_process_boolean_spellings_match_settings_policy(local,monkeypatch,value,expected):
 r=local;monkeypatch.setenv('POA_UPBIT_ESTIMATED_READ_ENABLED',value);p=r.prepare()
 if expected=='ready':assert p.host is not None
 elif expected=='disabled':assert p.status=='DISABLED'
 else:assert p.host is None
 assert not r.linux['http']
