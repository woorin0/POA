# PR23 → PR24 independent central execution-denial audit — VULTR OFFLINE ONLY

**STATE: DRAFT / HARD DENY / NOT A TRADING RELEASE / DO NOT MERGE OR DEPLOY.**

## Pinned source and scope

- Parent PR #23 final HEAD: `e01d3d827dea3b147b63fb6ec4cfcecc3cc95ebc`
  (Git Tree `f40cf61dda21f88e2ef5e0be31cea426d67284e3`).
- Parent PR #23 original Windows & Ubuntu full product CI:
  `https://github.com/woorin0/POA/actions/runs/42418364522`
  (parent baseline; NOT new candidate's full regression).
- PR #15–22 audit results are synthetic/unmerged and NOT operational
  attestations of genuine exchange account equality, permission restriction,
  global WORM/CAS or signer independence.
- Original `release_manifest_sha256.json`, immutable financial formulas,
  verified PnL, account identity, migrations, PocketBase, DB locations,
  strategy signal formats, package/deployment files and operating credentials
  remain unchanged. All existing files touched by this PR have **only two
  lines of executable code inserted per order/worker entrypoint**.
  A regression test restores and compares exact parent file bytes.
- This branch **does change execution control flow** of original order
  entrypoints; the existing parent-manifest verifier is not updated or
  weakened to misrepresent the candidate as a valid production release.
  Parent manifest is instead checked in its original detached checkout.

## Hard fail-closed execution gate

`exchange/order_execution_gate.py::require_order_disabled` always raises
`OrderExecutionBlocked("POA_ORDER_EXECUTION_BLOCKED")` and offers **no**
environment opt-in, CI allow mode, override hook or operational signing key.
This is deliberately **not ready for live use**; independent authorization
and a NEW release source/manifest review are needed before trading can be
supported again.

The guard is first in **50 callable product entrypoints**, before any client,
DB, secret, position read, order metadata mutation, exchange POST or ledger
write:

| Entry boundary | Guarded functions |
|---|---|
| main.py | startup, HTTP /order and /, order internal, HTTP /hedge, hedge internal |
| exchange/pexchange.py | generic retry loop (including create_order and leverage) |
| exchange/safe_retry.py | manager init, execution, private execution, safe_submit, pending recovery, reconciliation loop |
| Binance | leverage; market_order/buy/sell/entry/close; direct SL/TP create_order |
| Bitget | leverage; market_order/buy/sell/entry/close (including direct set_margin_mode) |
| Bybit | leverage; market_order/buy/sell/entry/close |
| OKX | leverage; market_order/buy/sell/entry/close |
| Upbit | market_order/buy/sell/entry/close |
| KIS stock | create_order, KRX/US buy/sell convenience functions, generic post and post_with_error_handling |

The existing `poa_stage.gateway` remains separate and non-operational:
it cannot delegate to `main.py`, and its candidate systemd unit is
intentionally disabled.

## Independent Mock attack suite

`tests/test_order_execution_deny.py` extracts the **actual changed
function bodies**, strips only parameter annotation lookup for standalone
execution, and executes them with hostile objects that raise on every
attribute, call, boolean or iteration. Synchronous and async functions
must raise the fixed `POA_ORDER_EXECUTION_BLOCKED` before touching
any fake dependency. No app imports, real network, webhook, credentials or
operational DB accesses occur.

Source-preservation tests remove precisely the 2 introduced guard statements
from every method and compare the remainder **byte-for-byte** to the PR #23
parent revision. AST checks prohibit mutating CCXT SDK calls outside guarded
adapter methods and ensure the guard has an unconditional raise. Environment
flag attacks (`CI`, `POA_LIVE_ORDERS`, etc.) are ineffective. KIS direct
HTTP POST and Binance SL/TP are included. The original stage gateway and
source manifest are separately pinned.

Windows & Ubuntu jobs:
1. Validate exact source diff and syntax; run isolated Mock attacks with
   `release_checks.isolation` and upload JUnit.
2. Detached checkout of immutable PR #23 baseline, rerun original 72-manifest
   and packaging checks and the parent 52-case stage Mock E2E.
3. Full parent product regression remains linked to its original successful
   run. The **modified hard-deny candidate intentionally cannot pass live
   order-success tests** unchanged. There is no fake "all product regression
   passed" claim. An independently approved future product manifest and
   full candidate regression is a separate release gate.

## Explicit exclusions, unresolved security boundaries

- A privileged operator may run another checkout of the unchanged original
  POA binary, or call `ccxt` directly outside POA. This per-function Python
  gate cannot stop those actions. Protect at Vultr service/IAM/network
  isolation and install only SHA-pinned code under a separately authorized
  release.
- The underlying `main.py` currently imports original package and settings
  at module import; this branch never imports it or starts it in CI. A
  hand-executed import may initialize existing dependencies. The actual
  staging ASGI has no such import.
- The KIS direct `get_hashkey` method still performs a credentialed HTTP
  POST for authentication/hash computation when called explicitly; it is NOT
  itself a trade-order request. **Orders cannot reach KIS.post through
  guarded product methods**, but independent arbitrary `requests.Session`
  calls are outside this boundary.
- This intentionally makes `main.startup()` fail closed. It is a **kill
  switch audit candidate**, not a functional production trading bot or
  a tested real Vultr installation.
- Database backups, actual exchange read-key/account attestations, VULTR
  server staging, 203-signal load, real IAM and operational authorization
  remain UNVERIFIED. No AWS is used. The previous PR23 dedicated read-key
  planner remains unmodified and its live read authorization stays BLOCKED.

**OPERATING_READY=NO. LIVE_ORDERS=BLOCKED_FOR_THIS_SOURCE.**
