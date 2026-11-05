"""Exact v1-import/HTTP adapter exception; protected bodies reconstruct baseline."""
import ast
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = 'de969040dec42a369274adb1a59a3d44a2ba3aa6'
PINS = {
    'main.py': '529dd0d634794a8ba69d572be3a93a908f8831665b15f3af25e5b72beaef7f2f',
    'exchange/model/schemas.py': '973225fbc000c486f98973ee93a401fdb9d572980ceb10f9f5cb64bbce5fdfdf',
    'exchange/verified_pnl/models.py': '6bd68367727915804ea3d45537a8d0bb3c3fbe808df3961390d7af239510f902',
    'exchange/pexchange.py': '011549ece7f80a6fd8e3a5249ed166cb969979cb97500cbe4a9785bb2865b159',
    'exchange/stock/schemas.py': 'c5c81a958d3747899485cffb19b5bcfaf234176a0cfd27dfc1b377a31e049264',
}


def restore_http_source(path, raw):
    from release_checks.fill_source_contract import restore_notification_source
    raw=restore_notification_source(path,raw)
    if path not in PINS:
        return raw
    assert hashlib.sha256(raw).hexdigest() == PINS[path], 'Unapproved HTTP/v1 source: ' + path
    original = subprocess.check_output(['git', 'show', BASE + ':' + path], cwd=ROOT)
    if path != 'main.py':
        assert raw.replace(b'from pydantic.v1 import ', b'from pydantic import ') == original
    else:
        before, after = ast.parse(original), ast.parse(raw)
        helpers = {'_v1_request', '_price_http', '_order_http', '_hedge_http'}
        after.body = [n for n in after.body if getattr(n, 'name', '') not in helpers and not
                      (isinstance(n, ast.ImportFrom) and n.module in ('pydantic.v1', 'pydantic.v1.fields')) and not
                      (isinstance(n, ast.Import) and [a.name for a in n.names] == ['json'])]
        for name in ('order', 'price', 'hedge'):
            old = next(n for n in before.body if getattr(n, 'name', '') == name)
            new = next(n for n in after.body if getattr(n, 'name', '') == name)
            new.decorator_list = old.decorator_list
        assert ast.dump(before) == ast.dump(after), 'Non-boundary main change'
    return original


def assert_http_source_pins(root=ROOT):
    for path in PINS:
        restore_http_source(path, (Path(root) / path).read_bytes())
    return frozenset(PINS)
