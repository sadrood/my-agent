# QQ 桥：配置与控制

用 QQ 当 agent 的入口：直接聊天，或者发任务让它干活。
实时画面 / 截图见 [QQBOT-LIVE.md](QQBOT-LIVE.md)；命令总览见 [COMMANDS.md](COMMANDS.md)。

## 一句话上手

```bash
cd ~/my_agent
cp .env.example .env && nano .env      # 填 LLM_API_KEY
.venv/bin/python -m agent.qqbot --bind # 手机 QQ 扫码，AppID/AppSecret 自动写进 .env
.venv/bin/python -m agent.qqbot        # 前台跑通后，交给 systemd 常驻
```

`--bind` 走腾讯轻量绑定接口（`agent/qqbot_bind.py`）：手机 QQ 扫一次码就把 AppID/AppSecret
写进 `.env`，密钥不回显、不进日志；比去开放平台手动复制省事。细节参数见
`python -m agent.qqbot_bind --help`（`--timeout` 等扫码秒数、`--no-write-env` 只看不写）。
选 `--no-write-env` 时密钥**既不落盘也不回显**，需要它请同时加 `--print-secret`
（会打到终端，注意别贴进聊天/日志）。

绑完想确认这对凭据真能用（不必再扫一次码）：`python -m agent.qqbot_bind --verify`。
它读的是 `.env` **文件**，并会点出"环境变量与文件不一致"——长驻进程（agent / 看板）会在
启动那一刻把 `.env` 冻结进自己的环境，此后改 `.env` 对**它**不生效；看到这句就从新终端启动。

平台侧必须先做两件事（否则一条消息都收不到）：

1. QQ 开放平台 → 开发设置/沙箱配置 → 把**你自己的 QQ 号**加为测试用户（群聊还要建沙箱群）
2. 用**重置后**的 AppSecret（`QQBOT_APP_SECRET`）

> **机器人上线后**：后台不再有"沙箱配置"入口（已上线的机器人用"使用范围"控制），
> 此时 `.env` 必须设 `QQBOT_SANDBOX=false`，否则连的是沙箱网关、收不到任何群事件。
> 判断是否已上线：后台统计能看到进群数就是已上线。详见文末排查实录坑 1。

第一次握手：给机器人发一句话 → 它回你的 **openid** → 填进 `QQBOT_ALLOW_FROM` → 重启桥。

> 群里没有"第一次握手"的对应物，但桥做了自反馈：群里 @ 它，被拒时会直接回你
> **群 OpenID**（不是群号），填进 `QQBOT_ALLOW_GROUPS` 即可。

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

## 排查实录：2026-10-13「私聊能回、群里完全不回」

同一天踩了三个互相叠加的坑，一度误判三次，最后靠"私聊正常"这条对照才定位准。记下来省下次一小时。

### 坑1：环境连错（`QQBOT_SANDBOX`）

**症状**：私聊能通，群里一条消息都不进，日志连被拒记录都没有。

**根因**：机器人在开放平台**已上线**（后台统计能看到"进群 1"），而 `.env` 里
`QQBOT_SANDBOX=true` 让botpy 走**沙箱网关**。沙箱里没有那个群，事件永远推不过来。
沙箱环境压根没有"添加群"的入口——因为已上线的机器人不需要它。别去找。

**判断依据**：后台统计能看到进群数 = 已上线= 该设 `QQBOT_SANDBOX=false`。

### 坑 2：群白名单填的是群号（填了永远不匹配）

**症状**：群里 @ 机器人，它回一句「这个群不在群白名单里。要放行请把下面这串加进
.env 的 QQBOT_ALLOW_GROUPS 后重启：`<群OpenID>`」。

**根因**：`allowed()` 比对的是 `group_openid`（腾讯发的不透明 ID），**不是人看的群号**。
这个 ID 只能从事件里拿，所以桥做成了"被拒时自回显"——不用去后台找。

**两道白名单**（`agent/qqbot.py` 的 `allowed()`）：

| 白名单 | 配置项 | 管什么 |
|---|---|---|
| 群在名单里 | `QQBOT_ALLOW_GROUPS` | 这个群允许用吗 |
| 发言人在名单里 | `QQBOT_ALLOW_FROM` | 这个人能指挥吗 |

即使群在名单里，**群成员不在 `QQBOT_ALLOW_FROM` 里照样不理你**。

### 坑 3（真正的元凶）：开了「获取群内全部消息」，而 botpy 不认这个事件

**症状**：一切正常后突然"完全不回"，进程存活、日志一片空白、连心跳都不打了。

**排查弯路**：先误判成 WebSocket 断线不重连（`botpy/connection.py` 的 `_runner` 确实
没有重连逻辑），但用户一句"为什么私聊没问题"直接推翻——私聊当时刚成功，WS 好得很。

**真根因**（stderr里只有一行，报错极轻）：

```
[ERROR] (gateway.py:97)on_message _parser unknown event group_message_create.
```

链条：

1. 手机 QQ 里把「机器人可获取的群聊消息范围」设成**获取群内全部消息**
2. 平台开始推 `GROUP_MESSAGE_CREATE`（群全量消息事件）
3. `botpy` 的 `ConnectionState` 靠扫描 `parse_xxx` 方法自动建事件表，
   而**它没有 `parse_group_message_create`**（只有 `parse_group_at_message_create`
   和 `parse_c2c_message_create`）
4. `gateway.py` 查表 KeyError → 打一行错误日志 → **该条事件被丢弃**

注意第4 步的准确行为：`on_message` 里是 `except KeyError: _log.error(...)`，
**不抛异常、不影响其他事件**。所以严格说不是"中断整个分发"，
而是：**全量事件来了没人接，@ 事件仍走原生解析器**。
真正让群 @ 失效的是平台侧——开了全量消息后，@ 消息被合并进全量通道推送，
而那个通道 SDK 接不住。

**解决**：把群设置改回**仅接收 @ 消息**。桥侧已加兜底补丁
（`_build_client()` 里给 `BotWebSocket.__init__` 打补丁，往parser 表注入
`group_message_create -> lambda payload: None`），保证即使再误开全量也不会再哑掉。

### 附：富文本（Markdown）能用了

官方 **2026-04-23** 起，单聊/群聊的自定义 Markdown 已对**所有机器人开放，无需申请模板**
（频道场景仍需内邀）。用法 `msg_type=2` + `markdown={"content": ...}`，
注意**传了markdown 后 `content` 必须为空**。支持标题、加粗/下划线/斜体/删除线、
链接、图片、有序无序嵌套列表、块引用、分割线。

`USE_MARKDOWN = False` 可退回"剥掉标记发纯文本"（`_md_to_text()`）。

切分不能按固定字数硬切——会把 ``` 围栏和标题劈开，用 `_chunks_markdown()`
按行边界切并跟踪围栏状态；条数封顶用 `_cap_markdown()`（**只裁块数不动块内结构**，
别复用 `_chunks_capped`，它内部硬切会破坏 Markdown）。

### 下次排查顺序（照这个走，别跳步）

```
私聊还能通吗？
├─ 不能 → 查连接层：进程存活？日志有没有新行？SDK 有没有报错
└─ 能   → 群侧问题，按序查：
          1. QQBOT_SANDBOX=false？（后台统计有进群数 = 已上线）
          2. 群里 @ 一下，看是否回显群 OpenID → 填 QQBOT_ALLOW_GROUPS
          3. stderr 搜 "unknown event" → 有就是坑 3，改回「仅 @ 消息」
          4. 群设置确认「仅接收 @ 消息」
```

最快的判别法：**私聊通= 连接和白名单没问题，问题一定在群侧**。
