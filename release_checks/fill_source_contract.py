"""Exact notification-only source delta; protected order/finance/CAS ASTs retained."""
import ast
import copy
import hashlib
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE='77ec5dabd00639e8e6cb4f1bb81f6c7bcca8e140'
PATHS=('exchange/model/schemas.py','exchange/verified_pnl/models.py','exchange/verified_pnl/dispatcher.py','exchange/verified_pnl/intake.py',
       'exchange/verified_pnl/delivery_store.py','exchange/verified_pnl/alert_presentation.py')
NEW=('exchange/verified_pnl/order_fill_read.py','exchange/verified_pnl/order_fill_confirmation.py')
PINFILE=Path(__file__).with_name('fill_source_pins.json')

def blob(path):return subprocess.check_output(['git','show',BASE+':'+path],cwd=ROOT)
def dump(node):return ast.dump(node,include_attributes=False)

def structure(path,raw):
    old=ast.parse(blob(path));new=ast.parse(raw)
    if path.endswith('/schemas.py'):
        cls=next(n for n in new.body if getattr(n,'name',None)=='Settings')
        flag=next(n for n in cls.body if isinstance(n,ast.AnnAssign) and getattr(n.target,'id',None)=='POA_EXCHANGE_FILL_CONFIRMATION_ENABLED')
        assert dump(flag.annotation)==dump(ast.Name(id='bool',ctx=ast.Load())) and isinstance(flag.value,ast.Constant) and flag.value.value is False
        cls.body.remove(flag)
    elif path.endswith('/models.py'):
        cls=next(n for n in new.body if getattr(n,'name',None)=='NotificationPayload')
        field=next(n for n in cls.body if isinstance(n,ast.AnnAssign) and getattr(n.target,'id',None)=='fill_confirmation')
        assert isinstance(field.value,ast.Constant) and field.value.value is None
        cls.body.remove(field)
        class Tuples(ast.NodeTransformer):
            def visit_Tuple(self,n):
                if [getattr(x,'value',None) for x in n.elts]==['notification_type','estimated_pnl','fill_confirmation']:n.elts=n.elts[:2]
                return self.generic_visit(n)
        for n in cls.body:
            if getattr(n,'name',None) in ('dict','json'):Tuples().visit(n)
    elif path.endswith('/alert_presentation.py'):
        fn=next(n for n in new.body if getattr(n,'name',None)=='apply')
        assert isinstance(fn.body[0],ast.ImportFrom) and fn.body[0].module=='order_fill_read'
        assert isinstance(fn.body[1],ast.If) and 'CONTRACT' in ast.unparse(fn.body[1].test)
        fn.body=fn.body[2:]
    elif path.endswith('/dispatcher.py'):
        class Remove(ast.NodeTransformer):
            def visit_ImportFrom(self,n):
                return None if n.module=='order_fill_confirmation' else n
            def visit_Expr(self,n):
                if isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name) and n.value.func.id in ('prepare','confirmation_tick'):return None
                return self.generic_visit(n)
            def visit_Assign(self,n):
                if len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='confirmation_host':return None
                return self.generic_visit(n)
            def visit_Call(self,n):
                n=self.generic_visit(n)
                if isinstance(n.func,ast.Attribute) and isinstance(n.func.value,ast.Name) and n.func.value.id=='intake' and n.func.attr=='record':
                    n.keywords=[k for k in n.keywords if k.arg is not None]
                return n
            def visit_If(self,n):
                text=ast.unparse(n.test)
                if text=='producer_entry is not None' or 'POA_EXCHANGE_FILL_CONFIRMATION_ENABLED' in text:return None
                return self.generic_visit(n)
            def visit_Try(self,n):
                # Only the standalone new enrollment try, never the old Intake try.
                if any(isinstance(x,ast.Expr) and isinstance(x.value,ast.Call) and isinstance(x.value.func,ast.Name) and x.value.func.id=='enroll' for x in n.body):return None
                return self.generic_visit(n)
        for n in new.body:
            if isinstance(n,ast.FunctionDef) and n.name in ('dispatch_verified_alert','dispatch_order_observation','verification_tick','start_worker'):
                if n.name=='dispatch_verified_alert':
                    assert len(n.args.kwonlyargs)==1 and n.args.kwonlyargs[0].arg=='producer_entry'
                    n.args.kwonlyargs=[];n.args.kw_defaults=[]
                Remove().visit(n)
    elif path.endswith('/intake.py'):
        cls=next(n for n in new.body if getattr(n,'name',None)=='IntakeStore')
        record=next(n for n in cls.body if getattr(n,'name',None)=='record')
        assert record.args.kwonlyargs[-1].arg=='confirmation_host' and isinstance(record.args.kw_defaults[-1],ast.Constant) and record.args.kw_defaults[-1].value is None
        record.args.kwonlyargs=record.args.kwonlyargs[:-1];record.args.kw_defaults=record.args.kw_defaults[:-1]
        class Remove(ast.NodeTransformer):
            def visit_If(self,n):
                if ast.unparse(n.test)=='confirmation_host is not None and (not review)':return None
                return self.generic_visit(n)
        Remove().visit(record)
    elif path.endswith('/delivery_store.py'):
        cls=next(n for n in new.body if getattr(n,'name',None)=='NotificationOutboxStore')
        oldcls=next(n for n in old.body if getattr(n,'name',None)=='NotificationOutboxStore')
        cls.body=[n for n in cls.body if getattr(n,'name',None)!='enqueue_confirmation']
        for n in cls.body:
            if getattr(n,'name',None)=='_scope':n.body=n.body[2:]
            elif getattr(n,'name',None)=='_claim_next':
                class Remove(ast.NodeTransformer):
                    def visit_ImportFrom(self,x):return None if x.module=='order_fill_read' else x
                    def visit_If(self,x):
                        if ast.unparse(x.test)=='p.notification_type == CONTRACT':return None
                        return self.generic_visit(x)
                Remove().visit(n)
            elif getattr(n,'name',None)=='_restore_ready':
                prior=next(x for x in oldcls.body if getattr(x,'name',None)=='_restore_ready')
                assert len(n.body)==4 and dump(n.body[0])==dump(prior.body[0])
                assert isinstance(n.body[1],ast.Assign) and dump(n.body[1].value)==dump(prior.body[1].value)
                assert ast.unparse(n.body[2].test)=='ready' and ast.unparse(n.body[3].value)=='ready'
                n.body=copy.deepcopy(prior.body)
    assert dump(new)==dump(old),'Unapproved notification structural delta: '+path

def restore_notification_source(path,raw):
    if path not in PATHS:return raw
    pins=json.loads(PINFILE.read_text(encoding='utf-8'))
    assert hashlib.sha256(raw).hexdigest()==pins[path],'Unapproved confirmation source: '+path
    structure(path,raw)
    return blob(path)

def verify():
    pins=json.loads(PINFILE.read_text(encoding='utf-8'))
    assert set(pins)==set(PATHS+NEW)
    for path in PATHS+NEW:
        raw=(ROOT/path).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==pins[path],path
        if path in PATHS:structure(path,raw)
    # These original sources, including all HTTP/order bodies and retry engine,
    # never receive a notification exception.
    for path in ('main.py','exchange/safe_retry.py','exchange/binance.py','exchange/bybit.py','exchange/okx.py','exchange/upbit.py',
                 'exchange/verified_pnl/outbox.py','exchange/verified_pnl/evidence.py','exchange/verified_pnl/verifier.py','exchange/verified_pnl/bootstrap.py',
                 'exchange/verified_pnl/history.py','exchange/verified_pnl/migrations.py','exchange/verified_pnl/ledger.py','exchange/verified_pnl/pnl_calculator.py','exchange/verified_pnl/reconciliation.py'):
        assert (ROOT/path).read_bytes()==blob(path),path
    print('CONFIRMATION_SOURCE_PINS=PASS; ORDER_FINANCE_ACK_CAS=UNCHANGED')

if __name__=='__main__':
    import sys
    if sys.argv[1:]==['--freeze']:
        for path in PATHS:structure(path,(ROOT/path).read_bytes())
        PINFILE.write_bytes((json.dumps({p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in PATHS+NEW},indent=2)+'\n').encode('utf-8'))
    verify()
