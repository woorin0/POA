# PR20 S3 Object Lock Compliance object-version commit contract

**Isolation:** This PR is a code-level **S3 API adapter and offline Stub E2E only**. It DOES NOT create an AWS client, connect to AWS/S3, change cloud buckets, write operational DB data, deploy on Vultr or issue orders.

Immutable parent: PR #20 final PASS HEAD `55760ece415b80e5bbc4cfd38c75029ed1bdc5b1`, audit Git Tree `8f6bf1d45e5bda56f9d65345d4d696daa56b8f8c`. Immutable product `ad88384b8b6030c1109843b09b6761395b3f2d1c`, Git Tree `663c82e1134f592beb9c8bc652daccc7b2185975`.

## Explicit protection boundary

### 1. Writer (cannot self-attest storage)
The caller injects a boto3-compatible client, an independently pinned 12-digit ExpectedBucketOwner, pinned bucket and key prefix. No boto3 SDK/client creation or AWS credential lookup is performed by the module. Preflight checks S3 bucket Versioning `Enabled` and ObjectLock `Enabled`. A detached PR19 signed APPEND envelope plus operator grant, signed WITNESS and checkpoint/ledger inclusion are reverified before upload. The immutable canonical JSON document includes the original signed frame and signed remote-checkpoint response.

A single conditional `PutObject` requests `IfNoneMatch='*'`, `ChecksumAlgorithm='SHA256'`, `ChecksumSHA256`, mode `COMPLIANCE`, and explicit `ObjectLockRetainUntilDate`, at a content-addressed key. The response **MUST** return a non-null `VersionId`; an ETag is never used as a SHA-256 proof. The result is only an **unattested locator**, NOT proof that any bytes were stored.

If `PutObject` fails or its ACK is lost, the adapter stops with `S3_PUT_UNCONFIRMED`. It does **not** infer that a commit happened from a changed HEAD, does not recreate a version, does not silently weaken retention, and does not claim success. Any operational retry/reconcile process requires a separately verified version ID and external state reconciliation.

### 2. Independently controlled read/attest principal
A separate read-only S3 interface checks exact `Bucket, Key, VersionId, ExpectedBucketOwner` using `HeadObject`, `GetObjectRetention`, and `GetObject` with `ChecksumMode=ENABLED`. The two independent retention surfaces must both report `COMPLIANCE` and consistent, sufficiently future-dated `RetainUntilDate`. The exact version must be returned; the `GetObject` bytes, SHA256 metadata and S3 SHA256 checksum are compared to a cryptographically computed local digest. The original signed PR19 operator approval, independent witness, checkpoint and entire committed ledger chain are independently checked again.

The independent attestor key is separate from PR19 writer, operator, checkpoint, auditor, PR17 observer/approver/witness keys, pinned out of band. It signs `POA_INDEPENDENT_S3_COMMIT_ATTESTATION_V1` over the exact object-version locator and pinned trust roots. A distinct verifier checks that signature **and repeats the S3 version-pinned read** before accepting proof. Neither a writer-supplied version ID nor an S3 ETag alone is an immutable commit receipt.

### 3. Threat model and meaningful negative tests
- Invalid versioning or Object Lock configuration, `GOVERNANCE` retention, shorter/expired retention, missing/null/forged `VersionId`, mismatched HEAD/GetObjectRetention, wrong SHA256/metadata or altered document: **fail closed**.
- CAS and same-key collisions with conditional `IfNoneMatch`, upload ACK lost after the fake write, inability to read exact version, corrupt response: **do not report committed**. Note AWS conditional writes use the **current key version**, so deleted marker/replacement attacks are resolved by pinning original `VersionId`, not by pretending conditional writes are global CAS.
- S3 delete marker on latest key: **does not remove protected version**, exact pinned VersionId may still be read. The fake enforces deny on version-specific deletion; a real S3 policy and real Compliance behavior remain UNVERIFIED.
- Forged checkpoint, missing original operator approval, forged attestation, signer-role collision, replacement root/bucket/key/account, payload or checksum tampering, metadata spoofing: **fail closed when trusted independent reader or pins remain intact**.
- Compromised storage + auditor, fake S3 provider, copied trust roots or global checkpoint fork: **UNRESOLVED** without independent cloud/provider evidence and an external monotonic consensus witness.

## Explicit missing deployment prerequisites

1. **Real cloud Object Lock Compliance enforcement is UNVERIFIED.** AWS docs state Compliance blocks deletion and retention shortening for protected **versions**, not arbitrary keys. No cloud service was contacted and no real provider-backed WORM retention was tested.
2. **Independent AWS account/principal policy & KMS/HSM** must enforce object owner and storage isolation, least privilege, external attestor root pins, and ensure writer cannot read as auditor or modify retention. Current role split is simulated only.
3. **Object version durability + write ACK reconciliation** must be backed by immutable provider evidence, full request logs (CloudTrail or equivalent), independent ledger checkpoints and double attestation. Provider success alone or an auditor private-key signature is not global proof of storage or anti-equivocation.
4. **Monotonic and quorum-based CAS across independent writers** remains the PR20 SQLite CAS domain only. S3 put conditional writes do not provide a global sequence-lock or multi-region linearizable log by themselves.
5. **Cloud lifecycle, legal hold, version delete markers, region/account pins, key rotations, audit logging and retention expiration/recovery** require a dedicated provider/owner-enforced integration and separate operational approval.
6. **PR #20 product integration is not done**; live trade routes, exchange read-only account identity, DB survival and Vultr failover are not qualified.

## Current official API reference (inspection only)
- [Object Lock retention and governance vs compliance](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
- [S3 Object Lock view/permissions and delete markers](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html)
- [PutObject headers and Object Lock checksum requirement](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html)
- [Conditional writes apply to current version](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)

**Result:** A passing Stub E2E would validate the **API and cryptographic verification contract only**. This Draft PR must NEVER be interpreted as S3 WORM enabled or POA production READY.
