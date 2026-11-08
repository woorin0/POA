# POA Ubuntu 22.04 single User Data — operational candidate

**Purpose:** a newly created Vultr Ubuntu 22.04 amd64 instance, not an OS reinstall of BOT03.

## Implementation and safety contract

- Cloud-init installs the fixed POA checkout, Python 3.10 environment and staged PocketBase via the existing verified Ubuntu22 initializer. It downloads *only five pinned helper scripts* at a fixed commit, verifies their SHA256 before execution, and checks all 72 original release-manifest items.
- User Data never includes keys, real DB files, webhook credentials or Discord secrets and never starts the POA app, PocketBase, Caddy or orders during provisioning.
- New **local operator helper**: `sudo poa-setup fresh --config /root/poa-config.json` selects PocketBase **0.16.6**, used by the original first-admin/collection schema bootstrap, constructs `.env` privately, then starts PocketBase and POA on **127.0.0.1:8090/8000**, checks `/api/health` and `/hi`, and enables those two services. The previously staged PocketBase 0.22.48 binary is preserved separately, not silently migrated.
- If the local startup is interrupted **after** creation of `/root/POA/LOCAL_READY`, `sudo poa-setup resume-local` retries PocketBase/POA local services with the **existing matching unit files and original DB/API keys**, without re-creating an admin, migrating databases or exposing the public order endpoint. If the marker is absent or service units differ, the command refuses to run; inspect local logs rather than deleting data or rerunning `fresh`.
- No public webhook is opened during `fresh`. After the operator has verified service health and DNS, `sudo poa-setup publish --domain <YOUR_VALID_DOMAIN>` is the separate deliberate exposure step; it backs up the packaged Caddyfile, verifies Caddy syntax and starts Caddy. **This exposes a live order endpoint. Do not run it until you intend to accept real TradingView orders.**
- On a restore, use `sudo poa-setup restore --backup /root/poa-offline-backup`. Source backup is a private `app` and `db` directory tree containing `app/.env`, `app/notifications.sqlite3`, optional `app/safe_orders.sqlite3`, `db/pocketbase`, and `db/pb_data/data.db`. The backup must be *consistently captured while old DB writers are quiesced* and WAL/SHM checkpointed. The helper validates SQLite on the offline copy, copies protected DB and secrets without printing them, and **preserves the original matching PocketBase executable**. It does not start restored services automatically. Follow with `sudo poa-setup activate-restored` after confirming the matching DB and account credentials. Extra external DB/ledger paths must be inventoried and preserved separately; the helper blocks unexpected backup entries rather than discard them.
- Any previous POA installation at `/root/POA`, `/root/db` or service path causes the original Stage install to refuse. **Never apply this User Data using Vultr Reinstall Server on BOT03.**

## Steps for a new server

1. In Vultr, choose **New Deploy**, Ubuntu 22.04 amd64. Paste the *single generated* `deployment/ubuntu22_turnkey_USER_DATA.yaml` as Cloud-Init User Data. This does NOT copy existing BOT03 state.
2. Check:
   ```bash
   sudo cloud-init status --wait
   sudo cat /root/POA_INSTALL_RESULT.json
   ```
   Expect `STAGED_NOT_LIVE` and pinned revision. A cloud-init failure is not a successful install.
3. **Fresh**, with no prior order history: create an operator-only 0600 JSON file based on `/root/poa-config.example.json`:
   ```bash
   sudo cp /root/poa-config.example.json /root/poa-config.json
   sudo nano /root/poa-config.json
   sudo chmod 600 /root/poa-config.json
   sudo poa-setup fresh --config /root/poa-config.json
   curl --fail http://127.0.0.1:8000/hi
   curl --fail http://127.0.0.1:8090/api/health
   sudo systemctl is-active poa pocketbase
   ```
   If local startup was interrupted after PocketBase initialization, run `sudo poa-setup resume-local`; first check `sudo journalctl -u poa -u pocketbase -n 80 --no-pager` to diagnose failures. This never substitutes for a missing database or initial setup.
   Replace `domain`, `PASSWORD`, `DISCORD_WEBHOOK_URL`, and at least one exchange's API key+secret. Never upload keys to GitHub/chat or type them in Vultr User Data. Both local services stay unexposed publicly.
4. **Restore** instead of fresh: securely transfer an existing quiesced private backup into `/root/poa-offline-backup` and run:
   ```bash
   sudo poa-setup restore --backup /root/poa-offline-backup
   sudo poa-setup activate-restored
   ```
   Do **not** initialize a new PocketBase admin schema over a restored DB.
5. Optional **real webhook activation** only after explicit operator decision, your domain's A/AAAA DNS configured and trading state reconciled:
   ```bash
   sudo poa-setup publish --domain your.domain.example
   ```
   This is a real operational activation action; **not executed by CI or ChatGPT**.

## Testing distinction

- GitHub Windows/Ubuntu CI: script assembly, SHA-pinned cloud-init, synthetic full restore/secret preservation, negative WAL/corruption, mock service startup, Caddy publication, unchanged source/manifest and original finance/Golden/four-venue Mock E2E.
- GitHub CI does **not** install onto real Vultr hardware, transfer the operating DB/API keys, prove PocketBase legacy binary against a particular user's DB, validate DNS/TLS externally, nor send a real order.
- No PR merge, operating deployment, or real order is included.

**Ready verdict:** source and installer path can be CI validated; external live readiness still depends on user-provided backups/secrets and the actual Vultr host smoke test. 
