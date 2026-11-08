# POA Integration PR B — sealed Stage/Read-only role-process boundary

**Draft stacked onto exact PR #28 PASS HEAD:** `a0faaf2b266722d535c666a1068a8f4c987b37e2`.
Original product main stays `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f`. **NO MERGE, DEPLOYMENT OR LIVE ORDER.**

## Scope, not a trading engine rewrite

- Adds `poa_runtime/sealed_boundary.py` and two sealed ASGI entrypoints.
  `poa_runtime.sealed_stage:app` permits only the original inert Stage
  health endpoint; `poa_runtime.sealed_readonly:app` permits only inert
  Read-only health. Both install the boundary **before** importing their app.
- Stage and Read-only processes reject imports of `main`, `run`,
  the entire `exchange` package (CCXT adapters, KIS, Safe Retry, stock,
  retry and reconciliation), raw `ccxt`/`ccxtpro` and direct DB modules.
  Rejected preloaded modules fail closed. Audit events deny CPython socket
  creation/connect/bind, subprocess, shell/fork/exec and ctypes loader events.
  Direct execution of original `main.py`, `run.py`, or
  `exchange/**` code objects is refused by path. No env/CI/secret flag
  grants Trading permission.
- Child-process hostile Mock E2E exercises the real sealed Python code:
  direct and nested module imports, synthetic SDK/network attempts, fake
  subprocess/Safe Retry/stock routes, role switching, preloaded modules,
  203 concurrent synthetic attempts per role, and ASGI 403 with **zero**
  request-body reads. No real original application code is run.
- Two separate new systemd unit candidates are intentionally unstartable:
  `RefuseManualStart=yes`, `ExecStartPre=/usr/bin/false`, no `[Install]`,
  no keys/EnvFile, private network, IP deny, AF_UNIX only, no capabilities.
  Existing PR A candidate units and all production service files remain
  byte-identical. These candidates are NOT deployment packages.
- Product original `main.py`, `run.py`, `exchange/**`, financial
  PnL/fees/ledger, Safe Retry, DB, keys, `requirements.txt`, existing
  installers, and `release_manifest_sha256.json` are unmodified.
  Parent exact-whitelist test is **strengthened to enumerate B's new files**,
  not disabled. Both `git diff` against PR A and the original 72-file
  verifier must pass on the same candidate.

## Unambiguous threat and trust boundaries

- `LEGACY_MAIN_SAME_PROCESS_IMPORT=DENIED`: only inside a sealed role
  interpreter; direct import, source execution, raw SDK and retry code paths
  are blocked before execution. Does not rewrite or patch original POA.
- `LEGACY_MAIN_OTHER_PROCESS=UNCONTROLLED`: separately launched `main.py`
  or a different original POA checkout can still trade.
- `RAW_CCXT_OUTSIDE_SEALED_ROLE=UNCONTROLLED`: independently started raw
  CCXT/KIS clients are outside Python role-process constraints.
- `NATIVE_PROCESS_COMPROMISE=UNVERIFIED`: Python import/audit hooks are
  defense in depth, **not a tamper-proof kernel sandbox**; a privileged
  attacker, native interpreter replacement, inherited socket FD or bypass
  of the new entrypoint can escape the role boundary.
- `VULTR_HOST_OS_ENFORCEMENT=UNVERIFIED`: no actual Vultr systemd,
  namespace, cgroup/IP filter, service activation, port routing or host
  firewall test was performed. PR #27 Linux seccomp *CI rehearsal*
  does not establish production containment.
- `READ_ONLY_EXCHANGE_ACCESS=BLOCKED`: no trusted exchange-side account,
  permission or key-owner proof; Bitget/KIS identity remains unverified.
- `DB_RESTORE=NOT_PERFORMED`: no production or copy DB restore, API key
  injection, operations server access, PocketBase migration or writes.
- `ACTUAL_ORDERS=0`: all attempted paths are offline Mock and source probes.
- `PRODUCTION_READY=BLOCKED`: **this PR is not an operational approval.**

## Verification gate

1. New B workflow pins the exact PR A HEAD and checks B's *exactly nine*
   changed paths, full finance/DB/manifest/installer byte preservation and
   Python compilation of all additions.
2. Run inherited PR #28 role/Stage E2E and new disposable-interpreter
   adversarial tests with `release_checks.isolation` in Windows and Ubuntu.
3. Re-execute original release/packaging, 65 HTTP and model contracts,
   4-venue confirmation on Ubuntu, offline Golden stages, Safe Retry and
   original Windows full pytest. Do not weaken failures or silently skip.
4. GitHub Actions 2/2 PASS at the final candidate HEAD is necessary, **not
   sufficient** for operational release.

Next approval gates: independently deploy **only** to new isolated Vultr
with original BOT03 kept untouched; restore verified DB copy; attest
provider-side read-only keys; independently test kernel isolation, inherited
FDs, socket activation, root escalation, legacy main process inventory,
rollback; obtain explicit authorization before any operational changes.
