# POA PR16 independent signer/approver compromise & evidence-chain attack review

**Audit-only. Product integration: NONE. Operational READY: BLOCKED.**
This Draft PR is branched from verified PR16 HEAD
c2b2e3bb16b611188afcbc3d1b114b4ed8070dbb
(Git Tree fecf7a04dbf687a159210ce5302810a3a7cab12e).
It preserves immutable product code
ad88384b8b6030c1109843b09b6761395b3f2d1c
(Git Tree 663c82e1134f592beb9c8bc652daccc7b2185975).

## Observed attack scenarios and minimal mitigations

1. **One stolen signer:** a forged observer signature alone invalidates the
   old approval's observation digest. A stolen approver signature alone cannot
   replace an already independently signed observer claim.
2. **Both signers collude:** PR16's two valid Ed25519 signatures are
   cryptographically self-consistent even when the parties deliberately lie
   about the account, source digest or permission scope. This cannot be
   solved by a stricter JSON schema. A separately pinned **third evidence
   witness** must independently verify the underlying exchange observations
   and sign original source digests, original observer envelopes, approval,
   exact venue/account/subaccount, key references, permissions, root commitment,
   challenge, predecessor, sequence and expiry. Tests simulate the attack,
   independent rejection and all-three-collude counterexample. This is **not**
   proof that any real exchange provides authentic account identity.
3. **Local trust-root swap:** the original validated root dictionary allowed
   caller mutation via its internal mapping. Convert to a mappingproxy,
   disallow ordinary slot replacement and expose a canonical root commitment.
   Independently pinned witness links that commitment; attacker-supplied
   new observer+approver roots and their self-consistent hashes cannot be
   substituted without changing the independently controlled witness pin.
   Full Python interpreter compromise / object.__setattr__ can still defeat
   in-process controls.
4. **Evidence substitution and signature representation:** JSON envelope
   inputs are snapshotted before signature verification and linked digest
   calculation. Reject noncanonical Base64 forms of valid Ed25519 signatures,
   forged source hashes, modified envelope fields, wrong context and approval
   substitution. The independent witness signs its own evidence claim;
   a malicious witness can still lie.
5. **Replay and concurrent append:** Mock registry tracks already-used
   challenges in addition to monotonic sequence, unique trade/read refs and
   CAS. Independent witness binds the approved predecessor and sequence.
   Threads competing for a signed receipt accept at most one append.
   **Critical residual:** a new in-memory registry restored from a forged
   genesis still accepts the same genuinely signed receipt. No remote
   durable WORM ledger or trusted monotonic checkpoint was implemented.
6. **Operator approval:** POA operating enablement remains hard-denied
   regardless of Mock success or witness count.

## Explicit trust and implementation limits

- The 'AUTHENTICATED_EXCHANGE_RESPONSE' string is a role signer's **claim**.
  Evidence provenance is meaningful only if an independent verifier controls
  the actual exchange requests and credentials outside the colluding signers.
  The third witness is a mock signer; no real exchange identity checks occur.
- Constructor supplied witness key hash and observer/approver root commitment
  are only safe if fixed in an *independent* read-only trust store. If attacker
  replaces witness key AND expected pin, all 3 signatures can collude.
- Trading/read-only key permissions cannot be certified from locally signed
  claims, and the key hash remains distinct from exchange-issued account ID.
- A local high-water sequence, local CAS lock, Git branch or signed JSON
  does not prevent snapshot rollback. Production needs an independently
  operated append-only/WORM ledger with atomic append, signed witnessed
  checkpoints, independent root-key custody, revocation and rollback gates.
- This Mock is in the audit_artifacts path; production ReadHost, account
  mapping, ledger, DB and market orders are untouched. No real secrets,
  operational DB, server, exchange GET or Discord call is used.
- Passing CI certifies only the tested Mock policies. Operational account
  identity, independent evidence collection, signer custody and root pin
  distribution, WORM durability, rollback defense and deployment are
  **UNVERIFIED**.

## CI evidence requirements

Windows + Ubuntu run on a detached immutable product worktree. Only audited
files are copied untracked. The runner installs no live credentials and
injects release_checks.isolation before pytest. Each suite emits JUnit,
and CI checks unchanged product SHA/tree and tracked-file diff.

A failed attack regression is BLOCKED and must not be hidden as expected
or reclassified as verified. Intermediate failing CI runs remain evidence.
