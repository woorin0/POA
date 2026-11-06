#!/usr/bin/env bash
# Isolated Ubuntu/Vultr-like non-root rehearsal; no secrets, no live API reads.
set -Eeuo pipefail
umask 077
PRODUCT_SHA=bf6e66e259c7ebbd5099e78b24089caa94936ec9
ROOT_SOURCE="${GITHUB_WORKSPACE:?GitHub runner workspace required}"
RUN_ROOT="${RUNNER_TEMP:?GitHub runner temp required}"
PY="$(command -v python)"
if [[ "$(id -u)" == 0 ]]; then
  echo "REHEARSAL_BLOCKED: run as nonroot GitHub runner" >&2
  exit 2
fi
if [[ ! -f "$ROOT_SOURCE/audit_artifacts/readonly_audit_v2/poa_readonly_audit_v2.py" ]]; then
  echo "REHEARSAL_BLOCKED: missing reviewed audit source" >&2
  exit 2
fi
NAME=poaauditv3
if id "$NAME" >/dev/null 2>&1; then
  echo "REHEARSAL_BLOCKED: reserved user already exists" >&2
  exit 2
fi

WORK="$(mktemp -d "$RUN_ROOT/poa-readonly-rehearsal.XXXXXX")"
USER_CREATED=0
cleanup() {
  status=$?
  trap - EXIT
  sudo rm -rf -- "$WORK" || true
  if [[ "$USER_CREATED" == 1 ]]; then
    sudo userdel "$NAME" || true
  fi
  exit "$status"
}
trap cleanup EXIT

# Clone only from CI's checked-out local repository, never from a production host.
git clone --no-local --quiet "$ROOT_SOURCE" "$WORK/source"
git -C "$WORK/source" checkout --quiet --detach "$PRODUCT_SHA"
[[ "$(git -C "$WORK/source" rev-parse HEAD)" == "$PRODUCT_SHA" ]]
cp "$ROOT_SOURCE/audit_artifacts/readonly_audit_v2/poa_readonly_audit_v2.py" \
  "$WORK/source/poa_readonly_audit_v2.py"
cmp -s "$ROOT_SOURCE/audit_artifacts/readonly_audit_v2/poa_readonly_audit_v2.py" \
  "$WORK/source/poa_readonly_audit_v2.py"
[[ -z "$(git -C "$WORK/source" status --porcelain --untracked-files=no)" ]]

sudo useradd --no-create-home --shell /usr/sbin/nologin "$NAME"
USER_CREATED=1
sudo chown -R "$NAME:$NAME" "$WORK"
sudo -u "$NAME" env WORK="$WORK" PY="$PY" bash -euo pipefail <<'AUDIT_NONROOT'
umask 077
mkdir -m 700 -p "$WORK/private"
cat > "$WORK/private/orders.json" <<'SYNTHETIC_ORDER'
{
  "orders": [{
    "exchange": "UPBIT", "market": "spot", "symbol": "SOL/KRW",
    "side": "buy", "order_id": "a1111111-2222-3333-4444-555555555555",
    "reference_source": "exchange_csv",
    "reference_fills": [
      {"price":"200000","quantity_base":"0.25"},
      {"price":"201000","quantity_base":"0.25"}
    ],
    "reference_total": "100250",
    "reference_quantity": "0.5"
  }]
}
SYNTHETIC_ORDER
chmod 600 "$WORK/private/orders.json"
"$PY" -m venv "$WORK/venv"
"$WORK/venv/bin/python" -m pip install -q \
  pip==26.3 setuptools==84.2.1
"$WORK/venv/bin/python" -m pip install -q -r "$WORK/source/requirements.txt"
"$WORK/venv/bin/python" -m pip check
cd "$WORK/source"

# Must produce a success without obtaining any keys or calling any venue.
"$WORK/venv/bin/python" -B poa_readonly_audit_v2.py \
  "$WORK/private/orders.json" --preflight > "$WORK/private/preflight.out"
grep -q '^PREFLIGHT_PASS:' "$WORK/private/preflight.out"

# Even with a valid manifest, a non-TTY service/CI may never request keys.
set +e
"$WORK/venv/bin/python" -B poa_readonly_audit_v2.py \
  "$WORK/private/orders.json" --live-read \
  </dev/null >"$WORK/private/live.out" 2>"$WORK/private/live.err"
status=$?
set -e
[[ "$status" == 2 ]]
grep -q 'LIVE_READ_CONFIRMATION_REQUIRED' "$WORK/private/live.err"
! grep -q 'API key:' "$WORK/private/live.err"

# No trade, receipt, DB, credential, webhook or outbox was produced by rehearsal.
[[ ! -e "$WORK/source/.env" ]]
if find "$WORK/private" -type f \( -name '*.db' -o -name '*.sqlite*' -o -name '*secret*' \) | grep -q .; then
  echo "REHEARSAL_BLOCKED: unexpected private output" >&2
  exit 2
fi
test -r "$WORK/private/orders.json"
test "$(stat -c %a "$WORK/private/orders.json")" == 600
test "$(stat -c %a "$WORK/private")" == 700
rm -f "$WORK/private/orders.json" "$WORK/private/live.out" \
  "$WORK/private/live.err" "$WORK/private/preflight.out"
[[ ! -e "$WORK/private/orders.json" ]]
echo "REHEARSAL_PASS: nonroot, isolated venv, immutable PR4, private synthetic manifest, no live read"
AUDIT_NONROOT
