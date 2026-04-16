#!/usr/bin/env bash
set -euo pipefail

PROFILE=""
CHECK_INTERVAL=5
MAX_WAIT_SECONDS=600
DOMAINS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"; shift 2;;
    --check-interval)
      CHECK_INTERVAL="$2"; shift 2;;
    --max-wait-seconds)
      MAX_WAIT_SECONDS="$2"; shift 2;;
    --domains)
      DOMAINS="$2"; shift 2;;
    -h|--help)
      cat <<'EOF'
Usage: auth_login_stable.sh [options]

Options:
  --profile <name>            lark-cli profile
  --domains <csv>             domains for auth login, e.g. im,docs
  --check-interval <seconds>  poll interval (default: 5)
  --max-wait-seconds <sec>    max wait time (default: 600)
EOF
      exit 0;;
    *)
      echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

LARK=(lark-cli)
if [[ -n "$PROFILE" ]]; then
  LARK+=(--profile "$PROFILE")
fi

status_ok() {
  if "${LARK[@]}" auth status >/tmp/lark_auth_status.json 2>/dev/null; then
    python3 - <<'PY' /tmp/lark_auth_status.json
import json,sys
p=sys.argv[1]
try:
  d=json.load(open(p,'r',encoding='utf-8'))
except Exception:
  print('0')
  raise SystemExit(0)
print('1' if d.get('tokenStatus')=='valid' else '0')
PY
  else
    echo "0"
  fi
}

if [[ "$(status_ok)" == "1" ]]; then
  echo "[auth] token already valid"
  exit 0
fi

LOGIN_ARGS=(auth login --no-wait --json)
if [[ -n "$DOMAINS" ]]; then
  LOGIN_ARGS+=(--domain "$DOMAINS")
fi

echo "[auth] requesting device authorization..."
RAW_OUT=$("${LARK[@]}" "${LOGIN_ARGS[@]}" 2>&1 || true)
echo "$RAW_OUT"

parse_val() {
  local key="$1"
  python3 - <<'PY' "$RAW_OUT" "$key"
import json,sys
raw=sys.argv[1]
key=sys.argv[2]

def walk(x):
  if isinstance(x, dict):
    for k,v in x.items():
      if k==key and isinstance(v,(str,int,float)):
        print(v)
        raise SystemExit(0)
      walk(v)
  elif isinstance(x, list):
    for i in x:
      walk(i)

try:
  obj=json.loads(raw)
except Exception:
  print("")
  raise SystemExit(0)
walk(obj)
print("")
PY
}

DEVICE_CODE=$(parse_val "device_code")
VERIFY_URL=$(parse_val "verification_uri_complete")
if [[ -z "$VERIFY_URL" ]]; then
  VERIFY_URL=$(parse_val "verification_url")
fi
if [[ -z "$VERIFY_URL" ]]; then
  VERIFY_URL=$(parse_val "verification_uri")
fi

if [[ -n "$VERIFY_URL" ]]; then
  echo "[auth] open this URL on phone/browser and complete authorization:"
  echo "$VERIFY_URL"
fi

start_ts=$(date +%s)
attempt=0

while true; do
  now=$(date +%s)
  elapsed=$(( now - start_ts ))
  if (( elapsed > MAX_WAIT_SECONDS )); then
    echo "[auth] timeout after ${MAX_WAIT_SECONDS}s" >&2
    exit 1
  fi

  if [[ "$(status_ok)" == "1" ]]; then
    echo "[auth] success: tokenStatus=valid"
    break
  fi

  attempt=$((attempt + 1))
  echo "[auth] waiting for user authorization... attempt=${attempt}, elapsed=${elapsed}s"

  if [[ -n "$DEVICE_CODE" ]]; then
    # Continue polling with existing device code.
    "${LARK[@]}" auth login --device-code "$DEVICE_CODE" >/tmp/lark_auth_poll.log 2>&1 || true
  else
    # Fallback: if device code missing, retry login initialization.
    "${LARK[@]}" auth login --json >/tmp/lark_auth_poll.log 2>&1 || true
  fi

  sleep "$CHECK_INTERVAL"
done

"${LARK[@]}" doctor || true
