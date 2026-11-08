"""Resume ONLY the known incomplete new-server BOT01 POA Stage after PocketBase curl failure.

Never deletes an operational DB, .env or service. Does not send orders.
Updates cloud-init helper scripts from a digest-pinned, already published
repository commit, then starts the ordinary one-click bootstrap. Running it
on an existing working POA server is blocked.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from urllib.request import urlopen

APP=Path("/root/POA")
DB=Path("/root/db")
STAGE=Path("/root/POA_INSTALL_RESULT.json")
CONFIG=Path("/root/poa-oneclick-input.json")
PIN="5b86b4368fb28adfe38bc98397bad32a01df9426"
MANIFEST="0c464b1387d7ced078d4d957a1a9bdc947b8bb1d425518aba631fcc623d279fa"
SOURCE="531daf84282de9957a59b1ea781770658afee2ef"
YAML_SHA="b195f4d3c0566f517b710515889f457e451f47c212a8189afe3f041e3b3e7ef0"
PB_SHA="1b461b6e558884b0c6ccd558c4c989c48944dfdb9c719c467306008b5baa1b1e"


def run(argv, error, timeout=1800):
    proc=subprocess.run(argv,capture_output=True,timeout=timeout,check=False)
    if proc.returncode:
        raise RuntimeError(error)
    return proc.stdout


def check():
    if os.geteuid()!=0 or 'VERSION_ID="22.04"' not in Path("/etc/os-release").read_text():
        raise RuntimeError("NEW_UBUNTU_2204_ROOT_ONLY")
    if not APP.is_dir() or not DB.is_dir() or not CONFIG.is_file():
        raise RuntimeError("EXPECTED_PARTIAL_STAGE_MISSING")
    if STAGE.exists() or (APP/".env").exists() or (APP/"LOCAL_READY").exists() or (DB/"pb_data").exists():
        raise RuntimeError("EXISTING_DATA_STOP_NO_OVERWRITE")
    if (DB/"pocketbase").exists():
        raise RuntimeError("POCKETBASE_BINARY_ALREADY_PRESENT")
    if run(["git","-C",str(APP),"rev-parse","HEAD"],"SOURCE_UNAVAILABLE").decode().strip()!=PIN:
        raise RuntimeError("SOURCE_PIN_MISMATCH")
    if not (APP/".venv/bin/python").is_file():
        raise RuntimeError("STAGE_PYTHON_NOT_READY")
    if CONFIG.stat().st_mode & 0o077:
        raise RuntimeError("USER_CONFIGURATION_NOT_PRIVATE")


def download():
    archive=DB/"pocketbase.zip"
    temp=DB/"pocketbase-recovery.tmp"
    if temp.exists():
        raise RuntimeError("RECOVERY_TEMP_ALREADY_PRESENT")
    if archive.exists() and hashlib.sha256(archive.read_bytes()).hexdigest()==PB_SHA:
        return archive
    url=("https://github.com/pocketbase/pocketbase/releases/download/v0.22.48/"
         "pocketbase_0.22.48_linux_amd64.zip")
    args=["curl","--fail","--silent","--show-error","--location",
          "--retry","6","--retry-all-errors","--retry-delay","4",
          "--connect-timeout","20","--max-time","180","--output",str(temp),
          "--write-out","%{http_code}",url]
    proc=subprocess.run(args,capture_output=True,timeout=1500,check=False)
    if proc.returncode:
        code=proc.stdout.decode("ascii",errors="ignore").strip()
        raise RuntimeError("POCKETBASE_DOWNLOAD_FAILED_HTTP_"+code[:3] if len(code)==3
                           else "POCKETBASE_DOWNLOAD_FAILED_NETWORK")
    if hashlib.sha256(temp.read_bytes()).hexdigest()!=PB_SHA:
        raise RuntimeError("POCKETBASE_SHA256_MISMATCH")
    os.replace(temp,archive)
    return archive


def resume():
    check()
    import yaml
    spec=importlib.util.spec_from_file_location("verified_installer",str(APP/"deployment/install_once.py"))
    inst=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)
    if inst.PB_SHA256!=PB_SHA:
        raise RuntimeError("POCKETBASE_SOURCE_HASH_CHANGED")
    inst.verify_source(MANIFEST)
    pkg=download()
    with zipfile.ZipFile(pkg) as z:
        if "pocketbase" not in z.namelist():
            raise RuntimeError("POCKETBASE_ARCHIVE_INVALID")
        with (DB/"pocketbase").open("xb") as output:
            output.write(z.read("pocketbase"))
    (DB/"pocketbase").chmod(0o700)
    python=str(APP/".venv/bin/python")
    run([python,"-m","pip","install","--no-cache-dir","pytest==9.1.1","PyYAML==6.0.3"],
        "OFFLINE_TEST_DEPS_FAILED")
    run([python,"-m","pip","check"],"PIP_CHECK_FAILED")
    run([python,str(APP/"release_checks/run.py"),"-q","--tb=short"],
        "OFFLINE_RELEASE_TEST_FAILED",timeout=900)
    run(["git","-C",str(APP),"diff","--exit-code","HEAD"],"SOURCE_CHANGED")
    inst.verify_source(MANIFEST)
    stage={"status":"STAGED_NOT_LIVE","commit":PIN,"manifest_sha256":MANIFEST,
           "RESTORE_VERIFIED":"NO","OPERATING_READY":"NO"}
    fd=os.open(STAGE,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as out:
        json.dump(stage,out)
    # Fetch the already-published one-click scripts only, never another user's credentials.
    url=("https://raw.githubusercontent.com/woorin0/POA/"+SOURCE+
         "/deployment/POA_VULTR_ONECLICK_USER_DATA.yaml")
    with urlopen(url,timeout=45) as response:
        original=response.read(20000)
    if hashlib.sha256(original).hexdigest()!=YAML_SHA:
        raise RuntimeError("RESCUE_SCRIPT_SOURCE_MISMATCH")
    records={x["path"]:x["content"] for x in yaml.safe_load(original)["write_files"]}
    for name in ("stage-full-history.py","ubuntu22_oneclick.py"):
        target=Path("/usr/local/lib/poa")/name
        raw=records[str(target)].encode("utf-8")
        temp=target.with_suffix(".rescue-new")
        if temp.exists() or target.is_symlink():
            raise RuntimeError("UNEXPECTED_HELPER_STATE")
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"wb") as out:
            out.write(raw)
        os.replace(temp,target)
    print("POA_STAGE_RECOVERED; starting normal one-click activation",flush=True)
    run([sys.executable,"/usr/local/lib/poa/ubuntu22_oneclick.py"],
        "POA_LOCAL_ACTIVATION_FAILED",timeout=2400)
    status=json.loads(Path("/root/POA_ONECLICK_RESULT.json").read_text())
    if status.get("state")!="RUNNING_HTTPS":
        raise RuntimeError("POA_HEALTH_RESULT_FAILED")
    print("POA_RECOVERY_RUNNING_HTTPS="+status["webhook_url"],flush=True)


if __name__=="__main__":
    try:
        resume()
    except Exception as e:
        # Only fixed enum-style errors are surfaced, never raw credentials.
        code=str(e)
        if not code.replace("_","").replace("0","").replace("1","").replace("2","").replace("3","").replace("4","").replace("5","").replace("6","").replace("7","").replace("8","").replace("9","").isupper():
            code="RECOVERY_UNEXPECTED_FAILURE"
        print("POA_RECOVERY_FAILED="+code,file=sys.stderr)
        raise SystemExit(75)
