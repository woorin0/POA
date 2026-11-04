import hashlib, importlib.util, json, sqlite3, zipfile
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('stage_installer',ROOT/'deployment/install_once.py')
I=importlib.util.module_from_spec(spec);spec.loader.exec_module(I)

def config():return dict(install_mode='NEW_SERVER_STAGE_ONLY',repo_ref='d'*40,release_manifest_sha256='a'*64)

@pytest.mark.parametrize('field,value',[('install_mode','REINSTALL'),('repo_ref','main'),('release_manifest_sha256','wrong'),('credentials',{})])
def test_stage_invalid_input_rejected(field,value):
    data=config();data[field]=value
    with pytest.raises(I.InstallError):I.validate_stage(data)

@pytest.mark.parametrize('name',['.env','pb_data','notifications.sqlite3','notifications.sqlite3-wal','safe_orders.sqlite3','notifications.sqlite3.intake.sqlite3','notifications.sqlite3.ops.sqlite3','strategy.json'])
def test_existing_data_blocks_before_commands(tmp_path,monkeypatch,name):
    target=tmp_path/name;target.write_bytes(b'preserve')
    monkeypatch.setattr(I,'STAGE_EXISTING',(str(target),))
    monkeypatch.setattr(I,'run',lambda *a,**k:pytest.fail('command before existing-data guard'))
    with pytest.raises(I.InstallError,match='PRESERVED'):I.stage_ubuntu(config())
    assert target.read_bytes()==b'preserve'

@pytest.mark.parametrize('fault',['bytes','hash','duplicate','traversal'])
def test_manifest_tamper_rejected(tmp_path,monkeypatch,fault):
    monkeypatch.setattr(I,'APP',tmp_path);(tmp_path/'main.py').write_bytes(b'x')
    entry=dict(Path='main.py',Bytes=1,SHA256=hashlib.sha256(b'x').hexdigest().upper())
    if fault=='bytes':entry['Bytes']=2
    if fault=='hash':entry['SHA256']='0'*64
    if fault=='traversal':entry['Path']='../main.py'
    data=[entry,entry] if fault=='duplicate' else [entry]
    (tmp_path/'release_manifest_sha256.json').write_text(json.dumps(data))
    with pytest.raises(I.InstallError):I.verify_source()

def test_synthetic_sqlite_consistent_backup_and_restore(tmp_path):
    # Quiesced writer group: both databases frozen before either backup begins.
    before={};after={}
    for name in ('notification','sidecar','safe_retry'):
        source=tmp_path/(name+'.sqlite3');backup=tmp_path/(name+'.backup')
        with sqlite3.connect(source) as c:
            c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=FULL')
            c.execute('CREATE TABLE immutable_state(id TEXT PRIMARY KEY,state TEXT,wire BLOB,token TEXT)')
            for state in ('SENT','UNKNOWN','HELD','POSTING','PENDING','SUBMITTED','SIGNAL_DEDUP'):
                c.execute('INSERT INTO immutable_state VALUES(?,?,?,?)',(state,state,b'frozen-test', 'test-ack'))
            c.commit();before[name]=c.execute('SELECT * FROM immutable_state ORDER BY id').fetchall()
            with sqlite3.connect(backup) as dst:c.backup(dst)
        with sqlite3.connect(backup) as c:
            assert c.execute('PRAGMA integrity_check').fetchone()==('ok',)
            after[name]=c.execute('SELECT * FROM immutable_state ORDER BY id').fetchall()
    assert before==after
    with sqlite3.connect(tmp_path/'notification.backup') as c:
        c.execute("DELETE FROM immutable_state WHERE state='UNKNOWN'")
        assert c.execute('SELECT * FROM immutable_state ORDER BY id').fetchall()!=before['notification']

@pytest.mark.parametrize('corrupt',[False,True])
def test_stage_only_no_service_start_no_credentials(tmp_path,monkeypatch,corrupt):
    app=tmp_path/'POA';db=tmp_path/'db';result=tmp_path/'result.json';calls=[]
    monkeypatch.setattr(I,'APP',app);monkeypatch.setattr(I,'DB',db);monkeypatch.setattr(I,'STAGE_RESULT',result)
    monkeypatch.setattr(I,'STAGE_EXISTING',(str(app),str(db),str(result)))
    monkeypatch.setattr(I.os,'geteuid',lambda:0,raising=False)
    read=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda p,*a,**k:'ID=ubuntu\nVERSION_ID="22.04"' if p.as_posix()=='/etc/os-release' else read(p,*a,**k))
    validations=[];monkeypatch.setattr(I,'verify_source',lambda digest:validations.append(digest))
    def command(args,code,**kw):
        calls.append(args)
        if args[:2]==['dpkg','--print-architecture']:return b'amd64'
        if args[:2]==['git','init']:app.mkdir()
        if 'rev-parse' in args:return b'd'*40
        if args[0]=='curl':
            with zipfile.ZipFile(db/'pocketbase.zip','w') as z:z.writestr('pocketbase',b'test-binary')
            monkeypatch.setattr(I,'PB_SHA256','0'*64 if corrupt else hashlib.sha256((db/'pocketbase.zip').read_bytes()).hexdigest())
        return b''
    monkeypatch.setattr(I,'run',command)
    if corrupt:
        with pytest.raises(I.InstallError,match='POCKETBASE_HASH_MISMATCH'):I.stage_ubuntu(config())
        assert not (db/'pocketbase').exists() and not result.exists()
        assert not any(c[0]=='systemctl' for c in calls)
        return
    I.stage_ubuntu(config())
    bootstrap=next(c for c in calls if 'pip==26.3' in c)
    assert bootstrap==[str(app/'.venv/bin/python'),'-m','pip','install','--no-cache-dir','pip==26.3','setuptools==84.2.1']
    assert any(c[:2]==[str(app/'.venv/bin/python'),'-c'] and "m.version('setuptools') == '84.2.1'" in c[2] for c in calls)
    assert validations==['a'*64]*2
    assert I.os.environ['PYTHONDONTWRITEBYTECODE']=='1'
    assert any(str(I.APP/'release_checks/run.py') in c for c in calls)
    assert not any(c[0]=='systemctl' for c in calls)
    assert not (app/'.env').exists() and not (app/'ENABLE_LIVE_SERVICE').exists()
    assert json.loads(result.read_text())['status']=='STAGED_NOT_LIVE'
    assert json.loads(result.read_text())['RESTORE_VERIFIED']=='NO'
    with pytest.raises(I.InstallError,match='PRESERVED'):I.stage_ubuntu(config())

def test_user_data_secret_free_and_embedded_sources_match():
    import yaml
    ci=yaml.load((ROOT/'.github/workflows/pr_verification.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    assert ci['on']['push']['branches']==['main'] and ci['on']['pull_request']['branches']==['main']
    steps=ci['jobs']['offline']['steps']
    install=next(s['run'] for s in steps if s.get('name')=='Install isolated test dependencies')
    assert 'pip==26.3 setuptools==84.2.1' in install and "m.version('setuptools') == '84.2.1'" in install
    audit=next(s['run'] for s in steps if s.get('name')=='Audit complete installed Python environment (no suppressions)')
    assert 'pip freeze --all' in audit and '--ignore-vuln' not in audit
    # Product D excludes packaging E. Test the actual builder here; the separate
    # packaging gate checks the complete stored E document against this output.
    import ast
    node=next(n for n in ast.parse((ROOT/'deployment/build_cloud_config.py').read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='build_ubuntu')
    ns={'HERE':ROOT/'deployment','json':json}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'actual_cloud_config_builder','exec'),ns)
    data=yaml.safe_load(ns['build_ubuntu']('d'*40,'a'*64))
    files={x['path']:x for x in data['write_files']}
    inputs=json.loads(files['/root/poa-stage-input.json']['content']);I.validate_stage(inputs)
    assert 'credentials' not in inputs
    for name in ('install_once.py','prepare_ubuntu.py'):
        assert files['/usr/local/lib/poa/'+name]['content']==(ROOT/'deployment'/name).read_text(encoding='utf-8')
    units=[v['content'] for k,v in files.items() if k.endswith('.service.staged')]
    assert len(units)==2 and all('ExecStartPre=/usr/bin/false' in s and 'Restart=no' in s for s in units)
    assert all('install_once.py' not in str(c) or '--live' not in str(c) for c in data['runcmd'])
    assert not any('systemctl enable' in str(c) or 'ENABLE_LIVE_SERVICE' in str(c) for c in data['runcmd'])


def test_ubuntu_route_cannot_fall_through_to_legacy_live_installer(monkeypatch):
    calls=[];data=config()
    monkeypatch.setattr(I,'runtime_for_os',lambda x:'python3.10')
    monkeypatch.setattr(Path,'read_text',lambda *a,**k:'ID=ubuntu')
    monkeypatch.setattr(I,'stage_ubuntu',lambda d:calls.append(d))
    monkeypatch.setattr(I,'validate',lambda d:pytest.fail('legacy live path invoked'))
    I.install(data);assert calls==[data]

def test_no_persistent_mask_collides_with_fresh_app_precheck():
    source=(ROOT/'deployment/prepare_ubuntu.py').read_text(encoding='utf-8')
    assert "['systemctl', 'mask', '--runtime', 'poa.service', 'pocketbase.service']" in source
    assert "['systemctl', 'mask', 'caddy.service']" in source
    assert source.index('assert_fresh_stage()')<source.index("command(['systemctl'")


def test_deployment_source_pins_and_preserved_debian():
    from release_checks.verify_release import assert_deployment_source_pins
    assert len(assert_deployment_source_pins(ROOT))==3

def test_unapproved_installer_change_rejected(tmp_path):
    from release_checks.verify_release import APPROVED_STAGE_PINS,assert_deployment_source_pins
    for name in APPROVED_STAGE_PINS:
        target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((ROOT/name).read_bytes())
    target=tmp_path/'deployment/install_once.py'
    target.write_bytes(target.read_bytes()+b'\n# unauthorized mutation\n')
    with pytest.raises(AssertionError):assert_deployment_source_pins(tmp_path)
