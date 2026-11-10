# POA Vultr Ubuntu 22.04 — first-boot automatic installation

Use **`deployment/POA_VULTR_ONECLICK_USER_DATA.yaml`** for a completely NEW Ubuntu 22.04 amd64 Vultr instance. This is the recommended one-click file, replacing the stage-only instructions for new deployments.

## Ten-server deployment: edit [1] only

Use the **same** User Data file separately for each of the 10 **new** Vultr Ubuntu 22.04 amd64 instances. The editable `[1] 서버별 설정` section is at the top. Do not change the auto-install scripts in section [2].

- **`domain: "SET_EXISTING_DOMAIN"`** — Required: replace it with **that server's existing FQDN only**, such as `bot01.example.com`. No `https://`, `/order` or port. `AUTO` is **not** the default for 10-server reuse.
- **TradingView webhook URL:** Exactly `https://<configured-domain>/order`. A separate webhook URL input is unnecessary; Caddy's routing is generated from `domain`. No automatic discovery of existing domains or DNS update is performed.
- **`PASSWORD`** — To preserve an existing TradingView alert *including its JSON password*, set exactly the existing per-server order password. `AUTO` generates a **different** password and therefore existing alert messages must be updated.
- **`DISCORD_WEBHOOK_URL`** — The actual private Discord URL used to send results; **not** the TradingView callback URL.
- **Exchange credentials** — Fill one or more matching `*_KEY` and `*_SECRET` pairs. Use `*_PASSPHRASE` for OKX/Bitget. Unused exchange fields remain `""`. You need at least one configured exchange and a valid Discord webhook for full automatic startup.

For server `bot01.example.com`, the installed webhook is `https://bot01.example.com/order`. For server `bot02.example.com`, it is `https://bot02.example.com/order`, and so on; these are **examples, not known domains**. Existing alerts can retain their URL when each configured domain, DNS target, webhook path, and order password match.

Each existing domain's DNS A record **must point to that new server's public IPv4** before Caddy can finish its HTTPS certificate and startup check. Moving a domain from an old server to a replacement server is a DNS change performed by the operator, not by User Data. Installing on a new server does **not** copy existing BOT03 SQLite databases, PocketBase collections, order history, or credentials from any older server.

**Credentials entered in cloud-init are sent to Vultr and retained in instance user-data metadata.** Use this one-click tradeoff only if acceptable. They are never committed to GitHub by the installer or printed to stdout. Do not share the completed User Data. The running app keeps its secrets in root-only `.env`.

## Server creation

1. Vultr → Deploy New Server → Ubuntu 22.04 x64/amd64. Use a **new instance**, not Reinstall of existing BOT03.
2. Enable Cloud-Init User Data. Paste the entire `POA_VULTR_ONECLICK_USER_DATA.yaml` after setting the per-server domain, webhook/password and necessary exchange keys in the first block.
3. Deploy. First boot runs the original pinned Stage, initializes PocketBase 0.16.6, creates the POA `.env` and first admin, starts `pocketbase.service` and `poa.service` on localhost and publishes the TradingView webhook through HTTPS using Caddy. There is **no additional setup command**, and provisioning never sends an exchange order. Live TradingView orders may be accepted *after* the webhook is published and an alert with the correct password is sent.

## Verify the installation

Run in Vultr Console/SSH:

```bash
sudo cloud-init status --wait
sudo cat /root/POA_ONECLICK_RESULT.json
sudo systemctl is-active poa pocketbase caddy
curl -fsS http://127.0.0.1:8000/hi
curl -fsS http://127.0.0.1:8090/api/health
```

Success: cloud-init status `done`; report `state=RUNNING_HTTPS`, valid `webhook_url`; services `active`, POA `"hi!!"`, PocketBase `code=200`. GET `/` on the public HTTPS address intentionally returns HTTP 403: POA is an **API trading webhook, not a web-dashboard UI**. The external valid endpoint for TradingView is the report's `webhook_url` ending in `/order`. Caddy's IP allowlist additionally restricts orders to configured TradingView sender IPs.

Read the generated webhook password locally (avoid sharing output):

```bash
sudo grep '^PASSWORD=' /root/POA/.env
```

TradingView alert content/strategy configuration still must match the existing POA order schema; merely opening the address in a browser cannot place an order.

If the report does not exist or cloud-init reports an error, inspect:

```bash
sudo cloud-init status --long
sudo tail -n 100 /var/log/cloud-init-output.log
sudo journalctl -u poa -u pocketbase -u caddy -n 80 --no-pager
```

No deletion, reinstallation, repeated `fresh` or DB migration is automatically performed after failure. If the failure occurs after initial DB bootstrap and creation of `/root/POA/LOCAL_READY`, `sudo poa-setup resume-local` can retry local services without re-creating the DB. DNS, certificate issuance, network access, provider status and API key validity are external and can still prevent a complete deployment; no real Vultr environment has been tested by GitHub Mock E2E.

## Existing BOT03 data

The automatic **fresh** path does not import BOT03 data. To preserve old trade state, do not apply the one-click User Data to the old server. Use the existing stage-only deployment and `poa-setup restore --backup ...` with a consistent offline backup instead. The new-server flow leaves the old server unchanged.
