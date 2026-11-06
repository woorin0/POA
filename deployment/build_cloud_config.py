"""Generate a secret-free cloud-config for a NEW Debian 12 amd64 server only."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

def build_ubuntu(commit, manifest):
    """No secrets, no live activation, no code patching after verification."""
    import re
    if not re.fullmatch('[0-9a-f]{40}', commit) or not re.fullmatch('[0-9a-f]{64}', manifest):
        raise ValueError('exact_release_pin_required')
    config = dict(package_update=False, timezone='UTC',
                  packages=['python3', 'ca-certificates'], write_files=[],
                  runcmd=[['bash', '-euc', 'umask 077; python3 /usr/local/lib/poa/prepare_ubuntu.py; python3 /usr/local/lib/poa/install_once.py']])
    def put(path, content, mode='0600'):
        config['write_files'].append(dict(path=path, owner='root:root', permissions=mode, content=content))
    put('/root/poa-stage-input.json', json.dumps(dict(install_mode='NEW_SERVER_STAGE_ONLY', repo_ref=commit, release_manifest_sha256=manifest), indent=2))
    for name in ('install_once.py', 'prepare_ubuntu.py'):
        put('/usr/local/lib/poa/'+name, (HERE/name).read_text(encoding='utf-8'))
    # Units are masked by preparation. No enable marker or credentials are shipped.
    for unit, working, command in (
        ('poa', '/root/POA', '/root/POA/.venv/bin/python run.py --host=127.0.0.1 --port=8000'),
        ('pocketbase', '/root/db', '/root/db/pocketbase serve --http=127.0.0.1:8090')):
        # No automatic path to OPERATING_READY. An approved external restore and
        # configuration review must replace this staging interlock separately.
        put('/usr/local/lib/poa/'+unit+'.service.staged',
            '[Unit]\nDescription=POA staged only\nConditionPathExists=/etc/poa/RESTORE_VERIFIED\n'
            '[Service]\nType=simple\nUser=root\nUMask=0077\nWorkingDirectory='+working+'\n'
            'ExecStartPre=/usr/bin/false\nExecStart='+command+'\nRestart=no\n')
    config['final_message'] = 'STAGED_NOT_LIVE only. User Data cannot preserve pre-reinstall data. RESTORE_VERIFIED=NO OPERATING_READY=NO'
    return '#cloud-config\n'+json.dumps(config, ensure_ascii=False, indent=2)+'\n'


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--ubuntu':
        if len(sys.argv) != 4:
            raise SystemExit('usage: build_cloud_config.py --ubuntu COMMIT MANIFEST_SHA256')
        (HERE/'cloud-config-ubuntu2204.yaml').write_text(build_ubuntu(sys.argv[2], sys.argv[3]), encoding='utf-8', newline='\n')
        raise SystemExit(0)

files = []
def add(path, content, permissions='0644'):
    files.append(dict(path=path, owner='root:root', permissions=permissions, content=content))

add('/usr/local/lib/poa/fix_alerts.py', (HERE/'fix_alerts.py').read_text(encoding='utf-8'))
add('/usr/local/lib/poa/validate_config.py', '''from dotenv import dotenv_values
from urllib.parse import urlsplit
from pathlib import Path
import sys
values = dotenv_values('/root/POA/.env')
required = ('PASSWORD', 'DB_ID', 'DB_PASSWORD', 'DISCORD_WEBHOOK_URL')
ok = all(values.get(key) for key in required)
url = urlsplit(values.get('DISCORD_WEBHOOK_URL') or '')
ok = ok and url.scheme == 'https' and url.hostname in ('discord.com', 'discordapp.com')
ok = ok and any(values.get(ex + '_KEY') and values.get(ex + '_SECRET')
                for ex in ('BINANCE', 'BYBIT', 'OKX', 'UPBIT', 'BITGET'))
ok = ok and not (Path('/root/POA/.env').stat().st_mode & 0o077)
if not ok:
    print('POA_CONFIGURATION_INCOMPLETE_OR_INSECURE; inspect .env locally', file=sys.stderr)
    sys.exit(1)
''')
add('/root/poa.env.template', '''# Fill locally after provisioning; NEVER put real keys in cloud-init user-data.
PASSWORD=""
BINANCE_KEY=""
BINANCE_SECRET=""
BYBIT_KEY=""
BYBIT_SECRET=""
OKX_KEY=""
OKX_SECRET=""
OKX_PASSPHRASE=""
UPBIT_KEY=""
UPBIT_SECRET=""
BITGET_KEY=""
BITGET_SECRET=""
BITGET_PASSPHRASE=""
DISCORD_WEBHOOK_URL=""
DB_ID=""
DB_PASSWORD=""
WHITELIST=["127.0.0.1"]
PORT=8000
VERIFIED_DISCORD_ALERTS=True
VERIFIED_FILL_PRICE=True
DISCORD_OUTBOX_ENABLED=True
DISCORD_RETRY_ENABLED=True
EXIT_REASON_CLASSIFICATION_V2=True
UPBIT_VERIFIED_ALERTS=True
NET_REALIZED_PNL_ENABLED=False
UPBIT_NET_PNL_ENABLED=False
VERIFIED_COST_BASIS_COMPLETE=False
PARTIAL_FILL_ALERTS=False
UPBIT_PARTIAL_FILL_ALERTS=False
EXCHANGE_NATIVE_TPSL_MONITOR=False
VERIFIED_NOTIFICATIONS_DB_PATH="/root/POA/notifications.sqlite3"
# Order execution flags retain their defaults; this template only enables alerts.
''', '0600')
add('/etc/systemd/system/poa.service', '''[Unit]
Description=POA webhook service
After=network-online.target pocketbase.service
Wants=network-online.target
Requires=pocketbase.service
ConditionPathExists=/root/POA/ENABLE_LIVE_SERVICE
[Service]
Type=simple
User=root
WorkingDirectory=/root/POA
Environment=TZ=UTC
Environment=PYTHONUNBUFFERED=1
UMask=0077
ExecStartPre=/root/POA/.venv/bin/python /usr/local/lib/poa/validate_config.py
ExecStart=/root/POA/.venv/bin/python run.py --host=127.0.0.1 --port=8000
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
[Install]
WantedBy=multi-user.target
''')
add('/etc/systemd/system/pocketbase.service', '''[Unit]
Description=POA PocketBase (legacy-compatible version)
After=network.target
[Service]
Type=simple
User=root
WorkingDirectory=/root/db
UMask=0077
ExecStart=/root/db/pocketbase serve --http=127.0.0.1:8090
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
''')
add('/root/Caddyfile.poa.template', '''# Replace example.com with your domain, then install as /etc/caddy/Caddyfile.
example.com {
    @tradingview {
        remote_ip 52.89.214.238 34.212.75.30 54.218.53.128 52.32.178.7 127.0.0.1
        method POST
        path / /order
    }
    handle @tradingview {
        reverse_proxy 127.0.0.1:8000
    }
    respond 403
}
''')
script = '''set -euo pipefail
umask 077
. /etc/os-release
test "$ID" = debian && test "$VERSION_ID" = 12
test "$(dpkg --print-architecture)" = amd64
test ! -e /root/POA
test ! -e /root/db
systemctl stop caddy
systemctl disable caddy
timedatectl set-ntp true
git clone --branch main --single-branch https://github.com/woorin0/POA.git /root/POA
git -C /root/POA rev-parse HEAD > /root/POA_INSTALLED_COMMIT
python3.11 -m venv /root/POA/.venv
/root/POA/.venv/bin/python -m pip install --no-cache-dir -r /root/POA/requirements.txt
/root/POA/.venv/bin/python -m pip check
install -m 0600 /root/poa.env.template /root/POA/.env
python3.11 /usr/local/lib/poa/fix_alerts.py /root/POA
mkdir -m 0700 /root/db
curl --fail --location --retry 3 --output /root/pocketbase.zip https://github.com/pocketbase/pocketbase/releases/download/v0.16.6/pocketbase_0.16.6_linux_amd64.zip
unzip -j /root/pocketbase.zip pocketbase -d /root/db
chmod 0700 /root/db/pocketbase
# PocketBase creates a fresh pb_data on first start. Never ship an account DB.
mkdir -m 0700 /root/db/pb_data
chmod -R go-rwx /root/db
systemctl daemon-reload
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
printf '%s\\n' 'Provisioning complete. Services NOT enabled or started. Complete credentials and review before activation.'
'''
config = dict(package_update=True, package_upgrade=True, timezone='UTC',
              packages=['git','ca-certificates','curl','unzip','ufw','caddy',
                        'python3.11','python3.11-venv','python3-pip','build-essential',
                        'python3.11-dev','systemd-timesyncd'],
              write_files=files, runcmd=[['bash', '-euc', script]])
# JSON is a YAML subset: no optional YAML library is required to build/validate it.
(HERE/'cloud-config-debian12.yaml').write_text('#cloud-config\n'+json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('Created cloud-config-debian12.yaml (new server only, services stopped).')
