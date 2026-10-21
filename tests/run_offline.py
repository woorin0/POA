import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sep = ";" if sys.platform == "win32" else ":"
current_pp = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = f"{ROOT}{sep}{ROOT / 'tests'}" + (f"{sep}{current_pp}" if current_pp else "")

# 1. Verify 76 order-execution & FastAPI route observations match Base (golden_legacy.json) 100%
subprocess.run([sys.executable, str(ROOT / "tests" / "legacy_snapshot.py"), str(ROOT),
                str(ROOT / "tests" / "current_legacy.json")], check=True, cwd=ROOT)
expected_legacy = json.loads((ROOT / "tests" / "golden_legacy.json").read_text(encoding="utf-8"))
actual_legacy = json.loads((ROOT / "tests" / "current_legacy.json").read_text(encoding="utf-8"))
if expected_legacy != actual_legacy:
    raise SystemExit("FAIL: golden_legacy.json mismatch")
print(f"PASS: {len(expected_legacy)} observations match golden_legacy.json", flush=True)

# 2. Verify extended order observations match Base (golden_extended.json) AND v2 embeds match golden_extended_v2.json
subprocess.run([sys.executable, str(ROOT / "tests" / "legacy_extended.py"), str(ROOT),
                str(ROOT / "tests" / "current_extended.json")], check=True, cwd=ROOT)
base_extended = json.loads((ROOT / "tests" / "golden_extended.json").read_text(encoding="utf-8"))
v2_extended = json.loads((ROOT / "tests" / "golden_extended_v2.json").read_text(encoding="utf-8"))
actual_extended = json.loads((ROOT / "tests" / "current_extended.json").read_text(encoding="utf-8"))
if actual_extended != v2_extended:
    raise SystemExit("FAIL: golden_extended_v2.json mismatch")
order_only_base = [r for r in base_extended if "embed" not in r]
order_only_actual = [r for r in actual_extended if "embed" not in r]
if order_only_base != order_only_actual:
    raise SystemExit("FAIL: order-execution records in legacy_extended.py diverged from Base")
print(f"PASS: {len(order_only_actual)} order records match Base & {len(actual_extended)} total records match golden_extended_v2.json", flush=True)

# 3. Verify PnL alert fixtures match v2 baseline (golden_pnl_v2.json) and preserve Base order metadata
pnl_output = ROOT / "tests/current_pnl_legacy.json"
subprocess.run([sys.executable, str(ROOT / "tests/pnl_legacy_snapshot.py"), str(ROOT), str(pnl_output)], check=True, cwd=ROOT)
base_pnl = json.loads((ROOT / "tests/golden_pnl_legacy.json").read_text(encoding="utf-8"))
v2_pnl = json.loads((ROOT / "tests/golden_pnl_v2.json").read_text(encoding="utf-8"))
actual_pnl = json.loads(pnl_output.read_text(encoding="utf-8"))
if actual_pnl != v2_pnl:
    raise SystemExit("FAIL: golden_pnl_v2.json mismatch")
for b, a in zip(base_pnl, actual_pnl):
    assert b["name"] == a["name"] and b["exit_type_preserved"] == a["exit_type_preserved"] and b["result"] == a["result"] and b["error"] == a["error"]
print(f"PASS: {len(actual_pnl)} alert fixtures match golden_pnl_v2.json and preserve Base order metadata", flush=True)

# Clean up temporary test output files
for tmp in [
    ROOT / "tests/current_legacy.json",
    ROOT / "tests/current_extended.json",
    ROOT / "tests/current_pnl_legacy.json",
]:
    if tmp.exists():
        tmp.unlink()

# 4. Run all 13 regression suites + v2 suite + Codex independent audit counterexamples + rollback test
for suite in [
    "test_safe_retry.py",
    "test_verified_pnl.py",
    "test_verified_integrity.py",
    "test_lightweight_profile.py",
    "test_upbit_verified.py",
    "test_upbit_safe_retry.py",
    "test_defect_fixes.py",
    "test_two_defects.py",
    "test_three_defects.py",
    "test_two_fixes_concurrency_dedup.py",
    "test_ccxt_shutdown_race.py",
    "test_atomic_lease_shutdown.py",
    "test_double_close_prevention.py",
    "test_discord_embed_v2.py",
    "test_independent_audit.py",
    "test_rollback.py",
]:
    subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests" / suite), "-q", "--tb=short"], check=True, cwd=ROOT)
