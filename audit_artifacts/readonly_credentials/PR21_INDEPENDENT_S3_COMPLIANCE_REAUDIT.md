# PR21 independent S3 Object Lock Compliance adversarial re-audit

**Security verdict:** offline Stub test only. Real AWS S3 compliance enforcement **UNVERIFIED**. POA PRODUCTION READY **BLOCKED**.

Original PR21 PASS HEAD: `5b6f9b4f3fb40a79a80ef0a6f75861af906e83fe`
Original PR21 Git Tree: `7ab02c4f81dabc1c21f352eb4e0abb2be7afab51`.
Protected application product: `ad88384b8b6030c1109843b09b6761395b3f2d1c`
Product Git Tree: `663c82e1134f592beb9c8bc652daccc7b2185975`.

The isolated Draft PR changes only the prior PR21 audit-only S3 adapter and adds this report, an offensive test suite and a new Windows/Ubuntu workflow. No product, financial ledger, main, older PR, real credentials, real S3 client or operating server is modified.

## Findings corrected in the audit-only adapter

| Finding | Original weakness | Minimal remediation |
|---|---|---|
| PR21-T1 — input TOCTOU | Writer validated mutable Python frames and then encoded the same caller-owned references; attestor and verifier read caller-owned locator/proof dictionaries across untrusted stub I/O | Copy canonical JSON once before signature verification, request encoding, locator reading, proof signature checking and return |
| PR21-T2 — mutable injected trust roots | S3 writer's client/policy/roots, attestor's reader/private key/roots and verifier's reader/public key/roots could be reassigned after construction | Reject ordinary reassignment; verifier supports replacing verification-time **clock** policy only when bucket/owner/prefix/minimum retention are exactly pinned |
| PR21-T3 — unpinned provider storage timestamp | Original signed attestation omitted S3 version `LastModified`: attacker-controlled stub metadata could change between issuance and future verification without a mismatch to the signed time | Include the exact provider-reported `storage_epoch` in the separate attestor's Ed25519-signed proof, require equality at every version-pinned reread, reject time rollback within same verifier process |
| PR21-T4 — ambiguous receipt replay | Proof verifier accepted a signed historical receipt without binding it to the caller's **current** expected request/sequence/chain head | Add explicit `verify_for_request(proof, request_digest, sequence, chain_hash)` fail-closed verification. Generic `verify` remains for historical read-only inspection, **not** append authorization |
| PR21-T5 — malformed S3 response object | `HeadObject.Metadata` could be a list/string and yield `AttributeError` instead of a classified fail-closed result | Require exact dict-like metadata shape before reading fields |

## Attack E2E (no AWS network)

- **COMPLIANCE vs GOVERNANCE:** downgrade, retention shorten and missing version/lock metadata rejected; inherited tests also challenge disabled versioning and S3 retention modes.
- **Version swap/delete marker:** exact historical `VersionId` proof remains valid after newest key becomes a delete marker; even after conditional PUT creates a *new* version, the original proof cannot be rebound to that version by changing locator fields. S3 conditional writes are NOT a global CAS/unique version guarantee.
- **LastModified fraud:** later mutation of provider-reported creation epoch breaks the attestor-signed timestamp. Backdated or clock-rollback attempts fail as simulated. A *fully dishonest AWS API endpoint or shared cloud+attestor compromise remains UNVERIFIED and impossible to establish from a Stub*.
- **Forged immutable object receipt:** content/metadata/checksum/version mismatch, repinned bucket/owner and changed request/head fail; original PR19 source+independent WITNESS signatures are still revalidated. No signature can prove actual remote storage if the provider and operator both lie.
- **Signer/approval role collision:** writer/attestor/checkpoint/verifier root reassignments and outside pin substitutions fail in ordinary in-process code. Python interpreter compromise can bypass `__setattr__`; real HSM, IAM, trusted read-only root distribution are absent.
- **ACK loss and racing conditional PUT:** rejected as ambiguous, no false success. An in-memory Stub can still create a second version if a delete marker is inserted; production must reconcile with an independently pinned object `VersionId`, not the latest key.
- **Operation mismatch:** `verify_for_request` requires the exact signed request digest, sequence and chain hash; a valid historical proof for another append is denied.

## Known unresolved attack / operational blockers

1. **No AWS access**: S3 Object Lock Compliance bucket policy, versioning, IAM/SCP, AWS account separation, KMS/HSM, actual retention and legal hold are **UNVERIFIED**.
2. **Provider truth and timestamp honesty**: S3 `LastModified` and ObjectLock mode are API-provided observations. Signing a forged response, compromised credentialed service, or fake S3 API may cause a valid *local* proof of invented storage. Separate provider logs/independent cloud account with external notary are required.
3. **No global CAS**: `IfNoneMatch='*'` applies only to **current version**, and a delete marker may permit new versions under the same key. No multi-region monotonic append, quorum or cross-auditor anti-equivocation is provided here.
4. **Signer collusion**: PR17 observer+approver+witness collusion or their real key theft is not proven impossible; trade/read-only key same-account claims remain **UNVERIFIED**.
5. **Clock independence**: a monotonic in-process clock check is a Mock, not an external trusted timestamp/high-water; process snapshot reset or privileged interpreter mutation can defeat it.
6. **ACK recovery**: a conditional PUT that succeeds without an ACK returns `S3_PUT_UNCONFIRMED`; no version discovery or finalization occurs. Loss plus delete marker may result in multiple protected versions. This is deliberately *BLOCKED for operational commit*, not retried blindly.
7. **No operating integration**: no application code/DB/actual orders have been changed. No cloud retention is deployed.

Official reference:
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html

**CI gate:** isolated Windows/Ubuntu GitHub Actions at the PR HEAD, synthetic signing keys, fake S3 clients only, original product Git Tree hard-pinned and unchanged, inherited PR15–PR21 regression plus new attacks. Mock PASS must never be stated as production WORM READY.
