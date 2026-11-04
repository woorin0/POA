"""Actual HTTP adapters; only exchange execution and notification sinks are Fake."""
import importlib.util
import ast,asyncio,importlib.metadata as M,ipaddress,json,math,os,sys,types
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=Path(sys.argv[1])
os.environ['PASSWORD']='synthetic-probe-password'
# Never import real exchange startup (DB/auth); no real key is supplied.
for name,folder in [('exchange','exchange'),('exchange.utility','exchange/utility')]:
    m=types.ModuleType(name);m.__path__=[str(ROOT/folder)];sys.modules[name]=m
sys.path.insert(0,str(ROOT))
from fastapi import FastAPI,Request,BackgroundTasks,status
from fastapi.responses import ORJSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
RESULT={'versions':{p:M.version(p) for p in ('fastapi','starlette','pydantic','httpx','h11')},'boundary_source':'actual_product'}
baseline=False
source=(OUT/'baseline/exchange/model/schemas.py') if baseline else ROOT/'exchange/model/schemas.py'
tree=ast.parse(source.read_text(encoding='utf-8'))
modern=M.version('pydantic').startswith('2.')
schema=types.ModuleType('probe_schema');schema.__file__=str(source);sys.modules[schema.__name__]=schema
exec(compile(tree,str(source),'exec'),schema.__dict__);schema.Settings.Config.env_file=None
if baseline:
    spec=importlib.util.spec_from_file_location('exchange.verified_pnl.models',OUT/'baseline/exchange/verified_pnl/models.py')
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
import inspect
calls=[]
def norm(x):
    if isinstance(x,float) and not math.isfinite(x):return {'nonfinite':str(x)}
    if isinstance(x,dict):return {k:norm(v) for k,v in x.items()}
    if isinstance(x,list):return [norm(v) for v in x]
    return x
class FakeBot:
    def init_info(self,info):self.order_info=info
    def record(self,info):
        fields=('exchange','base','quote','unified_symbol','side','amount','price','cost','percent','strategy_id','signal_id','order_name','tv_signal','is_futures','is_coinm','is_entry','is_close','is_buy','is_sell')
        calls.append(norm({k:getattr(info,k,None) for k in fields}));return {'id':'synthetic-order'}
    market_buy=record;market_sell=record;market_entry=record;market_close=record
app=FastAPI(default_response_class=ORJSONResponse)
ns=dict(Request=Request,BackgroundTasks=BackgroundTasks,MarketOrder=schema.MarketOrder,PriceRequest=schema.PriceRequest,HedgeData=schema.HedgeData,
    ORJSONResponse=ORJSONResponse,status=status,ipaddress=ipaddress,whitelist=['testclient','127.0.0.1'],settings=types.SimpleNamespace(SAFE_RETRY_ENABLED=False,VERIFIED_DISCORD_ALERTS=False),
    get_bot=lambda *a:FakeBot(),schedule_order_log=lambda *a:None,log_error_message=lambda *a:None,log_validation_error_message=lambda *a:None)
main_source=(OUT/'baseline/main.py') if baseline else ROOT/'main.py'
main=ast.parse(main_source.read_text(encoding='utf-8'))
ns.update(RequestValidationError=RequestValidationError,ModelField=__import__('pydantic.fields' if baseline else 'pydantic.v1.fields',fromlist=['ModelField']).ModelField,json=json,ValidationError=__import__('pydantic' if baseline else 'pydantic.v1',fromlist=['ValidationError']).ValidationError)
def prices(*a):
    calls.append({'price_args':list(a)});return '123.45'
ns['get_exchange']=lambda name:types.SimpleNamespace(dict=lambda:{name:types.SimpleNamespace(fetch_price=prices)})
for name in (('whitelist_middleware','validation_exception_handler','order','price','hedge') if baseline else ('whitelist_middleware','validation_exception_handler','_v1_request','order','price','hedge','_order_http','_price_http','_hedge_http')):
    n=next(n for n in main.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name);n.decorator_list=[]
    exec(compile(ast.Module(body=[n],type_ignores=[]),'actual_main_boundary','exec'),ns)
app.middleware('http')(ns['whitelist_middleware']);app.add_exception_handler(RequestValidationError,ns['validation_exception_handler'])
for route in ('order','price','hedge'):
    app.add_api_route('/'+route,ns[route if baseline else '_'+route+'_http'],methods=['POST'])
client=TestClient(app,raise_server_exceptions=False)
cases=[]
for venue,quote in [('UPBIT','KRW'),('BINANCE','USDT'),('BYBIT','USDT'),('OKX','USDT')]:
 for base in ('BTC','XRP'):
  for side in ('buy','sell'):
   cases.append((venue+'-'+base+'-'+side,dict(password='synthetic-probe-password',exchange=venue,base=base,quote=quote,side=side,amount=.005,price=100,order_name='LL' if side=='buy' else 'Close_HL',strategy_id='synthetic-strategy',signal_id=venue+base+side)))
base=cases[0][1]
for field,values in {'amount':['0.000001',0,-1,True,None,'NaN','Infinity',float('nan')], 'price':['100.1',0,-1,'NaN','Infinity'], 'password':['wrong',None,123], 'strategy_id':['synthetic-long-strategy',12,None], 'type':['limit','market'],'exchange':['wrong'],'side':['entry/buy','close/sell','wrong']}.items():
 for i,v in enumerate(values):cases.append((field+'-'+str(i),dict(base,**{field:v})))
for field in ('password','amount','side','quote'):
 data=dict(base);data.pop(field);cases.append(('missing-'+field,data))
cases += [('body-null',None),('body-list',[])]
signal=dict(kind='strategy_order_fill',price='100',quantity='0.005',action='buy',order_id='LL',signal_id='tv1',strategy_id='synthetic-strategy')
cases.append(('tv-valid',dict(base,signal_id='tv1',tv_signal=signal)))
cases.append(('tv-cross-strategy',dict(base,signal_id='tv1',tv_signal=dict(signal,strategy_id='different'))))
observations=[]
for name,data in cases:
    before=len(calls)
    response=client.post('/order',content=json.dumps(data,allow_nan=True),headers={'Content-Type':'application/json'})
    try:body=response.json()
    except ValueError:body={'non_json_status':response.status_code}
    observations.append(dict(case=name,status=response.status_code,response=body,engine_calls=calls[before:]))
# Price/hedge HTTP boundary, including null/malformed bodies. No hedge legs:
# original Safe Retry guard is kept enabled and must reject before get_bot.
for route,valid in [('price',dict(exchange='UPBIT',base='XRP',quote='KRW')),('hedge',dict(password='synthetic-probe-password',exchange='BINANCE',base='BTC',quote='USDT.P',amount=.01,hedge='ON'))]:
 for name,data in [('valid',valid),('null',None),('empty',{}),('list',[]),('wrong-venue',dict(valid,exchange='wrong')),('wrong-password',dict(valid,password='wrong'))]:
    ns['settings'].SAFE_RETRY_ENABLED=True
    before=len(calls);response=client.post('/'+route,content=json.dumps(data),headers={'Content-Type':'application/json'})
    observations.append(dict(case=route+'-'+name,status=response.status_code,response=response.json(),engine_calls=calls[before:]))
for raw in ('', '{', '[1]', '"text"'):
 before=len(calls);response=client.post('/order',content=raw,headers={'Content-Type':'application/json'})
 observations.append(dict(case='raw-'+repr(raw),status=response.status_code,response=response.json(),engine_calls=calls[before:]))
# Content-Type changes must preserve the old parser, authentication and calls.
ns['settings'].SAFE_RETRY_ENABLED=False
media=[]
for ct in ('text/plain','application/octet-stream','application/json','application/problem+json','application/json; charset=utf-8',''):
 for name,raw in [('valid',json.dumps(base)),('bad-auth',json.dumps(dict(base,password='wrong'))),('null','null'),('empty',''),('malformed','{'),('invalid-utf8',b'\xff')]:
    before=len(calls);headers={'Content-Type':ct} if ct else {}
    response=client.post('/order',content=raw,headers=headers)
    media.append(dict(case=ct+'|'+name,status=response.status_code,response=response.json(),engine_calls=calls[before:]))
RESULT['media_observations']=media
# Original whitelist on an explicitly unapproved public client, no real socket.
async def next_call(req):return ORJSONResponse({'allowed':True})
async def rejected():
 request=Request({'type':'http','method':'POST','path':'/order','headers':[],'client':('8.8.8.8',123),'server':('localhost',80),'scheme':'http','query_string':b''})
 response=await ns['whitelist_middleware'](request,next_call);return response.status_code
RESULT['whitelist_unapproved_status']=asyncio.run(rejected())
RESULT['observations']=observations
RESULT['scope']='Final source boundary / preserved baseline DTO + original middleware/auth/order function; only external bot/notification endpoints replaced with Fake. Bridge parses raw HTTP into preserved v1 DTO. No production transformation applied.'
(OUT/('http_contract_'+('baseline' if baseline else 'product')+'.json')).write_text(json.dumps(RESULT,ensure_ascii=False,indent=2))
print('HTTP cases',len(observations),'mode',('v2_bridge' if modern else 'baseline'),'whitelist',RESULT['whitelist_unapproved_status'])
