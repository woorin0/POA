"""Offline Python 3.10 vs 3.12 finance/time contract of original POA.

This test never imports the live exchange client, starts the application, opens
a DB or executes a trade. It executes ONLY the actual get_today_timestamp
function isolated by AST, and checks the independently calculated calendar
boundaries including DST transitions under the installed Pendulum version.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta
import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pendulum
import pytest

from deployment import py312_requirements as C

ROOT = Path(__file__).resolve().parents[1]
MAIN_FIXED = "c5566c4cc2a4ce716f1f0d66260bd60e4f95546f"
TARGET_PENDULUM = "3.2.0"


def _finance_datetime_function(today):
    """Extract the verbatim production function without importing its SDKs."""
    source = (ROOT / "exchange/pexchange.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == "get_today_timestamp"]
    assert len(matches) == 1
    fn = matches[0]
    expected = ast.parse(
        'def get_today_timestamp(timezone="Asia/Seoul"):\n'
        '    today = pendulum.today(timezone)\n'
        '    today_start = int(today.start_of("day").timestamp() * 1000)\n'
        '    today_end = int(today.end_of("day").timestamp() * 1000)\n'
        '    return today_start, today_end\n'
    ).body[0]
    assert ast.dump(fn, include_attributes=False) == ast.dump(
        expected, include_attributes=False
    ), "ORIGINAL_TRADING_TIME_FUNCTION_CHANGED"
    namespace = {"pendulum": SimpleNamespace(today=today)}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<isolated-finance-time>", "exec"),
         namespace)
    return namespace["get_today_timestamp"]


@pytest.mark.parametrize("zone,year,month,day", [
    ("UTC", 2026, 11, 8),
    ("Asia/Seoul", 2026, 11, 8),
    ("Asia/Tokyo", 2026, 11, 8),
    ("Asia/Seoul", 2024, 2, 29),
    ("America/New_York", 2026, 3, 8),   # DST spring: 23 hours
    ("America/New_York", 2026, 11, 1),  # DST fall: 25 hours
    ("Europe/Berlin", 2026, 3, 29),     # DST spring: 23 hours
    ("Europe/Berlin", 2026, 10, 25),    # DST fall: 25 hours
])
def test_original_financial_day_window_matches_independent_zoneinfo_oracle(
        zone, year, month, day):
    # Freeze Pendulum's today() without a clock or a live trading module.
    fixture = lambda tz: pendulum.datetime(year, month, day, 12, 34, 56, tz=tz)
    get_today = _finance_datetime_function(fixture)
    actual_start, actual_end = get_today(zone)
    start = datetime(year, month, day, tzinfo=ZoneInfo(zone))
    tomorrow = (start.date() + timedelta(days=1))
    next_start = datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                          tzinfo=ZoneInfo(zone))
    expected_start = int(start.timestamp() * 1000)
    expected_end = int(next_start.timestamp() * 1000) - 1
    assert (actual_start, actual_end) == (expected_start, expected_end)
    assert actual_end >= actual_start
    assert actual_end - actual_start + 1 in (
        23 * 3600000, 24 * 3600000, 25 * 3600000
    )


def test_complete_dependency_overlay_preserves_every_nonpendulum_pin(tmp_path):
    original = (ROOT / "requirements.txt").read_bytes()
    assert hashlib.sha256(original).hexdigest() == C.SOURCE_SHA256
    revised = C.render_from_bytes(original)
    src = original.decode("ascii").splitlines()
    dst = revised.decode("ascii").splitlines()
    assert len(src) == len(dst) == 17
    assert [(a, b) for a, b in zip(src, dst) if a != b] == [
        (C.LEGACY_LINE, C.PY312_LINE)
    ]
    assert C.PY312_LINE == "pendulum==" + TARGET_PENDULUM
    path = tmp_path / "requirements-py312.txt"
    result = C.create_overlay(path)
    assert path.read_bytes() == revised
    assert result["original_requirements_modified"] is False
    assert result["all_other_pins_unchanged"] is True
    assert result["install_executed"] is False
    assert result["operational_ready"] is False
    with pytest.raises(C.CompatibilityBlocked, match="OUTPUT_MUST_NOT_EXIST"):
        C.create_overlay(path)
    assert (ROOT / "requirements.txt").read_bytes() == original


@pytest.mark.parametrize("changed", [
    b"pendulum==3.2.0\n",
    b"pendulum==2.1.2\npendulum==2.1.2\n",
    b"pendulum>=2.1.2\n",
    b"ccxt==0.0.0\n",
    b"",
])
def test_unapproved_dependency_mutations_are_blocked(changed):
    with pytest.raises(C.CompatibilityBlocked):
        C.render_from_bytes(changed)


def test_overlay_must_not_write_into_release_tree(tmp_path):
    with pytest.raises(C.CompatibilityBlocked, match="DO_NOT_WRITE_IN_REPOSITORY"):
        C.create_overlay(ROOT / "requirements-py312.txt")
    assert not (ROOT / "requirements-py312.txt").exists()


@pytest.mark.parametrize("import_name", [
    "jwt", "ccxt", "ccxt.async_support", "fastapi", "starlette",
    "uvicorn", "fire", "httpx", "httpcore", "h11", "loguru",
    "pocketbase", "pydantic", "dotenv", "orjson", "pendulum",
])
def test_original_runtime_dependencies_import_without_live_io(import_name):
    imported = importlib.import_module(import_name)
    assert imported is not None


def test_exact_pendulum_version_on_target_python():
    from importlib.metadata import version
    if __import__("sys").version_info[:2] == (3, 12):
        assert version("pendulum") == TARGET_PENDULUM
    else:
        # Main 3.10/3.11 regression must retain original semantics/pin.
        assert version("pendulum") == "2.1.2"


def test_static_finance_code_uses_no_other_pendulum_operations():
    # Confirm the production package only uses the tested today/start/end API.
    imports = []
    for item in ("exchange/pexchange.py", "main.py", "run.py"):
        tree = ast.parse((ROOT / item).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend((item, a.name) for a in node.names)
    assert ("exchange/pexchange.py", "pendulum") in imports
    for name in ("main.py", "run.py"):
        assert (name, "pendulum") not in imports
