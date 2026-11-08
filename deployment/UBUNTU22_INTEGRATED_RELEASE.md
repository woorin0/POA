# POA Ubuntu 22.04 integrated release candidate — Draft-only handoff

**NO_MERGE_NO_DEPLOY_NO_LIVE_ORDERS.** This is release preparation, NOT production readiness.
Original main pinned: c5566c4cc2a4ce716f1f0d66260bd60e4f95546f. No existing financial engine, order, DB, dependency, PocketBase installer, cloud-init or systemd file is modified.

## Minimal reuse of verified work

1. deployment/bot03_reinstall.py copied byte-identically from PR #33 (source blob 96a54eaf47627a5ed4f135eaf87898e2841190e7). This is inspection and fixed-head source-only staging, not an application overwrite or service activation. Source ZIP/DB/API key files are not copied.
2. deployment/py312_requirements.py copied byte-identically from PR #34 (source blob c03b39160bde40099ea691281effd280f3d07063). For **Ubuntu 22.04 / Python 3.10** DO NOT enable this overlay. Original requirements SHA256 stays e103f650ea4c67a8abcbf7661e8e7e881ba6cef53edf18a8d63a5eaaba1369a4 with pendulum==2.1.2. Overlay is retained only as verified migration tooling for a later separate Python 3.12 release; it is never used by Ubuntu22 User Data.
3. deployment/ubuntu22_release_handoff.py adds strictly read-only SHA256 equality checks of *already quiesced and independently restored* backup copies; never open a running BOT03 DB. It rejects symlinked, group/world accessible, overlapping, journalled or inconsistent snapshots. Checks file-byte preservation of API key .env, notification DB, PocketBase binary and entire pb_data, plus immutable read-only SQLite PRAGMA integrity_check. No secret values or hashes are printed.
4. tests/test_ubuntu22_integrated_release.py uses synthetic-only backup/restores, tampering and failure cases, exercises actual repo fixed-commit staging and independent frozen KST/UTC/DST date calculations.
5. .github/workflows/ubuntu22_integrated_release.yml runs Windows and Ubuntu22 Python3.10 with original 17 dependency pins, main source-protection/Manifest 72, independent finance gates, original Golden/4 venue mocks, entire release verification via the unmodified pr_verification.yml. Never start BOT03/app/exchange service.

6. deployment/cloud-config-ubuntu2204-pr35-stage-only.yaml is a **secret-free new-server cloud-init** pinned to the independently green integrated candidate commit bc9f9af4807656b96cfd4da36efe169242181a4d, with the same original 72-entry Manifest hash. Its service units remain unstartable (ExecStartPre=/usr/bin/false). This static candidate does not track the latest PR HEAD because embedding a commit inside its own commit creates a circular pin. It is intentionally **STAGE-ONLY** and must never be described as fully operational User Data.

## Real-world limitations (explicit blockers)

- The original deployment/install_once.py Ubuntu22 path and cloud-config-ubuntu2204.yaml remain **NEW_SERVER_STAGE_ONLY**, with ExecStartPre=/usr/bin/false on staged units and RESTORE_VERIFIED=NO / OPERATING_READY=NO. This PR does NOT silently remove the interlock, touch Caddy/Firewall, connect exchange keys or start a POA bot.
- New-server cloud-init CANNOT preserve BOT03 files through a Vultr OS Reinstall. Do NOT select Reinstall on existing BOT03. Take an off-host consistent verified backup; verify on independently restored offline copies before any proposed cutover.
- Original Debian install path uses PocketBase 0.16.6 while Ubuntu22 stage path fetches PocketBase 0.22.48 by a fixed ZIP SHA256. Equality of snapshot bytes and SQLite integrity is **not** evidence of schema migration compatibility. Preserve original PocketBase binary until separate schema/migration rehearsal passes. No PocketBase migration or binary switch is performed here.
- SAFE_RETRY_DB_PATH may be outside the assumed default folders, and other order/ledger/UNKNOWN/HELD/notification data may exist. Actual paths, backup consistency, ownership, service topology, open positions, key permissions, webhook exclusive routing and exchange fill reconciliation are **UNVERIFIED**. Any one of them blocks trading.
- CI Mock PASS means only the specific fixture contract passes, not that the actual host was tested.
- PYTHON312_COMPAT=out_of_scope_for_ubuntu22; no speculative version change.

## Offline test-only commands

```bash
python3.10 -m pip install -r requirements.txt
python3.10 -m pip install pytest==9.1.1 PyYAML==6.0.3
python3.10 -m pytest tests/test_ubuntu22_integrated_release.py -q --tb=short
python3.10 release_checks/verify_release.py
python3.10 release_checks/verify_packaging.py
```

To compare an **already quiesced** backup mounted under four *different* private directories, in an isolated offline machine only:

```bash
python3.10 deployment/ubuntu22_release_handoff.py compare-offline-restore \
  --source-app /private/offline-backup/POA \
  --restored-app /private/offline-restore/POA \
  --source-db /private/offline-backup/db \
  --restored-db /private/offline-restore/db
```

The comparison outputs statuses only. Do not send API keys, .env values, private DB, or account identifiers to ChatGPT/GitHub Actions.

## Required independent operating sign-off

1. Read-only inventory of actual BOT03 service topology, Linux OS, interpreter, exact release and all DB/ledger paths. **Not performed by this PR**.
2. Consistent backup using the proper PocketBase/SQLite WAL discipline and all order/notification/UNKNOWN-HELD state; test repeatable restore in an isolated host. CI synthetic copies do not establish consistent live backups.
3. Compare existing vs candidate PocketBase binary and schema on a separate copy, without mutating either source or live DB. Validate cross-version migration/rollback.
4. External read-only exchange account reconciliation and exclusive TradingView ingress/no double live worker.
5. Full Windows and Ubuntu regression, offline Ubuntu22 installation rehearsal, negative CI gates and rollback proof.
6. Only following explicit separate operator authorization: reversible application cutover, no OS reinstall; manual service activation with trading switch independently reviewed.

Until then:
- PRODUCTION_READY=BLOCKED
- DB_BACKUP_AND_RESTORE=UNVERIFIED
- API_KEYS_PRESERVED_ON_HOST=UNVERIFIED
- POCKETBASE_SCHEMA_COMPATIBILITY=UNVERIFIED
- SERVICE_TOPOLOGY=UNVERIFIED
- NO_MERGE_NO_DEPLOY_NO_LIVE_ORDERS

**The user-requested turnkey User Data can only be labeled ready after all above gates; even a complete CI PASS cannot replace the missing actual-host evidence.**
