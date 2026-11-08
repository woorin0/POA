# Independent re-audit of PR #24 — Python module substitution, raw SDK, restart race and HTTP/KIS sinks

**SCOPE: separate Draft PR; never merge, deploy, connect exchange/cloud or issue orders.**

## Immutable original
- PR #24 final PASS HEAD: `df0d7faad850c2435ec77c5790dd7bf0ed2f1e3c`; Git tree: `73b6a1684ba38fb4ccecdac742c1700875b57aa8`.
- Original Windows+Ubuntu Mock run: https://github.com/woorin0/POA/actions/runs/42429906126 — 121/OS PASS.
- Parent PR #23 immutable HEAD: `e01d3d827dea3b147b63fb6ec4cfcecc3cc95ebc`; main `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f` prior to this audit.
- Original 72-file financial release manifest, verified financial ledgers, PnL calculations, DB schema, migrations, legacy webhook financial JSON contract, exchange amount formulas, operational server, secrets and release installer **unchanged**.

## Findings and corrections (only two-line call-site guard edits or new two-line guards)

| Threat | Reproduction / previous weakness | Audit-only remediation |
|---|---|---|
| Guard module swap | PR24's functions dynamically import and call `require_order_disabled()`. If a hostile in-process Python actor substitutes the function with a no-op, subsequent code could run | In 50 prior execution entrypoints, keep same two inserted lines but import an exception class and **raise it directly**, so a no-op guard function cannot authorize code execution. An invalid replacement exception class fails with `TypeError` before side effects |
| Existing exchange constructors | Direct `Binance/Bybit/Bitget/OKX/Upbit` object construction bypassed market-method call guards, possibly loading markets before any denial | Add first-statement deny to each constructor; also deny `pexchange.get_exchange/get_bot` before key/SDK access |
| KIS direct HTTP POST | `KoreaInvestment.get_hashkey` and `create_auth` perform their own `session.post` outside the already-blocked `post()`. `KoreaInvestment.__init__` calls `auth` and accesses the auth DB | Add first-statement deny to constructor, `get_hashkey`, `create_auth` and `auth`; original KIS order POST methods remain denied |
| Safe Retry restart and stale instances | Old `Store`/Manager/ReconciliationClients instances can be invoked directly even with `get_manager`, `reconcile_pending` and `reconciliation_loop` guarded | Deny constructors, `Store.connect/claim/save/pending/recover/backup`, `Adapter.reconcile`, `ReconciliationClients.get_client/acquire_lease/_get_or_create_client_locked` and `get_reconciliation_clients` ahead of DB, CCXT or state changes |
| Alternate HTTP paths | PR24 guards order/hedge and startup, but `_price_http`, direct `price()`, and `get_ip` can initiate outbound calls if startup is bypassed | Deny these direct handlers before clients/httpx |
| Contention | Multiple worker threads may attempt concurrent retry or order methods | Threaded Mock attack invokes the original guarded product functions 24× under an inert injected guard module and asserts every call is rejected before side effects |

### Attack execution details

The independent new tests **extract and execute original changed Python function bodies** from the candidate with hostile objects that fail if touched, and run without importing live `main.py`, instantiating real CCXT, using KIS HTTP, logging actual credentials, accessing DB, real network or Vultr. A separate class-constructor test explicitly denies all five crypto adapters, KIS and four Safe Retry classes. Tests also replace `sys.modules['exchange.order_execution_gate']` with malicious no-op guard functions, inject a bad exception class, and repeat both normal and concurrent worker entrypoints.

Original PR #24 tests are **retained**, adjusted only for the harder inline-raise call site and expanded to verify all new two-line guard insertions: remove every guard pair from each changed original source and compare **byte-for-byte** with PR #23. Source pin/72-manifest and packaging checks run in an isolated detached **PR23 original** worktree; no attempt to pass PR24/25 modified trade code off as approved original product.

### Mandatory unresolved boundaries / negative control (do not claim universal order safety)

- `RAW_CCXT_OUTSIDE_APP=UNRESOLVED`: a raw third-party CCXT object instantiated directly **outside the POA adapter** does not pass through this application-level gate. A synthetic raw client Mock proves the bypass capability; this test never creates a real CCXT client or sends an order. Real network egress must be blocked by a separately validated Vultr systemd/container/host policy.
- `PYTHON_INTERPRETER_REPLACEMENT=UNRESOLVED`: a privileged actor who can modify Python code, monkeypatch entire method objects, replace the executable, or subvert exception construction remains outside an in-process guard's trust boundary. The test explicitly proves a method replacement against a synthetic object; it is **not** proof such an actor was contained.
- `VULTR_HOST_NETWORK_ISOLATION=UNVERIFIED`: the existing stage-only candidate unit contains `PrivateNetwork=yes`, `ProtectSystem=strict`, `NoNewPrivileges=yes`, and is deliberately unstartable via `ExecStartPre=/usr/bin/false`. The real host, network namespace, firewall, root ownership, active unit, container network, and installed SHA have not been examined.
- `OPERATING_DB_WRITES=NOT_PERFORMED`: old finance/DB bytes are retained. No real operating PocketBase/SQLite backup or restoration, migration, POS/stock credentials, or live calls were attempted. App `main.py` imports settings and DB *before* its guarded startup; tests never import `main.py` as a whole. This guard does **not** certify import-time DB side effects as absent.
- Adapter KIS `get()` and `check_auth()` may issue read-only GET if manually called outside safe constructors. Privileged direct `session.post` bypass still exists if a caller holds/forges a raw session; covered by OS network restriction only.
- Operational trade enablement has **no override**. Candidate cannot be treated as an automatically functioning live bot. Original golden trade-success tests will fail by design if executed against hard-deny candidate; parent full regression is historical evidence, not candidate acceptance. Any return to live trading needs separate approved release/manifests, strong key separation, independent DB restoration and actual Vultr isolation proof.
- Independent read-key equal-account permission proof, WORM/Object Lock external provider, multiple audit signers remain Mock/BLOCKED as of PR #22.

**Cloud/exchange/operating DB/live trade = NOT USED. Merge/deploy = NOT DONE. PRODUCTION_READY=BLOCKED.**
