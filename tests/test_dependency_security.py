"""Patched dependency APIs. No bridge, external HTTP or real credential inputs."""
import inspect,json
from pathlib import Path
import jwt,orjson,pytest
from harness import ROOT,setup
schema,_,_=setup(ROOT)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import ORJSONResponse

def test_pocketbase_unsigned_expiry_metadata_api_preserved():
    token=jwt.encode({'exp':1,'sub':'synthetic'},'synthetic-test-key-at-least-32-bytes',algorithm='HS256')
    # Same API used by pocket.reauth; this is metadata parsing, not authentication.
    assert jwt.decode(token,options={'verify_signature':False})['exp']==1

def test_jwt_forgery_and_unknown_critical_extension_rejected():
    key='synthetic-test-key-at-least-32-bytes'
    token=jwt.encode({'sub':'synthetic'},key,algorithm='HS256')
    with pytest.raises(jwt.InvalidSignatureError):jwt.decode(token,'different-test-key-at-least-32-bytes',algorithms=['HS256'])
    token=jwt.encode({'sub':'synthetic'},key,algorithm='HS256',headers={'crit':['unsupported-test-extension'],'unsupported-test-extension':True})
    with pytest.raises(jwt.InvalidTokenError):jwt.decode(token,key,algorithms=['HS256'])

def test_orjson_encode_decode_recursion_bounds():
    value=0
    for _ in range(1500):value=[value]
    with pytest.raises(TypeError):orjson.dumps(value)
    with pytest.raises(orjson.JSONDecodeError):orjson.loads('['*1500+'0'+']'*1500)
    assert orjson.loads(orjson.dumps({'value':0.005,'unit':'KRW'}))=={'value':0.005,'unit':'KRW'}

@pytest.mark.parametrize('venue,quote',[('UPBIT','KRW'),('BINANCE','USDT'),('BYBIT','USDT'),('OKX','USDT')])
@pytest.mark.parametrize('side',['buy','sell'])
def test_pydantic_v1_order_api_and_orjson_response(venue,quote,side):
    import ast
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from pydantic.v1 import ValidationError
    from pydantic.v1.fields import ModelField
    node=next(n for n in ast.parse((ROOT/'main.py').read_text(encoding='utf-8')).body if getattr(n,'name','')=='_v1_request')
    ns=dict(Request=Request,RequestValidationError=RequestValidationError,ValidationError=ValidationError,ModelField=ModelField,json=json)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'actual_main_v1_boundary','exec'),ns)
    app=FastAPI(default_response_class=ORJSONResponse)
    @app.post('/order')
    async def route(request:Request):
        order=await ns['_v1_request'](request,schema.OrderRequest)
        return {'exchange':order.exchange,'symbol':order.unified_symbol,'side':order.side,'amount':order.amount}
    client=TestClient(app)
    response=client.post('/order',json={'exchange':venue,'base':'BTC','quote':quote,'side':side,'amount':0.005})
    assert response.status_code==200
    assert response.json()=={'exchange':venue,'symbol':'BTC/'+quote,'side':side,'amount':0.005}
    assert client.post('/order',json={'exchange':'not-a-venue','base':'BTC','quote':quote,'side':side,'amount':0.005}).status_code==422

def test_dependency_pin_is_exact_and_rejects_extra_changes(tmp_path):
    from release_checks.verify_release import assert_dependency_source_pin
    assert_dependency_source_pin(ROOT)
    p=tmp_path/'requirements.txt';p.write_bytes((ROOT/'requirements.txt').read_bytes()+b'extra-unreviewed==1.0\n')
    with pytest.raises(AssertionError):assert_dependency_source_pin(tmp_path)

def test_dependency_test_not_in_legacy_bridge():
    import conftest
    assert Path(__file__).name not in conftest.LEGACY_V2_TEST_FILES
    from exchange.verified_pnl import evidence
    assert inspect.getsourcefile(evidence.verify).replace('\\','/').endswith('exchange/verified_pnl/evidence.py')


def test_packaging_has_no_manifest_or_commit_self_reference():
    import ast,hashlib
    from pathlib import Path
    import json
    path=ROOT/'release_manifest_sha256.json';original=path.read_bytes()
    paths={e['Path'] for e in json.loads(original)}
    assert 'release_manifest_sha256.json' not in paths
    assert 'deployment/cloud-config-ubuntu2204.yaml' not in paths
    source=ROOT/'deployment/build_cloud_config.py'
    node=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='build_ubuntu')
    ns={'HERE':source.parent,'json':json}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
    digest=hashlib.sha256(original).hexdigest()
    first=ns['build_ubuntu']('1'*40,digest);second=ns['build_ubuntu']('2'*40,digest)
    assert first!=second and path.read_bytes()==original
    for text in (first,second):
        result=json.loads(text.split('\n',1)[1]);f=next(x for x in result['write_files'] if x['path']=='/root/poa-stage-input.json')
        assert json.loads(f['content'])['release_manifest_sha256']==digest
