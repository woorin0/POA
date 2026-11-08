# POA BOT03 in-place reinstall preparation — preserve all operating data

**DRAFT ONLY. NO_MERGE_NO_DEPLOY_NO_LIVE_ORDERS.**

## Independent source findings (2026-11-08)

- Operational `main` frozen at `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f`. This PR is based on **main** and does not cherry-pick experimental PR #23–#32 runtime roles or kill-switches, because their trading mode is not a replacement for the original running bot.
- `deployment/install_once.py` is **fresh-install only**. It refuses existing `/root/POA` and `/root/db`, its Ubuntu route is 22.04-only, and the Debian new-install route creates initial DB/admin credentials. **Never run it against BOT03 or an OS Reinstall** to upgrade an existing bot.
- `deployment/prepare_ubuntu.py` masks services and installs packages for a **fresh** server, not an update. `deployment/build_cloud_config.py` includes first-provisioning scripts. **Never paste this new-server cloud-init into BOT03 or reinstall its OS.**
- Existing `run.py` starts the original `main:app` and can place orders if authorized. Unreviewed startup is **not** part of this Draft.
- New PR #23–#32 Stage/Read-only research does **not** deliver fully integrated production Trading, DB restoration, independent live read-key attestation or a host-approved OS boundary. Do not combine them as an automatically usable update.

## Scope of minimal implementation

1. `python3 -I -B -S deployment/bot03_reinstall.py inspect --app /root/POA --db /root/db` is **metadata-only**. It never reads `.env` contents, API keys, PocketBase records, WAL/SHM or notification SQLite content; never opens a network socket or calls systemd. JSON exposes 6 existence gates and the locally detected Git commit (if readable). It does not print any actual secrets, DB paths, service arguments, environment variables or PIDs.
2. `python3 -I -B -S deployment/bot03_reinstall.py stage --source /path/to/verified/repository --destination /path/to/NEW-private-stage --expected-head <exact-candidate-sha>` is an **offline candidate-only preparation**, NOT production installation. This operation writes only to a **new, previously absent, owner-private staging directory**; it refuses destinations overlapping source, BOT03 app, BOT03 DB or /etc/poa. It requires an exact checked-out SHA, clean tracked tree, 72-file SHA256+byte-size manifest, and regular Git-tracked files. It refuses tracked `.env`, SQLite, WAL/SHM, certificates, pb_data and symlinks; historical Git-tracked `__pycache__/*.pyc` and other compiled `.pyc/.pyo` files are deliberately excluded (never substituted for source). Untracked files and operating DB/keys are never copied. It leaves an explicit `STAGED_ONLY_NOT_LIVE.json` marker.
3. There is **no** command to stop/start services, install or upgrade Python/PocketBase, change Caddy/UFW/systemd, rewrite `.env`, create PocketBase users, switch symlinks, activate a webhook, delete files or place orders. A private stage is not itself deployable. No automatic rollback of the running BOT03 is attempted or claimed.

## Required operating BOT03 evidence before designing actual controlled cutover

- **SERVICE_TOPOLOGY=UNVERIFIED**: independently identify installed Ubuntu version, actual service manager (systemd/Docker/other), service names, Python interpreter, current executable SHA and readiness/health endpoint. Unverified actual server: do not guess from repository.
- **DB_BACKUP_AND_RESTORE=UNVERIFIED**: inventory `/root/db/pb_data`, other PocketBase/SQLite files, `notifications.sqlite3` and actual configured paths, WAL/SHM, Safe Retry, Outbox, open positions, UNKNOWN/HELD, deduplication states. Take a *consistent* backup using the server/database's approved procedure; copy integrity must be verified in an isolated restore before ANY replacement. File existence alone is NOT a valid backup.
- **API_KEYS_PRESERVED_ON_HOST=UNVERIFIED**: `/root/POA/.env` and any other secret stores remain on original server under original permissions; do not copy them into CI, GitHub, user-data or ChatGPT. Do not reissue keys merely to stage a release.
- **EXISTING_WEBHOOK_BEHAVIOR=UNVERIFIED**: upstream Caddy routing/TradingView alerts must not be diverted, retried twice, or simultaneously routed to old and new instances. Ensure no duplicate live order execution, and independent service shutdown/rollback plan.
- **POCKETBASE_BINARY_COMPAT=UNVERIFIED**: original installer spans PocketBase 0.16.6 and Ubuntu staging 0.22.48. Do NOT upgrade, downgrade or initialize PocketBase against an existing `pb_data` without a verified compatibility and reversible copy test.
- **ACTUAL_EXCHANGE_RECONCILIATION=UNVERIFIED**: original live orders/fills, Safe Retry, ledger/notification consistency and external exchange responses have not been tested against BOT03.
- No operator has provided BOT03 OS/version/database snapshot/service identity to this Draft. No code-only CI can prove host preservation.

`PRODUCTION_READY=BLOCKED` until each listed host/restore/security gate passes and the operator explicitly approves a *separate* cutover procedure. A staged candidate is NOT a functioning live bot.

## Smallest next authorized path (do not perform in this PR)

A. Obtain **read-only BOT03 inventory JSON** and nonsecret operating topology evidence. For example, if the source is already copied to an authorized local workspace, run **inspect only**; do not run `install_once.py` or `prepare_ubuntu.py`.

B. Validate a consistent, offline-restorable DB+secrets backup *without printing or uploading the secrets*, and capture file ownership/permission/row-count/hash evidence. The backup is a manual, separately authorized operation, **not performed by this PR**.

C. Build and verify a **separate staged version** using the approved candidate HEAD; compare release, finance/PnL/ledger, notifications, Safe Retry, source pins and Golden tests against original. Keep `/root/POA` and `/root/db` untouched.

D. On a later, explicitly approved maintenance window, use a host-specific reversible application cutover (no OS reinstall), with strict exclusive webhook intake, closed duplicate-order window, checkpointed Safe Retry and one authoritative DB. Validate health and external exchange order reconciliation before enabling any real order path. If any gate fails, **STOP**, preserve the old working version and investigate without automatic retries.

## Verified by this Draft's CI

- Source diff must be exactly four additional files relative to original `main`. No financial, trading, SQLite/PocketBase, dependency, manifest, original installer or notification code may be changed.
- Windows and Ubuntu offline pytest: isolated synthetic filesystem and fake Git repo, new staging directory only, 72-manifest validation, protected-destination refusal, untracked secret exclusion, tracked secret hard block, symlink/untrusted path and dirty HEAD hard blocks.
- Existing release, packaging, 4-venue offline fill confirmation, Golden and Safe Retry original regressions continue through the repository's `pr_verification.yml` on PRs targeting `main`.

**This is delivery-preparation software, not BOT03 deployment approval.**
