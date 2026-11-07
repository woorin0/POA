# PR19 durable isolated WORM backend and independently persistent checkpoint

**Operational status: BLOCKED.** Isolated Windows/Ubuntu test implementation only.
**No actual external storage provider, cloud Object Lock, mTLS service, independent operator system or production integration is used.**

- Base PR #19 PASS HEAD: `5c1644b87f78b3bb7a7c9b82961c7dbbd1cbdd98`.
- Base PR #19 Git Tree: `53b833ea7eaa0a8c986578f8d67269bf78831d00`.
- Protected original application product: `ad88384b8b6030c1109843b09b6761395b3f2d1c`.
- Protected original product Git Tree: `663c82e1134f592beb9c8bc652daccc7b2185975`.
- Changes: a new audit-only durable backend, attack tests, this document and offline CI. No changes to main, earlier PRs, product/ledger/DB/config, real secrets or operating servers.

## What is **real** in this isolated implementation

The local backend is an actual on-disk SQLite file, not just a Python tuple:

1. **Transactional storage:** `DurableSQLiteWORMServer` uses SQLite WAL, synchronous=FULL, explicit `BEGIN IMMEDIATE` and `COMMIT`. Every append is a single SQLite transaction. It reuses PR19's triple-signed external observation and independent operator-grant verification BEFORE commit, and retains the original canonical signed request, signed original evidence, unique request ID, encoded commit entry and observed-at time. Every reopened process reloads rows, verifies signatures/chain/venue/market/nonce/account uniqueness and reconstructs the original protocol's idempotent request index. Transactions are serialized across independent SQLite connections/processes.
2. **Logical append-only ACL:** SQLite schema blocks UPDATE and DELETE of ledger rows/metadata with SQL triggers. Unique sequence/request ID constraints enforce write-once per logical request; the original PR19 key-pair uniqueness and challenge replay prevention are reconstructed from signed rows. No API endpoint to edit/delete old records is provided.
3. **Independent checkpoint persistence:** `DurableSQLiteAuditor` stores a separate monotonic checkpoint chain on an independent test filesystem path; signed checkpoint verification (PR19) occurs against the persisted high-water and already witnessed prefix. It persists every height and survives new Python instances and subprocess inspection. It independently checks original source/approver/witness pins.
4. **Simulated no-socket network:** The protocol remains the original signed `MockNetworkLink` byte-frame transport. No actual cloud/API/network connection, secrets or deployment. Operator grant signing uses ephemeral CI test keys only.
5. **Power-loss boundary:** fail injection after preparing the PR19 in-memory state and after SQL INSERT but before SQL COMMIT proves no durable write survives the failed operation. Lost HTTP-like ACK after commit, reconnect and exact retry produce one durable row. Separate connections running concurrent same-CAS requests accept exactly one.

## Controlled security attack coverage

- **Restart and OS visibility:** a real child process connects to the SQLite DB and observes the committed row; reopening the verifier reconstructs correct checkpoint, unique request and signed chain.
- **Delete/rewrite:** attempts to SQL DELETE/UPDATE row or checkpoint fail; manually deleting the actual DB file fails closed on reopen. A new genesis store is flagged as rollback when the separate auditor high-water remains.
- **Rollback and backup:** a test uses SQLite backup for an old snapshot and restores it after a newer commit; auditor detects the stale independently signed checkpoint even though the old snapshot is internally valid.
- **Fork:** two independently committed files can produce different valid sequence-1 chains. Shared independent auditor detects the split-view, two separately started auditors can accept their own forks. Without externally shared consensus or transparency, global fork safety is UNVERIFIED.
- **Atomic CAS:** concurrent writes through distinct DB handles/instances, exact request idempotence after restart, failure before commit, duplicate request ID, replay with a mismatched grant.
- **Signed false receipts:** inherited PR19 checks reject syntactically valid but inconsistent receipts. A compromised checkpoint signing key can still fabricate a fully coherent signed transcript that was never durably written; this is explicitly exercised as a **counterexample**, not claimed as solved.
- **Trust root replacement:** root-set commitments and signer public-key pins must remain externally stable; a substitute checkpoint private key cannot be used with the fixed signer pin.
- **Multi-domain compromise:** if both server and auditor DB files are deleted or rolled back, local software has no immutable external high-water. Tests prove this remaining fundamental threat.

## WORM limitations / required separate external service

**SQLite append-denying triggers and a local disk are NOT actual WORM** (not S3 Object Lock compliance, Azure immutable blob retention, GCS Bucket Lock, device-level WORM or an independent remote notary). Database ownership, superuser access, filesystem deletion, SQL `DROP TRIGGER`, detached snapshots and synchronized rollback defeat this local software boundary. It is useful for realistic transactional and restart validation, but **NOT permitted as the production trusted witness**.

Before operational READY require: true remote object-retention enforcement with immutable retention policy and independently owned storage credentials; append transaction and durable CAS; external signed/witnessed commit receipt grounded in provider-enforced object version/object hash; independent operator KMS/HSM key custody and disjoint principal accounts; a shared multi-auditor high-water/quorum transparency protocol; cryptographic chain proof that references immutable objects; restoration/rotation evidence; real exchange-side API same-account/read-only privileges; isolated Vultr backup/restore and final operational security review.

An isolated SQLite-only PASS validates Python + SQLite transaction semantics under tested conditions, **not** hardware/cloud regulatory immutability. No trading, real network, operating DB modification, main merge or deployment.
