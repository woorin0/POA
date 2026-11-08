# POA Python 3.12 dependency compatibility — independent Draft candidate

**No merge, no deployment, no BOT03 host/database/API key access, no real trades.**
Original main SHA `c5566c4cc2a4ce716f1f0d66260bd60e4f95546f` is the fixed product baseline.

## Finding and minimal scope

The legacy requirements pin `pendulum==2.1.2` (2020) does not have a reliable Python 3.12 build path because its build hook imports removed `distutils`. The issue is not merely optional: `exchange/pexchange.py::get_today_timestamp(timezone="Asia/Seoul")` calls `pendulum.today(timezone).start_of("day")` and `end_of("day")`, converts both timestamps to integer milliseconds, which can change order/fill query windows if semantics drift.

Python 3.12-only **exactly one dependency override**:
`pendulum==2.1.2` → `pendulum==3.2.0` (released 2026-01, supports Python 3.12). All 16 other top-level pins remain byte-identical. Original `requirements.txt` SHA256 must be `e103f650ea4c67a8abcbf7661e8e7e881ba6cef53edf18a8d63a5eaaba1369a4`. Any other pin change is BLOCKED.

## Implementation

- `deployment/py312_requirements.py` produces a requirements overlay at an explicit NEW absolute path outside the checkout. It runs only on CPython Python 3.12; it refuses an existing destination, symlink or source hash change; does NOT pip install, import the bot, read credentials, modify a DB, call a network endpoint or start services.
- `tests/test_py312_time_compat.py` extracts the verbatim production time-window function via AST, without importing the live exchange module, and checks calendar-day start/end milliseconds against an **independent Python `datetime` + `zoneinfo` oracle**. Frozen cases include KST, UTC, Asia/Tokyo, leap day, US and EU DST 23/25-hour days. It also imports original runtime dependencies under tests without running trading code, checks complete 17-pin integrity and hostile overlay attempts.
- `tests/conftest.py` changes **only** the test audit hook to recognize CPython 3.12 Windows stdlib `_fallback_socketpair()` (internal asyncio self-pipe) in addition to `socketpair()`, while allowing **only a stdlib caller with a loopback destination**. All external network connections, DB audit blocks and product hooks remain denied. The new Windows regression checks both a working local socketpair and explicit rejection of external audit events.
- `.github/workflows/py312_dependency_compat.yml` runs Windows Python 3.12, Ubuntu 24.04 Python 3.12, and a separate Ubuntu 22.04 Python 3.10 control. Enforces EXACT five-file PR delta, original main/product/finance/order/DB/deployment and 72-manifest byte conservation; Python 3.12 installs all original dependencies with exactly one pin changed, checks `pip check`, runs real Mock Golden, finance PnL/order regression and platform-correct pytest. The 3.10 control uses unchanged original requirements and same timestamp oracle.
- Original `.github/workflows/pr_verification.yml` also runs on this Draft PR to `main`, independently checking Windows/Ubuntu baseline, source pins, manifest, four-venue mock fills and financial Golden tests.

## Safe offline usage after independent 2/2 PASS, NOT a deployment command

On a disposable Python 3.12 CI runner with a verified fixed checkout:
```bash
python3.12 deployment/py312_requirements.py --output /tmp/poa-requirements-py312.txt
python3.12 -m pip install --no-cache-dir -r /tmp/poa-requirements-py312.txt
python3.12 -m pip check
python3.12 -m pytest tests/test_py312_time_compat.py -q
```

The original application server, PocketBase, database, API keys, Vultr cloud-init, installed BOT03 and all systemd / Caddy / Docker files are **unchanged**. This Draft alone does NOT make Ubuntu 24.04 a verified **production deployment**, does not authorize an automatic Trading start or prove external exchange read-only keys and actual database backups.

Original `deployment/install_once.py` still explicitly supports Debian 12 / Ubuntu 22.04 *fresh* installs only; **do not run it on existing BOT03 or Ubuntu 24.04**. A separate approved installer/User Data change would be necessary after this compatibility CI becomes fully PASS.

`HOST_RESTORE=UNVERIFIED`
`TRADING_PERMISSION=BLOCKED`
`PRODUCTION_READY=BLOCKED`
