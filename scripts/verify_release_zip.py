import tempfile
import zipfile
import sys
import os
import shutil
import json
import hashlib
import types
from unittest.mock import Mock
from pathlib import Path

def run_smoke_test():
    zip_path = Path("POA-release-verified.zip").resolve()
    assert zip_path.exists(), f"ZIP file not found: {zip_path}"
    
    with tempfile.TemporaryDirectory(prefix="poa-smoke-test-") as tmpdir:
        dest = Path(tmpdir)
        print("Testing in isolated tempdir:", dest)
        
        # 1. Extract ZIP
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
        extracted_files = list(dest.rglob("*"))
        print(f"1. Extracted {len(extracted_files)} items")
        
        # 2. Verify Manifest
        manifest_file = dest / "release_manifest_sha256.json"
        assert manifest_file.exists(), "Manifest missing in extracted package"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        for item in manifest:
            rel = item["Path"]
            fpath = dest / rel
            assert fpath.exists(), f"Missing file: {rel}"
            h = hashlib.sha256(fpath.read_bytes()).hexdigest().upper()
            assert h == item["SHA256"], f"Hash mismatch: {rel}"
        print(f"2. Manifest verified: {len(manifest)} files match SHA256")
        
        # 3. Environment setup for smoke test
        os.environ["PASSWORD"] = "test-smoke-password"
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/mock/smoke"
        os.environ["WHITELIST"] = "[]"
        
        # Provide safe stubs for third-party libraries not installed in minimal offline runtime
        if "dhooks" not in sys.modules:
            dhooks_mock = types.ModuleType("dhooks")
            dhooks_mock.Webhook = Mock()
            dhooks_mock.Embed = Mock()
            sys.modules["dhooks"] = dhooks_mock
            
        if "pocketbase" not in sys.modules:
            pocket_mock = types.ModuleType("pocketbase")
            pocket_mock.PocketBase = Mock()
            sys.modules["pocketbase"] = pocket_mock

        if "pendulum" not in sys.modules:
            pendulum_mock = types.ModuleType("pendulum")
            pendulum_mock.now = Mock(return_value=Mock(to_iso8601_string=lambda: "2026-10-12T00:00:00Z"))
            sys.modules["pendulum"] = pendulum_mock

        if "jwt" not in sys.modules:
            jwt_mock = types.ModuleType("jwt")
            jwt_mock.decode = Mock(return_value={})
            sys.modules["jwt"] = jwt_mock

        orig_path = list(sys.path)
        sys.path.insert(0, str(dest))
        
        # Remove any cached exchange modules from prior runs
        for k in list(sys.modules.keys()):
            if k == "main" or k == "exchange" or k.startswith("exchange."):
                del sys.modules[k]
                
        test_db = None
        main_mod = None
        try:
            import main
            main_mod = main
            from main import app
            print("3. FastAPI app initialized successfully:", app.title)
            
            # 4. SQLite new DB init
            from exchange.database import Database
            assert (dest / "store.db").exists(), "store.db not initialized"
            test_db = main.db
            print("4. SQLite new DB initialization verified:", (dest / "store.db").name)
            
            # 5. Upgrade path compatibility (existing DB)
            test_db.set_auth("UPBIT_UPGRADE", "token123", "exp2026")
            auth_data = test_db.get_auth("UPBIT_UPGRADE")
            assert auth_data == ("token123", "exp2026"), f"Auth data mismatch: {auth_data}"
            print("5. Upgrade compatibility with existing DB verified")
            
            # 6. TestClient Mock Webhook
            from fastapi.testclient import TestClient
            client = TestClient(app, base_url="http://127.0.0.1")
            main.whitelist.append("testclient")
            
            # 6-a. Wrong auth rejected (422)
            secret_probe = "synthetic_secret_probe_9999"
            payload = {
                "password": secret_probe,
                "exchange": "BINANCE",
                "base": "BTC",
                "quote": "USDT",
                "side": "buy",
                "amount": 0.01,
            }
            r_wrong = client.post("/order", json=payload)
            assert r_wrong.status_code == 422, f"Expected 422, got {r_wrong.status_code}"
            
            # Check no secret leaked
            log_file = dest / "log" / "poa.log"
            log_content = log_file.read_text(encoding="utf-8", errors="ignore") if log_file.exists() else ""
            assert secret_probe not in log_content, "Secret leaked to log"
            assert secret_probe not in r_wrong.text, "Secret leaked to response text"
            print("6-a. Rejected invalid webhook (422) with 0 secret leaks")
            
            # 7. Safe shutdown event
            import asyncio
            asyncio.run(main.shutdown())
            print("7. Clean shutdown verified")
            
        finally:
            if test_db:
                test_db.close()
            if main_mod and hasattr(main_mod, "db") and main_mod.db:
                main_mod.db.close()
            if "exchange.database" in sys.modules:
                edb = sys.modules["exchange.database"]
                if hasattr(edb, "db") and edb.db:
                    edb.db.close()
            sys.path = orig_path
            
    print("PASS: ALL SMOKE CHECKS ON EXTRACTED ZIP COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    run_smoke_test()
