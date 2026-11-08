"""Independent Vultr Ubuntu 22.04 no-egress systemd contract / Mock E2E.

No actual network, CCXT, HTTP, KIS, DB, systemctl, socket creation, service
start, or Vultr server. Child processes are *models*, not OS subprocesses.
Root/host bypasses and socket activation remain NOT PROVEN at runtime.
"""
from __future__ import annotations

import ast
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from poa_stage.egress_quarantine import (
    PARENT_SHA, UNIT_NAME, UNIT_REQUIRED, SERVICE_REQUIRED,
    Admission, MockProcess, QuarantineBlocked,
    TRADES, TRANSPORTS, validate_candidate,
)

ROOT=Path(__file__).resolve().parents[1]
UNIT=ROOT/"deployment"/UNIT_NAME


@pytest.fixture(scope="module")
def unit():
    return UNIT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def contract(unit):
    return validate_candidate(unit)


def denied(code, func, *args, **kw):
    with pytest.raises(QuarantineBlocked) as err:
        func(*args,**kw)
    assert err.value.code==code


def test_unit_is_strictly_unstartable_with_no_enabled_path(unit,contract):
    assert contract==Admission(
        unit_sha256=hashlib.sha256(unit.encode()).hexdigest())
    assert contract.systemd_version_required==249
    assert contract.ubuntu_release_required=="22.04"
    assert contract.arch_required=="amd64"
    assert contract.enabled is False
    assert contract.runnable is False
    assert contract.network_permitted is False
    assert contract.operational_ready is False
    assert "[Install]" not in unit
    assert "ExecStartPre=/usr/bin/false" in unit
    assert "RefuseManualStart=yes" in unit


def test_no_live_poa_app_or_trading_secret_loaded_as_service_target(unit):
    assert "main:app" not in unit
    assert "uvicorn" not in unit
    assert "poa_stage.gateway:app" not in unit
    assert "EnvironmentFile=" not in unit
    assert "LoadCredential=" not in unit
    assert "SetCredential=" not in unit
    assert "PassEnvironment=" not in unit
    assert "BindPaths=" not in unit
    assert "ReadWritePaths=" not in unit
    assert "Sockets=" not in unit
    assert "SocketBindAllow=" not in unit
    assert "ExecStartPost=" not in unit
    assert "NetworkNamespacePath=" not in unit
    assert "ExecStart=/usr/bin/python3 -I -S -B /opt/poa-stage/poa_stage/quarantine_idle.py" in unit
    assert "PRIVATE_KEY" not in unit and "FAKE_TRADE_SECRET" not in unit


@pytest.mark.parametrize("name,expected",list(UNIT_REQUIRED.items()))
def test_pinned_unit_keys_cannot_be_removed_or_replaced(unit,name,expected):
    # Every unit directive must be present exactly once.
    assert unit.count(name+"=")==1
    denied("QUARANTINE_POLICY_MISSING",validate_candidate,
           unit.replace(name+"="+expected+"\n","",1))
    denied("QUARANTINE_POLICY_DOWNGRADE",validate_candidate,
           unit.replace(name+"="+expected+"\n",name+"=UNTRUSTED\n",1))


@pytest.mark.parametrize("name,expected",list(SERVICE_REQUIRED.items()))
def test_all_systemd_hardening_keys_are_pinned(unit,name,expected):
    assert unit.count(name+"=")==1,(name,unit.count(name+"="))
    denied("QUARANTINE_POLICY_MISSING",validate_candidate,
           unit.replace(name+"="+expected+"\n","",1))
    denied("QUARANTINE_POLICY_DOWNGRADE",validate_candidate,
           unit.replace(name+"="+expected+"\n",name+"=UNTRUSTED\n",1))


@pytest.mark.parametrize("key,value",[
    ("PrivateNetwork","no"),
    ("RestrictAddressFamilies","AF_UNIX AF_INET AF_INET6"),
    ("RestrictAddressFamilies","AF_UNIX"),
    ("IPAddressDeny",""),
    ("IPAddressDeny","0.0.0.0/8"),
    ("RestrictNamespaces","no"),
    ("NoNewPrivileges","no"),
    ("CapabilityBoundingSet","CAP_NET_ADMIN CAP_SYS_ADMIN"),
    ("AmbientCapabilities","CAP_NET_RAW"),
    ("DynamicUser","no"),
    ("ProtectHome","no"),
    ("ProtectSystem","full"),
    ("PrivateIPC","no"),
    ("ProtectKernelTunables","no"),
    ("ProtectControlGroups","no"),
    ("SystemCallArchitectures",""),
    ("StandardInput","socket"),
    ("FileDescriptorStoreMax","64"),
    ("ExecStartPre","-/usr/bin/false"),
    ("ExecStartPre","/usr/bin/true"),
    ("ExecStartPre","+/usr/bin/false"),
    ("ExecStart","/usr/bin/python3 -m uvicorn main:app"),
    ("ExecStart","/bin/sh -c '/usr/bin/python3 run.py'"),
    ("Restart","always"),
    ("Delegate","yes"),
    ("InaccessiblePaths",""),
])
def test_common_order_bypass_policy_downgrades_fail_closed(unit,key,value):
    source=SERVICE_REQUIRED[key]
    assert "QUARANTINE_POLICY" in caught_code(validate_candidate,
        unit.replace(key+"="+source+"\n",key+"="+value+"\n",1))


def caught_code(fn,*args):
    try:
        fn(*args)
    except QuarantineBlocked as e:
        return e.code
    raise AssertionError("EXPECTED_DENY")


@pytest.mark.parametrize("addition",[
    "ExecStartPost=/usr/bin/curl http://mock.invalid\n",
    "ExecStartPre=/usr/bin/true\n",
    "EnvironmentFile=/root/.env\n",
    "PassEnvironment=BINANCE_KEY\n",
    "Sockets=poa-orders.socket\n",
    "RootDirectory=/\n",
    "BindPaths=/run/docker.sock\n",
    "ReadWritePaths=/root/db/pb_data\n",
    "IPAddressAllow=any\n",
    "SocketBindAllow=any\n",
    "NetworkNamespacePath=/proc/1/ns/net\n",
    "LoadCredential=key:/root/secret\n",
    "SetCredential=TRADING_TOKEN:FAKE\n",
    "ExecReload=/bin/sh -c 'true'\n",
])
def test_injected_systemd_unit_keys_rejected(unit,addition):
    denied("QUARANTINE_POLICY_UNEXPECTED",validate_candidate,
           unit.replace("[Service]\n","[Service]\n"+addition,1))


@pytest.mark.parametrize("tail",[
    "\n[Install]\nWantedBy=multi-user.target\n",
    "\n[Socket]\nListenStream=443\n",
    "\n[Path]\nPathExists=/root/db/pb_data\n",
])
def test_no_install_socket_or_activation_unit_sections_allowed(unit,tail):
    denied("QUARANTINE_EXTRA_SECTION",validate_candidate,unit+tail)


@pytest.mark.parametrize("attack",[
    "ExecStartPre=/usr/bin/true",
    "ExecStart=/usr/bin/python3 run.py",
    "PrivateNetwork=no",
    "RestrictAddressFamilies=AF_INET",
])
def test_duplicate_keys_and_systemd_reset_lists_rejected(unit,attack):
    denied("QUARANTINE_UNIT_SYNTAX_INVALID",validate_candidate,
           unit.replace("[Service]\n","[Service]\n"+attack+"\n",1))


@pytest.mark.parametrize("operation",sorted(TRADES))
@pytest.mark.parametrize("transport",["AF_INET","AF_INET6","AF_PACKET",
    "LOOPBACK","DNS","UNIX_CONTROL","AF_UNIX_PROXY","RAW_SOCKET"])
def test_ccxt_kis_subprocess_and_nested_fork_egress_denied_in_mock(contract,operation,transport):
    child=contract.new_mock_process("poa-quarantine").spawn("python").spawn("ccxt-child")
    denied("QUARANTINE_MOCK_EGRESS_DENIED",child.attempt,operation,transport)


@pytest.mark.parametrize("transport",[
    "SOCKET_ACTIVATION_FD","SCM_RIGHTS_FD","INHERITED_FD",
])
def test_preopened_socket_fd_does_not_claim_false_egress_proof(contract,transport):
    p=contract.new_mock_process()
    denied("QUARANTINE_EXTERNAL_FD_TRUST_NOT_PROVEN",
           p.attempt,"ccxt.create_order",transport)
    denied("QUARANTINE_EXTERNAL_FD_TRUST_NOT_PROVEN",
           p.attempt,"kis.session.post",transport)


def test_fd_injection_and_privileged_namespace_escapes_are_unverified(contract):
    p=contract.new_mock_process("inert")
    denied("QUARANTINE_SOCKET_FD_INJECTION_UNVERIFIED",p.spawn,
           "inherited-fd",inherited_fd=True)
    denied("QUARANTINE_PRIVILEGE_ESCALATION_BLOCKED_IN_MODEL",p.spawn,
           "nsenter",privileged=True)
    # Positive simulation is NOT a proof a compromised host cannot inject FDs.


def test_real_subprocess_inheritance_is_not_claimed_from_mock(contract):
    p=contract.new_mock_process("unit")
    for label in ("python-ccxt","kis-sdk","shell","child","grandchild"):
        p=p.spawn(label)
        assert p.admission is contract
        denied("QUARANTINE_MOCK_EGRESS_DENIED",p.attempt,
               "subprocess.curl","AF_INET")
    denied("QUARANTINE_SPAWN_LIMIT",
           p.spawn("a").spawn("b").spawn("c").spawn,"d")


@pytest.mark.parametrize("operation,transport",[
    ("ccxt.create_order","AF_INET"),
    ("ccxt.raw.private_post","AF_INET6"),
    ("kis.session.post","AF_INET"),
    ("kis.oauth","AF_INET6"),
    ("binance.sltp","AF_PACKET"),
    ("subprocess.python_ccxt","LOOPBACK"),
])
def test_parallel_actors_share_same_denial_contract(contract,operation,transport):
    p=contract.new_mock_process()
    def work(n):
        child=p.spawn("worker-"+str(n))
        return caught_code(child.attempt,operation,transport)
    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes=list(executor.map(work,range(96)))
    assert set(outcomes)=={"QUARANTINE_MOCK_EGRESS_DENIED"}


def test_genuine_raw_sdk_outside_our_service_remains_unprotected_counterexample():
    class FakeCcxt:
        def __init__(self):self.orders=0
        def create_order(self,*args):
            self.orders+=1
            return {"id":"MOCK_NOT_NETWORK"}
    fake=FakeCcxt()
    fake.create_order("BTC/USDT","market","buy",1)
    assert fake.orders==1
    # This is why a production Vultr process/egress audit is required.


def test_quarantine_idle_module_has_no_side_effect_imports():
    f=ROOT/"poa_stage/quarantine_idle.py"
    tree=ast.parse(f.read_text())
    assert not any(isinstance(n,(ast.Import,ast.ImportFrom))
                   and not isinstance(n,ast.ImportFrom) or False for n in ())
    imports=[n for n in ast.walk(tree)
             if isinstance(n,(ast.Import,ast.ImportFrom))]
    assert len(imports)==1
    assert isinstance(imports[0],ast.ImportFrom) and imports[0].module=="__future__"
    assert "raise SystemExit(main())" in f.read_text()
    assert "return 77" in f.read_text()


def test_original_main_financial_ledger_db_and_manifest_unchanged_in_pr26():
    changed=subprocess.check_output(
        ["git","diff","--name-only",PARENT_SHA,"HEAD"],
        cwd=ROOT,text=True).splitlines()
    allowed={
        "deployment/poa-vultr-no-egress.service.candidate",
        "poa_stage/quarantine_idle.py",
        "poa_stage/egress_quarantine.py",
        "tests/test_vultr_network_quarantine.py",
        "deployment/PR25_VULTR_EGRESS_QUARANTINE_CONTRACT.md",
        ".github/workflows/pr25_vultr_quarantine_mock.yml",
    }
    assert set(changed)==allowed,(set(changed)^allowed)
    assert not any(p in {"main.py","run.py","requirements.txt",
                         "release_manifest_sha256.json"}
                   or p.startswith("exchange/") or p.startswith("exchange/verified_pnl/")
                   for p in changed)


def test_security_report_discloses_no_host_enforcement_or_live_readiness():
    doc=(ROOT/"deployment/PR25_VULTR_EGRESS_QUARANTINE_CONTRACT.md").read_text()
    for key in ("VULTR_NETWORK_ISOLATION=UNVERIFIED",
                "OS_PROCESS_INHERITANCE=UNVERIFIED",
                "HOST_SOCKET_ACTIVATION=UNVERIFIED",
                "PREOPENED_FD=UNVERIFIED",
                "RAW_CCXT_OUTSIDE_QUARANTINE=UNBLOCKED",
                "PRODUCTION_READY=BLOCKED"):
        assert key in doc


@pytest.mark.parametrize("invalid",[
    None,123,"", "\x00[Unit]", "x"*17000
])
def test_invalid_unit_input_closed(invalid):
    denied("QUARANTINE_UNIT_INPUT_INVALID",validate_candidate,invalid)
