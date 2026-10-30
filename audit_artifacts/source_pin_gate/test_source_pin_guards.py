"""Negative controls for the narrowed pins; product sources are never mutated."""
import ast,hashlib,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from notification_source_contract import PINS,APPROVED_COMMIT,_node,_span,assert_reviewed_notification_source
ROOT=Path(__file__).resolve().parents[2]


def function_mutation(raw,name):
    tree=ast.parse(raw);node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)
    lines=raw.splitlines(keepends=True);line=node.body[0].lineno-1
    indent=len(lines[line])-len(lines[line].lstrip())
    lines.insert(line,b' '*indent+b"raise RuntimeError('negative-control')\n")
    return b''.join(lines)


@pytest.mark.parametrize('path,key',[(p,k) for p,r in PINS.items() for k in r['approved']])
def test_each_approved_span_is_exact_not_blanket_exception(path,key):
    raw=(ROOT/path).read_bytes()
    if key.startswith('Settings.'):
        changed=raw.replace(b'POA_NOTIFICATION_SERVER_ALIAS: str | None = None',b"POA_NOTIFICATION_SERVER_ALIAS: str | None = 'arbitrary'")
    else:changed=function_mutation(raw,key.split('.')[-1])
    assert changed!=raw
    with pytest.raises(AssertionError):assert_reviewed_notification_source(path,changed)


@pytest.mark.parametrize('method',['_wire','_attempt','begin_post','ack','claim_next','_notice_gate','enqueue','recover'])
def test_every_protected_outbox_boundary_rejects_edits(method):
    path='exchange/verified_pnl/delivery_store.py';changed=function_mutation((ROOT/path).read_bytes(),method)
    with pytest.raises(AssertionError):assert_reviewed_notification_source(path,changed)


@pytest.mark.parametrize('replacement',[
 (b'amount: float | None = None',b'amount: float | None = 100'),
 (b'SAFE_RETRY_ENABLED: bool = False',b'SAFE_RETRY_ENABLED: bool = True'),
 (b'if v != setting.PASSWORD:',b'if False:'),
 (b'values |= get_extra_order_info(values)',b'values = values'),
])
def test_order_quantity_settings_password_and_normalization_still_pinned(replacement):
    path='exchange/model/schemas.py';raw=(ROOT/path).read_bytes();changed=raw.replace(*replacement);assert changed!=raw
    with pytest.raises(AssertionError):assert_reviewed_notification_source(path,changed)


@pytest.mark.parametrize('path',list(PINS))
def test_extra_code_and_duplicate_declaration_not_exempt(path):
    raw=(ROOT/path).read_bytes()
    with pytest.raises(AssertionError):assert_reviewed_notification_source(path,raw+b'\nunsafe_global = True\n')
    with pytest.raises(AssertionError):assert_reviewed_notification_source(path,raw+b'\nclass NotificationOutboxStore:\n    pass\n')


def test_pins_are_the_reviewed_commit_and_exactly_five_named_spans():
    assert APPROVED_COMMIT=='3a9523341ac75a550b31b4f9c090258814d91f76'
    assert set(PINS)=={'exchange/model/schemas.py','exchange/verified_pnl/delivery_store.py'}
    assert set(PINS['exchange/model/schemas.py']['approved'])=={'OrderRequest.validate_tv_binding','Settings.POA_NOTIFICATION_SERVER_ALIAS'}
    assert set(PINS['exchange/verified_pnl/delivery_store.py']['approved'])=={'NotificationOutboxStore.submit_observed','NotificationOutboxStore._estimated_authority','NotificationOutboxStore._buy_notification_authority'}
    for path,rule in PINS.items():
        raw=subprocess.check_output(['git','-C',str(ROOT),'show',APPROVED_COMMIT+':'+path]);tree=ast.parse(raw);lines=raw.splitlines(keepends=True)
        for key,sha in rule['approved'].items():
            start,end=_span(_node(tree,key));assert hashlib.sha256(b''.join(lines[start-1:end])).hexdigest()==sha
        assert_reviewed_notification_source(path)


@pytest.mark.parametrize('path',[
 'main.py','exchange/safe_retry.py','exchange/verified_pnl/verifier.py','exchange/verified_pnl/evidence.py',
 'exchange/verified_pnl/history.py','exchange/verified_pnl/ledger.py','exchange/verified_pnl/bootstrap.py',
 'exchange/verified_pnl/pnl_calculator.py','exchange/verified_pnl/reconciliation.py','exchange/verified_pnl/outbox.py'])
def test_original_guard_rejects_execution_and_financial_file_mutations(path):
    sys.path.insert(0,str(ROOT/'audit_artifacts/multi_market'))
    from test_multi_market import test_main_order_and_financial_guards_unchanged as check
    original=Path.read_bytes
    def altered(p):
        raw=original(p)
        return raw+b'\n# unauthorized edit negative control\n' if p.resolve()==(ROOT/path).resolve() else raw
    with patch.object(Path,'read_bytes',altered):
        with pytest.raises(AssertionError):check()
