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


def assert_reviewed_notification_source(path,source=None):
    """Validate exact approved spans AND byte equality of everything outside them."""
    rule=PINS[path]
    current=Path(ROOT/path).read_bytes() if source is None else source
    historic=subprocess.check_output(['git','-C',str(ROOT),'show',rule['baseline']+':'+path])
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
