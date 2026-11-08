# PR25 → PR26 Vultr Ubuntu 22.04 no-egress sandbox — independent OFFLINE contract

**RESULT TYPE:** Draft-only reference contract + synthetic network/process E2E.
**NO REAL VULTR / AWS / EXCHANGE / CCXT / KIS / SYSTEMCTL / ORDER / DB CALL.**

## Immutable source and supported platform

- Exact source PR #25 PASS HEAD: `db343f2cda151fd396f58b5d8d0544bb7e5c5e6f`; Git tree `2eade4630335344248b4fb159c0d411a98fe84c1`.
- PR25 Windows/Ubuntu PASS evidence: `https://github.com/woorin0/POA/actions/runs/42432091126` (213/OS).
- Original main untouched: `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f`.
- Platform contract: **newly provisioned, separate Vultr Ubuntu 22.04 amd64** with systemd 249. No in-place upgrade, reinstallation, live server, real customer DB or key access.
- Exactly SIX **added** audit-only files: `deployment/poa-vultr-no-egress.service.candidate`, `poa_stage/egress_quarantine.py`, `poa_stage/quarantine_idle.py`, `tests/test_vultr_network_quarantine.py`, this report, and `.github/workflows/pr25_vultr_quarantine_mock.yml`. Existing product Python/financial formulas/order adapter/Safe Retry/PocketBase/DB/service unit, release manifest, PR #25, prior Draft PRs and main are not modified.

## Isolation contract and fail-closed policy

`poa-vultr-no-egress.service.candidate` is deliberately NOT a deployable unit:
- `RefuseManualStart=yes`, **no [Install] section**, `ExecStartPre=/usr/bin/false`, `Restart=no`; `ExecStart` points only at `quarantine_idle.py`, which exits nonzero and cannot load trading code.
- `PrivateNetwork=yes` creates a private network namespace; **this alone does not prohibit loopback**, so `RestrictAddressFamilies=none` prohibits new `socket(2)` calls for all address families, while `IPAddressDeny=any` is a defense-in-depth cgroup IP rule (only when supported/enforced by the actual host).
- `RestrictNamespaces=yes`, `CapabilityBoundingSet=` empty, `AmbientCapabilities=` empty, `NoNewPrivileges=yes`, `DynamicUser=yes`, `PrivateIPC=yes`, `PrivateMounts=yes`, `PrivateDevices=yes`, `SystemCallArchitectures=native`: limit root escalation, network namespace manipulation and cross-service primitives.
- `ProtectHome=yes`, `ProtectSystem=strict`, `ProtectProc=invisible`, `ProcSubset=pid`, `InaccessiblePaths` for Docker/Podman/systemd manager sockets, `StandardInput=null`, `FileDescriptorStoreMax=0`, `NotifyAccess=none`, no `Sockets`, `PassEnvironment`, `EnvironmentFile`, `LoadCredential`, `BindPaths` or `ReadWritePaths`. No API key, PocketBase, database, actual POA app or listening HTTP endpoint is loaded.
- Exact-only fail-closed parser: refuses duplicate/redefined/missing directives, hostile extra options, socket unit sections, `ExecStart` substitution, `ExecStartPre` ignored-failure `-` prefixes, capability additions, IP allowlists, AF_INET/AF_INET6, root ownership, extra environment/credentials or writable paths.
- Child and grandchild process execution inherits a service's cgroup, network namespace, resource and seccomp restrictions when actually enforced by systemd; the tests **model** inheritance but do NOT exercise an actual sandbox on a privileged Vultr host.

## Attack matrix (pure Mock: neither sockets nor subprocess processes are created)

| Attack type | Mock expected result | Actual Vultr |
|---|---|---|
| CCXT spot/futures create_order, raw private POST, Binance SL/TP, Upbit/Bybit/Bitget/OKX | Denied for AF_INET, AF_INET6, AF_PACKET, loopback, DNS, Unix control proxy | UNVERIFIED |
| KIS stock order POST, hashkey/OAuth direct POST | Denied, no real KIS session | UNVERIFIED |
| Python/curl/subprocess, child and grandchild raw SDK | Denied by inherited mock policy | OS process tree unverified |
| 96 concurrent child attempts across 6 types | All denied in Mock without races | Real scheduler/cgroups unverified |
| Loose `PrivateNetwork`, IP allow, AF_INET, `ExecStartPre=-/usr/bin/false`, `ExecStart=main:app`, added credentials, Docker/systemd paths, namespace privileges | Malicious unit rejected before launch | Actual installed unit + drop-in scan unverified |
| socket activation, SCM_RIGHTS, inherited preopened FD | Model refuses to assert proof | Host FD and unix socket mediation UNVERIFIED |
| privileged host process runs unrelated POA/CCXT or modifies unit/binary | Separate negative control shows mock external raw call succeeds | **OUT OF SCOPE / UNBLOCKED** |

## Security limitations that no in-process/CI Mock may conceal

`VULTR_NETWORK_ISOLATION=UNVERIFIED` — GitHub runners are not a Vultr host. Actual systemd version, cgroup v2 / BPF enforcement, namespace creation, service limits, active unit/drop-ins, firewall/nftables, process owner, package SHA and network route inventory need a separate **authorized isolated-new-server** validation. `systemd-analyze verify` is syntax/semantic linter only; never claim it proves runtime egress blocked.

`OS_PROCESS_INHERITANCE=UNVERIFIED` — a modeled child inherits policy, but no privileged OS-level fork/exec or network test is run.

`HOST_SOCKET_ACTIVATION=UNVERIFIED` — `RestrictAddressFamilies=none` restricts `socket(2)`, not socket activation, `socketpair()`, a preopened socket passed by systemd or AF_UNIX file descriptor passing. A socket unit targeting this service, systemd FD store, running host relay, or host-side network proxy may defeat a simple app policy. Avoid any trusted socket FD, disable all associated `.socket` units, audit `LISTEN_FDS`, inherited FDs, control sockets and actual cgroup boundaries before approval.

`PREOPENED_FD=UNVERIFIED` — the candidate forbids known service settings but does not attest the host's initial file descriptor table or externally managed socket units. The Mock fails closed when handed an injected FD, rather than claiming it can revoke a real socket.

`RAW_CCXT_OUTSIDE_QUARANTINE=UNBLOCKED` — an unrelated process or an unmodified POA checkout can use real SDKs if the *host* allows egress; this service does not install global firewall rules, block all host traffic or modify existing POA services.

**Host privilege compromise, physical console access, kernel vulnerabilities, cgroup/BPF gaps, podman/docker socket mounts and inherited FDs remain external threats.** No claim of universal protection. All simulated "blocked" outcomes mean only that the modeled contract rejected the request.

## CI, evidence, rollback / operating authorization

- Windows: parse and attack-test the exact unit + pure Python Mock, assert exactly six added audit files, pin PR25 HEAD/tree, and run original PR25 tests in an isolated detached worktree.
- Ubuntu 22.04: same plus `systemd-analyze verify` of a temporary **copy** with `.service` extension. No `systemctl start/enable`, `systemd-run`, firewall, namespace creation, network packet or provider calls.
- Original detached PR25: 161 security attack tests; detached PR23: 52 stage/read-key tests and original 72-file release manifest/packaging checks. Always distinguish parent historical tests from the new candidate.
- All tests use synthetic mocks and no operational `.env`, DB, trading credentials, HTTP, real exchange, orders or server.
- Future host verification requires an independently authorized disposable Vultr Ubuntu server **without** any exchange keys or production data; only inspect and test an inert service, check namespace/AF/IP filtering, inherited FDs, process/cgroup escape, and attest actual installed SHA. Do not remove `ExecStartPre=/usr/bin/false` in this PR. Existing production server and DB must never be reused for this experiment.
- Exit / rollback: remove the **unused** candidate file on the disposable test host if independently authorized; no existing service, real DB or keys are changed. This PR contains no deployment scripts and no operational instructions that activate a bot.

Systemd 249 reference: https://www.freedesktop.org/software/systemd/man/249/systemd.exec.html

**PRODUCTION_READY=BLOCKED. MERGE=NO. DEPLOY=NO. REAL_ORDERS=0.**
