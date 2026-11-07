# P0 Dedicated Read-Only Exchange Credentials — Offline Design

**Status: MOCK_VERIFIED_ONLY; OPERATIONAL_READY=NO.**
This isolated audit does not modify main.py, exchange/, Settings, DB schemas, current product branches, or operating servers.

## Immutable product and proven incompatibility

- Product commit: ad88384b8b6030c1109843b09b6761395b3f2d1c
- Git Tree: 663c82e1134f592beb9c8bc652daccc7b2185975
- Current ReadHost.credential reads VENUE_KEY, VENUE_SECRET and OKX_PASSPHRASE from shared trading Settings.
- order_fill_confirmation.record_intent and enroll bind the historical source_account_ref to ledger.identity(venue, TRADING_API_KEY). ReadHost._client enforces the same key-derived hash.
- A genuinely separate read API key generates a different reference even for the same actual exchange account. Blind substitution therefore fails at FILL_ACCOUNT_MISMATCH. Do not overwrite historical source refs or disable account binding to make the tests green.
- GET-only HTTP transport does not prove exchange-side read-only permissions or disable the application's unrelated order routes.

## Dedicated secret names (proposal only)

POA_FILL_READ_ONLY_UPBIT_KEY / POA_FILL_READ_ONLY_UPBIT_SECRET

POA_FILL_READ_ONLY_BYBIT_KEY / POA_FILL_READ_ONLY_BYBIT_SECRET

POA_FILL_READ_ONLY_BINANCE_KEY / POA_FILL_READ_ONLY_BINANCE_SECRET

POA_FILL_READ_ONLY_OKX_KEY / POA_FILL_READ_ONLY_OKX_SECRET / POA_FILL_READ_ONLY_OKX_PASSPHRASE

The independent isolated_broker.load_dedicated_key accepts only an explicitly supplied exact-field, per-venue secret mapping. It does not read the trading environment, original application Settings or order secrets, and never falls back to them. Unsupported venues, incomplete, aliased or extra legacy fields fail closed.

The OfflineReadHostSettings adapter exercises the genuine, unmodified ReadHost in Mock-only tests. It is NOT a production isolation boundary. Malicious in-process code could introspect keys; use separate OS/process privileges for any future deployment.

The plan_separate_read decision always returns authorized=False. require_operational_authorization ALWAYS rejects. No env flag, unsigned JSON, one successful GET, operator statement or key fingerprint can elevate this prototype.

## External evidence required before any separate operational integration

1. Provision distinct exchange-side read-only API keys for each actual market/account. Inspect the exchange's own key permissions and prove order placement, transfers and withdrawals are disabled. Verify the true spot and derivatives permissions, IP allowlist, scope and expiry. Never upload real secrets or account identifiers into GitHub.
2. Independently prove the new read key and historical trading key refer to the same venue account/subaccount using authenticated exchange-controlled identity, not matching display labels or local request values.
3. Design an immutable ledger account binding that preserves old source_account_ref and intake lineage. Reject ambiguous accounts, rotations, revocations, stale mapping and rollback. No implicit key-hash aliasing.
4. Execute the reader as a separate OS principal, separate process/host or container, separate secret mount with network egress restricted to approved GET-only endpoints. No trading-key, order-signer, webhook ingress or write DB access. A Python class alone cannot enforce those boundaries.
5. Reconcile historical orders, trades, quantity, fees, exact average, finality, venue-specific spot/futures scope and duplicate-send protection using authenticated read-only API observations. Use out-of-scope and missing-permission negative tests.
6. Stage future product integration with explicit authenticated same-account proof, receipt lineage, account rotation/revocation, restarts, retry, and rollbacks; new operation must default disabled.
7. Verify operating DB/WAL backup and isolated restore, then require independent operator approval. No live trading during inspection.

## Mock-only evidence

- Isolation of dedicated read credential namespace, no fallback.
- Real ledger.identity key-dependence and mismatch across separate keys.
- Genuine ReadHost.credential and _client fail-closed behavior.
- Trading-key reuse rejection, secret masking, activation denied.
- Immutable product Git Tree and absence of tracked modifications.

**UNVERIFIED:** real read privileges; account matching; real exchange fills; production trade isolation; operating DB restore; production readiness.

Do not merge, deploy, enable readers, run live orders or touch original products.
