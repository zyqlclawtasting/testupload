#!/bin/bash
set -uo pipefail

# 统一绝对路径，避免从相对路径/不同 cwd 启动时落到两套 state/lock
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# StepClaw <-> ChickenClaw bridge (group only)
# Trigger: sender app_id == TARGET_APP_ID and message includes @ChickenClaw
# Action: send normal group message (non-reply) that @mentions MENTION_USER_ID and answers with context

CHAT_ID="oc_a24d08840b2a8da3ca3472cbb901df78"
DOMAIN="https://open.feishu.cn"
APP_ID="cli_a94bb6b1bfba5bca"
APP_SECRET="holZMQqCXQVqWQuRHgDFnekWyE2mmxE5"

TARGET_APP_ID="cli_a94bba9abdb95bce"
MENTION_USER_ID="ou_51ba9eb6710c464457ed704e80cf7e98"
MENTION_USER_NAME="StepClaw"
BOT_NAME="ChickenClaw"

# Optional LLM config (for richer contextual answers)
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
LLM_MODEL="${LLM_MODEL:-openai/gpt-4o-mini}"

POLL_INTERVAL=3
# 每30分钟最多回复10条，避免双机器人互相刷屏
RATE_LIMIT_WINDOW_SEC=1800
RATE_LIMIT_MAX=10

STATE_FILE="${SCRIPT_DIR}/.feishu_bot_bridge_state.json"
CONTEXT_FILE="${SCRIPT_DIR}/.feishu_bot_bridge_context.json"
LOCK_DIR="${SCRIPT_DIR}/.feishu_bot_bridge.lock"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    trap 'rm -f "$LOCK_DIR/pid"; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
    return 0
  fi

  if [ -f "$LOCK_DIR/pid" ]; then
    local old_pid
    old_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
    if [ -n "$old_pid" ] && ! kill -0 "$old_pid" 2>/dev/null; then
      rm -f "$LOCK_DIR/pid"
      rmdir "$LOCK_DIR" 2>/dev/null || true
      mkdir "$LOCK_DIR" 2>/dev/null || { log "another instance is running"; exit 0; }
      echo $$ > "$LOCK_DIR/pid"
      trap 'rm -f "$LOCK_DIR/pid"; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
      return 0
    fi
  fi

  log "another instance is running"
  exit 0
}

init_state() {
  if [ ! -f "$STATE_FILE" ]; then
    echo '{"processed":[],"rate":{"window_start":0,"count":0}}' > "$STATE_FILE"
    return
  fi

  # 兼容旧状态文件
  local tmp
  tmp=$(mktemp)
  jq 'if .rate == null then . + {rate:{window_start:0,count:0}} else . end' "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
}

init_context() {
  [ -f "$CONTEXT_FILE" ] || echo '{"dialog":[]}' > "$CONTEXT_FILE"
}

is_processed() {
  local msg_id="$1"
  jq -e --arg id "$msg_id" '.processed[] | select(. == $id)' "$STATE_FILE" >/dev/null 2>&1
}

mark_processed() {
  local msg_id="$1" tmp
  tmp=$(mktemp)
  jq --arg id "$msg_id" '.processed += [$id] | if (.processed|length) > 1200 then .processed = .processed[-600:] else . end' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

rate_limit_allow_and_inc() {
  local now window_start count tmp
  now=$(date +%s)
  window_start=$(jq -r '.rate.window_start // 0' "$STATE_FILE")
  count=$(jq -r '.rate.count // 0' "$STATE_FILE")

  if [ $((now - window_start)) -ge "$RATE_LIMIT_WINDOW_SEC" ] || [ "$window_start" -eq 0 ]; then
    tmp=$(mktemp)
    jq --argjson now "$now" '.rate.window_start=$now | .rate.count=0' "$STATE_FILE" > "$tmp"
    mv "$tmp" "$STATE_FILE"
    count=0
  fi

  if [ "$count" -ge "$RATE_LIMIT_MAX" ]; then
    return 1
  fi

  tmp=$(mktemp)
  jq '.rate.count += 1' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
  return 0
}

rate_limit_status() {
  jq -r '"window_start=\(.rate.window_start // 0), count=\(.rate.count // 0)"' "$STATE_FILE"
}

append_context() {
  local role="$1" text="$2" tmp
  tmp=$(mktemp)
  jq --arg role "$role" --arg text "$text" --arg t "$(date '+%Y-%m-%d %H:%M:%S')" '
    .dialog += [{t:$t, role:$role, text:$text}] |
    if (.dialog|length) > 60 then .dialog = .dialog[-30:] else . end
  ' "$CONTEXT_FILE" > "$tmp"
  mv "$tmp" "$CONTEXT_FILE"
}

recent_context_text() {
  jq -r '.dialog[-8:][]? | (.role + ": " + .text)' "$CONTEXT_FILE" 2>/dev/null | tail -n 8
}

get_token() {
  curl -s -X POST "${DOMAIN}/open-apis/auth/v3/tenant_access_token/internal" \
    -H "Content-Type: application/json; charset=utf-8" \
    -d "{\"app_id\":\"${APP_ID}\",\"app_secret\":\"${APP_SECRET}\"}" | jq -r '.tenant_access_token'
}

list_messages() {
  local token="$1"
  curl -s -X GET "${DOMAIN}/open-apis/im/v1/messages?container_id_type=chat&container_id=${CHAT_ID}&page_size=20&sort_type=ByCreateTimeDesc" \
    -H "Authorization: Bearer ${token}"
}

extract_text() {
  local item="$1"
  local msg_type content_str
  msg_type=$(echo "$item" | jq -r '.msg_type // empty')
  content_str=$(echo "$item" | jq -r '.body.content // empty')
  [ -z "$content_str" ] && { echo ""; return; }

  if [ "$msg_type" = "text" ]; then
    echo "$content_str" | jq -r '.text // empty' 2>/dev/null || echo ""
    return
  fi

  if [ "$msg_type" = "post" ]; then
    echo "$content_str" | jq -r '
      (.content // [])[]
      | map(
          if .tag == "text" then .text
          elif .tag == "md" then .text
          elif .tag == "at" then "<at user_id=\"" + (.user_id // "") + "\" user_name=\"" + (.user_name // "") + "\">"
          else ""
          end
        )
      | join(" ")
    ' 2>/dev/null | tr '\n' ' '
    return
  fi

  echo ""
}

clean_question() {
  local raw_text="$1"
  echo "$raw_text" \
    | sed -E 's/<at[^>]*>//g' \
    | sed -E 's/^[[:space:]]+|[[:space:]]+$//g'
}

build_answer_by_llm() {
  local question="$1"
  local ctx="$2"

  [ -n "$OPENROUTER_API_KEY" ] || return 1

  local payload resp answer
  payload=$(jq -nc \
    --arg model "$LLM_MODEL" \
    --arg q "$question" \
    --arg ctx "$ctx" '
    {
      model: $model,
      temperature: 0.4,
      max_tokens: 180,
      messages: [
        {role:"system", content:"你是飞书群里的 ChickenClaw。请根据当前问题和最近上下文，用中文给出简短、自然、有信息量的回复。1-2句为主，不要复述整段问题，不要输出多余前缀。"},
        {role:"user", content:("最近上下文:\n" + $ctx + "\n\n当前问题:\n" + $q)}
      ]
    }
  ')

  resp=$(curl -s -X POST "https://openrouter.ai/api/v1/chat/completions" \
    -H "Authorization: Bearer ${OPENROUTER_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$payload")

  answer=$(echo "$resp" | jq -r '.choices[0].message.content // empty' 2>/dev/null)
  [ -n "$answer" ] || return 1
  echo "$answer" | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g; s/^ +| +$//g'
}

build_answer() {
  local question="$1"
  local ctx
  ctx=$(recent_context_text)

  if [ -z "$question" ]; then
    echo "收到"
    return
  fi

  # 明确要求“回一句收到”时严格执行
  if echo "$question" | grep -Eqi '请回我一句.?收到|回我一句.?收到|回复.?收到|请回复.?收到'; then
    echo "收到"
    return
  fi

  # 优先尝试 LLM（有上下文）
  local answer
  if answer=$(build_answer_by_llm "$question" "$ctx"); then
    echo "$answer"
    return
  fi

  # 无 LLM 时的上下文感知兜底
  if echo "$question" | grep -Eqi '在线吗|在吗|握手|ping'; then
    echo "收到，我在线 🤝"
    return
  fi

  echo "收到：$question"
}

send_group_post() {
  local token="$1"
  local markdown_text="$2"

  local content payload resp
  content=$(jq -nc --arg md "$markdown_text" '{"zh_cn":{"content":[[{"tag":"md","text":$md}]]}}')
  payload=$(jq -nc --arg rid "$CHAT_ID" --arg c "$content" '{receive_id:$rid,msg_type:"post",content:$c}')

  resp=$(curl -s -X POST "${DOMAIN}/open-apis/im/v1/messages?receive_id_type=chat_id" \
    -H "Content-Type: application/json; charset=utf-8" \
    -H "Authorization: Bearer ${token}" \
    -d "$payload")

  echo "$resp" | jq -e '.code == 0' >/dev/null 2>&1
}

main() {
  acquire_lock
  init_state
  init_context
  log "bridge start: chat=${CHAT_ID}, target_app=${TARGET_APP_ID}"

  while true; do
    token=$(get_token)
    if [ -z "${token:-}" ] || [ "$token" = "null" ]; then
      log "token fetch failed; retry"
      sleep "$POLL_INTERVAL"
      continue
    fi

    msgs=$(list_messages "$token")
    if ! echo "$msgs" | jq -e '.code == 0 and (.data.items | type == "array")' >/dev/null 2>&1; then
      log "list_messages failed: $(echo "$msgs" | jq -c '{code,msg}' 2>/dev/null || echo "$msgs")"
      sleep "$POLL_INTERVAL"
      continue
    fi

    while IFS= read -r item; do
      msg_id=$(echo "$item" | jq -r '.message_id // empty')
      sender=$(echo "$item" | jq -r '.sender.id // empty')
      [ -z "$msg_id" ] && continue

      is_processed "$msg_id" && continue
      mark_processed "$msg_id"

      [ "$sender" != "$TARGET_APP_ID" ] && continue

      text=$(extract_text "$item")
      echo "$text" | grep -q 'user_name="ChickenClaw"' || continue

      question=$(clean_question "$text")
      append_context "chicken" "$question"

      if ! rate_limit_allow_and_inc; then
        log "rate limit hit (30min max ${RATE_LIMIT_MAX}), skip msg_id=${msg_id}, $(rate_limit_status)"
        continue
      fi

      answer=$(build_answer "$question")
      out="<at user_id=\"${MENTION_USER_ID}\">${MENTION_USER_NAME}</at> ${answer}"

      if send_group_post "$token" "$out"; then
        append_context "step" "$answer"
        log "replied to msg_id=${msg_id}, $(rate_limit_status)"
      else
        log "reply failed for msg_id=${msg_id}, $(rate_limit_status)"
      fi
    done < <(echo "$msgs" | jq -c '.data.items | reverse[]?')

    sleep "$POLL_INTERVAL"
  done
}

main
