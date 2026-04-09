#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)


@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


WORKDIR = "/root/.openclaw/workspace"
DATA_PATH = os.path.join(WORKDIR, "kimi_task_hub_data.json")
SETTINGS_PATH = os.path.join(WORKDIR, "kimi_task_hub_8084_settings.json")

VALID_STATUS = ["todo", "running", "blocked", "review", "done", "failed"]

lock = threading.Lock()
state = {
    "tasks": [],
    "events": [],
}

# LLM Settings with defaults
settings = {
    "llm_enabled": False,
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key": "",
    "llm_model": "gpt-4",
    "review_prompt": "请review以下任务执行结果，给出简短评价和改进建议：",
    "auto_review_interval": 0,  # 0 means disabled, minutes
    "last_auto_review_at": None,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def save_state() -> None:
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)


def load_state() -> None:
    if not os.path.exists(DATA_PATH):
        save_state()
        return
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        state["tasks"] = data.get("tasks", [])
        state["events"] = data.get("events", [])
    except Exception:
        state["tasks"] = []
        state["events"] = []


def save_settings() -> None:
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def load_settings() -> None:
    global settings
    if not os.path.exists(SETTINGS_PATH):
        save_settings()
        return
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in settings:
                settings[k] = v
    except Exception:
        pass


def mask_api_key(key: str) -> str:
    """Mask API key for display, only show last 4 chars"""
    if not key or len(key) < 8:
        return ""
    return "*" * (len(key) - 4) + key[-4:]


def get_settings_for_display() -> dict:
    """Return settings with masked API key"""
    display = dict(settings)
    display["llm_api_key_masked"] = mask_api_key(settings.get("llm_api_key", ""))
    display.pop("llm_api_key", None)
    return display


def add_event(kind: str, task_id: str | None, message: str, extra: dict | None = None):
    ev = {
        "id": uuid.uuid4().hex[:10],
        "at": now_iso(),
        "kind": kind,
        "task_id": task_id,
        "message": message,
        "extra": extra or {},
    }
    state["events"].append(ev)
    if len(state["events"]) > 500:
        state["events"] = state["events"][-500:]


def find_task(task_id: str):
    for t in state["tasks"]:
        if t["task_id"] == task_id:
            return t
    return None


def create_task(payload: dict) -> dict:
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")

    steps = payload.get("steps")
    if isinstance(steps, str):
        steps = [s.strip() for s in steps.splitlines() if s.strip()]
    elif not isinstance(steps, list):
        steps = []

    task = {
        "task_id": "tsk_" + uuid.uuid4().hex[:8],
        "title": title,
        "goal": (payload.get("goal") or "").strip(),
        "inputs": payload.get("inputs") or "",
        "steps": steps,
        "owner": payload.get("owner") or "kimi",
        "status": payload.get("status") if payload.get("status") in VALID_STATUS else "todo",
        "result_summary": "",
        "artifacts": [],
        "next_action": payload.get("next_action") or "待下发给 Kimi",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "dispatch": {
            "running": False,
            "last_command": "",
            "last_exit_code": None,
            "last_run_at": None,
        },
        "review_result": None,  # Store LLM review result
    }
    return task


def build_kimi_prompt(task: dict) -> str:
    step_lines = "\n".join([f"{i+1}. {s}" for i, s in enumerate(task.get("steps", []))]) or "1. 按任务目标自行拆解并执行"
    return (
        "你是执行开发任务的工程代理。请完成以下子任务，并只输出执行结果摘要：\n\n"
        f"任务ID: {task['task_id']}\n"
        f"标题: {task['title']}\n"
        f"目标: {task.get('goal', '') or '（未填写）'}\n"
        f"输入: {task.get('inputs', '') or '（未填写）'}\n"
        f"步骤:\n{step_lines}\n\n"
        "要求：\n"
        "- 在当前工作目录执行\n"
        "- 如需改文件，直接改并说明改动\n"
        "- 若被阻塞，明确写出阻塞原因和下一步建议\n"
    )


def build_review_prompt(task: dict) -> str:
    """Build prompt for LLM review"""
    base_prompt = settings.get("review_prompt", "")
    artifacts = task.get("artifacts", [])
    last_output = ""
    if artifacts:
        last = artifacts[-1]
        stdout = last.get("stdout", "")
        stderr = last.get("stderr", "")
        last_output = f"\n执行输出:\n{stdout[:2000]}\n{stderr[:1000]}"
    
    return (
        f"{base_prompt}\n\n"
        f"任务标题: {task.get('title', '')}\n"
        f"任务目标: {task.get('goal', '')}\n"
        f"执行状态: {task.get('status', '')}\n"
        f"执行结果摘要: {task.get('result_summary', '')}"
        f"{last_output}\n\n"
        "请给出review评价（通过/需改进/阻塞）和具体建议："
    )


def call_llm_review(prompt: str) -> dict:
    """Call OpenAI-compatible API for review"""
    if not settings.get("llm_enabled") or not settings.get("llm_api_key"):
        return {"error": "LLM not configured", "review": "", "passed": None}
    
    base_url = settings.get("llm_base_url", "https://api.openai.com/v1").rstrip("/")
    api_key = settings.get("llm_api_key", "")
    model = settings.get("llm_model", "gpt-4")
    
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一个代码review助手，请简洁地评价任务执行结果。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 500,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # Simple pass/fail detection
        passed = None
        lower = content.lower()
        if "通过" in lower or "passed" in lower or "✓" in lower or "✅" in lower:
            passed = True
        elif "阻塞" in lower or "blocked" in lower or "失败" in lower or "failed" in lower:
            passed = False
        
        return {"review": content, "passed": passed, "error": None}
    except Exception as e:
        return {"error": str(e), "review": "", "passed": None}


def run_task_review(task_id: str, manual: bool = False):
    """Run LLM review for a task"""
    with lock:
        task = find_task(task_id)
        if not task:
            return
        if task.get("status") not in ["review", "done", "blocked"]:
            if manual:
                pass  # Allow manual review on any status
            else:
                return
        
        prompt = build_review_prompt(task)
        result = call_llm_review(prompt)
        
        task["review_result"] = {
            "at": now_iso(),
            "manual": manual,
            "review": result.get("review", ""),
            "passed": result.get("passed"),
            "error": result.get("error"),
        }
        
        kind_msg = "手动review完成" if manual else "自动review完成"
        add_event("review", task_id, kind_msg, {"passed": result.get("passed")})
        save_state()


def auto_review_checker():
    """Background thread for auto review"""
    while True:
        time.sleep(60)  # Check every minute
        interval = settings.get("auto_review_interval", 0)
        if interval <= 0:
            continue
        
        last_str = settings.get("last_auto_review_at")
        last_time = None
        if last_str:
            try:
                last_time = datetime.fromisoformat(last_str)
            except:
                pass
        
        now = datetime.now(timezone.utc).astimezone()
        should_run = False
        if last_time is None:
            should_run = True
        else:
            elapsed = (now - last_time).total_seconds() / 60
            if elapsed >= interval:
                should_run = True
        
        if should_run:
            # Find tasks in review status without recent review
            with lock:
                for t in state["tasks"]:
                    if t.get("status") == "review" and not t.get("dispatch", {}).get("running"):
                        review_info = t.get("review_result")
                        if not review_info or review_info.get("error"):
                            # Run review in background thread
                            threading.Thread(target=run_task_review, args=(t["task_id"], False), daemon=True).start()
            
            settings["last_auto_review_at"] = now_iso()
            save_settings()


def should_log_dispatch_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False

    noisy_prefixes = (
        "TurnBegin(", "StepBegin(", "ThinkPart(", "ToolCall(", "ToolResult(",
        "TokenUsage(", "AssistantMessage(", "Response(", "raw_payload=", "mcp_status=",
        "message_id=", "plan_mode=", "To resume this session:",
    )
    if s.startswith(noisy_prefixes):
        return False

    noisy_contains = ("input_cache_", "encrypted=None")
    if any(x in s for x in noisy_contains):
        return False

    return True


def dispatch_task(task_id: str):
    with lock:
        task = find_task(task_id)
        if not task:
            return
        if task["dispatch"]["running"]:
            return
        task["dispatch"]["running"] = True
        task["status"] = "running"
        task["updated_at"] = now_iso()
        task["dispatch"]["last_run_at"] = now_iso()
        task["review_result"] = None  # Clear previous review
        add_event("dispatch", task_id, "任务已下发给 Kimi")
        save_state()

    prompt = build_kimi_prompt(task)
    cmd = ["kimi", "-p", prompt, "--print", "-y", "--work-dir", WORKDIR]

    out_lines: list[str] = []
    err_lines: list[str] = []
    code = 1

    q: queue.Queue[tuple[str, str]] = queue.Queue()

    def _reader(pipe, tag: str):
        try:
            for line in iter(pipe.readline, ""):
                if not line:
                    break
                q.put((tag, line.rstrip("\n")))
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=WORKDIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        t_out = threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True)
        t_out.start()
        t_err.start()

        last_progress_at = 0.0
        while True:
            try:
                tag, line = q.get(timeout=1.0)
                if tag == "stdout":
                    out_lines.append(line)
                else:
                    err_lines.append(line)

                if line.strip():
                    with lock:
                        task = find_task(task_id)
                        if task:
                            task["updated_at"] = now_iso()
                            if should_log_dispatch_line(line):
                                add_event("dispatch_log", task_id, line[:280], {"stream": tag})
                            save_state()
            except queue.Empty:
                pass

            now_ts = time.time()
            if now_ts - last_progress_at >= 10:
                with lock:
                    task = find_task(task_id)
                    if task and task["dispatch"]["running"]:
                        task["updated_at"] = now_iso()
                        add_event("dispatch_progress", task_id, "Kimi 执行中…")
                        save_state()
                last_progress_at = now_ts

            if proc.poll() is not None and q.empty():
                break

        code = proc.returncode if proc.returncode is not None else 1
    except Exception as e:
        err_lines.append(str(e))

    out_text = "\n".join(out_lines).strip()
    err_text = "\n".join(err_lines).strip()

    with lock:
        task = find_task(task_id)
        if not task:
            return

        task["dispatch"]["running"] = False
        task["dispatch"]["last_command"] = " ".join(cmd)
        task["dispatch"]["last_exit_code"] = code
        task["updated_at"] = now_iso()

        artifact = {
            "at": now_iso(),
            "type": "kimi_run",
            "exit_code": code,
            "stdout": out_text[-12000:],
            "stderr": err_text[-6000:],
        }
        task["artifacts"].append(artifact)
        if len(task["artifacts"]) > 20:
            task["artifacts"] = task["artifacts"][-20:]

        if code == 0:
            task["status"] = "review"
            task["result_summary"] = (out_text[:500] if out_text else "Kimi 已执行完成（无摘要输出）")
            task["next_action"] = "等待 review / 人工确认"
            add_event("dispatch_done", task_id, "Kimi 执行完成，进入 review")
            # Auto-trigger LLM review if enabled
            if settings.get("llm_enabled"):
                threading.Thread(target=run_task_review, args=(task_id, False), daemon=True).start()
        else:
            task["status"] = "blocked"
            prefer_summary = err_text if err_text.strip() else out_text
            task["result_summary"] = (prefer_summary[:500] if prefer_summary else "Kimi 执行失败")
            task["next_action"] = "检查错误并修正任务描述后重试"
            add_event("dispatch_failed", task_id, "Kimi 执行失败", {"exit_code": code})

        save_state()


PAGE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kimi Task Hub M2</title>
  <style>
    body { margin: 0; font-family: Inter, -apple-system, Segoe UI, sans-serif; background:#0b1020; color:#e6edf3; }
    .wrap { max-width: 1400px; margin: 0 auto; padding: 16px; }
    .title-row { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom: 6px; }
    .title { font-size: 22px; color:#7dd3fc; }
    .version-badge { font-size:12px; color:#0b1020; background:#22d3ee; padding:4px 10px; border-radius:999px; font-weight:700; }
    .sub { color:#9fb3c8; margin-bottom: 4px; }
    .refresh-time { color:#67e8f9; font-size:12px; margin-bottom: 14px; }
    
    /* Collapsible create task card */
    .create-card { background:#111827; border:1px solid #1f2937; border-radius:10px; margin-bottom: 16px; overflow:hidden; }
    .create-header { display:flex; align-items:center; justify-content:space-between; padding:12px 16px; cursor:pointer; background:#0f172a; }
    .create-header:hover { background:#1f2937; }
    .create-title { font-size:16px; font-weight:600; color:#7dd3fc; }
    .create-toggle { font-size:18px; color:#9fb3c8; transition:transform 0.2s; }
    .create-toggle.expanded { transform:rotate(90deg); }
    .create-body { display:none; padding:16px; border-top:1px solid #1f2937; }
    .create-body.expanded { display:block; }
    
    label { display:block; font-size:12px; color:#9fb3c8; margin:8px 0 4px; }
    input, textarea, select, button { width:100%; box-sizing:border-box; border-radius:8px; border:1px solid #334155; background:#0f172a; color:#e6edf3; padding:8px; }
    textarea { min-height:72px; }
    button { cursor:pointer; background:#2563eb; border-color:#2563eb; margin-top:10px; }
    button:disabled { opacity:0.6; cursor:not-allowed; }
    
    /* One-line kanban layout */
    .kanban-row { display:flex; gap:12px; overflow-x:auto; padding-bottom:8px; }
    .kanban-col { min-width:220px; flex:1; background:#0f172a; border:1px solid #1f2937; border-radius:10px; padding:10px; }
    .kanban-col h4 { margin:0 0 10px; padding-bottom:8px; border-bottom:2px solid #334155; font-size:13px; text-transform:uppercase; }
    .kanban-col.todo h4 { color:#94a3b8; border-color:#94a3b8; }
    .kanban-col.running h4 { color:#22d3ee; border-color:#22d3ee; }
    .kanban-col.blocked h4 { color:#ef4444; border-color:#ef4444; }
    .kanban-col.review h4 { color:#f59e0b; border-color:#f59e0b; }
    .kanban-col.done h4 { color:#22c55e; border-color:#22c55e; }
    .kanban-col.failed h4 { color:#f97316; border-color:#f97316; }
    
    .task { border:1px solid #334155; border-radius:8px; padding:10px; margin-bottom:10px; background:#111827; cursor:pointer; transition:transform 0.1s, box-shadow 0.1s; }
    .task:hover { transform:translateY(-2px); box-shadow:0 4px 12px rgba(0,0,0,0.3); }
    .task h5 { margin:0 0 6px; font-size:13px; color:#e2e8f0; line-height:1.3; }
    .task .meta { font-size:11px; color:#64748b; margin-bottom:6px; }
    .task .goal { font-size:11px; color:#9fb3c8; line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
    .task .next { font-size:11px; color:#7dd3fc; margin-top:6px; }
    .task .review-badge { display:inline-block; font-size:10px; padding:2px 6px; border-radius:4px; margin-top:6px; }
    .review-passed { background:rgba(34,197,94,0.2); color:#22c55e; }
    .review-failed { background:rgba(239,68,68,0.2); color:#ef4444; }
    .review-pending { background:rgba(245,158,11,0.2); color:#f59e0b; }
    
    .btns { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }
    .btns button { margin-top:0; font-size:11px; padding:5px 8px; width:auto; flex:1; min-width:60px; }
    .ghost { background:transparent; border-color:#334155; }
    
    .feed { max-height:200px; overflow:auto; font-size:12px; line-height:1.5; background:#0f172a; border:1px solid #1f2937; border-radius:8px; padding:10px; }
    .feed-item { border-bottom:1px dashed #243042; padding:6px 0; }
    .feed-item:last-child { border-bottom:none; }
    .small { font-size:12px; color:#9fb3c8; }
    
    /* Settings panel */
    .settings-card { background:#111827; border:1px solid #1f2937; border-radius:10px; padding:16px; margin-bottom:16px; }
    .settings-card h3 { margin:0 0 12px; color:#7dd3fc; font-size:16px; }
    .settings-row { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px; }
    .settings-row label { margin-top:0; }
    .settings-actions { display:flex; gap:8px; margin-top:16px; }
    .settings-actions button { width:auto; padding:8px 20px; }
    .settings-status { font-size:12px; color:#22c55e; margin-top:8px; }
    
    /* Review modal */
    .modal-overlay { display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.7); z-index:100; align-items:center; justify-content:center; }
    .modal-overlay.active { display:flex; }
    .modal { background:#111827; border:1px solid #334155; border-radius:12px; padding:20px; max-width:600px; width:90%; max-height:80vh; overflow:auto; }
    .modal h3 { margin:0 0 16px; color:#7dd3fc; }
    .modal pre { background:#0f172a; padding:12px; border-radius:8px; overflow:auto; font-size:12px; white-space:pre-wrap; word-break:break-word; }
    .modal .close-btn { margin-top:16px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="title-row">
      <div class="title">Kimi Task Hub · M2</div>
      <div class="version-badge">M2 · 折叠+Review版</div>
    </div>
    <div class="sub">任务规划 + 执行看板 + LLM Review · 端口 8084</div>
    <div class="refresh-time" id="refreshTime">最后刷新：--</div>

    <!-- Collapsible Create Task -->
    <div class="create-card">
      <div class="create-header" onclick="toggleCreate()">
        <span class="create-title">📋 新建开发任务</span>
        <span class="create-toggle" id="createToggle">▶</span>
      </div>
      <div class="create-body" id="createBody">
        <label>标题</label>
        <input id="title" placeholder="例如：实现登录 API" />
        <label>目标（DoD）</label>
        <textarea id="goal" placeholder="例如：/api/login 可返回 token，附基础校验"></textarea>
        <label>输入（上下文/文件路径）</label>
        <textarea id="inputs" placeholder="例如：参考 auth.py、README.md"></textarea>
        <label>步骤（每行一步）</label>
        <textarea id="steps" placeholder="1) 新增路由&#10;2) 写单测&#10;3) 更新文档"></textarea>
        <button onclick="createTask()">创建任务（自动执行）</button>
        <div class="small" style="margin-top:8px;">说明：新任务默认会自动下发给 Kimi 开始执行。</div>
        <div class="small" id="msg"></div>
      </div>
    </div>

    <!-- Settings Panel -->
    <div class="settings-card">
      <h3>🔧 LLM Review 配置 (OpenAI兼容)</h3>
      <div class="settings-row">
        <div>
          <label>启用 LLM Review</label>
          <select id="llmEnabled">
            <option value="false">禁用</option>
            <option value="true">启用</option>
          </select>
        </div>
        <div>
          <label>API Base URL</label>
          <input id="llmBaseUrl" placeholder="https://api.openai.com/v1" />
        </div>
      </div>
      <div class="settings-row">
        <div>
          <label>API Key (留空保持原值)</label>
          <input id="llmApiKey" type="password" placeholder="sk-..." />
        </div>
        <div>
          <label>模型名称</label>
          <input id="llmModel" placeholder="gpt-4" />
        </div>
      </div>
      <div class="settings-row">
        <div>
          <label>自动Review间隔(分钟,0=禁用)</label>
          <input id="autoReviewInterval" type="number" placeholder="0" />
        </div>
        <div>
          <label>当前Key (已脱敏)</label>
          <input id="llmKeyMasked" disabled placeholder="未配置" />
        </div>
      </div>
      <label>Review Prompt</label>
      <textarea id="reviewPrompt" placeholder="请review以下任务执行结果..." style="min-height:60px;"></textarea>
      <div class="settings-actions">
        <button onclick="saveSettings()">保存配置</button>
        <button class="ghost" onclick="loadSettings()">刷新配置</button>
      </div>
      <div class="settings-status" id="settingsStatus"></div>
    </div>

    <!-- Kanban Board - One Row -->
    <div class="kanban-row" id="board"></div>

    <!-- Event Feed -->
    <h3 style="color:#7dd3fc; font-size:14px; margin:16px 0 8px;">执行反馈流</h3>
    <div class="feed" id="feed"></div>
  </div>

  <!-- Review Modal -->
  <div class="modal-overlay" id="reviewModal" onclick="closeModal(event)">
    <div class="modal" onclick="event.stopPropagation()">
      <h3>📝 Review 详情</h3>
      <div id="modalContent"></div>
      <button class="close-btn" onclick="hideModal()">关闭</button>
    </div>
  </div>

<script>
var statusCols = ['todo','running','blocked','review','done','failed'];
var statusNames = {todo:'待办', running:'执行中', blocked:'阻塞', review:'待Review', done:'完成', failed:'失败'};

function esc(s){
  s = s || '';
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/\"/g,'&quot;');
}

function api(url, method, body, onOk, onErr){
  var xhr = new XMLHttpRequest();
  xhr.open(method || 'GET', url, true);
  xhr.setRequestHeader('Content-Type','application/json');
  xhr.onreadystatechange = function(){
    if (xhr.readyState !== 4) return;
    if (xhr.status >= 200 && xhr.status < 300){
      try { onOk && onOk(JSON.parse(xhr.responseText || '{}')); }
      catch(e){ onErr && onErr(e); }
    } else {
      onErr && onErr(new Error(xhr.responseText || ('HTTP ' + xhr.status)));
    }
  };
  xhr.onerror = function(){ onErr && onErr(new Error('网络错误')); };
  xhr.send(body ? JSON.stringify(body) : null);
}

function nowText(){
  var ts = new Date();
  function pad(n){ n = String(n); return n.length < 2 ? ('0'+n) : n; }
  return ts.getFullYear() + '-' + pad(ts.getMonth()+1) + '-' + pad(ts.getDate()) + ' ' + pad(ts.getHours()) + ':' + pad(ts.getMinutes()) + ':' + pad(ts.getSeconds());
}

function toggleCreate(){
  var body = document.getElementById('createBody');
  var toggle = document.getElementById('createToggle');
  body.classList.toggle('expanded');
  toggle.classList.toggle('expanded');
}

function renderBoard(tasks){
  var board = document.getElementById('board');
  var colsHtml = '';
  for (var i=0;i<statusCols.length;i++){
    var s = statusCols[i];
    colsHtml += '<div class="kanban-col ' + s + '"><h4>' + esc(statusNames[s]) + '</h4><div id="c_' + s + '"></div></div>';
  }
  board.innerHTML = colsHtml;

  for (var j=0;j<tasks.length;j++){
    var t = tasks[j] || {};
    var c = document.getElementById('c_' + t.status);
    if (!c) continue;
    var running = !!(t.dispatch && t.dispatch.running);
    var reviewResult = t.review_result || {};
    var reviewBadge = '';
    if (reviewResult.review) {
      var badgeClass = reviewResult.passed === true ? 'review-passed' : (reviewResult.passed === false ? 'review-failed' : 'review-pending');
      var badgeText = reviewResult.passed === true ? '✓ 通过' : (reviewResult.passed === false ? '✗ 需改进' : '? 待确认');
      reviewBadge = '<span class="review-badge ' + badgeClass + '" onclick="showReview(\'' + esc(t.task_id) + '\', event)">' + badgeText + '</span>';
    }
    
    var html = '';
    html += '<div class="task" onclick="showTaskDetail(\'' + esc(t.task_id) + '\')">';
    html += '<h5>' + esc(t.title) + '</h5>';
    html += '<div class="meta">' + esc(t.task_id) + ' · ' + esc(t.owner) + '</div>';
    html += '<div class="goal">' + esc(t.goal || '无目标说明') + '</div>';
    html += '<div class="next">' + (running ? '⏳ 执行中...' : esc(t.next_action || '')) + '</div>';
    html += reviewBadge;
    html += '<div class="btns" onclick="event.stopPropagation()">';
    html += '<button onclick="dispatchTask(\'' + esc(t.task_id) + '\')" ' + (running ? 'disabled' : '') + '>' + (running ? '执行中' : '下发') + '</button>';
    html += '<button class="ghost" onclick="runReview(\'' + esc(t.task_id) + '\')">Review</button>';
    html += '<button class="ghost" onclick="setStatus(\'' + esc(t.task_id) + '\', \'done\')">完成</button>';
    html += '</div></div>';
    c.innerHTML += html;
  }
}

function renderFeed(events){
  var feed = document.getElementById('feed');
  feed.innerHTML = '';
  var list = (events || []).slice().reverse().slice(0,80);
  for (var i=0;i<list.length;i++){
    var ev = list[i] || {};
    feed.innerHTML += '<div class="feed-item">[' + esc(ev.at) + '] <b>' + esc(ev.kind) + '</b> ' + esc(ev.task_id || '') + ' - ' + esc(ev.message || '') + '</div>';
  }
}

function load(){
  api('/api/tasks', 'GET', null, function(data){
    var tasks = data.tasks || [];
    var events = data.events || [];
    renderBoard(tasks);
    renderFeed(events);
    document.getElementById('refreshTime').textContent = '最后刷新：' + nowText();
  }, function(err){
    document.getElementById('msg').textContent = '加载失败: ' + (err && err.message ? err.message : err);
  });
}

function createTask(){
  var body = {
    title: document.getElementById('title').value,
    goal: document.getElementById('goal').value,
    inputs: document.getElementById('inputs').value,
    steps: document.getElementById('steps').value,
    owner: 'kimi'
  };
  api('/api/tasks', 'POST', body, function(){
    document.getElementById('msg').textContent = '创建成功';
    document.getElementById('title').value = '';
    document.getElementById('goal').value = '';
    document.getElementById('inputs').value = '';
    document.getElementById('steps').value = '';
    load();
  }, function(err){
    document.getElementById('msg').textContent = '创建失败: ' + (err && err.message ? err.message : err);
  });
}

function setStatus(taskId, status){
  api('/api/tasks/' + taskId, 'PATCH', {status: status}, function(){ load(); }, function(err){
    document.getElementById('msg').textContent = '更新失败: ' + (err && err.message ? err.message : err);
  });
}

function dispatchTask(taskId){
  api('/api/tasks/' + taskId + '/dispatch', 'POST', null, function(){ load(); }, function(err){
    document.getElementById('msg').textContent = '下发失败: ' + (err && err.message ? err.message : err);
  });
}

function runReview(taskId){
  api('/api/tasks/' + taskId + '/review', 'POST', null, function(data){
    document.getElementById('msg').textContent = 'Review 已触发';
    load();
    if (data && data.review_result) {
      showReviewData(data.review_result);
    }
  }, function(err){
    document.getElementById('msg').textContent = 'Review 失败: ' + (err && err.message ? err.message : err);
  });
}

function showReview(taskId, event){
  if (event) event.stopPropagation();
  api('/api/tasks/' + taskId, 'GET', null, function(data){
    if (data && data.review_result) {
      showReviewData(data.review_result);
    }
  });
}

function showReviewData(review){
  var content = '';
  content += '<p><b>时间:</b> ' + esc(review.at) + '</p>';
  content += '<p><b>类型:</b> ' + (review.manual ? '手动Review' : '自动Review') + '</p>';
  content += '<p><b>结果:</b> ' + (review.passed === true ? '✓ 通过' : (review.passed === false ? '✗ 需改进' : '待定')) + '</p>';
  if (review.error) {
    content += '<p style="color:#ef4444;"><b>错误:</b> ' + esc(review.error) + '</p>';
  }
  content += '<p><b>Review内容:</b></p>';
  content += '<pre>' + esc(review.review) + '</pre>';
  document.getElementById('modalContent').innerHTML = content;
  document.getElementById('reviewModal').classList.add('active');
}

function showTaskDetail(taskId){
  // Can be expanded to show full task details
}

function hideModal(){
  document.getElementById('reviewModal').classList.remove('active');
}

function closeModal(e){
  if (e.target === document.getElementById('reviewModal')) {
    hideModal();
  }
}

// Settings
function loadSettings(){
  api('/api/settings', 'GET', null, function(data){
    document.getElementById('llmEnabled').value = String(data.llm_enabled || false);
    document.getElementById('llmBaseUrl').value = data.llm_base_url || '';
    document.getElementById('llmModel').value = data.llm_model || '';
    document.getElementById('llmApiKey').value = '';
    document.getElementById('llmKeyMasked').value = data.llm_api_key_masked || '';
    document.getElementById('autoReviewInterval').value = data.auto_review_interval || 0;
    document.getElementById('reviewPrompt').value = data.review_prompt || '';
    document.getElementById('settingsStatus').textContent = '配置已加载';
  }, function(err){
    document.getElementById('settingsStatus').textContent = '加载失败: ' + (err && err.message ? err.message : err);
  });
}

function saveSettings(){
  var body = {
    llm_enabled: document.getElementById('llmEnabled').value === 'true',
    llm_base_url: document.getElementById('llmBaseUrl').value,
    llm_api_key: document.getElementById('llmApiKey').value,
    llm_model: document.getElementById('llmModel').value,
    auto_review_interval: parseInt(document.getElementById('autoReviewInterval').value || '0'),
    review_prompt: document.getElementById('reviewPrompt').value,
  };
  api('/api/settings', 'POST', body, function(data){
    document.getElementById('settingsStatus').textContent = '配置已保存';
    document.getElementById('llmApiKey').value = '';
    loadSettings();
  }, function(err){
    document.getElementById('settingsStatus').textContent = '保存失败: ' + (err && err.message ? err.message : err);
  });
}

// Initialize
setInterval(load, 3000);
load();
loadSettings();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(PAGE)


@app.get("/api/tasks")
def api_tasks():
    with lock:
        tasks = list(state["tasks"])
        events = list(state["events"])
    tasks.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return jsonify({"tasks": tasks, "events": events[-200:], "server_time": now_iso(), "tasks_count": len(tasks)})


@app.get("/api/tasks/<task_id>")
def api_get_task(task_id: str):
    with lock:
        task = find_task(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        return jsonify(task)


@app.post("/api/tasks")
def api_create_task():
    payload = request.get_json(silent=True) or {}
    auto_dispatch = payload.get("auto_dispatch", True)
    try:
        task = create_task(payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    with lock:
        state["tasks"].append(task)
        add_event("task_created", task["task_id"], f"创建任务: {task['title']}")
        save_state()

    if auto_dispatch:
        t = threading.Thread(target=dispatch_task, args=(task["task_id"],), daemon=True)
        t.start()

    return jsonify(task)


@app.patch("/api/tasks/<task_id>")
def api_update_task(task_id: str):
    payload = request.get_json(silent=True) or {}
    with lock:
        task = find_task(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404

        for k in ["title", "goal", "inputs", "owner", "result_summary", "next_action"]:
            if k in payload:
                task[k] = payload[k]

        if "steps" in payload:
            if isinstance(payload["steps"], str):
                task["steps"] = [s.strip() for s in payload["steps"].splitlines() if s.strip()]
            elif isinstance(payload["steps"], list):
                task["steps"] = payload["steps"]

        if "status" in payload and payload["status"] in VALID_STATUS:
            task["status"] = payload["status"]
            add_event("task_status", task_id, f"状态更新为 {payload['status']}")

        task["updated_at"] = now_iso()
        save_state()

    return jsonify(task)


@app.post("/api/tasks/<task_id>/dispatch")
def api_dispatch(task_id: str):
    with lock:
        task = find_task(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        if task["dispatch"]["running"]:
            return jsonify({"error": "task already running"}), 409

    t = threading.Thread(target=dispatch_task, args=(task_id,), daemon=True)
    t.start()
    return jsonify({"ok": True, "task_id": task_id})


@app.post("/api/tasks/<task_id>/review")
def api_review_task(task_id: str):
    """Manual review endpoint"""
    with lock:
        task = find_task(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
    
    # Run review in background thread
    t = threading.Thread(target=run_task_review, args=(task_id, True), daemon=True)
    t.start()
    
    # Return current review result if available
    with lock:
        task = find_task(task_id)
        return jsonify({"ok": True, "task_id": task_id, "review_result": task.get("review_result")})


@app.get("/api/settings")
def api_get_settings():
    return jsonify(get_settings_for_display())


@app.post("/api/settings")
def api_update_settings():
    payload = request.get_json(silent=True) or {}
    global settings
    
    if "llm_enabled" in payload:
        settings["llm_enabled"] = bool(payload["llm_enabled"])
    if "llm_base_url" in payload:
        settings["llm_base_url"] = payload["llm_base_url"]
    if "llm_api_key" in payload:
        key = payload["llm_api_key"]
        if key and len(key) > 0:  # Only update if provided
            settings["llm_api_key"] = key
    if "llm_model" in payload:
        settings["llm_model"] = payload["llm_model"]
    if "auto_review_interval" in payload:
        settings["auto_review_interval"] = int(payload["auto_review_interval"])
    if "review_prompt" in payload:
        settings["review_prompt"] = payload["review_prompt"]
    
    save_settings()
    return jsonify(get_settings_for_display())


if __name__ == "__main__":
    load_state()
    load_settings()
    # Start auto-review background thread
    threading.Thread(target=auto_review_checker, daemon=True).start()
    app.run(host="0.0.0.0", port=8084, debug=False, threaded=True)
