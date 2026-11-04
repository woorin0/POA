"""Verify non-circular User Data pin against an existing product commit."""
import hashlib
import ast
import json
from pathlib import Path
import re
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]


def verify():
    data = yaml.safe_load((ROOT/'deployment/cloud-config-ubuntu2204.yaml').read_text(encoding='utf-8'))
    files = {f['path']: f['content'] for f in data['write_files']}
    config = json.loads(files['/root/poa-stage-input.json'])
    assert config['install_mode'] == 'NEW_SERVER_STAGE_ONLY'
    commit = config['repo_ref']; digest = config['release_manifest_sha256']
    assert re.fullmatch('[0-9a-f]{40}', commit) and re.fullmatch('[0-9a-f]{64}', digest)
    node=next(n for n in ast.parse((ROOT/'deployment/build_cloud_config.py').read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='build_ubuntu')
    ns={'HERE':ROOT/'deployment','json':json}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'actual_cloud_config_builder','exec'),ns)
    assert (ROOT/'deployment/cloud-config-ubuntu2204.yaml').read_bytes()==ns['build_ubuntu'](commit,digest).encode('utf-8')
    def blob(path):
        return subprocess.check_output(['git', 'show', commit+':'+path], cwd=ROOT)
    manifest = blob('release_manifest_sha256.json')
    assert manifest == (ROOT/'release_manifest_sha256.json').read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == digest
    entries = json.loads(manifest)
    assert len(entries) == 70
    assert not {'release_manifest_sha256.json', 'deployment/cloud-config-ubuntu2204.yaml'} & {e['Path'] for e in entries}
    for e in entries:
        raw = blob(e['Path'])
        assert raw == (ROOT/e['Path']).read_bytes()
        assert len(raw) == e['Bytes'] and hashlib.sha256(raw).hexdigest().upper() == e['SHA256']
    for name in ('install_once.py', 'prepare_ubuntu.py'):
        assert files['/usr/local/lib/poa/'+name] == blob('deployment/'+name).decode('utf-8')
    units = [v for k, v in files.items() if k.endswith('.service.staged')]
    assert len(units) == 2 and all('ExecStartPre=/usr/bin/false' in u and 'Restart=no' in u for u in units)
    assert set(config) == {'install_mode', 'repo_ref', 'release_manifest_sha256'}
    print('PRODUCT_COMMIT_B='+commit+'; MANIFEST_SHA256='+digest+'; PACKAGING=PASS')


if __name__ == '__main__':
    verify()
