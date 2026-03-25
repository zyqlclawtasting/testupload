const http = require('http');
const { execSync, spawn } = require('child_process');
const { randomUUID } = require('crypto');

const PORT = 8090;
const HOST = '0.0.0.0';

const MAX_EXEC_TIMEOUT_MS = 30000;
const MAX_EXEC_BUFFER = 10 * 1024 * 1024;
const SESSION_IDLE_MS = 30 * 60 * 1000; // 30 min idle cleanup
const MAX_EVENTS = 4000;

const sessions = new Map();

function json(res, code, payload) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

function now() {
  return Date.now();
}

function makeSession() {
  const id = randomUUID();
  const shell = process.env.SHELL || 'bash';

  // `script` gives us a PTY so interactive TUI apps (like kimi) can run.
  const proc = spawn('script', ['-qf', '/dev/null', '-c', `${shell} -i`], {
    cwd: process.cwd(),
    env: process.env,
    stdio: ['pipe', 'pipe', 'pipe']
  });

  const session = {
    id,
    proc,
    seq: 0,
    events: [],
    alive: true,
    lastActiveAt: now()
  };

  const push = (data) => {
    if (!data) return;
    session.seq += 1;
    session.events.push({ seq: session.seq, data: data.toString('utf-8') });
    if (session.events.length > MAX_EVENTS) {
      session.events.splice(0, session.events.length - MAX_EVENTS);
    }
  };

  proc.stdout.on('data', chunk => {
    session.lastActiveAt = now();
    push(chunk);
  });

  proc.stderr.on('data', chunk => {
    session.lastActiveAt = now();
    push(chunk);
  });

  proc.on('close', (code, signal) => {
    session.alive = false;
    push(`\r\n[process exited: code=${code ?? 'null'}, signal=${signal ?? 'null'}]\r\n`);
  });

  sessions.set(id, session);
  return session;
}

function getSession(id) {
  if (!id) return null;
  const s = sessions.get(id);
  if (!s) return null;
  s.lastActiveAt = now();
  return s;
}

function closeSession(id) {
  const s = sessions.get(id);
  if (!s) return;
  try {
    s.proc.kill('SIGTERM');
  } catch {}
  sessions.delete(id);
}

setInterval(() => {
  const t = now();
  for (const [id, s] of sessions.entries()) {
    if (!s.alive || t - s.lastActiveAt > SESSION_IDLE_MS) {
      closeSession(id);
    }
  }
}, 60 * 1000);

const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>蹦蹦虾 Web CLI（支持交互式 TUI）</title>
  <link rel="stylesheet" href="https://unpkg.com/xterm/css/xterm.css" />
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0e1320; color: #e8eefc; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 14px; }
    .title { font-size: 18px; font-weight: 700; color: #76b7ff; margin-bottom: 10px; }
    .panel { background: #121a2b; border: 1px solid #23304d; border-radius: 10px; padding: 12px; margin-bottom: 12px; }
    .muted { color: #9ab0d0; font-size: 13px; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    input, button {
      border-radius: 8px;
      border: 1px solid #32466f;
      background: #0e1424;
      color: #e8eefc;
      padding: 8px 10px;
      font-size: 14px;
    }
    input { flex: 1; min-width: 260px; }
    button { cursor: pointer; }
    button:hover { background: #1a2642; }
    #terminal { height: 62vh; width: 100%; border-radius: 8px; overflow: hidden; border: 1px solid #2f3f63; }
    .ok { color: #55d68d; }
    .err { color: #ff6b6b; }
    .tip { margin-top: 8px; font-size: 12px; color: #95a6c7; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="title">🦐 蹦蹦虾 Web CLI（支持交互式 TUI）</div>

    <div class="panel">
      <div class="row" style="margin-bottom:8px;">
        <button id="btnNew">新建终端会话</button>
        <button id="btnClose">关闭当前会话</button>
        <span class="muted">会话ID：<code id="sid">-</code></span>
      </div>
      <div id="terminal"></div>
      <div class="tip">提示：现在可以直接在这里运行交互式程序（如 <code>kimi</code>）。</div>
    </div>

    <div class="panel">
      <div class="row" style="margin-bottom:8px;">
        <input id="quickCmd" placeholder="快速执行（非交互）：例如 ls -al" />
        <button id="btnExec">执行</button>
      </div>
      <pre id="quickOut" class="muted" style="white-space:pre-wrap; min-height:40px;"></pre>
    </div>
  </div>

  <script src="https://unpkg.com/xterm/lib/xterm.js"></script>
  <script>
    const term = new Terminal({
      cursorBlink: true,
      convertEol: false,
      fontSize: 13,
      theme: {
        background: '#0b1120',
        foreground: '#d9e3ff'
      }
    });

    const elSid = document.getElementById('sid');
    const elQuickOut = document.getElementById('quickOut');
    const elQuickCmd = document.getElementById('quickCmd');

    let sid = null;
    let fromSeq = 0;
    let polling = false;

    term.open(document.getElementById('terminal'));
    term.writeln('\\x1b[1;34m[蹦蹦虾] 正在初始化交互终端...\\x1b[0m');

    async function api(path, method = 'GET', body) {
      const res = await fetch(path, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
    }

    async function newSession() {
      const data = await api('/tui/session', 'POST');
      sid = data.sessionId;
      fromSeq = 0;
      elSid.textContent = sid;
      term.reset();
      term.writeln('\\x1b[1;32m[会话已创建] ' + sid + '\\x1b[0m');
      if (!polling) pollLoop();
    }

    async function closeSession() {
      if (!sid) return;
      await api('/tui/close', 'POST', { sessionId: sid });
      term.writeln('\\r\\n\\x1b[1;31m[会话已关闭]\\x1b[0m');
      sid = null;
      elSid.textContent = '-';
    }

    async function pollLoop() {
      polling = true;
      while (true) {
        try {
          if (!sid) {
            await new Promise(r => setTimeout(r, 250));
            continue;
          }
          const data = await api('/tui/poll?sessionId=' + encodeURIComponent(sid) + '&from=' + fromSeq);
          if (Array.isArray(data.events) && data.events.length > 0) {
            for (const ev of data.events) {
              term.write(ev.data || '');
              fromSeq = Math.max(fromSeq, ev.seq || fromSeq);
            }
          }
          if (!data.alive) {
            term.writeln('\\r\\n\\x1b[31m[进程已退出，可点“新建终端会话”重开]\\x1b[0m');
            sid = null;
            elSid.textContent = '-';
          }
          await new Promise(r => setTimeout(r, 70));
        } catch (e) {
          term.writeln('\\r\\n\\x1b[31m[连接异常] ' + e.message + '\\x1b[0m');
          await new Promise(r => setTimeout(r, 800));
        }
      }
    }

    term.onData(async (data) => {
      if (!sid) return;
      try {
        await api('/tui/input', 'POST', { sessionId: sid, data });
      } catch (e) {
        // do not spam terminal for transient input errors
      }
    });

    document.getElementById('btnNew').addEventListener('click', () => {
      newSession().catch(err => term.writeln('\\x1b[31m创建会话失败: ' + err.message + '\\x1b[0m'));
    });

    document.getElementById('btnClose').addEventListener('click', () => {
      closeSession().catch(err => term.writeln('\\x1b[31m关闭失败: ' + err.message + '\\x1b[0m'));
    });

    document.getElementById('btnExec').addEventListener('click', async () => {
      const command = elQuickCmd.value.trim();
      if (!command) return;
      elQuickOut.textContent = '执行中...';
      try {
        const data = await api('/exec', 'POST', { command });
        elQuickOut.textContent = data.error || data.output || '(无输出)';
      } catch (e) {
        elQuickOut.textContent = '执行失败: ' + e.message;
      }
    });

    elQuickCmd.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') document.getElementById('btnExec').click();
    });

    // auto boot one session
    newSession().catch(err => term.writeln('\\x1b[31m初始化失败: ' + err.message + '\\x1b[0m'));
  </script>
</body>
</html>`;

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || '127.0.0.1'}`);

  if (req.method === 'GET' && url.pathname === '/') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(html);
    return;
  }

  if (req.method === 'POST' && url.pathname === '/exec') {
    let body = '';
    req.on('data', c => (body += c));
    req.on('end', () => {
      try {
        const { command } = JSON.parse(body || '{}');
        if (!command || typeof command !== 'string') {
          return json(res, 400, { error: 'command 不能为空' });
        }

        const blocked = ['rm -rf /', 'mkfs', 'dd if=/dev/zero'];
        if (blocked.some(b => command.includes(b))) {
          return json(res, 400, { error: '危险命令被阻止' });
        }

        const output = execSync(command, {
          encoding: 'utf-8',
          timeout: MAX_EXEC_TIMEOUT_MS,
          maxBuffer: MAX_EXEC_BUFFER
        });
        return json(res, 200, { output: output || '(无输出)' });
      } catch (e) {
        return json(res, 200, { output: e?.stdout || e?.stderr || e?.message || '执行失败' });
      }
    });
    return;
  }

  if (req.method === 'POST' && url.pathname === '/tui/session') {
    const s = makeSession();
    return json(res, 200, { sessionId: s.id });
  }

  if (req.method === 'GET' && url.pathname === '/tui/poll') {
    const id = url.searchParams.get('sessionId');
    const from = Number(url.searchParams.get('from') || '0');
    const s = getSession(id);
    if (!s) return json(res, 404, { error: 'session not found' });
    const events = s.events.filter(ev => ev.seq > from);
    return json(res, 200, { alive: s.alive, events, latestSeq: s.seq });
  }

  if (req.method === 'POST' && url.pathname === '/tui/input') {
    let body = '';
    req.on('data', c => (body += c));
    req.on('end', () => {
      try {
        const { sessionId, data } = JSON.parse(body || '{}');
        const s = getSession(sessionId);
        if (!s) return json(res, 404, { error: 'session not found' });
        if (!s.alive) return json(res, 400, { error: 'session closed' });
        s.proc.stdin.write(String(data || ''), 'utf-8');
        return json(res, 200, { ok: true });
      } catch (e) {
        return json(res, 400, { error: e.message || 'bad input' });
      }
    });
    return;
  }

  if (req.method === 'POST' && url.pathname === '/tui/close') {
    let body = '';
    req.on('data', c => (body += c));
    req.on('end', () => {
      try {
        const { sessionId } = JSON.parse(body || '{}');
        closeSession(sessionId);
        return json(res, 200, { ok: true });
      } catch (e) {
        return json(res, 400, { error: e.message || 'bad input' });
      }
    });
    return;
  }

  res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end('Not Found');
});

server.listen(PORT, HOST, () => {
  console.log(`🦐 蹦蹦虾 Web CLI 运行在 http://${HOST}:${PORT}`);
});
