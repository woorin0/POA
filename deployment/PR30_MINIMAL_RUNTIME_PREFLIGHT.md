# POA minimal Stage + Read-only executable research bundle / Ubuntu 24.04 preflight

**DRAFT ONLY. DO NOT MERGE, DEPLOY, INSTALL, ENABLE SYSTEMD, MIGRATE DB, INJECT KEYS OR PLACE REAL ORDERS.**

## Fixed immutable parent and choice of security evidence

- Branch originated at PR #30 exact 2/2 PASS HEAD: \`554ccab03c05e2e4d4a50f75630500704b4654e3\`, Actions #42441014961, Windows 1,425 passed / 3 skipped / 0 failed; Ubuntu success.
- PR #31 Ubuntu passed; **Windows and workflow completion had NOT been independently confirmed at branch creation**, therefore **none of PR #31's new code or kernel probes were adopted**. \`PR31_PENDING_NOT_ADOPTED\`. Reconsider using only verifiably passing isolated test utilities after final Windows+Ubuntu success, not a blanket PR merge.
- Original \`main\` reference: \`c5566c4cc2a4ce716f1f0d66260bd60e4f95546f\`. Exact incremental scope: 11 paths (five runtime/preflight/service files, report, test, workflow, and three original-test strict allowlist extensions). Original \`main.py\`, \`run.py\`, \`exchange/**\`, Safe Retry, KIS, CCXT, PnL, fees, financial ledger, historical account identity, original PocketBase/SQLite, DB migrations, API keys, dependency constraints, 72-file release Manifest and existing install/recovery programs remain **byte-identical**.
- \`FINANCE_AND_DB_SOURCE=UNCHANGED\` in CI is a protected Git byte diff check and the original release/manifest tests. No real operating database was opened or validated.

## Architecture: runnable local Unix transport, sealed order-disabled subprocess

1. **Local transport**: \`poa_runtime/local_unix_gateway.py\` is an executable, dependency-free AF_UNIX server **only**. No AF_INET/AF_INET6, DNS, HTTP client, actual SDK import or connection to real POA. Socket must be in an already existing **owner-private directory (0700)**, socket is mode **0600**, and existing file/socket is refused without unlinking. It parses a bounded HTTP request line (max 512 bytes), without reading a body or interpreting headers, and never routes to an order engine.
2. **Sealed worker**: the transport starts \`poa_runtime/local_worker.py\` once per request with \`python -I -B -S\`, sanitized environment, no shell, \`close_fds=True\`, request timeout, and a size-bounded JSON/stdin response protocol. Worker imports the exact PR #30 **\`poa_runtime.sealed_stage:app\` or \`poa_runtime.sealed_readonly:app\`** under Python import/audit hooks, *after* data ingestion and *before* ASGI dispatch. The worker has no socket handle (only a non-socket stdin/stdout pipe) and returns only fixed health/denied JSON. Untrusted bodies are never read or passed to trading code.
3. **Available local endpoints**: Stage \`GET/HEAD /__stage/health\` -> 200; Read-only \`GET/HEAD /__read/health\` -> 200; everything else including \`/order\`, \`/hedge\`, websocket and unknown routes -> 403 or malformed request -> 400. Health JSON explicitly states orders disabled, exchange reads disabled, DB unattached.
4. **This package is executable in an isolated test session**, but is not yet an approved installable deployment. Two proposed systemd candidates (\`deployment/poa-stage-uds.service.candidate\`, \`deployment/poa-readonly-uds.service.candidate\`) remain deliberately UNSTARTABLE with \`RefuseManualStart=yes\`, \`ExecStartPre=/usr/bin/false\`, no \`[Install]\`, no secrets, no live DB, \`PrivateNetwork=yes\`, \`RestrictAddressFamilies=AF_UNIX\`, \`IPAddressDeny=any\`, \`NoNewPrivileges\`, and private runtime directories. Only a separate supervised host test and explicit approval may remove the inert stop; this PR does NOT do so. The proposed systemd policy does not attest to real host isolation without execution under PID 1.

For an **offline disposable test environment**, not BOT03 or the production Vultr server, a reviewer can create a fresh owner-private temporary directory and manually run the local Unix gateway there with the chosen \`--role stage\` or \`--role read-only\`; the CI provides this exact process E2E. It is intentionally not exposed over public HTTP or Tailscale and has no container image/installer/cloud-init/user-data automation.

## Read-only Ubuntu 24.04 preinstall Preflight (NO writes)

\`python3 deployment/poa_ubuntu24_preflight.py --target /\` prints one stable JSON report, with exit 0 for **static preinstall PASS** and 75 for BLOCKED. It reads only OS release, CPU count, \`/proc/meminfo\`, \`statvfs\`, local \`systemd-analyze --version\`, cgroup/seccomp metadata, its own open FDs, and *only process command names* for independent legacy POA detection. Checks non-root invocation, absence of any inherited socket FD and of a Docker control socket or trading-sensitive environment variables; **never prints their contents, keys, process argv or PIDs**. It never runs Docker, systemctl, apt, cloud-init, network requests, git pushes, file writes, database queries or secrets operations.

Minimal static capacity assumptions are advisory **safe gates**, not externally benchmarked production sizing: Ubuntu **24.04 amd64**, \`systemd >=255\`, at least **2 vCPU**, **2 GiB RAM** and **10 GiB free** on the chosen filesystem, with cgroup v2 and seccomp actions exposed. These gates may be tightened for full POA or DB restores. Their success never means the **real systemd namespace/cgroup network deny** or trading absence has been proven.

## Outstanding approvals and rollback

\`PRODUCTION_READY=BLOCKED\`
\`ORIGINAL_MAIN_EXTERNAL_PROCESS=UNCONTROLLED\`
\`ACTUAL_VULTR_HOST=UNVERIFIED\`
\`DB_RESTORE=NOT_PERFORMED\`
\`READ_ONLY_KEY_ATTESTATION=UNVERIFIED\`
\`LIVE_ORDERS=0\`

Before any deployment to a **brand-new isolated Vultr 24.04** host:
- Preserve operating BOT03 **unchanged**. Inventory and consistent point-in-time copy of PocketBase data, SQLite database + WAL/SHM, Outbox, Safe Retry, UNKNOWN/HELD and ledger; verify ownership/permissions and the original trading keys in their original vault, **never copy trading keys to Stage/Read-only**.
- Validate backup on a disconnected **copy**: PocketBase binary version compatibility, table counts, ledger/PnL/account identities, original vs restored record checksums and transactional rollback. If any mismatch, hard BLOCK.
- Verify provider-side **distinct read-only API key** permission, exact original account/subaccount binding, no trade/withdraw permissions and no fallback to trading keys. PR #30 still rejects all live exchange reads; Bitget/KIS identity needs a separate gate.
- Independently verify on the actual host: systemd permission restrictions, AF_UNIX/FD inheritance and proxy safety, network namespace/cgroup egress denial including nested subprocess and external legacy process inventory, firewall and secret isolation. Ubuntu GitHub CI alone does NOT prove host isolation.
- Test startup/shutdown/restart/rollback of an independently **approved** service package with no trading credentials, no order calls, no DB changes and identical original financial regressions. Keep existing BOT03 running untouched. Only after written authorization can a separate deployment PR remove \`ExecStartPre=/usr/bin/false\` and systemd manual-start restrictions; never auto-enable from this PR.
- The local gateway is a health-only research transport, **not a production authentication gateway or signed webhook listener**. Unix socket holders can request health; it has no live orders or reads, but an untrusted gateway host or unconfined sibling/native process may bypass Python's sealing and attempt network egress.

## Verification

New independent Windows + Ubuntu **24.04** Actions matrix pins the parent HEAD and the candidate HEAD, checks exactly 11 modified paths and original source hash equality, compile/static unit security contracts, offline executable sealed-child E2E (Windows/Linux), Linux local AF_UNIX process E2E, pure preflight positive/negative cases, inherited PR #23/#28/#29/#30 tests and original release, 72-file Manifest, 4-venue native fills, Golden, Safe Retry, and full Windows pytest with no suppressed failures.

**No live key, remote host, production DB, order or deployment has been touched.**
