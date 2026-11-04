"""Frozen original FastAPI/v1 HTTP oracle; actual current adapters and DTOs."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_original_http_order_price_hedge_contracts(tmp_path):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8',
           'PYTHONPATH': os.pathsep.join(map(str, (ROOT/'audit_artifacts', ROOT/'tests', ROOT)))}
    result = subprocess.run([sys.executable, str(ROOT/'release_checks/http_probe.py'), str(tmp_path)],
                            cwd=ROOT, env=env, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    actual = json.loads((tmp_path/'http_contract_product.json').read_text(encoding='utf-8'))
    expected = json.loads((ROOT/'release_checks/http_v1_expected.json').read_text(encoding='utf-8'))
    assert len(expected['observations']) == 65
    assert actual['observations'] == expected['observations']
    media = json.loads((ROOT/'release_checks/http_media_expected.json').read_text(encoding='utf-8'))
    assert len(media) == 36 and actual['media_observations'] == media
    assert all(not row['engine_calls'] for row in actual['media_observations'] if row['status'] >= 400)
    rejected = [row for row in actual['media_observations']
                if row['case'] in ('text/plain|valid', 'application/octet-stream|valid')]
    assert len(rejected) == 2 and all(row['status'] == 422 and not row['engine_calls'] for row in rejected)
    assert actual['whitelist_unapproved_status'] == expected['whitelist_unapproved_status'] == 403
    assert all(not row['engine_calls'] for row in actual['observations'] if row['status'] >= 400)


def test_shared_financial_model_coercion_remains_v1():
    from pydantic.v1 import BaseModel
    from exchange.verified_pnl.models import NotificationPayload, VerifiedFill
    assert issubclass(NotificationPayload, BaseModel) and issubclass(VerifiedFill, BaseModel)
    # Exercise original constructors/serialization, without issuing any receipt.
    fill = VerifiedFill(exchange='BINANCE', symbol='BTC/USDT', order_id=123)
    assert fill.order_id == '123' and fill.dict()['order_id'] == '123'
    payload = NotificationPayload(event_id='v1-test', exchange='BINANCE', symbol='BTC/USDT',
                                  side='매도', order_name='test', exit_reason='UNKNOWN',
                                  pnl_result='UNKNOWN', fill_status='UNVERIFIED',
                                  event_timestamp=0, net_pnl=8)
    assert payload.net_pnl == '8' and json.loads(payload.json())['net_pnl'] == '8'
