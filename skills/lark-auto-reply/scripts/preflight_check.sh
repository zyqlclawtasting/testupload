#!/usr/bin/env bash
set -euo pipefail

PROFILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"; shift 2;;
    -h|--help)
      cat <<'EOF'
Usage: preflight_check.sh [options]

Options:
  --profile <name>   lark-cli profile
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

ok=1

echo "[preflight] checking lark-cli binary..."
if ! command -v lark-cli >/dev/null 2>&1; then
  echo "[x] lark-cli not found"
  echo "    next: run lark-cli-setup skill to install and initialize lark-cli"
  ok=0
else
  echo "[ok] lark-cli found: $(command -v lark-cli)"
fi

if [[ "$ok" == "1" ]]; then
  echo "[preflight] checking config init..."
  auth_json=$("${LARK[@]}" auth status 2>/tmp/lark_preflight_auth_err.log || true)
  app_id=$(python3 - <<'PY' "$auth_json"
import json,sys
raw=sys.argv[1]
try:
  d=json.loads(raw)
except Exception:
  print("")
  raise SystemExit(0)
print(d.get("appId", ""))
PY
)

  token_status=$(python3 - <<'PY' "$auth_json"
import json,sys
raw=sys.argv[1]
try:
  d=json.loads(raw)
except Exception:
  print("")
  raise SystemExit(0)
print(d.get("tokenStatus", ""))
PY
)

  if [[ -z "$app_id" ]]; then
    echo "[x] lark-cli not configured (missing appId)"
    echo "    next: run: lark-cli config init --new"
    ok=0
  else
    echo "[ok] config present: appId=${app_id}"
  fi

  echo "[preflight] checking token status..."
  if [[ "$token_status" != "valid" ]]; then
    echo "[x] tokenStatus=${token_status:-unknown}"
    echo "    next: run: bash scripts/auth_login_stable.sh"
    ok=0
  else
    echo "[ok] tokenStatus=valid"
  fi
fi

if [[ "$ok" == "1" ]]; then
  echo "[preflight] all checks passed"
  exit 0
else
  echo "[preflight] checks failed"
  exit 1
fi
