# POA Vultr Phase 0–1 — immutable product and stage-only integration gate

**STATUS: STAGE_ONLY_IMPLEMENTED / LIVE_ORDERS_BLOCKED_IN_STAGE_GATEWAY / OPERATING_READY=NO.**
Do **NOT** merge or deploy this Draft PR. The purpose of this phase is to prove a
safe process that has no path into the POA order/hedge engine, and to preserve
the existing product and DB while documenting the next product integration gate.

## Immutable refs and scope

- Source main: `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f`.
- Preexisting PR #22 head: `55a7bc0d62ad11fb810681a42d5b6b7b88663ce9`
  (inherited PR #15–22 offline protocol evidence only; not merged).
- The protected application files, all financial algorithms, original
  ledger/migrations, Safe Retry, HTTP v1 behavior, PocketBase files and existing
  72-file release manifest remain **byte-for-byte identical** to main.
- This branch adds only `poa_stage/`, a staging-only service candidate,
  `tests/test_vultr_stage.py`, this document and an isolated CI workflow.
  GitHub's original `pr_verification.yml` runs the full main Windows+Ubuntu
  regression for the Draft PR. The additional staging workflow checks exact
  protected Git bytes and runs hostile ASGI/read-key Mock tests.

## Central order-denial architecture — staging, not a product-order patch

`poa_stage.gateway:app` is a completely separate ASGI executable, **not**
an import/wrapper around the production `main.py` or a reverse proxy to it.
It uses only the Python standard library at import, has **no** order-client
capabilities, DB, PocketBase or Discord modules, and denies **every** HTTP
request except GET/HEAD `/__stage/health`. Its health response reports
`orders_enabled=false`, `read_enabled=false`, `db_attached=false`.
WebSockets are closed immediately. In CI, even order/hedge/price endpoints,
alternate methods, malformed routes and synthetic payloads cannot invoke
the trading engine, enqueue Safe Retry, or load any trading keys.

**Important scope limit:** This does not intercept arbitrary direct Python
calls into production `main.py`, `exchange.pexchange.get_bot`, CCXT or
`exchange.safe_retry.safe_submit` if someone **separately starts the original
production app**. It is not a globally installed safety hook. No existing
production service is changed here; using old POA main in an unsafe network
would still be hazardous. Before any stage integration with actual main.py,
an independently audited execution-level guard at *every* exchange/order
writer and worker is required, together with a fresh signed product release
manifest and full regression. Never weaken the existing source-pin gates to
make that migration pass.

## Dedicated read keys — design boundary only, no real exchange reads

`poa_stage.read_credentials` uses the original PR15
`POA_FILL_READ_ONLY_<VENUE>_{KEY,SECRET}` names with OKX passphrase, and
accepts only an explicit separate per-venue vault mapping. **No** fallback to
`BINANCE_KEY`, `BYBIT_KEY`, `OKX_KEY`, `UPBIT_KEY`, any `.env` or old
product Settings. The original immutable account ref equals the actual
product's `ledger.identity(venue, ORIGINAL_TRADING_API_KEY)`; a different
read key generates a different ref even for the same venue account.
The planner rejects secret reuse and never rewrites the original ledger ref.

The actual main `ReadHost` remains unchanged and fails closed on a
read-key/account-ref mismatch in the offline test. The plan **always**
returns `authorized=False`; `require_proven_live_read` **always blocks**.
PR16–22 provided synthetic signed evidence, not venue-side proof of equal
account/subaccount and actual read-only API permissions. **No real read key
is loaded, used or issued into CI or staging.** This phase supports
UPBIT/BINANCE/BYBIT/OKX from PR15; BITGET and securities read-only identity
are outside verified read scope. Never claim all five exchanges are proven.

## Vultr isolation and installation contract

- For an *additional* test server only: Ubuntu **22.04 amd64**, Python 3.10,
  an exact preapproved SHA and new source manifest. Existing
  `deployment/build_cloud_config.py --ubuntu COMMIT MANIFEST_SHA256`
  is **NEW_SERVER_STAGE_ONLY** and cannot restore a pre-existing disk.
- Existing operating server **must not** run this stage User Data, reboot,
  reinstall, or execute any script from this branch without a new approval.
- The `deployment/poa-stage-only.service.candidate` file is intentionally
  **inert** (`ExecStartPre=/usr/bin/false`, `Restart=no`, `PrivateNetwork=yes`,
  `ProtectHome=yes`, immutable OS and no privilege escalation). It is
  NOT a ready-to-enable service and no installer in this branch enables it.
- Do not load actual `.env`, API keys, Discord webhook or operating DB.
  The stage health endpoint and synthetic Mock tests require **none** of them.
  CI checks no import of `main`, `exchange`, `ccxt` or SQLite for stage ASGI.
- Storage, production Caddy and old POA service units remain untouched.
  Even when staged on a new host, no trading webhook route is to be published.

## Operator-specific DB/credential preflight (not executed here)

Per server identify the real `systemd` WorkingDirectory, all configured
SQLite/WAL/SHM, `/root/db/pb_data`, notification main/.intake/.ops,
Safe Retry, signal dedup, strategy positions, secrets, service units and
PocketBase binary/data version. On *approved maintenance*, with webhook
ingress closed and every writer quiesced, perform supported SQLite backups,
preserve sealed forensic originals and independently restore into a separate
directory/VM. Match schema and every record digest, pending/UNKNOWN/HELD,
ACK lineage, outbox and receipts, rather than matching row counts only.
Compare PocketBase **0.16.6 versus 0.22.48** compatibility with a copy.
Never copy a live SQLite main file without its WAL or replace a production DB.

No backups, credentials, actual Vultr server access, service installation or
restore happened in this PR. `RESTORE_VERIFIED=NO`, `OPERATING_READY=NO`.

## CI and release gates

1. Current main's full `pr_verification.yml` Windows/Ubuntu workflow
   runs against the proposed Draft PR with all legacy/PnL, order,
   admission, source manifest, packaging, Safe Retry, notification,
   Discord, security and offline finance tests.
2. New `vultr_stage_integration.yml` on Windows/Ubuntu runs
   `tests/test_vultr_stage.py` under `release_checks.isolation`, asserts
   no product/manifest/deployment protected changes, reruns the release and
   packaging gates and stores JUnit artifact evidence.
3. PR #22 original 391-tests-per-OS security Stub baseline remains
   **historical CI evidence**. Its test files are not imported into this
   branch's production package and its synthetic claims do not activate
   real credential or WORM access.
4. For any later actual products changes: each execution API sink
   (spot/futures/hedge, stock, CCXT raw methods, retry workers, internal
   direct calls) must have a **real** fail-closed order gate, verified on
   mock clients with zero network/order events and a standalone security
   review. Original finance math must remain unchanged.
5. After full isolated Linux restore, simulated crash recovery, replay,
   203 concurrent signals, 0 live orders and verified read-only
   venue permissions, an independently authorized operator can consider
   a **separate** product-integration PR. This document does not grant that
   permission or provide a live activation recipe.

## AWS dependency and optional WORM

**AWS_REQUIRED=NO.** PR21/22 S3 Object Lock Compliance Stub work is NOT
copied or linked into the stage runtime. Optional Vultr Object Storage
capabilities and true Object Lock retention need provider-specific official
documentation and live isolated tests; never set `WORM_VERIFIED=true` from
a Stub. The first priority is a safe and reproducible Vultr deployment path,
not an unverified external WORM dependency.

**Production READY remains BLOCKED.** Stage gateway protects only traffic
that actually enters the stage process; it does not replace a full central
execution guard in the existing POA trading process.
