# web-cli-8081

一个基于 Flask + xterm.js 的网页终端项目，默认监听 `8081` 端口。

## 启动

```bash
python3 web_cli_8081.py
```

然后浏览器访问：

- `http://<你的设备IP>:8081`

## 说明

- 服务端：Flask + PTY shell
- 前端：xterm.js
- 默认工作目录：`/root/.openclaw/workspace`
