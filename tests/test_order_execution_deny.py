"""PR23 -> PR24 independent central ORDER EXECUTION DENY Mock attack E2E.

Runs *actual method bodies extracted by AST* from the changed POA candidate,
with hostile client/DB objects which fail on *any* access. Every mutation-capable
entrypoint must deny BEFORE consulting any client, credential, state, retry,
account, response, network, or database. Tests never import main.py/POA DB,
never access exchange network, and never alter production financial data.

The unchanged PR23 product's full Windows/Ubuntu regression is separate
historical evidence. Deny-mode production candidate is NOT approved for live
execution or for golden live-order regression equivalence.
"""
from __future__ import annotations

import ast
import copy
import inspect
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = "e01d3d827dea3b147b63fb6ec4cfcecc3cc95ebc"
SINKS = {
    "main.py": ("startup", "_order_http", "order", "_hedge_http", "hedge"),
    "exchange/pexchange.py": ("retry",),
    "exchange/safe_retry.py": ("execute", "_execute", "safe_submit",
                               "get_manager", "reconcile_pending", "reconciliation_loop"),
    "exchange/binance.py": ("set_leverage", "market_order", "market_buy",
                            "market_sell", "market_entry", "market_sltp_order", "market_close"),
    "exchange/bitget.py": ("set_leverage", "market_order", "market_buy", "market_sell",
                           "market_entry", "market_close"),
    "exchange/bybit.py": ("set_leverage", "market_order", "market_buy", "market_sell",
                          "market_entry", "market_close"),
    "exchange/okx.py": ("set_leverage", "market_order", "market_buy", "market_sell",
                        "market_entry", "market_close"),
    "exchange/upbit.py": ("market_order", "market_buy", "market_sell",
                          "market_entry", "market_close"),
    "exchange/stock/kis.py": ("post_with_error_handling", "post", "create_order",
                              "create_market_buy_order", "create_market_sell_order",
                              "create_korea_market_buy_order", "create_korea_market_sell_order",
                              "create_usa_market_buy_order"),
}
EXTRA_SINKS = {
    "main.py": ("get_ip", "_price_http", "price"),
    "exchange/pexchange.py": ("get_exchange", "get_bot"),
    "exchange/safe_retry.py": ("get_reconciliation_clients",),
    "exchange/binance.py": ("__init__",),
    "exchange/bitget.py": ("__init__",),
    "exchange/bybit.py": ("__init__",),
    "exchange/okx.py": ("__init__",),
    "exchange/upbit.py": ("__init__",),
    "exchange/stock/kis.py": ("__init__", "get_hashkey", "create_auth", "auth"),
}
# Unique for each module; Safe Retry contains four separate class constructors
# that are covered by the separate independent PR25 scoped-AST attack tests.
SINK_LIST = [(path, name) for path, names in SINKS.items() for name in names]


def source(path):
    return (ROOT/path).read_text(encoding="utf-8")


def baseline(path):
    return subprocess.check_output(["git", "show", BASE+":"+path], cwd=ROOT).decode("utf-8")


def functions(path):
    root=ast.parse(source(path),filename=path)
    funcs={}
    for node in ast.walk(root):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            funcs.setdefault(node.name,[]).append(node)
    return funcs


def method(path,name):
    nodes=functions(path).get(name,[])
    assert len(nodes)==1,(path,name,len(nodes))
    return nodes[0]


def strip_annotations(func):
    fn=copy.deepcopy(func)
    fn.decorator_list=[]
    fn.returns=None
    for a in (*fn.args.posonlyargs,*fn.args.args,*fn.args.kwonlyargs):
        a.annotation=None
    if fn.args.vararg: fn.args.vararg.annotation=None
    if fn.args.kwarg: fn.args.kwarg.annotation=None
    return fn


def guard_namespace():
    name="exchange.order_execution_gate"
    ns={"__name__":name}
    exec(compile(source("exchange/order_execution_gate.py"),name,"exec"),ns)
    module=types.ModuleType(name)
    module.__dict__.update(ns)
    fake_exchange=types.ModuleType("exchange")
    fake_exchange.__path__=[]
    return ns,module,fake_exchange


class NeverTouch:
    def __getattribute__(self,attr):
        raise AssertionError("PRE_GUARD_DEPENDENCY_ACCESS")
    def __call__(self,*args,**kwargs):
        raise AssertionError("PRE_GUARD_DEPENDENCY_CALL")
    def __bool__(self):
        raise AssertionError("PRE_GUARD_DEPENDENCY_TRUTH")
    def __iter__(self):
        raise AssertionError("PRE_GUARD_DEPENDENCY_ITERATION")


def call_real_candidate_body(path,name):
    original=method(path,name)
    fn=strip_annotations(original)
    fn_module=ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[]))
    ns_guard, module, package=guard_namespace()
    scope={}
    exec(compile(fn_module,path,"exec"),scope)
    callback=scope[name]
    signature=inspect.signature(callback)
    positional=[]
    keyword={}
    for p in signature.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY,p.POSITIONAL_OR_KEYWORD):
            if p.default is p.empty:
                positional.append(NeverTouch())
        elif p.kind==p.KEYWORD_ONLY and p.default is p.empty:
            keyword[p.name]=NeverTouch()
    with patch.dict(sys.modules,{"exchange":package,"exchange.order_execution_gate":module}):
        if inspect.iscoroutinefunction(callback):
            coroutine=callback(*positional,**keyword)
            try:
                with pytest.raises(ns_guard["OrderExecutionBlocked"]) as blocked:
                    coroutine.send(None)
            finally:
                coroutine.close()
        else:
            with pytest.raises(ns_guard["OrderExecutionBlocked"]) as blocked:
                callback(*positional,**keyword)
    assert blocked.value.code=="POA_ORDER_EXECUTION_BLOCKED"
    assert str(blocked.value)=="POA_ORDER_EXECUTION_BLOCKED"


@pytest.mark.parametrize("path,name",SINK_LIST,ids=lambda x:str(x))
def test_each_real_order_path_is_blocked_before_all_side_effects(path,name):
    call_real_candidate_body(path,name)


@pytest.mark.parametrize("path",list(SINKS),ids=lambda x:str(x))
def test_only_two_guard_statements_added_per_entrypoint_exact_original_bytes(path):
    data=source(path)
    lines=data.splitlines(keepends=True)
    tree=ast.parse(data)
    used=set()
    for name in SINKS[path]:
        fn=method(path,name)
        assert len(fn.body)>=2
        first,second=fn.body[:2]
        assert isinstance(first,ast.ImportFrom)
        assert first.module=="exchange.order_execution_gate"
        assert len(first.names)==1
        assert first.names[0].name=="OrderExecutionBlocked"
        assert isinstance(second,ast.Raise)
        call=second.exc
        assert isinstance(call,ast.Call) and isinstance(call.func,ast.Name)
        assert call.func.id=="OrderExecutionBlocked"
        assert not call.args and not call.keywords
        # Strictly remove only the two known new lines; preserve all original
        # bytes and original AST (financial formulas/retries/DB unchanged).
        assert first.end_lineno==first.lineno and second.end_lineno==second.lineno
        used.update((first.lineno,second.lineno))
    # Guarded *new* endpoints are also exactly two statements relative to
    # PR23, not financial-refactoring changes.
    for name in EXTRA_SINKS.get(path, ()):
        if name == "__init__":
            root=ast.parse(data)
            nodes=[n for n in ast.walk(root) if isinstance(n,ast.FunctionDef)
                   and n.name == "__init__"
                   and len(n.body)>=2
                   and isinstance(n.body[0],ast.ImportFrom)
                   and n.body[0].module=="exchange.order_execution_gate"]
            assert len(nodes)==1,(path,len(nodes))
            fn=nodes[0]
        else:
            fn=method(path,name)
        first,second=fn.body[:2]
        assert isinstance(first,ast.ImportFrom) and first.module=="exchange.order_execution_gate"
        assert first.names[0].name=="OrderExecutionBlocked"
        assert isinstance(second,ast.Raise)
        assert isinstance(second.exc,ast.Call)
        assert isinstance(second.exc.func,ast.Name) and second.exc.func.id=="OrderExecutionBlocked"
        used.update((first.lineno,second.lineno))
    if path=="exchange/safe_retry.py":
        root=ast.parse(data)
        names={"Store","Adapter","Manager","ReconciliationClients"}
        for cl in root.body:
            if isinstance(cl,ast.ClassDef) and cl.name in names:
                constructors=[n for n in cl.body if isinstance(n,ast.FunctionDef) and n.name=="__init__"]
                assert len(constructors)==1
                fn=constructors[0]
                assert isinstance(fn.body[0],ast.ImportFrom)
                assert fn.body[0].module=="exchange.order_execution_gate"
                assert fn.body[0].names[0].name=="OrderExecutionBlocked"
                assert isinstance(fn.body[1],ast.Raise)
                used.update((fn.body[0].lineno,fn.body[1].lineno))
    if path=="exchange/safe_retry.py":
        for name in ("connect","claim","save","pending","recover","backup",
                     "reconcile","_get_or_create_client_locked",
                     "acquire_lease","get_client"):
            fn=method(path,name)
            assert isinstance(fn.body[0],ast.ImportFrom)
            assert fn.body[0].module=="exchange.order_execution_gate"
            assert fn.body[0].names[0].name=="OrderExecutionBlocked"
            assert isinstance(fn.body[1],ast.Raise)
            assert isinstance(fn.body[1].exc,ast.Call)
            assert isinstance(fn.body[1].exc.func,ast.Name)
            assert fn.body[1].exc.func.id=="OrderExecutionBlocked"
            used.update((fn.body[0].lineno,fn.body[1].lineno))
    restored="".join(line for i,line in enumerate(lines,1) if i not in used)
    assert restored==baseline(path)
    extra=len(EXTRA_SINKS.get(path,()))
    if path=="exchange/safe_retry.py":extra+=14
    assert len(used)==2*(len(SINKS[path])+extra)


def test_execution_gate_is_irreversible_without_source_change():
    tree=ast.parse(source("exchange/order_execution_gate.py"))
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef)
            and n.name=="require_order_disabled")
    body=[n for n in fn.body if not isinstance(n,ast.Expr) or not isinstance(n.value,ast.Constant)]
    assert len(body)==1
    stmt=body[0]
    assert isinstance(stmt,ast.Raise)
    assert isinstance(stmt.exc,ast.Call)
    assert isinstance(stmt.exc.func,ast.Name)
    assert stmt.exc.func.id=="OrderExecutionBlocked"
    assert not stmt.exc.args
    # Neither opt-in environment variables nor arbitrary test overrides exist.
    bad=("os.getenv","os.environ","getenv(","test_mode","ALLOW_LIVE",
         "LIVE_ORDERS","APPROVED","__import__","sys.modules","contextvars")
    assert not any(x in ast.get_source_segment(source("exchange/order_execution_gate.py"),fn)
                   for x in bad)


def test_no_other_protected_financial_code_or_original_manifest_changed():
    changed=subprocess.check_output(
        ["git","diff","--name-only",BASE,"HEAD"],cwd=ROOT,text=True).splitlines()
    allowed = set(SINKS)|{
        "exchange/order_execution_gate.py",
        "tests/test_order_execution_deny.py",
        "tests/test_pr24_independent_order_reattack.py",
        ".github/workflows/pr24_independent_order_reattack.yml",
        "deployment/PR24_INDEPENDENT_ORDER_REAUDIT.md",
        ".github/workflows/order_execution_deny_ci.yml",
        "deployment/PR23_CENTRAL_ORDER_DENY_AUDIT.md",
    }
    assert changed and set(changed)<=allowed
    assert "release_manifest_sha256.json" not in changed
    assert "requirements.txt" not in changed
    assert not any(p.startswith("exchange/verified_pnl/") for p in changed)
    assert not any(p in ("exchange/database.py","exchange/pocket.py",
                        "exchange/model/schemas.py","run.py") for p in changed)
    assert not any(p.startswith("deployment/cloud-config-") or
                   p.startswith("deployment/install_once") or
                   p.startswith("deployment/build_cloud_config") for p in changed)
    assert not any(p.startswith("poa_stage/") for p in changed)


def test_stage_gateway_is_not_coupled_to_imported_production_app():
    code=source("poa_stage/gateway.py")
    assert "import main" not in code and "from exchange" not in code
    assert "poa_stage.gateway:app" in source("deployment/poa-stage-only.service.candidate")
    assert "ExecStartPre=/usr/bin/false" in source("deployment/poa-stage-only.service.candidate")


def test_real_order_api_calls_are_behind_entrypoint_gate():
    """Audit direct CCXT mutators; unsupported raw-client access is reported."""
    for path in ("exchange/binance.py","exchange/bitget.py","exchange/bybit.py",
                 "exchange/okx.py","exchange/upbit.py"):
        tree=ast.parse(source(path))
        for n in ast.walk(tree):
            if not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(n):
                if not isinstance(call,ast.Call):continue
                fn=call.func
                if isinstance(fn,ast.Attribute) and fn.attr in (
                    "create_order","set_leverage","set_margin_mode"):
                    # All direct mutating adapter SDK calls must be contained
                    # in a method with the absolute first-statement deny guard.
                    assert n.name in SINKS[path],(path,n.name,fn.attr)


def test_stock_direct_post_must_have_same_guard():
    for name in ("post_with_error_handling","post","create_order"):
        call_real_candidate_body("exchange/stock/kis.py",name)


def test_safe_retry_db_access_is_blocked_before_manager_initialization():
    for name in ("get_manager","safe_submit","execute","_execute",
                 "reconcile_pending","reconciliation_loop"):
        call_real_candidate_body("exchange/safe_retry.py",name)


@pytest.mark.parametrize("old_env",[
    {"POA_LIVE_ORDERS":"1"},
    {"POA_STAGE_EXECUTION_BLOCK":"0"},
    {"POA_ORDER_EXECUTION_POLICY":"allow"},
    {"CI":"true","POA_ALLOW_MOCK_ORDERS":"1"},
])
def test_environment_cannot_enable_orders(monkeypatch,old_env):
    for k,v in old_env.items():monkeypatch.setenv(k,v)
    call_real_candidate_body("exchange/pexchange.py","retry")
    call_real_candidate_body("main.py","order")
    call_real_candidate_body("exchange/stock/kis.py","create_order")
