# Independent P0 re-audit — PR18 external WORM / checkpoint / CAS

**Status:** MOCK AUDIT ONLY. Remote immutable storage **NOT DEPLOYED**. Production **BLOCKED**.

Baseline PR18 final PASS HEAD: `3ed8700f394be4e187e86e8ba46dbb413089db26`
Git Tree: `c6243c5ca66e9545b6a31f56d09191fe43e9fa89`.
Actual product fixed and unchanged: `ad88384b8b6030c1109843b09b6761395b3f2d1c`
Git Tree: `663c82e1134f592beb9c8bc652daccc7b2185975`.
This isolated Draft PR changes only the **audit-only remote_worm_mock.py**, a new attacker-test file, this document and a CI workflow. It does NOT alter main, earlier PRs, production code/financial ledger/DB, running servers or real exchange secrets.

## Newly identified findings and minimal fixes

**Finding F1: false-positive append acknowledgement (Mock security bug, fixed).** PR18 client accepted any checkpoint-key-signed `purpose=APPEND` response, even a signed genesis HEAD with **no committed append**. The verifier also advanced its independent high-water before checking response purpose. New code checks the response purpose *before* state mutation and requires **exactly one receipt entry for the exact signed frame digest**, at expected predecessor+1, with matching key refs, venue/market, challenge, approval and both signed observation digests. The client recomputes the expected successor hash from original PR16 evidence before pinning the checkpoint. Tests attempt valid checkpoint-key forgery with empty/missing/mismatched receipts and purpose confusion.

**Finding F2: unverifiable chain links (Mock integrity bug, fixed).** Original anchor accepted signed sequences with only contiguous entry numbers and predecessor labels, while the checkpoint signer alone controlled each arbitrary `chain_hash`. Append entries now include the two original signed observation digests and the independent verifier recomputes **every** PR16 ledger chain hash from `domain, sequence, previous, approval_digest, trade_observation_digest, read_observation_digest`. A valid checkpoint-key signature with a forged ledger hash is rejected, including for a *fresh* auditor with no prior floor.

**Finding F3: interruption between state writes (Mock atomicity risk, fixed).** Original server separately assigned records, HEAD, unique-key indexes, nonce set and completed-request map. An exception after the first assignment could leave inconsistent state, despite an in-memory lock. The mock now copies and prepares the **complete six-part state**, then publishes it via one tuple-reference assignment. Fault injection after preparation confirms no partial state and safe retry. A single Python pointer assignment is **NOT** an ACID database transaction, process durability, or genuine remote WORM.

## Attack E2E coverage

- Server replacement after auditor high-water: reject old checkpoint.
- Two independent forged server forks at the same genesis: **both can locally commit** valid distinct approvals; one shared anchor detects conflicting same-sequence head, but independent fresh anchors do NOT coordinate. This is an explicit WORM/quorum blocker, not a success claim.
- Signed checkpoint with no append entry or wrong request ID, market, account, observation/approval hash: reject.
- Forged predecessor/ledger hash despite checkpoint-key compromise: reject if prior signed evidence hash does not match.
- CAS 30-way contention: zero or one atomic Mock commit; no duplicate nonce or account-binding mutation.
- Failure after prepare and before commit: zero writes, safe retriable request.
- Drop after commit but before ACK: exact request replay yields one commit.
- Role escalation by operator impersonating writer/checkpoint, writer impersonating operator, absent grants and grant replay: reject.
- Same receipt reused for a different signed frame: reject without advancing client's high-water.
- Server+auditor state rollback together: still **detectably unprotected** in this purely in-memory demonstration.
- **Residual counterexample:** if the *checkpoint signer* is compromised, it can forge a **cryptographically coherent full committed entry and checkpoint for a signed writer+operator request** even when the backend never wrote anything. Exact receipt inclusion and chain recomputation do NOT prove physical WORM retention. This counterexample deliberately passes as evidence of why production stays BLOCKED.

## Unresolved and non-negotiable external gates

1. Real remote WORM storage provider with retention policy, independent access and delete privileges, durable append transaction and audit of storage enforcement.
2. Independent, durable and monotonic high-water witness, multi-observer fork reconciliation, root pins and signer rotation/revocation. Shared in-memory registry and independent *volatile* auditor are not sufficient.
3. Atomic durable CAS store + idempotency records + crash-recovery/witnessed committed receipt; worker failover cannot lose uniqueness indices, completed requests or checkpoints.
4. Hardware-backed independent operator approval, separate writer/issuer/checkpoint keys, externally enforced network/mTLS identities; a module-level Python private key cannot satisfy security boundaries.
5. Authentic exchange account/subaccount same-account evidence, permissions verification for separated read keys, offline backup/restore and release authorization.
6. Verify actual live-signing trust root and checkpoint reconciliation from an independent administrative service; do not configure or deploy in this audit PR.

All E2E is in-process network simulation with ephemeral TEST keys. A Mock PASS means conditional protocol logic has passed, **not** that real WORM, stable signatures, enterprise key custody or production rollout is completed. No merges, deployment, exchanges, trades or real operational DB touched.
