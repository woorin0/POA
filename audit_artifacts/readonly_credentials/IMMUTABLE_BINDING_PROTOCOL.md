# P0 immutable account-key binding — signed-claim Mock protocol

**State:** AUDIT ONLY / NOT OPERATIONAL / NO EXCHANGE PROOF COLLECTED / DO NOT MERGE.

Source: PR #15 PASS HEAD 57c3c1aa59e6fa2e7580ff02db6b6b8775cb4681.
Immutable live product under audit: ad88384b8b6030c1109843b09b6761395b3f2d1c,
Git Tree 663c82e1134f592beb9c8bc652daccc7b2185975.
This Draft PR introduces only additional audit artifacts and an isolated CI workflow.
It never edits financial ledgers, runtime reader, trading paths, configuration, or DB.

## Central invariant

The legacy POA account reference is ledger.identity(venue, ORIGINAL_TRADING_API_KEY).
It is **not** an exchange-issued account or subaccount identifier. A new
read-only API key necessarily derives a different reference even when the
two keys belong to one trading account. Do not rewrite or alias the historic
ledger key hash just because a new read key can fetch an order.

The new Mock verifier requires two *separately authenticated venue principals*
and independent examiner signatures to bind historical trade-ref to read-ref:

1. An **observer** must independently authenticate the trade key and the
   read key against the venue, extract the immutable exchange-controlled
   account/subaccount identity and market scope, and normalize real
   permission details from an authenticated response.
2. For each key, observer signs an Ed25519 KEY_OBSERVATION with venue, spot
   or linear scope, role, pseudonymous account/subaccount tags, immutable
   key reference, normalized permission classes, SHA-256 of evidence,
   externally generated challenge and short validity window.
3. A separately trusted **approver** checks the underlying raw evidence and
   independent source (not just an observer's summary) and signs a
   BINDING_APPROVAL explicitly covering **both observation digests**,
   both distinct refs, venue, market, subaccount, nonce, sequence, previous
   hash and expiration. Disjoint Ed25519 keys and out-of-band root pins
   are mandatory; no private key belongs in the repo, a CI artifact or POA.
4. The verifier rejects any mismatched account, subaccount, market,
   permission, key reference, challenge, signature, signer role, domain,
   freshness, link digest, replay, skipped sequence or changed predecessor.
   Read permissions must normalize to EXACTLY READ; trading key observations
   in this conservative prototype normalize to READ+TRADE, no transfer
   or withdrawal permissions.
5. An append proposal is produced only after validation. A mock CAS registry
   serializes concurrent append attempts and extends its hash chain. It also
   enforces **one-to-one account-key binding** in both directions per
   venue/market: a later fully signed approval CANNOT silently rebind one
   historical trade key to another read key, claim one read key for a second
   legacy identity, or reapprove the same pair. Genuine key rotation and
   revocation need a separately audited protocol.
   **Neither the proposed link nor in-memory append is durable.**
6. In a real future implementation the prior checkpoint MUST originate
   from an independently operated, signed, externally monotonic WORM/append
   ledger. The append MUST use durable atomic compare-and-swap and publish
   a witnessed successor; local Python state, a Git branch, or a user
   supplied head is NOT a trustworthy anti-rollback source.
7. Runtime activation is **always rejected**. Separately audited key,
   account binding, proof lineage, registry driver and order-blocking
   integration would be required in a subsequent product PR.

## Venue-specific external feasibility (official API docs checked 2026-11-07)

- **Bybit** GET /v5/user/query-api documents readOnly, permissions, userID,
  parentUid and isMaster. The independently privileged examiner must verify
  that two separately signed queries use the same real UID and exact
  subaccount, and the read key's restriction is effective.
  https://bybit-exchange.github.io/docs/v5/user/apikey-info
- **Upbit** GET /v1/pockets/api_keys documents pocket UUID and API key list
  / permission metadata but requires a main-pocket key with
  manage_pockets privileges. It MUST NOT run using the unprivileged
  notification-reader key or be introduced into a live bot. A separate
  secured examiner can only use it with explicit approval.
  https://docs.upbit.com/kr/reference/list-pocket-api-keys
- **Binance** differentiates USER_DATA (private order status/trades)
  from TRADE (placing/canceling orders). A signed read alone does not
  prove same account; an independently authenticated account identity and
  exchange-side permission settings are needed.
  https://developers.binance.com/en/docs/products/spot/rest-api
- **OKX** documents Read, Trade, Withdraw key permissions and
  GET /api/v5/account/config. The examiner must verify official
  account/subaccount scope and actual API key permissions separately.
  https://www.okx.com/docs-v5/en/

Endpoints and available identity or permission fields vary by venue,
account type, residency and API revision; the sources do not establish
a universal exchange-signed claim format. The observer's source_kind
AUTHENTICATED_EXCHANGE_RESPONSE is only a **signed assertion** in this
Mock, NOT a trustable claim without independent raw venue evidence.

## Attack testing and hard boundaries

Simulated attacks: fabricated signatures, same-role root reuse, pinned
root substitution, changed observer or approver fields, wrong domain,
key/venue/subaccount/scope swapping, order/transfer/withdraw permissions
on read key, unauthorized source claim, stale or future data, replayed
challenge, repeated or skipped sequence, forged predecessor, concurrent
same-sequence append and activation bypass. No sockets, real account IDs,
credentials, API calls, operation DB writes or trading orders.

**Remaining BLOCKED:**
- Actual exchange-authenticated identity observations for *both* keys
- Independently witnessed account/subaccount identity equality
- Genuine read-only key permission restrictions / transfer and withdraw denial
- Safe storage of independent observer and approver keys, revocation and root pins
- External durable monotonic WORM checkpoint and signed append/recovery
- Independent server clock and challenge generation
- Actual production integration and operating DB/receipt preservation

The Mock verifier can prove signed claims are consistent with each other.
It CANNOT prove underlying claims are true if both signers lie/are compromised,
if the source was forged before signing, if the trust pins are replaced, or
if the external checkpoint is rolled back with the application. Operational
READY remains BLOCKED by design.
