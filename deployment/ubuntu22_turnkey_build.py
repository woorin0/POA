"""Produce one compact Vultr Ubuntu 22.04 cloud-init for a PINNED Git commit.

Downloads ONLY audited scripts at that exact revision, SHA256 checks each
before running the pre-existing NEW_SERVER_STAGE_ONLY installation.
Cloud-init never includes API keys, restores real DBs, exposes /order or
starts a trading-capable service. One local poa-setup command after secret
configuration activates localhost PocketBase/POA (fresh or restored).
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
SCRIPTS = (
    "install_once.py",
    "prepare_ubuntu.py",
    "ubuntu22_operational.py",
    "ubuntu22_release_handoff.py",
    "validate_config.py",
)
MANIFEST_DIGEST = "0c464b1387d7ced078d4d957a1a9bdc947b8bb1d425518aba631fcc623d279fa"

# Kept small and stdlib-only so cloud-init can execute on bare Ubuntu22.
BOOTSTRAP = r'''import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen

def main():
    os.umask(0o077)
    pinned = json.loads(Path("/root/poa-stage-input.json").read_text())
    commit = pinned["repo_ref"]
    scripts = json.loads(Path("/usr/local/lib/poa/verified_script_hashes.json").read_text())
    target = Path("/usr/local/lib/poa")
    if not target.is_dir() or len(scripts) != 5:
        raise RuntimeError("STAGE_SCRIPT_INVENTORY_INVALID")
    for name, digest in scripts.items():
        if name not in ("install_once.py","prepare_ubuntu.py",
                        "ubuntu22_operational.py","ubuntu22_release_handoff.py",
                        "validate_config.py"):
            raise RuntimeError("STAGE_SCRIPT_NOT_APPROVED")
        url = ("https://raw.githubusercontent.com/woorin0/POA/"
               + commit + "/deployment/" + name)
        with urlopen(Request(url, headers={"User-Agent":"POA-Stage"}), timeout=45) as resp:
            content = resp.read(90000)
        if hashlib.sha256(content).hexdigest() != digest:
            raise RuntimeError("PINNED_STAGE_SCRIPT_HASH_MISMATCH")
        f = target / name
        with f.open("xb") as output:
            output.write(content)
        f.chmod(0o600)
    # Runtime package/service preparation uses original verified install code.
    for name in ("prepare_ubuntu.py", "install_once.py"):
        p = subprocess.run(["python3", str(target/name)], check=False, timeout=2400)
        if p.returncode:
            raise RuntimeError("STAGE_PREPARATION_OR_INSTALL_FAILED")
    result = json.loads(Path("/root/POA_INSTALL_RESULT.json").read_text())
    if result.get("status") != "STAGED_NOT_LIVE" or result.get("commit") != commit:
        raise RuntimeError("STAGE_COMPLETION_UNVERIFIED")
    print("POA_STAGE_INSTALLED; run poa-setup fresh or restore locally")
if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print("POA_BOOTSTRAP_BLOCKED:",str(exc),file=sys.stderr)
        raise SystemExit(75)
'''

def create(commit: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("PINNED_COMMIT_REQUIRED")
    hashes = {}
    for name in SCRIPTS:
        script = HERE / name
        if not script.is_file() or script.is_symlink():
            raise ValueError("SOURCE_SCRIPT_UNAVAILABLE")
        raw = script.read_bytes()
        if len(raw) > 90000:
            raise ValueError("SCRIPT_TOO_LARGE")
        hashes[name] = hashlib.sha256(raw).hexdigest()
    config = {
        "package_update": False,
        "timezone": "UTC",
        "packages": ["python3", "ca-certificates"],
        "write_files": [],
        "runcmd": [["bash", "-euc",
                    "umask 077; python3 /usr/local/lib/poa/bootstrap-ubuntu22.py"]],
        "final_message": (
            "STAGED_READY_NO_TRADES. To configure: "
            "sudo cp /root/poa-config.example.json /root/poa-config.json; "
            "sudo nano /root/poa-config.json; "
            "sudo poa-setup fresh --config /root/poa-config.json. "
            "No API keys are included in this User Data."
        ),
    }
    def add(path: str, body: str, permissions="0600"):
        config["write_files"].append({
            "path": path, "owner": "root:root", "permissions": permissions,
            "content": body,
        })
    add("/root/poa-stage-input.json", json.dumps({
        "install_mode": "NEW_SERVER_STAGE_ONLY",
        "repo_ref": commit,
        "release_manifest_sha256": MANIFEST_DIGEST,
    }, separators=(",", ":")))
    add("/usr/local/lib/poa/verified_script_hashes.json",
        json.dumps(hashes, sort_keys=True))
    add("/usr/local/lib/poa/bootstrap-ubuntu22.py", BOOTSTRAP)
    add("/usr/local/bin/poa-setup",
        "#!/bin/sh\nexec /usr/bin/python3 /usr/local/lib/poa/ubuntu22_operational.py \"$@\"\n",
        "0700")
    add("/root/poa-config.example.json", json.dumps({
        "domain": "your-published-domain.example.org",
        "db_admin_email": "admin@your-domain.example.org",
        "credentials": {
            "PASSWORD": "CHANGE_ME_TRADINGVIEW_PASSWORD",
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/REPLACE",
            "UPBIT_KEY": "CHANGE_ME_UPBIT_KEY",
            "UPBIT_SECRET": "CHANGE_ME_UPBIT_SECRET",
        },
    }, indent=2), "0600")
    result = "#cloud-config\n" + json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if len(result.encode()) > 16000:
        raise ValueError("USER_DATA_SIZE_EXCESSIVE")
    return result

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Write pinned Ubuntu22 Vultr cloud-init")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("OUTPUT_ALREADY_EXISTS")
    args.output.write_text(create(args.commit), encoding="utf-8", newline="\n")
    print("USERDATA_CREATED_STAGE_ONLY; NO_ORDERS; LOCAL_SETUP_REQUIRED")
