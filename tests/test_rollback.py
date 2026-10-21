"""Restore into a newly owned temporary directory; never overwrite the workspace."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
backup = ROOT / "pnl_revision_baseline"


def normalize_rel_path(raw_path: str) -> Path:
    # Convert Windows backslashes and POSIX forward slashes safely
    normalized = raw_path.replace("\\", "/").strip()
    if normalized.startswith("/") or normalized.startswith("\\"):
        raise ValueError(f"Absolute path detected: {raw_path}")
    p = Path(normalized)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        raise ValueError(f"Path traversal or invalid path detected: {raw_path}")
    return p


def test_rollback():
    manifest_path = backup / "manifest.json"
    if not manifest_path.exists():
        # Verify normalize_rel_path standalone when external revision backup folder is not present
        assert normalize_rel_path("exchange\\binance.py") == Path("exchange/binance.py")
        pytest.skip("Rollback baseline files not present (pnl_revision_baseline/manifest.json missing)")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    with tempfile.TemporaryDirectory(prefix="poa-rollback-") as directory:
        restored = Path(directory).resolve()
        for entry in manifest:
            rel_path = normalize_rel_path(entry["Path"])
            target = (restored / rel_path).resolve()
            assert target.is_relative_to(restored), f"Target escaping root: {target}"
            source = (backup / rel_path).resolve()
            assert source.is_relative_to(backup.resolve()), f"Source escaping root: {source}"
            assert hashlib.sha256(source.read_bytes()).hexdigest().upper() == entry["SHA256"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            assert hashlib.sha256(target.read_bytes()).hexdigest().upper() == entry["SHA256"]
        print(f"PASS: restore {len(manifest)} revision baseline files by SHA256", flush=True)

        env = dict(os.environ)
        sep = ";" if sys.platform == "win32" else ":"
        pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{ROOT}{sep}{ROOT / 'tests'}" + (f"{sep}{pp}" if pp else "")

        for script, golden in [("legacy_snapshot.py", "golden_legacy.json"),
                               ("legacy_extended.py", "golden_extended.json"),
                               ("pnl_legacy_snapshot.py", "golden_pnl_legacy.json")]:
            output = restored / (script + ".json")
            subprocess.run([sys.executable, str(ROOT / "tests" / script), str(restored), str(output)], check=True, cwd=ROOT, env=env)
            assert json.loads(output.read_text(encoding="utf-8")) == json.loads((ROOT / "tests" / golden).read_text(encoding="utf-8"))
        print("PASS: restored source/settings webhook and Discord fixtures (123 observations)")


if __name__ == "__main__":
    try:
        test_rollback()
    except pytest.skip.Exception as e:
        print(f"SKIP: {e}", flush=True)
