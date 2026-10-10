# QQ 桥：配置与控制

用 QQ 当 agent 的入口：直接聊天，或者发任务让它干活。
实时画面 / 截图见 [QQBOT-LIVE.md](QQBOT-LIVE.md)；命令总览见 [COMMANDS.md](COMMANDS.md)。

## 一句话上手

```bash
cd ~/my_agent
cp .env.example .env && nano .env      # 填 LLM_API_KEY 与 QQBOT_APP_ID/SECRET
.venv/bin/python -m agent.qqbot        # 前台跑通后，交给 systemd 常驻
```

平台侧必须先做两件事（否则一条消息都收不到）：

1. QQ 开放平台 → 开发设置/沙箱配置 → 把**你自己的 QQ 号**加为测试用户（群聊还要建沙箱群）
2. 用**重置后**的 AppSecret（`QQBOT_APP_SECRET`）

第一次握手：给机器人发一句话 → 它回你的 **openid** → 填进 `QQBOT_ALLOW_FROM` → 重启桥。

## 对话 vs 任务（两套通道）

| 你发 | 结果 |
|---|---|
| 寒暄（你好 / hi / 在吗 / 谢谢…，12 字以内） | **对话**：只聊天，不调工具、不开浏览器、不写盘 |
| 普通一句话（默认 `task` 模式） | **任务**：走完整 agent 循环（工具 + 审批 + 检查点） |
| `/do <任务>` | **执行一次任务**（在 chat 模式下也照做） |
| `/chat` | 切到**对话模式**：之后直接说话都是聊天 |
| `/chat <内容>` | 只这一条按对话回 |
| `/task` | 切回**任务模式**：发什么都当任务（寒暄仍走对话） |
| `/shot` | 把当前浏览器画面截图发到聊天 |
| `/status` | 会话号 · 模式 · 权限档 · 完成轮数 · 上次结果尾部 |
| `/new` | 开新会话（清上下文） |
| `/help` | 命令列表 |
| `y` / `a` / `n` | 审批答复：允许一次 / 本会话始终允许该工具 / 拒绝（超时=拒绝） |

### 上下文（跟"龙虾"一样的连续对话）

| | 说明 |
|---|---|
| 闲聊与任务 | **共用同一个会话**：聊过的内容，之后发任务它还记得（反之亦然） |
| 落盘 | 会话存在 `memory/sessions/qq-<你的 openid 哈希>.json` → **桥重启/服务器重启都不丢** |
| 清空上下文 | `/new`（同时清掉闲聊记忆） |
| 闲聊引擎 | `QQBOT_CHAT_ENGINE=agent`（默认，共享上下文、需要时能用工具）/ `llm`（轻量纯聊天，不落盘、省 token） |
| 每个 QQ 号 | 各自一个独立会话，互不串台 |

**想把它当聊天窗口用**（默认不跑任务）：

```ini
QQBOT_DEFAULT_MODE=chat      # 直接说话=聊天；干活显式 /do <任务>
QQBOT_CHAT_TURNS=10          # 对话记忆轮数
QQBOT_CHAT_MAX_TOKENS=1000   # 聊天回复上限（别开大，省配额）
```

**想让它默认干活**（推荐配合 `/chat` 偶尔闲聊）：保持默认 `QQBOT_DEFAULT_MODE=task`，
寒暄会自动走对话，不会被当成任务扔进工具循环。

## 常驻（systemd）

```bash
sudo cp examples/systemd/my-agent-qqbot.service /etc/systemd/system/
sudo sed -i "s#/home/ubuntu/my_agent#$HOME/my_agent#g; s#^User=.*#User=$USER#" \
  /etc/systemd/system/my-agent-qqbot.service
sudo systemctl daemon-reload && sudo systemctl enable --now my-agent-qqbot
journalctl -u my-agent-qqbot -f
```

- 桥**本身就是 agent 进程**（agent 跑在桥里），不需要再单起一个 agent。
- 改完 `.env` 要 `sudo systemctl restart my-agent-qqbot` 才生效。
- 升级后同样要重启：`bash scripts/upgrade.sh && sudo systemctl restart my-agent-qqbot`。

## 安全与限制（务必知道）

- **权限档锁死**：QQ 会话只允许 `ask` / `block`（`QQBOT_PERMISSION`），**不接受 full** ——
  聊天通道不允许提权；`/permission full` 这类只在本机 CLI 有效。
- **白名单**：`QQBOT_ALLOW_FROM` 之外一律拒绝（并把对方 openid 回显给你）；群聊还要 `QQBOT_ALLOW_GROUPS`。
- **主动消息每月仅 4 条**，所以只有"你发它回"可靠；别指望它自己定时推给你。
- 同一个机器人 token **只能跑一个桥**（两个进程会抢消息）。
- 别和本机 agent **共用同一个 LLM key**（配额共享，很容易一上来就 429）。
- 长任务受 `QQBOT_TASK_TIMEOUT`（默认 1800 秒）限制，到点优雅中断、已完成成果保留。
