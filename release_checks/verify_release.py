"""Read-only release checks; no secrets, application startup or external requests."""
import hashlib,json,re,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

APPROVED_STAGE_PINS = {'deployment/build_cloud_config.py': '8093e3b61591972b3983e9fb0b007449b196355feaccbb60bf8036b7585fec64', 'deployment/install_once.py': 'c54171f248126db1c20b73f8514b2fb8cb9e8611f170ae0dad9709d4fc466816', 'deployment/prepare_ubuntu.py': '7d8f570f756cd41694001de12b271f4dc443fd21a45a06acc1b68c972bad605b'}

def assert_deployment_source_pins(root=ROOT):
    """Only these exact Ubuntu staging bytes; no broad deployment exception."""
    import ast,copy
    root=Path(root)
    for name,expected in APPROVED_STAGE_PINS.items():
        assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected,name
    base='de969040dec42a369274adb1a59a3d44a2ba3aa6'
    def before(name):
        return ast.parse(subprocess.check_output(['git','show',base+':'+name],cwd=ROOT).decode('utf-8'))
    name='deployment/build_cloud_config.py';old=before(name);new=ast.parse((root/name).read_text(encoding='utf-8'))
    new.body=[n for n in new.body if not (isinstance(n,ast.FunctionDef) and n.name=='build_ubuntu') and not isinstance(n,ast.If)]
    assert ast.dump(old,include_attributes=False)==ast.dump(new,include_attributes=False),'Debian builder changed'
    name='deployment/install_once.py';old=before(name);new=ast.parse((root/name).read_text(encoding='utf-8'))
    additions={'STAGE_EXISTING','STAGE_INPUT','STAGE_RESULT','PB_SHA256'}
    functions={'validate_stage','assert_fresh_stage','stage_ubuntu'}
    new.body=[n for n in new.body if not (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in additions for t in n.targets)) and not (isinstance(n,ast.FunctionDef) and n.name in functions)]
    for i,n in enumerate(new.body):
        if isinstance(n,ast.FunctionDef) and n.name=='install':
            assert isinstance(n.body[0],ast.If)
            n.body=n.body[1:]
        elif isinstance(n,ast.FunctionDef) and n.name=='verify_source':
            new.body[i]=copy.deepcopy(next(x for x in old.body if isinstance(x,ast.FunctionDef) and x.name=='verify_source'))
        elif isinstance(n,ast.If):
            new.body[i]=copy.deepcopy(next(x for x in old.body if isinstance(x,ast.If)))
    assert ast.dump(old,include_attributes=False)==ast.dump(new,include_attributes=False),'Legacy installer changed'
    return frozenset(APPROVED_STAGE_PINS)


APPROVED_DEPENDENCY_PATCH = {'PyJWT': ('2.7.0','2.15.1'), 'fastapi': ('0.99.0','0.133.0'), 'httpx': ('0.23.3','0.27.2'), 'pocketbase': ('0.8.2','0.12.2'), 'pydantic[dotenv]': ('1.10.10',None), 'orjson': ('3.9.1','3.11.6')}
ADDED_SECURITY_PINS = {'starlette':'1.3.1','httpcore':'1.0.9','h11':'0.16.0','pydantic':'2.9.2','python-dotenv':'1.2.4'}

def assert_dependency_source_pin(root=ROOT):
    original=subprocess.check_output(['git','show','de969040dec42a369274adb1a59a3d44a2ba3aa6:requirements.txt'],cwd=ROOT).decode('utf-8').splitlines()
    expected=dict(line.split('==') for line in original if line)
    for name,(before,after) in APPROVED_DEPENDENCY_PATCH.items():
        assert expected[name]==before
        if after is None:del expected[name]
        else:expected[name]=after
    assert not set(ADDED_SECURITY_PINS)&set(expected)
    expected.update(ADDED_SECURITY_PINS)
    lines=(Path(root)/'requirements.txt').read_text(encoding='utf-8').splitlines()
    actual=dict(line.split('==') for line in lines)
    assert len(lines)==len(actual) and actual==expected,'Unapproved dependency change'


def verify():
    assert_deployment_source_pins()
    assert_dependency_source_pin()
    from release_checks.http_source_contract import restore_http_source,assert_http_source_pins
    assert_http_source_pins()
    entries=json.loads((ROOT/'release_manifest_sha256.json').read_text(encoding='utf-8'))
    assert len(entries)==70 and len({e['Path'] for e in entries})==70
    for e in entries:
        p=ROOT/e['Path'];assert ROOT in p.resolve().parents
        raw=p.read_bytes();assert len(raw)==e['Bytes'] and hashlib.sha256(raw).hexdigest().upper()==e['SHA256'],e['Path']
    # Notification and trade implementation MUST remain byte-identical to main.
    names=subprocess.check_output(['git','ls-tree','-r','--name-only','de969040dec42a369274adb1a59a3d44a2ba3aa6'],cwd=ROOT).decode().splitlines()
    protected=[n for n in names if n.startswith('exchange/') or n in ('main.py','run.py','requirements.txt')]
    for n in protected:
        if n=='requirements.txt':continue # Exact four-version patch asserted above.
        original=subprocess.check_output(['git','show','de969040dec42a369274adb1a59a3d44a2ba3aa6:'+n],cwd=ROOT)
        assert restore_http_source(n,(ROOT/n).read_bytes())==original,n
    print('MANIFEST_70=PASS; PROTECTED_APP_FILES='+str(len(protected)-1)+'; EXACT_DEPENDENCY_PATCH=PASS')
    print('MANIFEST_SHA256='+hashlib.sha256((ROOT/'release_manifest_sha256.json').read_bytes()).hexdigest())
if __name__=='__main__':verify()
