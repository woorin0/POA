"""Windows GitHub Actions tests of historical audit v2: synthetic data, zero live IO.

Runs only in an isolated worktree pinned to PR4. The observed ReadHost,
Settings, render, and Discord Embed are fakes. No trading, exchange API,
production database, or Discord network connection is ever attempted.
"""
from __future__ import annotations
import argparse
import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from decimal import Decimal, localcontext
from unittest.mock import patch

PIN = 'e5986ba37e9ab7670f54b59cceca48cef27142a5'

def fixture():
    return {'orders': [
        {'exchange':'UPBIT','market':'spot','symbol':'SOL/KRW','side':'buy',
         'order_id':'a1111111-2222-3333-4444-555555555555',
         'reference_fills':[{'price':'200000','quantity_base':'0.25'},
                            {'price':'201000','quantity_base':'0.25'}],
         'reference_total':'100250','reference_quantity':'0.5'},
        {'exchange':'BYBIT','market':'linear','symbol':'BTC/USDT:USDT','side':'sell',
         'is_close':True,'order_id':'10000002',
         'reference_fills':[{'price':'100000','quantity_base':'0.001'}],
         'reference_total':'100','reference_quantity':'0.001'},
        {'exchange':'OKX','market':'linear','symbol':'BTC/USDT:USDT','side':'buy',
         'is_close':False,'order_id':'10000003','contract_size':'0.01',
         'reference_fills':[{'price':'100000','quantity_base':'0.01'}],
         'reference_total':'1000','reference_quantity':'0.01'}]}

def fake_modules(audit, rows, tamper=False, omit_side=False):
    keys=['exchange','exchange.model','exchange.model.schemas','exchange.verified_pnl',
          'exchange.verified_pnl.ledger','exchange.verified_pnl.order_fill_read',
          'exchange.verified_pnl.order_fill_confirmation']
    modules={k:types.ModuleType(k) for k in keys}
    class Settings:
        def __init__(self,**kw):self.__dict__.update(kw)
    class ReadRejected(ValueError):
        def __init__(self,code):self.code=code;super().__init__(code)
    class Observation:
        def __init__(self,value):self.encoded=json.dumps(value)
    class ReadHost:
        def __init__(self,settings):pass
        def observe(self,request):
            info=request['info'];order=request['result']['id']
            item=next(x for x in rows['orders'] if x['exchange']==info['exchange'] and x['order_id']==order)
            fills=[{'price':v['price'],'quantity':v['quantity_base']} for v in item['reference_fills']]
            if tamper and info['exchange']=='UPBIT':fills[0]['quantity']='0.125'
            with localcontext() as ctx:
                ctx.prec=512
                quantity=sum((Decimal(v['quantity']) for v in fills),Decimal(0))
                total=sum((Decimal(v['price'])*Decimal(v['quantity']) for v in fills),Decimal(0))
            scope={'exchange':info['exchange'],'order_id':order,'symbol':info['unified_symbol'],
                   'side':info['side'],'market_type':'linear' if info['is_futures'] else 'spot',
                   'settlement':'KRW' if info['exchange']=='UPBIT' else 'USDT'}
            return Observation({'scope':scope,'trades':fills,'filled':str(quantity),
                                'total':str(total),'average':str(total/quantity)})
    class Embed:
        def __init__(self,d):self.data=d
    def render(proof):
        s=proof['scope'];linear=s['market_type']=='linear'
        side='매수' if s['side']=='buy' else '매도'
        if omit_side and linear:side='롱 진입'
        currency='원' if s['settlement']=='KRW' else ' USDT'
        base=s['symbol'].split('/')[0]
        label='계약 명목가치' if linear else '실제 총 매수액' if s['side']=='buy' else '실제 총 매도액'
        desc='\n'.join(['실제 평균 체결가: '+audit.money(proof['average'])+currency,
                        '실제 체결수량: '+audit.money(proof['filled'])+' '+base,
                        label+': '+audit.money(proof['total'])+currency+' (수수료 제외)'])
        return Embed({'title':'['+s['exchange']+'] '+s['symbol']+' '+side+' 체결 확인','description':desc})
    modules['exchange.model.schemas'].Settings=Settings
    modules['exchange.verified_pnl.ledger'].identity=lambda venue,key:'synthetic-account'
    r=modules['exchange.verified_pnl.order_fill_read']
    r.ReadHost=ReadHost;r.ReadRejected=ReadRejected;r.validate=lambda x:True
    c=modules['exchange.verified_pnl.order_fill_confirmation']
    c.make_payload=lambda x:x;c.render=render
    return modules

class WindowsAuditV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(os.environ['POA_AUDIT_PINNED']).resolve()
        got=subprocess.check_output(['git','rev-parse','HEAD'],cwd=cls.root,text=True).strip()
        assert got==PIN, 'pinned product SHA mismatch'
        script=cls.root/'poa_readonly_audit_v2.py'
        assert script.is_file(), 'audit script not installed'
        spec=importlib.util.spec_from_file_location('readonly_v2',script)
        cls.audit=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.audit)
        assert cls.audit.EXPECTED_SHA==PIN
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data=fixture()
        self.manifest=Path(self.tmp.name)/'orders.json'
        self.save()
    def save(self):
        self.manifest.write_text(json.dumps(self.data),encoding='utf-8')
    def preflight(self,cwd=None):
        return subprocess.run([sys.executable,'-I',str(self.root/'poa_readonly_audit_v2.py'),
                               str(self.manifest),'--preflight'],
                              cwd=cwd or self.root,encoding='utf-8',errors='replace',
                              capture_output=True,timeout=30,
                              env={k:v for k,v in os.environ.items() if k not in ('GITHUB_TOKEN','GH_TOKEN','DISCORD_WEBHOOK_URL')})
    def test_clean_pr4_preflight_no_api_credentials(self):
        r=self.preflight()
        self.assertEqual(r.returncode,0,r.stdout+'\n'+r.stderr)
        self.assertIn('PREFLIGHT_PASS:',r.stdout)
        self.assertIn('No API request performed.',r.stdout)
    def test_placeholder_order_id_rejected(self):
        self.data['orders'][0]['order_id']='REPLACE_ORDER_ID'
        self.save()
        r=self.preflight()
        self.assertEqual(r.returncode,2)
        self.assertIn('PREFLIGHT_FAILED:',r.stderr)
    def test_wrong_sha_rejected_before_any_credentials(self):
        r=self.preflight(cwd=Path(os.environ['GITHUB_WORKSPACE']))
        self.assertEqual(r.returncode,2)
        self.assertIn('PREFLIGHT_FAILED:',r.stderr)
        self.assertNotIn('API key:',r.stderr)
    def test_only_https_get_allowed_without_real_network(self):
        called=[]
        def original(conn,method,path,*a,**k):
            called.append((conn.host,method,path));return 'MOCK'
        mod=types.ModuleType('requests')
        mod.sessions=types.SimpleNamespace(Session=type('FakeSession',(),{'request':lambda *a,**kw:None}))
        with patch.object(http.client.HTTPSConnection,'request',original), \
             patch.object(http.client.HTTPConnection,'request',original), \
             patch.dict(sys.modules,{'requests':mod}):
            self.audit.limit_network_to_exchange_gets()
            ok=types.SimpleNamespace(host='api.upbit.com')
            self.assertEqual(http.client.HTTPSConnection.request(ok,'GET','/v1/order'),'MOCK')
            for host,method in [('api.upbit.com','POST'),('evil.example','GET'),('api.binance.com','DELETE')]:
                with self.assertRaisesRegex(RuntimeError,'AUDIT_NETWORK_WRITE_OR_HOST_BLOCKED'):
                    http.client.HTTPSConnection.request(types.SimpleNamespace(host=host),method,'/x')
            with self.assertRaisesRegex(RuntimeError,'AUDIT_PLAINTEXT_HTTP_BLOCKED'):
                http.client.HTTPConnection.request(ok,'GET','/v1/order')
            with self.assertRaisesRegex(RuntimeError,'AUDIT_REQUESTS_TRANSPORT_BLOCKED'):
                mod.sessions.Session().request('GET','https://api.upbit.com')
        self.assertEqual(called,[('api.upbit.com','GET','/v1/order')])
    def run_mock(self,tamper=False,omit_side=False):
        inputs=iter(['fake-key','fake-secret','fake-key','fake-secret','fake-key','fake-secret','fake-passphrase'])
        out=io.StringIO();err=io.StringIO()
        mods=fake_modules(self.audit,self.data,tamper=tamper,omit_side=omit_side)
        old=Path.cwd()
        with patch.dict(sys.modules,mods),patch.object(sys,'argv',['poa_readonly_audit_v2.py',str(self.manifest)]), \
             patch.object(self.audit.getpass,'getpass',side_effect=lambda prompt:next(inputs)), \
             patch.object(self.audit,'limit_network_to_exchange_gets',return_value=None), \
             patch.object(socket.socket,'connect',side_effect=AssertionError('NETWORK_BLOCKED')), \
             patch.object(socket.socket,'connect_ex',side_effect=AssertionError('NETWORK_BLOCKED')), \
             contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:
                os.chdir(self.root)
                status=self.audit.main()
            finally:
                os.chdir(old)
        return status,out.getvalue(),err.getvalue()
    def test_synthetic_totals_embed_and_no_secret_output(self):
        status,out,err=self.run_mock()
        self.assertEqual(status,0,out+'\n'+err)
        self.assertIn('FINAL: 3/3',out)
        self.assertEqual(sum(x.startswith('PASS ') for x in out.splitlines()),3)
        self.assertNotIn('fake-secret',out+err)
    def test_quantity_mutation_fails_closed(self):
        status,out,_=self.run_mock(tamper=True)
        self.assertEqual(status,1,out)
        self.assertIn('source fill rows differ',out)
    def test_futures_direction_omission_fails_closed(self):
        status,out,_=self.run_mock(omit_side=True)
        self.assertEqual(status,1,out)
        self.assertIn('Futures title does not explicitly display buy/sell side',out)

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--pinned-root',required=True)
    args=p.parse_args()
    os.environ['POA_AUDIT_PINNED']=str(Path(args.pinned_root).resolve())
    unittest.main(argv=[sys.argv[0]],verbosity=2)
