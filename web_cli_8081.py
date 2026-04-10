#!/usr/bin/env python3
from flask import Flask, request, jsonify, render_template_string
import os
import pty
import fcntl
import select
import threading
import signal
import re

app = Flask(__name__)
WORKDIR = "/root/.openclaw/workspace"

state = {
    "pid": None,
    "fd": None,
    "buffer": "",
    "alive": False,
}
lock = threading.Lock()


def _append_output(text: str):
    with lock:
        state["buffer"] += text
        if len(state["buffer"]) > 500_000:
            state["buffer"] = state["buffer"][-500_000:]


def _sanitize_output(text: str) -> str:
    # 去掉 OSC 标题序列，避免网页里出现奇怪字符；保留 CSI 给 xterm.js 渲染
    return re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)


def _read_loop(fd: int):
    while True:
        with lock:
            alive = state["alive"] and state["fd"] == fd
        if not alive:
            break

        try:
            r, _, _ = select.select([fd], [], [], 0.2)
            if fd in r:
                data = os.read(fd, 4096)
                if not data:
                    _append_output("\r\n[session closed]\r\n")
                    with lock:
                        state["alive"] = False
                    break
                _append_output(_sanitize_output(data.decode(errors="replace")))
        except (BlockingIOError, InterruptedError):
            continue
        except OSError:
            with lock:
                state["alive"] = False
            break
        except Exception as e:
            _append_output(f"\r\n[read error] {e}\r\n")
            with lock:
                state["alive"] = False
            break


def stop_shell():
    with lock:
        pid = state["pid"]
        fd = state["fd"]
        state["alive"] = False
        state["pid"] = None
        state["fd"] = None

    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    if fd:
        try:
            os.close(fd)
        except Exception:
            pass


def start_shell(force=False):
    with lock:
        if state["alive"] and not force:
            return

    if force:
        stop_shell()

    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(WORKDIR)
        os.environ["TERM"] = "xterm-256color"
        os.execvp("bash", ["bash", "-i"])
    else:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        with lock:
            state["pid"] = pid
            state["fd"] = fd
            state["alive"] = True
            state["buffer"] = "\r\n[session started]\r\n"
        t = threading.Thread(target=_read_loop, args=(fd,), daemon=True)
        t.start()


PAGE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ClawPhone Web CLI (xterm.js)</title>
  <link rel="stylesheet" href="https://unpkg.com/xterm/css/xterm.css" />
  <style>
    :root { color-scheme: dark; }
    body {
      margin: 0; padding: 14px; background: #0b1020; color: #e5e7eb;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    .wrap { max-width: 1024px; margin: 0 auto; }
    .title { color: #7dd3fc; font-size: 18px; margin-bottom: 8px; }
    .hint { color: #94a3b8; font-size: 13px; margin-bottom: 10px; }
    #terminal {
      height: 560px;
      border: 1px solid #1e293b;
      border-radius: 10px;
      overflow: hidden;
      background: #020617;
    }
    .bar { display: flex; gap: 8px; margin-top: 10px; }
    input {
      flex: 1; padding: 10px; border-radius: 8px; border: 1px solid #334155;
      background: #111827; color: #e5e7eb;
    }
    button {
      padding: 10px 12px; border: 1px solid #2563eb; border-radius: 8px;
      background: #2563eb; color: #fff; cursor: pointer;
    }
    button.ghost { background: transparent; border-color: #334155; color: #cbd5e1; }
  </style>
</head>
<body>
<div class="wrap">
  <div class="title">ClawPhone 网页命令行（8081）</div>
  <div class="hint">已切到终端渲染模式，支持 kimi CLI 交互（方向键/退格/颜色控制符）。</div>

  <div id="terminal"></div>

  <div class="bar">
    <input id="line" placeholder="手机上可在这里输入并发送整行（可选）" autocomplete="off" />
    <button id="sendBtn">发送</button>
    <button id="ctrlcBtn" class="ghost">Ctrl+C</button>
    <button id="ctrldBtn" class="ghost">Ctrl+D</button>
    <button id="restartBtn" class="ghost">重启会话</button>
  </div>
</div>

<script src="https://unpkg.com/xterm/lib/xterm.js"></script>
<script>
const term = new Terminal({
  cursorBlink: true,
  convertEol: false,
  fontSize: 14,
  fontFamily: 'Menlo, Monaco, Consolas, monospace',
  theme: { background: '#020617' }
});
term.open(document.getElementById('terminal'));
term.focus();

let offset = 0;
let sending = false;
let polling = false;

async function poll(){
  if (polling) return;
  polling = true;
  try {
    const res = await fetch('/api/poll?since=' + offset);
    const data = await res.json();
    if (data.text) term.write(data.text);
    offset = data.next ?? offset;
  } catch (_) {} finally {
    polling = false;
  }
}

async function sendRaw(data){
  if (sending) return;
  sending = true;
  try {
    await fetch('/api/send', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({data})
    });
  } finally {
    sending = false;
  }
}

term.onData((data) => {
  sendRaw(data);
});

async function sendLine(){
  const line = document.getElementById('line').value;
  if (!line) return;
  document.getElementById('line').value = '';
  await fetch('/api/line', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({line})
  });
  term.focus();
}

document.getElementById('sendBtn').addEventListener('click', sendLine);
document.getElementById('line').addEventListener('keydown', (e)=>{ if(e.key==='Enter') sendLine(); });
document.getElementById('ctrlcBtn').addEventListener('click', ()=>sendRaw('\\u0003'));
document.getElementById('ctrldBtn').addEventListener('click', ()=>sendRaw('\\u0004'));
document.getElementById('restartBtn').addEventListener('click', async ()=>{
  await fetch('/api/restart', {method:'POST'});
  term.clear();
  term.reset();
  offset = 0;
  poll();
  term.focus();
});

setInterval(poll, 120);
poll();
</script>
</body>
</html>
"""


@app.get('/')
def index():
    return render_template_string(PAGE)


@app.get('/api/poll')
def api_poll():
    try:
        since = int(request.args.get('since', '0'))
    except ValueError:
        since = 0

    with lock:
        buf = state["buffer"]
        alive = state["alive"]

    if since < 0:
        since = 0
    if since > len(buf):
        since = len(buf)

    return jsonify({
        "text": buf[since:],
        "next": len(buf),
        "alive": alive,
    })


@app.post('/api/line')
def api_line():
    payload = request.get_json(silent=True) or {}
    line = payload.get('line', '')
    with lock:
        fd = state["fd"]
        alive = state["alive"]
    if not alive or fd is None:
        return jsonify({"error": "session not running"}), 409
    os.write(fd, (line + "\n").encode())
    return jsonify({"ok": True})


@app.post('/api/send')
def api_send():
    payload = request.get_json(silent=True) or {}
    data = payload.get('data', '')
    with lock:
        fd = state["fd"]
        alive = state["alive"]
    if not alive or fd is None:
        return jsonify({"error": "session not running"}), 409
    os.write(fd, data.encode())
    return jsonify({"ok": True})


@app.post('/api/restart')
def api_restart():
    start_shell(force=True)
    return jsonify({"ok": True})


if __name__ == '__main__':
    start_shell(force=True)
    app.run(host='0.0.0.0', port=8081, debug=False, threaded=True)
