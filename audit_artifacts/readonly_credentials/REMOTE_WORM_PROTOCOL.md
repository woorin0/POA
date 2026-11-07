# PR17 -> Remote WORM signed checkpoint / operator boundary — isolated protocol v1

**STATUS: MOCK E2E ONLY. REMOTE WORM SERVICE NOT DEPLOYED. PRODUCTION READY = BLOCKED.**

- Base PR17 final PASS HEAD: `deeba06ec6462587777bccc8c14ed0e647787083`
- PR17 audit Git Tree: `68c2369545e6b75619fbc1bcde4a367e72c6b0fe`
- Original application product SHA: `ad88384b8b6030c1109843b09b6761395b3f2d1c`
- Original product Git Tree: `663c82e1134f592beb9c8bc652daccc7b2185975`

No production POA files, ledger, DB, Settings, real API keys, order executor, Discord, main or prior PRs are touched. The code here is an **audit-only protocol reference**, not a remote WORM service implementation. The network is simulated with canonical JSON byte frames and an untrusted `MockNetworkLink`; all "remote" execution is in-memory with no socket.

## Threat boundaries / remote roles

Four disjoint, out-of-band **public key trust roots** are fixed by an independent administrator, in addition to the original PR17 observer, approver and third evidence witness:

| Role | Allowed | Explicitly disallowed |
|---|---|---|
| Writer/proposer | Authenticated HEAD query; submit a signed append proposal | Grant its own append, issue checkpoints, rotate pins |
| Operator | Authorize a **specific** writer request digest and expected head with short-lived Ed25519 grant | Submit writer request, issue checkpoint, read bot trading secrets |
| Auditor | Request and verify checkpoint / full prefix evidence, preserve a separate high-water | Append or authorize append |
| Checkpoint signer | Server signs actual CAS commit or HEAD response | Act as operator or client, amend previous records |
| External source witness | PR17 observes / signs separate synthetic account evidence | Grant an operator authorization or sign checkpoint |

Every root must have an independent, externally pinned fingerprint. The **server checkpoint key must remain separate from the operator grant key** and the PR17 observer/approver/evidence-witness roots. All cryptographic keys created in CI are short-lived TEST keys, never transmitted to GitHub as source or test artifacts.

## Wire protocol

Only canonical UTF-8 JSON byte frames are accepted (duplicate keys, NaN, noncanonical spacing, oversized payloads are rejected). All Ed25519 envelope signatures bind `domain=POA_REMOTE_WORM_BOUNDARY_V1`, explicit role/kind/kid and canonical body. Every signature is checked against a pre-pinned role public key; a request cannot supply its own trust pin.

**1. HEAD query**
Writer or auditor signs `CHECKPOINT_QUERY` containing a new nonce. The server returns a remote-checkpoint-key-signed `SIGNED_CHECKPOINT` bound to the exact query digest, operator root commitment, PR17 source/witness root commitments, current sequence/head, digest of all entries, and purpose=HEAD. Auditor validates every sequence and predecessor link and pins a separately maintained high-water and prefix digest. An older *valid* signed checkpoint is rejected once its external floor advances.

**2. APPEND**
Writer signs `APPEND_PROOF` carrying a unique request ID, expected CAS head, and the complete original PR17 signed observation/approval/independent witness envelopes. The independent operator signs `APPEND_GRANT` for the **exact** writer envelope digest, same request ID, same CAS head, expected source/witness root commitments and short expiry. Operator grant never comes from the writer's key.

**3. SERVER verification and CAS**
Verify writer signature and operator grant against their separate pinned keys. Verify grant expiry using independent remote server clock. Check sequence+head equality, verify the PR17 witnessed account-binding signatures and exact venue/account/scope/permissions/challenge against the current head. Enforce unique writer request ID, unique external challenge and one-to-one trade/read key binding. Atomically append the next immutable record and update head/nonces/indexes under one lock. Return signed **APPEND checkpoint** bound to the request's exact digest. Repeating the exact signed frame can return its committed checkpoint without a duplicate record; reusing the request ID for different bytes is rejected.

**4. INTERRUPTION / rollback**
The network simulator can drop the request before commit, fail before transaction, drop ACK after commit, corrupt a signed response or replay an earlier checkpoint. Tests require atomic zero-or-one commit, idempotent retry, audit cross-check and fail-closed ambiguous state. The separately modeled auditor high-water rejects a remote server restored from a stale snapshot or an alternate signed same-sequence checkpoint. A malicious checkpoint signer that forges a *future* coherent chain may require stronger external witnesses or cross-audit; the Mock does not prove production anti-equivocation.

## Essential production requirements NOT provided here

- A genuine remote storage service with server-side **WORM/immutability enforcement** (Object Lock/compliance equivalent, append-only durable ledger, retention policy, immutable audit trail, deletion restrictions). A Python tuple/list and a GitHub Actions artifact cannot meet WORM.
- A durable, transactional store that **atomically** commits new entry, challenge/request-ID uniqueness, unique account bindings and checkpoint state across process power loss. The current in-memory critical section models these semantics but does not survive process crash.
- Externally anchored and signed **monotonic high-water** at a distinct trust principal: verifying a checkpoint signature alone does not prevent rollback when server and verifier snapshots are both replaced. The `IndependentMockAnchor` here is volatile.
- A hardened operator grant service, role-specific HSM/KMS-backed key custody, revocation/rotation, independently audited permission checks, clock authority, key recovery and multi-party audit.
- Enforced mTLS and network identities, strict egress allowlists, authenticated source provenance from Upbit/Bybit/Binance/OKX, true account/subaccount identity and API read-only permissions.
- Recovery protocol that reconciles remote CAS commits and outbox exactly once after an ACK loss, durable operator-grant receipt, branch/fork detection, and cryptographically witnessed log extension.
- Audit of evidence byte truncation, very long chains, storage quota, account rotation, signer compromise and multi-region consistency.
- No activation permission is added. PR17's `require_operational_activation` continues to hard-deny; this mock cannot turn it on.

## Negative-test categories and verdict

- Writer acting as operator/auditor; operator forging writer, auditor attempting append, key reuse, root replacement: reject.
- Expired/mismatched operator grant, wrong request digest, forged signer, bad witness: reject.
- Stale CAS, parallel competing appends, duplicate request IDs, lost-before-commit and lost-after-commit ACK: zero-or-one commit semantics in Mock.
- Checkpoint replay with changed query nonce, log tamper, forged checkpoint signature, signed fork, stale server after auditor high-water: reject.
- JSON duplicate fields, noncanonical JSON, NaN, oversize, malformed transport bytes: reject.
- **Explicit counterexample:** if *both* WORM server and auditor high-water are reset to genesis, a fresh unanchored verifier can accept rollback. This is **UNRESOLVED** until a genuinely independent durable, externally pinned WORM anchor exists.
- **Explicit limit:** compromise of independent checkpoint signer and all trust roots can defeat local cryptographic checks; external transparency and operator separation are mandatory.

**Final classification:** Protocol/Mock PASS does not mean remote WORM deployed, real evidence obtained, actual operator permissions protected or POA production READY. Those remain BLOCKED.
