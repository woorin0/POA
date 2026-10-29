"""Fresh Debian 12 / Ubuntu 22.04 amd64 provisioning; never overwrite an old app."""
import hashlib
import json
import os
import ipaddress
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

INPUT = Path('/root/poa-install-input.json')
APP = Path('/root/POA')
DB = Path('/root/db')
ENV_KEYS = ('PASSWORD', 'BINANCE_KEY', 'BINANCE_SECRET', 'BYBIT_KEY', 'BYBIT_SECRET',
            'OKX_KEY', 'OKX_SECRET', 'OKX_PASSPHRASE', 'UPBIT_KEY', 'UPBIT_SECRET',
            'BITGET_KEY', 'BITGET_SECRET', 'BITGET_PASSPHRASE', 'DISCORD_WEBHOOK_URL')
FLAGS = {'VERIFIED_DISCORD_ALERTS': True, 'VERIFIED_FILL_PRICE': True,
         'DISCORD_OUTBOX_ENABLED': True, 'DISCORD_RETRY_ENABLED': True,
         'EXIT_REASON_CLASSIFICATION_V2': True, 'UPBIT_VERIFIED_ALERTS': True,
         'NET_REALIZED_PNL_ENABLED': False, 'UPBIT_NET_PNL_ENABLED': False,
         'VERIFIED_COST_BASIS_COMPLETE': False, 'PARTIAL_FILL_ALERTS': False,
         'UPBIT_PARTIAL_FILL_ALERTS': False, 'EXCHANGE_NATIVE_TPSL_MONITOR': False}


class InstallError(Exception):
    pass



def upbit_read_configuration(data, values):
    """Pure config builder. Operator binding, never exchange identity certification."""
    config = data.get('upbit_estimated_read', {'enabled': True, 'credential_mode': 'simple'})
    if not isinstance(config, dict) or type(config.get('enabled', False)) is not bool:
        raise InstallError('UPBIT_READ_CONFIG_INVALID')
    env = {'POA_UPBIT_ESTIMATED_READ_ENABLED': False,
           'POA_UPBIT_READ_CREDENTIAL_MODE': 'disabled',
           'POA_UPBIT_READ_BINDING_FILE': '/etc/poa/upbit-notification-binding.json',
           'POA_UPBIT_READ_BINDING_SHA256': ''}
    if not config.get('enabled', False):
        return env, None
    if config.get('credential_mode', 'simple') == 'simple':
        key, secret = values.get('UPBIT_KEY'), values.get('UPBIT_SECRET')
        if any(type(v) is not str or not 1 <= len(v) <= 4096 or not v.isascii() or not v.isprintable()
               for v in (key, secret)):
            return env, None # Optional read feature only; no invented identity/config.
        env.update(POA_UPBIT_ESTIMATED_READ_ENABLED=True, POA_UPBIT_READ_CREDENTIAL_MODE='simple',
                   POA_UPBIT_READ_BINDING_FILE='', POA_UPBIT_READ_BINDING_SHA256='')
        return env, None
    try:
        import uuid
        for field in ('account_uuid', 'pocket_uuid'):
            value = config[field]
            if type(value) is not str or str(uuid.UUID(value)) != value:
                raise ValueError()
        strategy = config['strategy']
        if (type(strategy) is not str or not 1 <= len(strategy) <= 128
                or strategy in ('default', '주문') or not strategy.isprintable()
                or re.search(r'(?i)bearer|api.?key|secret|passphrase|webhook|canary|authorization|https?://|%[0-9a-f]{2}', strategy)):
            raise ValueError()
        if config.get('credential_mode') != 'poa_settings':
            raise ValueError()
        permissions = config['permissions']
        if (type(permissions) is not list or 'order_query' not in permissions
                or len(set(permissions)) != len(permissions)
                or not set(permissions).issubset({'order_query', 'order'})):
            raise ValueError()
        ips = config['allowed_ips']
        if type(ips) is not list or not 1 <= len(ips) <= 8:
            raise ValueError()
        for address in ips:
            ipaddress.ip_address(address)
        evidence = config['evidence']
        if type(evidence) is not dict or set(evidence) != {'source', 'reference', 'digest', 'expires_at'}:
            raise ValueError()
        for field in ('source', 'reference'):
            v = evidence[field]
            if type(v) is not str or not 1 <= len(v) <= 256 or not v.isprintable():
                raise ValueError()
        if not re.fullmatch('[0-9a-f]{64}', evidence['digest']):
            raise ValueError()
        expiry = evidence['expires_at']
        if type(expiry) not in (int, float) or not time.time() < expiry < time.time() + 366 * 86400:
            raise ValueError()
        key, secret = values.get('UPBIT_KEY'), values.get('UPBIT_SECRET')
        if any(type(v) is not str or not 1 <= len(v) <= 4096 or not v.isascii() or not v.isprintable()
               for v in (key, secret)):
            raise ValueError()
        ref = hashlib.sha256(json.dumps(('UPBIT', key), ensure_ascii=True,
                                       separators=(',', ':')).encode()).hexdigest()
        binding = {'format': 'POA_UPBIT_LINUX_BINDING_V1',
                   'config': {'account_uuid': config['account_uuid'], 'pocket_uuid': config['pocket_uuid'],
                              'source_account_ref': ref, 'strategy': strategy,
                              'read_key_fingerprint': hashlib.sha256(key.encode()).hexdigest(),
                              'producer': 'POA_MAIN_LOG_V1'},
                   'evidence': evidence, 'permissions': permissions,
                   'key_usage': 'POA_SETTINGS_SHARED_TRADING_KEY', 'allowed_ips': ips}
        content = json.dumps(binding, ensure_ascii=False, separators=(',', ':'))
        if len(content.encode()) > 8192:
            raise ValueError()
        env.update(POA_UPBIT_ESTIMATED_READ_ENABLED=True,
                   POA_UPBIT_READ_CREDENTIAL_MODE='poa_settings',
                   POA_UPBIT_READ_BINDING_SHA256=hashlib.sha256(content.encode()).hexdigest())
        return env, content
    except Exception:
        raise InstallError('UPBIT_READ_BINDING_REQUIRED_OR_INVALID') from None


def validate(data):
    domain = data.get('domain', '')
    if (not isinstance(domain, str) or not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?', domain)
            or '.' not in domain or '..' in domain or domain.endswith(('.example', '.invalid'))
            or domain == 'example.com' or any(len(p) > 63 or p.startswith('-') or p.endswith('-') for p in domain.split('.'))):
        raise InstallError('DOMAIN_REQUIRED')
    values = data.get('credentials')
    if not isinstance(values, dict):
        raise InstallError('CREDENTIALS_REQUIRED')
    for key in ENV_KEYS:
        value = values.get(key, '')
        if not isinstance(value, str) or any(c in value for c in '\n\r\x00') or '${' in value:
            raise InstallError('INVALID_CREDENTIAL_FORMAT')
        if value.startswith(('CHANGE_ME', 'YOUR_')):
            raise InstallError('REPLACE_PLACEHOLDERS')
    if not values.get('PASSWORD'):
        raise InstallError('WEBHOOK_PASSWORD_REQUIRED')
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise InstallError('DOMAIN_REQUIRED')
    from urllib.parse import urlsplit
    try:
        url = urlsplit(values.get('DISCORD_WEBHOOK_URL', ''))
        port = url.port
    except ValueError:
        raise InstallError('DISCORD_WEBHOOK_REQUIRED') from None
    if (url.scheme != 'https' or url.hostname not in ('discord.com', 'discordapp.com')
            or url.username or url.password or port or url.query or url.fragment
            or not re.fullmatch(r'/api/webhooks/\d+/[A-Za-z0-9_-]+', url.path)):
        raise InstallError('DISCORD_WEBHOOK_REQUIRED')
    configured = False
    for ex in ('BINANCE', 'BYBIT', 'OKX', 'UPBIT', 'BITGET'):
        key, secret = values.get(ex + '_KEY'), values.get(ex + '_SECRET')
        if bool(key) != bool(secret):
            raise InstallError('INCOMPLETE_EXCHANGE_CREDENTIALS')
        if key:
            configured = True
            if ex in ('OKX', 'BITGET') and not values.get(ex + '_PASSPHRASE'):
                raise InstallError('EXCHANGE_PASSPHRASE_REQUIRED')
    if not configured:
        raise InstallError('EXCHANGE_CREDENTIALS_REQUIRED')
    ref = data.get('repo_ref', 'main')
    if not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}', ref) or '..' in ref:
        raise InstallError('INVALID_REPOSITORY_REF')
    email = data.get('db_admin_email', 'poa-admin@example.com')
    if not isinstance(email, str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise InstallError('INVALID_DB_ADMIN_EMAIL')
    expected = data.get('release_manifest_sha256')
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected)):
        raise InstallError('INVALID_RELEASE_MANIFEST_DIGEST')
    upbit_read_configuration(data, values)
    return values


def run(args, code, cwd=None, timeout=300):
    # Captured command output is not printed: API tokens must never enter logs.
    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
    if result.returncode:
        raise InstallError(code)
    return result.stdout


def write_private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)


def api(path, data=None, token=None):
    request = Request('http://127.0.0.1:8090' + path,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/json', **({'Authorization': token} if token else {})})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def bootstrap_db(email, password, request=api):
    # v0.16.6 permits first-admin creation only while there are no admins.
    request('/api/admins', {'email': email, 'password': password, 'passwordConfirm': password})
    auth = request('/api/admins/auth-with-password', {'identity': email, 'password': password})
    token = auth['token']
    fields = [{'id': hashlib.sha256(name.encode()).hexdigest()[:8], 'name': name,
               'type': 'number' if name == 'amount' else 'text', 'required': True,
               'unique': False, 'system': False,
               'options': {'min': None, 'max': None, **({'noDecimal': False} if name == 'amount' else {'pattern': ''})}}
              for name in ('exchange', 'base', 'quote', 'amount')]
    request('/api/collections', {'name': 'kimp', 'type': 'base', 'schema': fields,
                                'listRule': None, 'viewRule': None, 'createRule': None,
                                'updateRule': None, 'deleteRule': None}, token)
    check = request('/api/collections/kimp', token=token)
    if {f['name']: f['type'] for f in check['schema']} != {f['name']: f['type'] for f in fields}:
        raise InstallError('DB_SCHEMA_CHECK_FAILED')


def wait_ready(check, code, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, URLError, HTTPError, ValueError):
            pass
        time.sleep(1)
    raise InstallError(code)


def local_https(domain):
    # Verify the actual local Caddy certificate; never POST a trading webhook.
    with socket.create_connection(('127.0.0.1', 443), timeout=5) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=domain) as tls:
            tls.sendall(('GET / HTTP/1.1\r\nHost: ' + domain + '\r\nConnection: close\r\n\r\n').encode())
            return tls.recv(128).startswith(b'HTTP/1.1 403')


def verify_source(expected_manifest=None):
    path = APP / 'release_manifest_sha256.json'
    if not path.is_file():
        raise InstallError('UPLOAD_VERIFIED_GITHUB_ZIP_FIRST')
    raw = path.read_bytes()
    if expected_manifest and hashlib.sha256(raw).hexdigest().lower() != expected_manifest.lower():
        raise InstallError('RELEASE_MANIFEST_MISMATCH_USE_MATCHING_APP_ZIP')
    entries = json.loads(raw.decode('utf-8'))
    if not isinstance(entries, list) or not entries:
        raise InstallError('SOURCE_MANIFEST_INVALID')
    seen = set()
    for entry in entries:
        name = entry['Path']
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.\-/]{1,200}', name)
                or name in seen or name.startswith('/') or '..' in name.split('/')):
            raise InstallError('SOURCE_MANIFEST_INVALID')
        seen.add(name)
        file = (APP / name).resolve()
        if not file.is_relative_to(APP.resolve()) or not file.is_file():
            raise InstallError('SOURCE_MANIFEST_INVALID')
        if hashlib.sha256(file.read_bytes()).hexdigest().upper() != entry['SHA256']:
            raise InstallError('SOURCE_HASH_MISMATCH:' + name)
    if expected_manifest and not {'main.py', 'run.py', 'requirements.txt'}.issubset(seen):
        raise InstallError('SOURCE_MANIFEST_MISSING_APP_FILES')


def runtime_for_os(content):
    fields = dict(line.split('=', 1) for line in content.splitlines() if '=' in line)
    identity = (fields.get('ID', '').strip('"'), fields.get('VERSION_ID', '').strip('"'))
    supported = {('debian', '12'): 'python3.11', ('ubuntu', '22.04'): 'python3.10'}
    if identity not in supported:
        raise InstallError('SUPPORTED_OS_REQUIRED_DEBIAN12_OR_UBUNTU2204')
    return supported[identity]


def install(data):
    values = validate(data)
    if os.geteuid() != 0 or APP.exists() or DB.exists():
        raise InstallError('NEW_SERVER_ONLY')
    os_release = Path('/etc/os-release').read_text()
    runtime = runtime_for_os(os_release)
    if run(['dpkg', '--print-architecture'], 'ARCH_CHECK_FAILED').strip() != b'amd64':
        raise InstallError('AMD64_REQUIRED')
    owned = False
    try:
        print('POA_INSTALL_STAGE=SOURCE', flush=True)
        run(['git', 'init', str(APP)], 'GIT_INIT_FAILED')
        owned = True
        run(['git', '-C', str(APP), 'config', 'core.autocrlf', 'false'], 'GIT_EOL_CONFIG_FAILED')
        run(['git', '-C', str(APP), 'remote', 'add', 'origin', 'https://github.com/woorin0/POA.git'], 'GIT_REMOTE_FAILED')
        run(['git', '-C', str(APP), 'fetch', '--depth', '1', 'origin', data.get('repo_ref', 'main')], 'GIT_FETCH_FAILED')
        run(['git', '-C', str(APP), 'checkout', '--detach', 'FETCH_HEAD'], 'GIT_CHECKOUT_FAILED')
        verify_source(data.get('release_manifest_sha256'))
        commit = run(['git', '-C', str(APP), 'rev-parse', 'HEAD'], 'COMMIT_CHECK_FAILED').decode().strip()
        print('POA_INSTALL_STAGE=DEPENDENCIES', flush=True)
        run([runtime, '-m', 'venv', str(APP / '.venv')], 'VENV_FAILED')
        python = str(APP / '.venv/bin/python')
        run([python, '-m', 'pip', 'install', '--no-cache-dir', '-r', str(APP / 'requirements.txt')], 'REQUIREMENTS_INSTALL_FAILED', timeout=1800)
        run([python, '-m', 'pip', 'check'], 'PIP_CHECK_FAILED')
        password = secrets.token_urlsafe(32)
        email = data.get('db_admin_email', 'poa-admin@example.com')
        read_env, binding = upbit_read_configuration(data, values)
        if binding is not None:
            parent = Path('/etc/poa')
            parent.mkdir(mode=0o700, exist_ok=True)
            if parent.is_symlink() or parent.stat().st_mode & 0o077 or parent.stat().st_uid != 0:
                raise InstallError('UPBIT_BINDING_DIRECTORY_INSECURE')
            write_private(parent / 'upbit-notification-binding.json', binding)
        env = {**{key: values.get(key, '') for key in ENV_KEYS}, 'DB_ID': email, 'DB_PASSWORD': password,
               'PORT': 8000, 'WHITELIST': ['127.0.0.1'], **FLAGS, **read_env,
               'VERIFIED_NOTIFICATIONS_DB_PATH': str(APP / 'notifications.sqlite3')}
        write_private(APP / '.env', ''.join(key + '=' + (json.dumps(value, ensure_ascii=False) if isinstance(value, str)
                      else json.dumps(value)) + '\n' for key, value in env.items()))
        run([runtime, '/usr/local/lib/poa/fix_alerts.py', str(APP)], 'CLOCK_PATCH_FAILED')
        run([python, '/usr/local/lib/poa/validate_config.py'], 'CONFIG_CHECK_FAILED')
        print('POA_INSTALL_STAGE=DATABASE', flush=True)
        DB.mkdir(mode=0o700)
        archive = DB / 'pocketbase.zip'
        run(['curl', '--fail', '--location', '--retry', '3', '--output', str(archive),
             'https://github.com/pocketbase/pocketbase/releases/download/v0.16.6/pocketbase_0.16.6_linux_amd64.zip'], 'POCKETBASE_DOWNLOAD_FAILED')
        with zipfile.ZipFile(archive) as z:
            (DB / 'pocketbase').write_bytes(z.read('pocketbase'))
        (DB / 'pocketbase').chmod(0o700)
        archive.unlink()
        run(['systemctl', 'daemon-reload'], 'SYSTEMD_RELOAD_FAILED')
        run(['systemctl', 'start', 'pocketbase'], 'POCKETBASE_START_FAILED')
        wait_ready(lambda: api('/api/health').get('code') == 200, 'POCKETBASE_NOT_READY')
        bootstrap_db(email, password)
        print('POA_INSTALL_STAGE=WEBHOOK', flush=True)
        (APP / 'ENABLE_LIVE_SERVICE').touch(mode=0o600)
        run(['systemctl', 'start', 'poa'], 'POA_START_FAILED')
        def poa_ready():
            with urlopen('http://127.0.0.1:8000/hi', timeout=5) as response:
                return json.load(response) == 'hi!!'
        wait_ready(poa_ready, 'POA_NOT_READY')
        print('POA_INSTALL_STAGE=HTTPS', flush=True)
        domain = data['domain']
        caddy = domain + ''' {
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
'''
        Path('/etc/caddy/Caddyfile').write_text(caddy)
        run(['/usr/bin/caddy', 'validate', '--config', '/etc/caddy/Caddyfile'], 'CADDY_CONFIG_FAILED')
        for port in ('22/tcp', '80/tcp', '443/tcp'):
            run(['ufw', 'allow', port], 'FIREWALL_FAILED')
        run(['ufw', '--force', 'enable'], 'FIREWALL_FAILED')
        run(['systemctl', 'unmask', 'caddy'], 'CADDY_UNMASK_FAILED')
        run(['systemctl', 'start', 'caddy'], 'CADDY_START_FAILED')
        wait_ready(lambda: local_https(domain), 'HTTPS_NOT_READY_CHECK_DNS', seconds=180)
        run(['systemctl', 'enable', 'pocketbase', 'poa', 'caddy'], 'AUTOSTART_FAILED')
        for service in ('pocketbase', 'poa', 'caddy'):
            run(['systemctl', 'is-active', '--quiet', service], 'SERVICE_HEALTH_FAILED')
        write_private(Path('/root/POA_INSTALL_RESULT.json'), json.dumps(
            {'status': 'RUNNING', 'commit': commit, 'webhook_url': 'https://' + domain + '/order',
             'python': run([python, '--version'], 'PYTHON_VERSION_FAILED').decode().strip()}, indent=2))
        print('POA_INSTALL_SUCCESS', flush=True)
    except Exception:
        if owned:
            # Keep all journals and databases; only stop this new installation.
            for service in ('caddy', 'poa', 'pocketbase'):
                try:
                    run(['systemctl', 'stop', service], 'CLEANUP_STOP_FAILED', timeout=150)
                    run(['systemctl', 'disable', service], 'CLEANUP_DISABLE_FAILED')
                except Exception:
                    print('POA_INSTALL_CLEANUP_REQUIRES_REVIEW', flush=True)
            (APP / 'ENABLE_LIVE_SERVICE').unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    os.umask(0o077)
    try:
        install(json.loads(INPUT.read_text(encoding='utf-8')))
    except InstallError as error:
        # Only codes raised by this file; never dump command, request or secrets.
        print('POA_INSTALL_FAILED=' + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('POA_INSTALL_FAILED=UNEXPECTED_FAILURE', file=sys.stderr)
        sys.exit(1)
