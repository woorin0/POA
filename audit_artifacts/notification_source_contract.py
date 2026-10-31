"""Exact source pins for reviewed notification-only changes; test code only.

Historic file pins remain the baseline. Only five exact source spans are
accepted, each SHA256-pinned to the reviewed 3a952334 commit. Replacing those
spans with their historical source must reconstruct the entire old file.
No arbitrary function/class/financial/CAS changes are exempted.
"""
import ast,hashlib,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
APPROVED_COMMIT='3a9523341ac75a550b31b4f9c090258814d91f76'
PINS={
    "exchange/model/schemas.py": {
        "baseline": "c99be38640d0b1d665ca7f921612380028443771",
        "approved": {
            "OrderRequest.validate_tv_binding": "7cd60668da3222723b3af83245f1ab0d0a12f9b2b919848a8e7e126df059129f",
            "Settings.POA_NOTIFICATION_SERVER_ALIAS": "02a97b04929a13a1650201c51784991dae4e55eb369b41e14ce65bb98a618d86"
        }
    },
    "exchange/verified_pnl/delivery_store.py": {
        "baseline": "6a84d84b6cbb740a42773f0b89adfe4454407eb3",
        "approved": {
            "NotificationOutboxStore.submit_observed": "1517c75b8b3f59dbb548c99d9894e51f84c2ad7d5a832daafaadc22312d3b1e3",
            "NotificationOutboxStore._estimated_authority": "fb5a4ced719795f264f2e74f46fec705ba0f49fed269bb18b798b7c99b49452b",
            "NotificationOutboxStore._buy_notification_authority": "1fa297df6dfa91cf093934226254df709b30cfbe2e54f8a47c05ead2d0f4e3cf"
        }
    }
}

BOT03_MODULE_SHA256='dbd0203e4687b14b4b56f192cfccb06eb80d8ecf15bdbe37a8dc662a958860de'

# Explicit BOT03 acceptance->observed-fill admission delta; no CAS/sender exception.
BOT03_PINS={
    "NotificationOutboxStore._buy_notification_authority": "0d21b4b0cdcd4d35b61c1a34cd93cfba5acde90678b062a0424017f38c1fc46f",
    "NotificationOutboxStore._notice_gate": "e7ccacf7fd87f11eee27a5cb78855d3c968fed8f1ab769777ff4f422947fabbe"
}


def _node(tree,key,required=True):
    body=tree.body
    for part in key.split('.'):
        found=[n for n in body if getattr(n,'name',None)==part or
               (isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id==part)]
        assert len(found)<=1, 'duplicate pinned declaration: '+key
        if not found:
            assert not required, 'missing pinned declaration: '+key
            return None
        node=found[0];body=getattr(node,'body',[])
    return node


def _span(node):
    return min([node.lineno]+[d.lineno for d in getattr(node,'decorator_list',[])]),node.end_lineno


def _bot03_delta(path,current):
    if path!='exchange/verified_pnl/delivery_store.py':return current
    assert hashlib.sha256((ROOT/'exchange/verified_pnl/buy_ack.py').read_bytes()).hexdigest()==BOT03_MODULE_SHA256, 'unapproved buy acknowledgement authority module'
    oldraw=subprocess.check_output(['git','-C',str(ROOT),'show',APPROVED_COMMIT+':'+path])
    tree=ast.parse(current);old=ast.parse(oldraw);lines=current.splitlines(keepends=True);oldlines=oldraw.splitlines(keepends=True);edits=[]
    for key,sha in BOT03_PINS.items():
        start,end=_span(_node(tree,key))
        assert hashlib.sha256(b''.join(lines[start-1:end])).hexdigest()==sha, 'unapproved BOT03 notification delta: '+key
        begin,finish=_span(_node(old,key));edits.append((start-1,end,oldlines[begin-1:finish]))
    for start,end,original in sorted(edits,reverse=True):lines[start:end]=original
    return b''.join(lines)


PRESENTATION_PINS={
    "main.py": {
        "startup": "e8d241400fd2d73e46ce2c12d674f5b6e434d116cdedb5cc15cbc06a0462a14a",
        "shutdown": "8faaf6d85ac77373df6da30ef78a5d0a59c55c4329d75adafd367409c5f9ebd7",
        "order": "c5ae65d460eff9eccdb4d2cfd0c1afa4603c1aaba02af9577071514f4ff3e3bc"
    },
    "exchange/utility/LogMaker.py": {
        "log_error_message": "e4a48121a0c8efd7a816d535012c8cff7ddb3706c2b0fee17d911bc577d7752c",
        "log_order_error_message": "c40ed7aeb77a3aa5b9f6f3b213d64fbeb7f65b7effd71e48bcdb9ca2f8b45d3f",
        "log_order_message": "d9c67d2cb5355adc406e558a0eeac62bc8ca38f03913d9be7faf765ebe05ec5e",
        "log_alert_message": "6e665581682acf342399a8f6420f0a6093f992319051754965b7139a93d36901",
        "log_validation_error_message": "46f7c2e72f5cb8a93077b2f0b8ccfb7ad5cfa9afab774e554551ab9e2b2bcd48"
    },
    "exchange/verified_pnl/delivery_store.py": {
        "NotificationOutboxStore.enqueue": "14063eadddd01f77ef56c364cd2a982c61939b0abcc5dc75382695541c93fb84"
    }
}
PRESENTATION_MODULES={
    "exchange/verified_pnl/alert_presentation.py": "12ab42a4db7bbaf088f2128e1c3975490e3a388d888149d918481fae3a6e4ddc",
    "exchange/verified_pnl/operational_alerts.py": "587d11fa451b2091c636eea439bb63a6deeddf81593fcdc238fae9f34811c424"
}


def _presentation_modules():
    for path,expected in PRESENTATION_MODULES.items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==expected, 'unapproved presentation/operational authority module: '+path


def _presentation_delta(path,current):
    _presentation_modules()
    rules=PRESENTATION_PINS.get(path,{})
    if not rules:return current
    prior=subprocess.check_output(['git','-C',str(ROOT),'show',APPROVED_COMMIT+':'+path])
    tree=ast.parse(current);old=ast.parse(prior);lines=current.splitlines(keepends=True);before=prior.splitlines(keepends=True);edits=[]
    for key,sha in rules.items():
        start,end=_span(_node(tree,key))
        assert hashlib.sha256(b''.join(lines[start-1:end])).hexdigest()==sha, 'unapproved notification instrumentation: '+path+':'+key
        begin,finish=_span(_node(old,key));edits.append((start-1,end,before[begin-1:finish]))
    for start,end,original in sorted(edits,reverse=True):lines[start:end]=original
    return b''.join(lines)


def assert_notification_execution_unchanged(source=None):
    path='main.py';current=(ROOT/path).read_bytes() if source is None else source
    prior=subprocess.check_output(['git','-C',str(ROOT),'show',APPROVED_COMMIT+':'+path])
    tree=ast.parse(current);old=ast.parse(prior);node=_node(tree,'order');previous=_node(old,'order')
    expected=ast.parse("from exchange.verified_pnl.operational_alerts import order_failure\norder_failure(settings,e,order_info)\n").body
    for handler in [n for n in node.body if isinstance(n,ast.Try)][0].handlers:
        if isinstance(handler.type,ast.Name) and handler.type.id in ('TypeError','Exception'):
            assert [ast.dump(n) for n in handler.body[:2]]==[ast.dump(n) for n in expected],'only the exact two error-report statements are allowed'
            handler.body=handler.body[2:]
    assert ast.dump(node)==ast.dump(previous),'order execution/quantity/Safe Retry changed'
    # Startup's protected reconciliation branch is independent of error reporting.
    assert ast.dump(_node(tree,'startup').body[0])==ast.dump(_node(old,'startup').body[0]),'Safe Retry startup changed'


def assert_notification_main_source():
    raw=(ROOT/'main.py').read_bytes();assert_notification_execution_unchanged(raw)
    restored=_presentation_delta('main.py',raw)
    assert restored==subprocess.check_output(['git','-C',str(ROOT),'show',APPROVED_COMMIT+':main.py']),'non-notification main source changed'
    path='exchange/utility/LogMaker.py'
    restored=_presentation_delta(path,(ROOT/path).read_bytes())
    assert restored==subprocess.check_output(['git','-C',str(ROOT),'show',APPROVED_COMMIT+':'+path]),'non-error LogMaker source changed'

def assert_reviewed_notification_source(path,source=None):
    """Validate exact approved spans AND byte equality of everything outside them."""
    rule=PINS[path]
    current=Path(ROOT/path).read_bytes() if source is None else source
    historic=subprocess.check_output(['git','-C',str(ROOT),'show',rule['baseline']+':'+path])
    current=_presentation_delta(path,current)
    current=_bot03_delta(path,current)
    tree=ast.parse(current);old=ast.parse(historic)
    lines=current.splitlines(keepends=True);oldlines=historic.splitlines(keepends=True)
    replacements=[]
    for key,expected in rule['approved'].items():
        node=_node(tree,key);start,end=_span(node)
        actual=hashlib.sha256(b''.join(lines[start-1:end])).hexdigest()
        assert actual==expected, 'unapproved notification code: '+path+':'+key
        previous=_node(old,key,required=False)
        if previous is None:
            assert key=='Settings.POA_NOTIFICATION_SERVER_ALIAS', 'unapproved new declaration'
            original=[]
        else:
            begin,finish=_span(previous);original=oldlines[begin-1:finish]
        replacements.append((start-1,end,original))
    for start,end,original in sorted(replacements,reverse=True):lines[start:end]=original
    assert b''.join(lines)==historic, 'protected source changed outside approved notification spans: '+path
