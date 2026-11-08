"""Independent PR24 adversarial re-audit; all execution stays in process Mock.

No actual ccxt client, real KIS session, main.py import, DB, network or order.
Privileged Python interpreter compromise and raw third-party imports remain
separate OS-boundary threats; negative-control tests prove the residual.
"""
from __future__ import annotations

import ast
import copy
import inspect
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from test_order_execution_deny import (
    SINKS, EXTRA_SINKS, NeverTouch, guard_namespace, method, source,
)

CLASS_CONSTRUCTORS = {
    "exchange/safe_retry.py": ("Store", "Adapter", "Manager", "ReconciliationClients"),
    "exchange/binance.py": ("Binance",),
    "exchange/bitget.py": ("Bitget",),
    "exchange/bybit.py": ("Bybit",),
    "exchange/okx.py": ("Okx",),
    "exchange/upbit.py": ("Upbit",),
    "exchange/stock/kis.py": ("KoreaInvestment",),
}
STALE_WORKER = ("connect", "claim", "save", "pending", "recover", "backup",
                "reconcile", "_get_or_create_client_locked", "acquire_lease", "get_client")
NEW_FUNCTIONS = [
    (p, n) for p, names in EXTRA_SINKS.items() for n in names if n != "__init__"
] + [("exchange/safe_retry.py", n) for n in STALE_WORKER]
CONSTRUCTORS = [(p, c) for p, names in CLASS_CONSTRUCTORS.items() for c in names]
EXAMPLE_CORE = (
    ("main.py", "_order_http"), ("main.py", "order"),
    ("main.py", "_hedge_http"), ("main.py", "hedge"),
    ("exchange/pexchange.py", "retry"),
    ("exchange/binance.py", "market_sltp_order"),
    ("exchange/stock/kis.py", "post"),
    ("exchange/stock/kis.py", "get_hashkey"),
    ("exchange/safe_retry.py", "reconcile_pending"),
)


def class_method(path, cls, method_name="__init__"):
    tree = ast.parse(source(path), filename=path)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls]
    assert len(classes) == 1, (path, cls)
    nodes = [n for n in classes[0].body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == method_name]
    assert len(nodes) == 1, (path, cls, method_name)
    return nodes[0]


def compile_method(path, node):
    n = copy.deepcopy(node)
    n.decorator_list = []
    n.returns = None
    for a in (*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs):
        a.annotation = None
    if n.args.vararg:
        n.args.vararg.annotation = None
    if n.args.kwarg:
        n.args.kwarg.annotation = None
    # Avoid evaluating product Config(), time.clock or settings defaults.
    n.args.defaults = [ast.Constant(None) for _ in n.args.defaults]
    n.args.kw_defaults = [ast.Constant(None) if a is not None else None
                          for a in n.args.kw_defaults]
    n = ast.fix_missing_locations(ast.Module(body=[n], type_ignores=[]))
    ns = {}
    exec(compile(n, path, "exec"), ns)
    return ns[node.name]


def call_blocked(fn, exception_type, *, expected_code="POA_ORDER_EXECUTION_BLOCKED"):
    args, keywords = [], {}
    for p in inspect.signature(fn).parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty:
            args.append(NeverTouch())
        elif p.kind == p.KEYWORD_ONLY and p.default is p.empty:
            keywords[p.name] = NeverTouch()
    with pytest.raises(exception_type) as outcome:
        if inspect.iscoroutinefunction(fn):
            co = fn(*args, **keywords)
            try:
                co.send(None)
            finally:
                co.close()
        elif inspect.isgeneratorfunction(fn):
            gen = fn(*args, **keywords)
            try:
                next(gen)
            finally:
                gen.close()
        else:
            fn(*args, **keywords)
    if expected_code is not None:
        assert getattr(outcome.value, "code", None) == expected_code
    return outcome.value


def run_fake(path, node, *, module=None, exc_type=None, expected_code="POA_ORDER_EXECUTION_BLOCKED"):
    guard, genuine, package = guard_namespace()
    callback = compile_method(path, node)
    fake = module if module is not None else genuine
    exc = exc_type if exc_type is not None else guard["OrderExecutionBlocked"]
    with patch.dict(sys.modules, {"exchange": package,
                                  "exchange.order_execution_gate": fake}):
        return call_blocked(callback, exc, expected_code=expected_code)


@pytest.mark.parametrize("path,name", NEW_FUNCTIONS)
def test_additional_http_auth_and_stale_retry_entrypoints_deny_before_io(path, name):
    run_fake(path, method(path, name))


@pytest.mark.parametrize("path,cls", CONSTRUCTORS)
def test_exchange_kis_and_safe_retry_constructors_inert(path, cls):
    run_fake(path, class_method(path, cls))


@pytest.mark.parametrize("path,name", EXAMPLE_CORE)
def test_replacing_original_dynamic_guard_with_noop_cannot_allow_order(path, name):
    guard, genuine, package = guard_namespace()
    forged = types.ModuleType("exchange.order_execution_gate")
    forged.require_order_disabled = lambda: None
    forged.OrderExecutionBlocked = guard["OrderExecutionBlocked"]
    run_fake(path, method(path, name), module=forged)


@pytest.mark.parametrize("path,name", EXAMPLE_CORE)
def test_invalid_replacement_exception_type_still_fails_closed(path, name):
    forged = types.ModuleType("exchange.order_execution_gate")
    forged.require_order_disabled = lambda: None
    forged.OrderExecutionBlocked = lambda: None
    run_fake(path, method(path, name), module=forged,
             exc_type=TypeError, expected_code=None)


@pytest.mark.parametrize("path,name", (
    ("main.py", "_order_http"), ("main.py", "_hedge_http"),
    ("main.py", "_price_http"), ("main.py", "order"),
    ("main.py", "hedge"), ("main.py", "price"), ("main.py", "get_ip"),
    ("exchange/pexchange.py", "get_bot"),
    ("exchange/pexchange.py", "get_exchange"),
))
def test_direct_http_and_internal_entrypoints_block_without_parsing_payload(path, name):
    run_fake(path, method(path, name))


@pytest.mark.parametrize("name", (
    "post", "post_with_error_handling", "get_hashkey",
    "create_auth", "auth", "create_order",
))
def test_kis_stock_direct_post_auth_and_hashkey_blocked(name):
    run_fake("exchange/stock/kis.py", method("exchange/stock/kis.py", name))


@pytest.mark.parametrize("name", (
    "connect", "claim", "save", "pending", "recover", "backup",
    "reconcile", "_get_or_create_client_locked", "acquire_lease", "get_client",
    "get_reconciliation_clients", "reconcile_pending", "reconciliation_loop",
    "safe_submit", "get_manager",
))
def test_safe_retry_worker_restart_and_stale_instances_never_write_db(name):
    run_fake("exchange/safe_retry.py", method("exchange/safe_retry.py", name))


@pytest.mark.parametrize("path,name", EXAMPLE_CORE)
def test_parallel_workers_never_escape_gate_with_no_module_swap(path, name):
    guard, module, package = guard_namespace()
    target = compile_method(path, method(path, name))
    with patch.dict(sys.modules, {"exchange": package,
                                  "exchange.order_execution_gate": module}):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: call_blocked(
                target, guard["OrderExecutionBlocked"]), range(24)))
    assert len(results) == 24
    assert all(x.code == "POA_ORDER_EXECUTION_BLOCKED" for x in results)


def test_raw_ccxt_direct_call_bypasses_application_function_guard_in_mock():
    # Explicitly reported, not a PASS claim for OS egress enforcement.
    class FakeUncontrolledCcxt:
        def __init__(self):
            self.calls = []
        def create_order(self, *args):
            self.calls.append(args)
            return {"id": "SYNTHETIC_ONLY"}
    fake = FakeUncontrolledCcxt()
    assert fake.create_order("BTC/USDT", "market", "buy", 1)["id"] == "SYNTHETIC_ONLY"
    assert len(fake.calls) == 1
    unit = source("deployment/poa-stage-only.service.candidate")
    assert "ExecStartPre=/usr/bin/false" in unit
    assert "PrivateNetwork=yes" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "ProtectSystem=strict" in unit


def test_python_interpreter_function_replacement_not_covered():
    class ArbitraryPython:
        def execute(self):
            return "MOCK_FIRST"
    obj = ArbitraryPython()
    obj.execute = lambda: "MOCK_REPLACED"
    assert obj.execute() == "MOCK_REPLACED"
    assert "PrivateNetwork=yes" in source("deployment/poa-stage-only.service.candidate")


def test_report_labels_privileged_interpreter_and_raw_sdk_residuals():
    doc = source("deployment/PR24_INDEPENDENT_ORDER_REAUDIT.md")
    for flag in ("RAW_CCXT_OUTSIDE_APP=UNRESOLVED",
                 "PYTHON_INTERPRETER_REPLACEMENT=UNRESOLVED",
                 "VULTR_HOST_NETWORK_ISOLATION=UNVERIFIED",
                 "OPERATING_DB_WRITES=NOT_PERFORMED"):
        assert flag in doc


def test_unchanged_financial_source_after_removing_only_guard_statements():
    from test_order_execution_deny import test_only_two_guard_statements_added_per_entrypoint_exact_original_bytes
    for path in SINKS:
        test_only_two_guard_statements_added_per_entrypoint_exact_original_bytes(path)


def test_strict_product_scope_and_original_release_manifest_unchanged():
    from test_order_execution_deny import test_no_other_protected_financial_code_or_original_manifest_changed
    test_no_other_protected_financial_code_or_original_manifest_changed()


def test_stage_gateway_does_not_delegate_to_live_main_app():
    assert "from exchange" not in source("poa_stage/gateway.py")
    assert "import main" not in source("poa_stage/gateway.py")
    assert "main:app" not in source("deployment/poa-stage-only.service.candidate")
