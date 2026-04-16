# lark-auto-reply 命令速查

## 1) 安装/配置/授权

```bash
lark-cli --version
lark-cli config init
lark-cli auth login
lark-cli auth status
lark-cli doctor
```

> 注意：`auth login` 看到授权链接后必须继续等待用户授权完成，不可提前退出。

## 2) 目标解析

### 联系人 -> open_id
```bash
lark-cli contact +search-user --query "李翔" --format json
```

### 群聊 -> chat_id
```bash
lark-cli im +chat-search --query "001小白鼠群" --format json
```

## 3) 拉取消息

### 私聊
```bash
lark-cli im +chat-messages-list --user-id "ou_xxx" --page-size 50 --sort desc --format json
```

### 群聊
```bash
lark-cli im +chat-messages-list --chat-id "oc_xxx" --page-size 50 --sort desc --format json
```

### 线程消息（可选）
```bash
lark-cli im +threads-messages-list --thread "om_xxx或omt_xxx" --format json
```

## 4) 发送回复

### 发给联系人
```bash
lark-cli im +messages-send --as user --user-id "ou_xxx" --text "你的回复内容"
```

### 发到群聊
```bash
lark-cli im +messages-send --as user --chat-id "oc_xxx" --text "你的回复内容"
```

## 5) 脚本入口

```bash
bash scripts/auth_login_stable.sh

python3 scripts/lark_auto_reply_once.py --user-query "李翔" --history-limit 50
python3 scripts/lark_auto_reply_once.py --chat-query "001小白鼠群" --history-limit 50
```
