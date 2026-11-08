# POA Integration PR A — Vultr minimal role separation, stage-only and independent read-key preflight

**GATE: PRODUCTION_READY=BLOCKED. Draft PR for verification only. Do not merge or deploy.**

## Exact sources and immutable application

- Original production main: `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f`.
- Reused **byte-identical** PR #23 inputs: `e01d3d827dea3b147b63fb6ec4cfcecc3cc95ebc`:
  `poa_stage/{__init__,gateway,read_credentials}.py`, `tests/test_vultr_stage.py`, and `deployment/poa-stage-only.service.candidate`.
- PR #25 original hard-deny head: `db343f2cda151fd396f58b5d8d0544bb7e5c5e6f`, CI 213 PASS per OS in original 2026-11-08 work; PR #27 kernel seccomp test head: `b77a8bed945c30d25f3dcd9f658bd75124be1a00`, CI 249 PASS per OS.
- **Only new files** added on top of main; `main.py`, `run.py`, every `exchange/**` file, original financial formulas, position calculations, PnL/ledger, all PocketBase/SQLite/notification DB code, `requirements.txt`, `release_manifest_sha256.json` and deployment installers are **byte identical** to main. The original **72-file** manifest verifier and packaging verifier are not weakened.
- No production systemd activation, no API credentials, actual Vultr server, cloud, real exchange calls or real orders are part of this PR.

## Runtime model: explicit distinct entrypoints, all operations disabled

| Role | ASGI or inspection entrypoint | Safe functionality | Order capability |
|---|---|---|---|
| Stage | `poa_stage.gateway:app` | GET/HEAD `/__stage/health`; all other HTTP 403; websocket 4403 | None |
| Read-only | `poa_runtime.readonly_gateway:app` | GET/HEAD `/__read/health` returns `exchange_reads_enabled=false`; all other HTTP 403 and no request-body parsing | None, live read disabled |
| Trading | `poa_runtime.trade_gate`, `poa_runtime.roles.asgi_for(Role.TRADING)` | Unconditional `RoleBlocked(POA_RUNTIME_TRADING_DISABLED)` at entry | None |

- Inert dry-run CLI `python -m poa_runtime --role stage` or `--role read-only` prints synthetic readiness **without starting a server**. `--role trading` exits nonzero. This is not an installer or service start instruction.
- All three candidate systemd units are non-operational, have `ExecStartPre=/usr/bin/false`, never read production `.env`, and are **not approved for installation or enabling**. The newly written Read-only/Trading units contain no `[Install]` target. PR #27 seccomp policy is NOT blindly added to a working HTTP service: it disallows socket creation and makes HTTP listeners impossible.
- `poa_runtime.roles.deny_order_execution` is a simple, unconditional central deny entrypoint for this **new runtime only**. It has no environment or CI opt-in, and is exercised by 203 parallel synthetic attempts.

**Critical limitation:** `LEGACY_MAIN_INTERNAL_SINK_GUARD=NOT_INTEGRATED`. The old `main.py`, `run.py`, `exchange.get_bot`, CCXT, Safe Retry, Binance SL/TP and KIS original execution routes were deliberately **not modified**. An operator starting the old product independently can still send an order. The new runtime gate is **not** an OS firewall and is **not** attached to those original sinks. Never claim actual POA global order blocking. The next product acceptance gate is a separately audited native sink guard and Vultr network/cgroup policy, along with actual order-success Golden regression in a no-network Mock. Do not merge/operate this PR to disable an existing bot.

## Distinct read credentials and identity compatibility

- Exact PR #23 dedicated credential names: `POA_FILL_READ_ONLY_{UPBIT,BINANCE,BYBIT,OKX}_{KEY,SECRET}` and `POA_FILL_READ_ONLY_OKX_PASSPHRASE`. The only interface is an explicit per-venue vault mapping; no `.env`, DB, OS env or product trade-key fallback. Bitget and KIS are not yet approved for separate read-key use.
- Retain original `exchange.verified_pnl.ledger.identity(venue, original_trade_key)` as the immutable historical ref; a different read-only API key creates a distinct ref. `OfflineReadHostSettings` validates the real existing product's `ReadHost` account-mismatch fail-closed behavior **only in an isolated Mock** and does not issue a network call.
- `readonly_preflight` emits a plan with `authorized=False`. `authorize_live_read` and PR #23 `require_proven_live_read` **always raise**. A key or synthetic signature is NOT a trusted provider attestation of exact account/subaccount and absence of order privileges. No real venue keys or read API calls are used.
- `LIVE_READ_AUTHORIZATION=BLOCKED` until separately verified provider-side permission/account binding and operational change approval, including expected 401/403/429/timeouts.

## Required product release integration after this PR

1. Audit startup import-time DB access and all direct order sinks; introduce a native, sealed no-orders execution mode that lets the original HTTP parser and accounting formulas run against fake clients without reaching CCXT/KIS/worker side effects. PR25's always-raise in *all* paths is a research kill switch and cannot run the normal bot as-is. Production runtime enablement needs an independent external approval, source-manifest renewal and rollback plan.
2. Integrate a *true* separate-credential read host by preserving the trading-key original account ref, authenticating account/subaccount ownership and read-only permissions, and denying key fallback. Without this, read-only remains synthetic.
3. Establish actual new Vultr Ubuntu 22.04 host OS/process/network isolation, received/closed socket FDs and subprocess egress controls. Do not re-use the inert PR27 unit as a ready-to-run service.
4. Inventory and consistently back up original PocketBase `pb_data`, SQLite/WAL/SHM, notification outbox, Intake/Ops, Safe Retry, open/UNKNOWN/HELD, duplicate signal states, and secrets. Restore a **copy only** in a new isolated server; compare row-level hashes/ledger sums; investigate PocketBase 0.16.6 to 0.22.48 compatibility without touching the original.
5. Rerun original product financial regression and new security tests on **the same final integration candidate**, including 203 concurrent synthetic signals, webhook retry, restart, fee/PnL/ledger equality, websocket/HTTP bypasses, full manifest+packaging, Linux systemd and DB-restore validation.

## Independent CI evidence contract

- Original `.github/workflows/pr_verification.yml` is triggered on this PR to `main`, runs the full offline release safety gates on both Windows and Ubuntu, and Windows full pytest.
- New `.github/workflows/vultr_role_integration_a.yml` validates only approved new files, checks the protected application/release are byte identical to main, runs both PR23 inherited Stage tests and new role/credential/203-signal attack E2E with `release_checks.isolation`, verifies original release and packaging, uploads synthetic JUnit.
- A CI PASS proves only this *additive stage and role shell*. It does NOT certify the unmodified original POA with a globally installed trade guard or a real working Vulkan server.

`TRADING_ROLE_ACTIVATION=BLOCKED`
`LIVE_READ_AUTHORIZATION=BLOCKED`
`LEGACY_MAIN_INTERNAL_SINK_GUARD=NOT_INTEGRATED`
`VULTR_HOST_TEST=NOT_PERFORMED`
`DB_RESTORE=NOT_VERIFIED`
`PRODUCTION_READY=BLOCKED`

**No main mutation, merge, deployment, operating server action, real keys, production DB reads or writes, live exchange network/API calls or actual orders were performed.**
