"""Fresh Ubuntu 22.04 only. No credential output; do not run on an existing POA."""
import json
import os
from pathlib import Path
import subprocess
import sys


def command(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=1800, env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})
    if result.returncode:
        raise RuntimeError('PREPARATION_COMMAND_FAILED_' + args[0])
    return result.stdout


def prepare():
    from install_once import runtime_for_os, validate_stage, assert_fresh_stage, STAGE_INPUT
    if os.geteuid() != 0:
        raise RuntimeError('ROOT_REQUIRED')
    if runtime_for_os(Path('/etc/os-release').read_text()) != 'python3.10':
        raise RuntimeError('UBUNTU2204_REQUIRED')
    assert_fresh_stage()
    validate_stage(json.loads(STAGE_INPUT.read_text()))
    if command(['dpkg', '--print-architecture']).strip() != b'amd64':
        raise RuntimeError('AMD64_REQUIRED')
    command(['systemctl', 'mask', '--runtime', 'poa.service', 'pocketbase.service'])
    # Only the Caddy snap is disabled; other snaps and their data are untouched.
    if Path('/snap/caddy/current').exists():
        command(['snap', 'stop', '--disable', 'caddy'])
    command(['systemctl', 'mask', 'caddy.service'])
    command(['apt-get', 'update'])
    command(['apt-get', 'install', '-y', 'git', 'curl', 'ca-certificates', 'gnupg',
             'debian-keyring', 'debian-archive-keyring', 'apt-transport-https',
             'python3.10', 'python3.10-venv', 'python3.10-dev', 'build-essential', 'ufw'])
    # Official Caddy repository, not a Python PPA. Key and list downloads must succeed.
    command(['curl', '--fail', '--location', '--output', '/root/poa-caddy-key.asc',
             'https://dl.cloudsmith.io/public/caddy/stable/gpg.key'])
    command(['gpg', '--batch', '--yes', '--dearmor', '--output',
             '/usr/share/keyrings/caddy-stable-archive-keyring.gpg', '/root/poa-caddy-key.asc'])
    command(['curl', '--fail', '--location', '--output', '/etc/apt/sources.list.d/caddy-stable.list',
             'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt'])
    for p in ('/usr/share/keyrings/caddy-stable-archive-keyring.gpg',
              '/etc/apt/sources.list.d/caddy-stable.list'):
        Path(p).chmod(0o644)
    command(['apt-get', 'update'])
    command(['apt-get', 'install', '-y', 'caddy'])
    print('POA_UBUNTU_PREPARATION_SUCCESS', flush=True)


if __name__ == '__main__':
    try:
        prepare()
    except Exception:
        print('POA_UBUNTU_PREPARATION_FAILED; no installation success claimed', file=sys.stderr)
        sys.exit(1)
