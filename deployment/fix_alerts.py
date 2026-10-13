"""Offline local-file update. No server restart, network, order or Discord calls.

Run with the service stopped: python3 fix_alerts.py /root/POA --enable-alerts
Existing .env fields and DB files are preserved. Backups contain secrets: keep private.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import shutil


def apply(root, enable=False):
    root = Path(root).resolve()
    target = root / 'exchange/utility/LogMaker.py'
    original = target.read_text(encoding='utf-8')
    old = 'datetime.utcnow().timestamp()'
    new = 'datetime.now(timezone.utc).timestamp()'
    if original.count(old) not in (0, 3):
        raise SystemExit('Unexpected source version; review manually. No files changed.')
    if old not in original and original.count(new) != 3:
        raise SystemExit('Unsupported source layout. No files changed.')
    compile(original.replace(old, new), str(target), 'exec')
    env_path = root / '.env'
    settings = {
        'VERIFIED_DISCORD_ALERTS': 'True', 'VERIFIED_FILL_PRICE': 'True',
        'DISCORD_OUTBOX_ENABLED': 'True', 'DISCORD_RETRY_ENABLED': 'True',
        'EXIT_REASON_CLASSIFICATION_V2': 'True', 'UPBIT_VERIFIED_ALERTS': 'True',
    }
    if enable:
        schema = (root / 'exchange/model/schemas.py').read_text(encoding='utf-8')
        if not all(re.search(r'^    ' + key + r'\s*:', schema, re.M) for key in settings):
            raise SystemExit('Installed code does not support required alert flags.')
        if not (root / 'exchange/verified_pnl/dispatcher.py').is_file() or not env_path.is_file():
            raise SystemExit('Missing verification module or .env; no files changed.')
        env = env_path.read_text(encoding='utf-8')
        for key, value in settings.items():
            pattern = r'^\s*(?:export\s+)?' + key + r'\s*=.*$'
            if re.search(pattern, env, re.M):
                env = re.sub(pattern, key + '=' + value, env, flags=re.M)
            else:
                env = env.rstrip() + '\n' + key + '=' + value + '\n'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = root / ('alert-fix-backup-' + stamp)
    backup.mkdir(mode=0o700)
    shutil.copy2(target, backup / 'LogMaker.py')
    if enable:
        shutil.copy2(env_path, backup / '.env')
        os.chmod(backup / '.env', 0o600)
    def write(path, content, mode):
        temp = path.with_name(path.name + '.alert-fix-' + stamp)
        try:
            with temp.open('x', encoding='utf-8', newline='\n') as f:
                os.chmod(temp, mode)
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()
    write(target, original.replace(old, new), target.stat().st_mode & 0o777)
    if enable:
        write(env_path, env, 0o600)
    print('Updated notification clock' + (' and six alert flags.' if enable else '.'))
    print('Private backup directory:', backup)
    print('No service started or restarted; no DB files modified.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    parser.add_argument('--enable-alerts', action='store_true')
    args = parser.parse_args()
    apply(args.root, args.enable_alerts)
