# Ubuntu 22.04 staging / reinstall safety gate

Status: STAGED_NOT_LIVE only. RESTORE_VERIFIED=NO, OPERATING_READY=NO.
A successful local synthetic test does not prove a server backup or restoration.

## Fresh install versus reinstall

User Data runs AFTER Vultr replaces the OS/disk. It cannot preserve the old disk.
Do not select Reinstall for any existing trading server until its external backup
and an isolated restore rehearsal have been verified by the operator. An install
mode string or a newly created local marker is not backup/restore evidence.
The installer accepts NEW_SERVER_STAGE_ONLY only and refuses existing app, DB,
result or configuration/service paths before package/app operations. Partial
installations remain for manual review; no destructive retry/cleanup is provided.

## Required external evidence per server (not implemented as automatic activation)

Before an approved maintenance window: identify the actual systemd WorkingDirectory,
all DB paths from configuration, PocketBase --dir, linked mounts and external data.
Use read-only inventory, without printing secrets. Stop Webhook ingress and then
quiesce every writer (POA, worker, Safe Retry, PocketBase, supervisors/other processes).
Observe actual process termination; service stop alone does not prove all writers stopped.
Without an established cross-file write barrier, do not claim a consistent pair.

Preserve, with original permissions and root-only encryption/key custody:
- .env, credential/binding material held outside Git, service and strategy configuration;
- /root/db/pb_data including collections/auth and auxiliary files (resolve actual path);
- configured VERIFIED_NOTIFICATIONS_DB_PATH, .intake.sqlite3 and .ops.sqlite3 siblings;
- Safe Retry store (default safe_orders.sqlite3), strategy virtual positions/signal dedup;
- all discovered SQLite files, WAL/SHM and recovery metadata, deployment SHA and manifest.
These defaults are inventory hints, not a complete list of a real server's storage.

While ALL writers are quiesced, create each SQLite backup with sqlite3.Connection.backup
(or the vendor's supported full backup). Copy other files only under the same barrier.
Do not copy a live .db file alone, omit live WAL, combine backups from different times,
or use a current balance as a past checkpoint. Keep a forensic copy of stopped original
DB/WAL/SHM sets separately; do not attach mismatched WAL/SHM to sqlite backup outputs.

The external backup inventory must identify server alias, source deployment, exact
storage/configuration set, barrier interval, hashes/sizes, external location, key custodian
and operator. Restore into a different isolated directory/machine with no network or
service startup; run integrity_check and foreign_key_check, compare all retained table
contents/state/digests/ACKs and relationships, not only row counts. Specifically compare
SENT/UNKNOWN/HELD/POSTING, intake lineage/ACK pair, leases, pending/retry orders, frozen
wire/message IDs, signal dedup and strategy positions. Missing file/row or unknown
reconciliation scope remains BLOCKED. Test that replay cannot resend or reorder.
Record independent reviewer/approval and restore rehearsal evidence externally.
Do not upload backups, inventory with private identifiers, or credentials to GitHub.

## Stage-only installer

Generate with: python deployment/build_cloud_config.py --ubuntu COMMIT MANIFEST_SHA256
Use a product commit whose 70-entry manifest matches raw bytes. The cloud-config is
excluded from that manifest. Pin the generated cloud-config in a later packaging commit;
never hash a manifest into itself or guess a future commit SHA.
The generated User Data must pin the product commit and its manifest blob, verified
by release_checks/verify_packaging.py. A branch name or a base SHA is not a release pin.

Ubuntu uses a distinct /root/poa-stage-input.json; the legacy credential-bearing
/root/poa-install-input.json is never overwritten by this User Data.

No secrets belong in User Data or cloud-init output. Provision secret configuration
later via an operator-controlled root-only local secure channel. No real credentials
are loaded during staging. Existing OS/PocketBase identity is not invented.

Ubuntu install: exact Git commit check, all manifest hashes AND sizes, Python 3.10 venv,
pip check/import + real Ed25519 self-test, official pinned PocketBase ZIP checksum,
independent release_checks tests (pytest 9.1.1/PyYAML 6.0.3 test tools only), clean tracked Git bytes,
final manifest recheck. No post-verification source patch. No PocketBase bootstrap,
.env creation, trading Webhook, live-service marker, service start or enable.
POA/PocketBase unit candidates are inert .staged files with ExecStartPre=/usr/bin/false;
Caddy stays masked. Runtime masks prevent app start while staging. No activation
command is supplied in this PR. Removing an interlock is a separate approved operation,
not proof that restoration or account/permission checks succeeded.

OPERATING_READY transition requires server-specific external restore evidence,
configuration/permission checks, safe pending-state review, an approved activation
procedure and explicit operator approval. This task does not implement or execute
that transition. Fresh servers still require explicit no-prior-state inventory and
account/ownership/credential review; new disks do not prove absence of prior trading state.

Ubuntu staging pins PocketBase 0.22.48 and the Python SDK is 0.12.2. Existing operational
0.16.6 databases are never started or migrated by staging. Real restore, log preservation,
Go-binary security review and safe rollback remain separate operating gates; synthetic
API/upgrade tests do not establish them. Local tests do not establish 512MB VPS capacity,
203-strategy concurrency, real exchange permission or real Discord delivery.
