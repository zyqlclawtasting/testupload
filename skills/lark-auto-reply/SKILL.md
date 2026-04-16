---
name: lark-auto-reply
description: 使用 lark-cli 搭建飞书自动回复与定时监控流程（联系人私聊或群聊）。当用户要求“开发/整理飞书自动回复 skill”“监控指定联系人或群聊并自动回复”“梳理 lark-cli 安装配置与授权流程”“做消息检索+判断+自动发送闭环”时使用。包含稳定授权（避免仅拿到授权链接就提前退出）、目标解析、消息拉取、判定去重、发送回复、cron 任务接入。
---

# Lark Auto Reply

## Quick Start

0. 先做安装/配置前置检查（新增机制）：
   ```bash
   bash scripts/preflight_check.sh
   ```
   - 若提示未安装 `lark-cli`：先走 `lark-cli-setup` skill 完成安装
   - 若提示未配置或 token 无效：按提示先完成 `config init` 与授权

1. 跑稳定授权（必须等到授权完成，不是拿到链接就结束）：
   ```bash
   bash scripts/auth_login_stable.sh
   ```
2. 跑一次“监控+判断+自动回复”验证闭环：
   ```bash
   python3 scripts/lark_auto_reply_once.py --user-query "李翔" --history-limit 50
   ```
3. 需要群聊时改为：
   ```bash
   python3 scripts/lark_auto_reply_once.py --chat-query "001小白鼠群" --history-limit 50
   ```

---

## 稳定授权流程（核心约束）

### 背景问题
不同模型/执行器稳定性不同：
- 好的执行器会在出现授权链接后持续等待用户授权，并在授权后保持流程完成；
- 不稳定执行器会在“检测到链接”后直接退出，导致假成功。

### 必做约束
- 登录完成条件必须是：
  - `lark-cli auth status` 返回 `tokenStatus=valid`；
  - 建议再过 `lark-cli doctor`。
- “出现授权链接”只代表授权流程开始，不代表完成。
- 若中途退出，必须重试直到状态有效或超时。

### 推荐执行方式
- 用可持续观察输出的方式执行（例如会话/进程轮询）；
- 或直接使用本 skill 的 `scripts/auth_login_stable.sh`。

---

## 工作流：监控 -> 判断 -> 回复

### Step 1) 解析目标
- 联系人：`lark-cli contact +search-user --query "关键词"`
- 群聊：`lark-cli im +chat-search --query "群名关键词"`

### Step 2) 拉取最新消息
- 私聊：`lark-cli im +chat-messages-list --user-id <open_id> --page-size 50 --sort desc`
- 群聊：`lark-cli im +chat-messages-list --chat-id <chat_id> --page-size 50 --sort desc`

### Step 3) 判定是否需要回复
建议规则（可叠加）：
- 明确提问（`?` / `？`）；
- 命中关键词（如：自动回复、怎么实现、流程、卡住）；
- 群里点名你（@你 / 指名）；
- 排除删除消息、系统消息、自己发出的消息。

### Step 4) 去重与冷却
- 按“消息归一化文本+目标”做指纹；
- 同题冷却窗口内跳过（例如 30 分钟 / 24 小时）。

### Step 5) 发送回复
- `lark-cli im +messages-send --as user --user-id ... --text ...`
- 或 `--chat-id ...`
- 推荐末尾固定加：`（本条消息由 ClawPhone 助手代发）`

---

## 定时任务接入（cron）

`cron` 的 `payload.kind=agentTurn` 文案建议短而可执行，包含：
1) 监控目标（open_id/chat_id）；
2) 拉取范围（最近50条+线程）；
3) 判定规则；
4) 去重窗口；
5) 回复格式要求（1-3段，先结论后步骤）。

常用周期：
- 每天：`0 9 * * *`
- 每小时：`0 * * * *`

---

## Scripts

### `scripts/preflight_check.sh`
安装/配置前置检查脚本。职责：
- 检查 `lark-cli` 是否已安装；
- 检查是否已完成 `config init`（appId 存在）；
- 检查 `auth status` 中 `tokenStatus` 是否为 `valid`；
- 输出明确下一步（安装 / 配置 / 授权）。

### `scripts/auth_login_stable.sh`
稳定授权脚本。职责：
- 检查当前 token；
- 发起 `auth login --no-wait --json` 获取授权信息；
- 持续等待并轮询直至授权成功；
- 最终以 `auth status` 有效为准。

### `scripts/lark_auto_reply_once.py`
单次执行“解析目标 -> 拉消息 -> 判定 -> 去重 -> 自动回复”。

示例：
```bash
python3 scripts/lark_auto_reply_once.py \
  --user-query "李翔" \
  --history-limit 50 \
  --keywords "自动回复,怎么实现,流程,卡住" \
  --signature "（本条消息由 ClawPhone 助手代发）"
```

---

## References

- `references/command-cheatsheet.md`: lark-cli 常用命令清单
- `references/design-notes.md`: 技术选型、失败模式、防呆策略、Skill 扩展建议
