# my_agent 快捷命令手册

> 全局启动：任何目录直接敲 `my-agent`（自动切换项目目录，无需 python main.py）
> 全套测试覆盖（pytest）；本文档随代码更新，以 `main.py --help` 为准。

## 一、全局启动命令（任何目录）

```cmd
my-agent                          # 交互模式
my-agent "任务描述"                # 单次任务
my-agent -q "任务"                 # 安静模式（无渲染输出）
my-agent --list-tools             # 查看全部工具（含 JSON Schema）
my-agent --team "复杂任务"         # 多 Agent 团队协作
my-agent --research "主题"         # 深度研究模式
my-agent --dashboard              # 已禁用（web 端停用；参数保留兼容，不再拉起服务）
my-agent --mcp-server             # 以 MCP server 运行（供上游宿主平台调用）
```

## 二、临时改配置（主流 CLI 风格，不写入 .env）

```cmd
# 1. 命令行参数
my-agent --model deepseek-v4-pro "任务"
my-agent --model <名> --base-url <API地址> --api-key <key> "任务"
my-agent --dangerously-skip-permissions "任务"   # = --approval never（主流 CLI 同名别名）
my-agent -r                                     # 恢复最近一次对话（-r 风格）

# 2. 会话内命令
> /model                          # 查看当前模型
> /model deepseek-v4-pro          # 切换模型（临时，并绑定到当前对话）
> /model <名>@<API地址>            # 连地址一起换
> /config                         # 查看当前生效配置（key 打码）

# 3. 环境变量（支持 ANTHROPIC_* 同名变量）
$env:ANTHROPIC_MODEL="deepseek-v4-pro"; my-agent     # 别名：MY_AGENT_MODEL / LLM_DEFAULT_MODEL
$env:ANTHROPIC_BASE_URL="https://..."; my-agent      # 别名：MY_AGENT_BASE_URL / LLM_BASE_URL
$env:ANTHROPIC_API_KEY="sk-..."; my-agent            # 别名：MY_AGENT_API_KEY / LLM_API_KEY
$env:MY_AGENT_MINIMAL="1"; my-agent                  # 极简模式（关掉 Guardian/追踪/快照/RepoMap 等非必要功能）
```

**对话绑定模型**：每个对话记录它当时用的模型；`--session <ID>` / `/open <ID>` / `-r` 恢复时自动切回。切换只影响主模型；视觉与 Guardian 各自独立配置。

## 三、执行模式与安全参数

```cmd
my-agent --loop "任务"             # 单循环模式（默认，主循环式）
my-agent --plan "任务"             # 经典计划模式（规划→逐步执行→总结）
my-agent --auto-mode "任务"        # 意图自动路由：调研/报告类→研究；团队/并行类→团队
my-agent --approval never "任务"   # 审批策略: untrusted | on-failure | on-request | never
my-agent --sandbox read-only "任务" # 沙箱: read-only | workspace-write | danger-full-access
my-agent --no-guardian             # 关闭 Guardian 审校
my-agent --no-rollout              # 关闭事件追踪日志
my-agent --no-stream               # 关闭流式输出
my-agent --no-vision               # 关闭视觉能力
my-agent --max-ops 40              # 单循环整次任务最大操作轮数（默认 24）
my-agent --max-step-ops 15         # 计划模式单步骤最大操作数（默认 12）
my-agent --max-steps 30            # 计划模式最大步骤数（默认 20）
my-agent --session <对话ID>        # 恢复指定对话（见下方对话管理）
```

## 三、交互模式命令（`>` 提示符下输入）

| 命令 | 说明 |
|---|---|
| `exit` / `quit` / `q` | 退出 |
| `/tools` | 查看工具列表 |
| `/team <任务>` | 团队协作（可连写：`/team任务`） |
| `/research <主题>` | 深度研究（可连写：`/research主题`） |
| `/image <描述>` | 文生图（SenseNova U1.5 Lite，生成并保存到 `generated_images/`） |
| `/memory` | 记忆总览（各层条数 + 完成/失败步骤）；`/memory prune 100` 清理长期记忆 |
| `/help` | 查看全部可用命令 |
| `/sessions` | **查看全部对话 ID**（● 标记当前对话） |
| `/open <对话ID>` | 打开并恢复某对话的全部记录，继续聊 |
| `/new` | 新开一个对话 |
| `Ctrl+C` | 中断当前任务，回到提示符（不会退出程序） |
| 打错命令 | 自动提示最近命令（如 `/reasearch` → 建议 /research） |

## 三·五、多行输入（对齐主流 agent 的粘贴体验）
- **直接粘贴多行文本**：回车一次提交整块，内部换行完整保留，不会像单行
  读取那样只剩第一行。收到后会提示"已接收多行输入（N 行）"。
- **手工换行**：行尾输入 `\` 再回车（主流 CLI 同款续行约定），续行提示符 `…`：
  ```
  > 写一个脚本：\
  > … 读取 data.json \
  > … 输出统计结果
  ```
- 注意：Windows 路径行尾 `C:\path\`（单个反斜杠结尾）也会触发续行，
  如要输入字面反斜杠请写两个 `\\`。
- 管道/重定向（非 TTY）模式保持逐行一条消息，不自动合并。

## 三·六、文生图（SenseNova Token Plan）

- 交互命令：`/image <图片描述>`，例如：
  ```
  > /image 一张信息图海报：标题「AI Agent 架构」，蓝色扁平风格，留白充足
  ```
- Agent 任务中：直接让它"生成一张 XX 的图片"即可，会自动调用 `image_gen` 工具。
- 生成结果保存到 `./generated_images/`（已在 .gitignore），返回本地文件路径。
- 配置（.env，OpenAI 兼容 `/images/generations` 端点）：
  ```
  IMAGE_GEN_API_KEY=sk-...                 # 留空回退主 LLM key
  IMAGE_GEN_BASE_URL=https://token.sensenova.cn/v1
  IMAGE_GEN_MODEL=sensenova-u1.5-lite      # 或 sensenova-u1-fast（信息图加速）
  IMAGE_GEN_SIZE=1024x1024                 # 1024x1024 / 768x1024 / 1280x720 ...
  IMAGE_GEN_SAVE_DIR=./generated_images
  IMAGE_GEN_WATERMARK=false                # 公测期免费开放去水印：false = 不带水印
  ```
- 模型选择：`sensenova-u1.5-lite`（构图/光影/文字渲染增强）、
  `sensenova-u1-fast`（信息图加速版）。

## 三·七、运行中卡住的排查

**症状**：程序跑着跑着不动了，但进程还在（CPU 为 0）。

**第一反应（Windows conhost 输出阻塞，最常见）**：
1. 点一下窗口按 `Esc`（取消 QuickEdit 文本选区）或右键；
2. 如果按过 `Ctrl+S`（暂停输出），按 `Ctrl+Q` 恢复；
3. 仍不行按 `Ctrl+C` 中断当前任务回到 `>` 提示符（对话不丢）。

**预防**：`.env` 设 `MY_AGENT_DISABLE_QUICKEDIT=true`——启动时自动关闭
QuickEdit 选区（退出时恢复），代价是该窗口失去鼠标框选复制
（Windows Terminal 不受影响）。

**查日志**：每次运行的事件流在 `rollouts/run-*.jsonl`，文件 mtime 停在
哪个事件、进程有无子进程/网络连接，可以定位卡点（卡在输出写 vs 卡在
等待模型 vs 卡在命令执行，现象各不相同）。

## 四、对话 ID（会话）管理——重点

**查看对话 ID 的方式：**

1. **启动时**：欢迎信息下方显示当前对话 ID
   ```
   · 对话 ID: conv-20260825-a1b2c3（下次用 --session conv-... 恢复全部记录）
   ```
2. **交互中输入 `/sessions`**：列出全部历史对话
   ```
   · 历史对话（2 个）:
     ● conv-20260825-a1b2c3  帮我写报告  12 条 · 2026-08-25 09:30
       conv-20260824-x9y8z7  计算任务    4 条 · 2026-08-24 23:47
   ```
3. **直接看文件**：`memory/sessions/` 目录下的 JSON 文件名就是对话 ID

**恢复对话（打开全部记录）：**
```cmd
my-agent --session conv-20260825-a1b2c3     # 启动时恢复
> /open conv-20260825-a1b2c3                # 交互中切换
```

**特点**：每轮结束自动全量保存（不截断）；标题取第一条用户消息；最多保留 50 个对话（SESSION_MAX）。

## 五、运行时快捷键

| 键 | 作用 |
|---|---|
| `Ctrl+C` | 任务执行中：中断并回到 `>` 提示符；提示符下：退出程序 |
| 审批提示时 | `y` 批准 / 回车或 `n` 拒绝 |

## 六、环境变量（.env 常用项）

| 变量 | 默认 | 说明 |
|---|---|---|
| LLM_DEFAULT_MODEL | — | 主模型（当前 deepseek-v4-flash） |
| LLM_BASE_URL / LLM_API_KEY | — | 主模型端点 |
| LLM_TIMEOUT | 300 | 请求超时秒数（防挂死） |
| LLM_MAX_RETRIES | 2 | 限流自动重试次数 |
| APPROVAL_POLICY | on-failure | 审批策略（当前 .env 已设 never） |
| SANDBOX_MODE | workspace-write | 沙箱等级 |
| VISION_MODEL / VISION_API_KEY / VISION_BASE_URL | 回退主 LLM | 视觉模型（可走独立端点） |
| GUARDIAN_ENABLED | true | Guardian 审校开关 |
| GUARDIAN_MODEL | 主模型 | 审校模型（当前 deepseek-v4-flash） |
| ROLLOUT_ENABLED / ROLLOUT_DIR | true / ./rollouts | 事件追踪（JSONL 日志） |
| SESSION_DIR / SESSION_MAX | ./memory/sessions / 50 | 对话存储 |
| SNAPSHOT_ENABLED | true | 运行前 git 快照安全网 |
| TEST_COMMAND | .venv\Scripts\python -m pytest tests -q | Agent 自测命令 |
| EDIT_PREFLIGHT | false | edit 改 .py 后自动跑测试，失败自动回滚（true 开启） |
| AUTO_MODE | false | 意图自动路由（等同 --auto-mode） |
| TUI_STATUS_BAR | true | 常驻状态栏：每轮刷新 token 计数/沙箱/策略（verbose 下） |
| TEAM_PARALLEL | false | Team 并行执行独立子任务（并行安全门 + 自动回退串行） |
| TEAM_WORKER_TIMEOUT | 900 | 并行 Worker 硬超时秒数（超时标记该子任务失败） |
| APPROVAL_COMMAND_WHITELIST | false | 命令白名单深度防御：终端命令未命中白名单需人工批准（never 下直接拒绝） |
| APPROVAL_COMMAND_WHITELIST_EXTRA | 空 | 追加白名单正则（用 \| 分隔） |
| SANDBOX_EXECUTION | off | OS 级沙箱：appcontainer = 终端前台命令进 Windows AppContainer（仅工作区可写、无网络、fail-closed） |
| SANDBOX_EXEC_TIMEOUT | 120 | 沙箱内命令硬超时秒数 |
| SANDBOX_GRANT_TOOLS | true | 沙箱启动时给 Python/Node/Git 解释器目录授予容器只读执行权限（icacls best-effort，false 关闭） |
| SANDBOX_GRANT_DIRS | 空 | 额外只读授权目录清单（; 分隔，工作区之外的共享库/数据集等） |
| SANDBOX_ALLOW_NETWORK | false | 容器网络放行：true 注入 internetClient 等三个 capability（实测私网可达；外网另受本机网络出口限制） |
| TERMINAL_FOREGROUND_TIMEOUT | 120 | 终端前台命令超时秒数（60s 会掐断负载下的全量测试；后台命令不受限） |
| HOOKS_ENABLED | false | 工具钩子开关：加载 HOOKS_FILE 中的 on_pre_tool_use / on_post_tool_use 回调（fail-open） |
| HOOKS_FILE | ./hooks.py | 钩子模块路径 |
| APPROVAL_EXEC_POLICY_ENABLED | false | execpolicy 结构化命令策略开关（deny 优先 + fail-open；不能豁免黑名单/沙箱等级） |
| APPROVAL_EXEC_POLICY_FILE | ./execpolicy.json | 策略规则文件（JSON 数组：match 的 tool/command_prefix/pattern + decision 的 allow/deny/ask） |
| SKILLS_ENABLED | true | Skills 技能包开关：扫描技能目录，goal 命中关键词时把 SKILL.md 正文注入系统提示 |
| SKILLS_DIR | ./skills | 项目级技能目录（每个含 SKILL.md 的子目录即一个技能） |
| SKILLS_USER_DIR | ~/.my_agent/skills | 用户级技能目录 |
| SKILLS_MAX_CHARS | 6000 | 注入系统提示的技能文本总长上限 |

**技能目录结构**：`skills/<技能名>/SKILL.md`（frontmatter：name/description/triggers）+ 同目录普通文件自动登记为「附带脚本」。脚本仅被**告知**给 agent（渲染绝对路径清单），执行仍走终端 → 审批门原样生效；需免询问请在 execpolicy.json 显式加 allow 规则。

**技能包安装/更新**（完整性校验：MANIFEST.sha256 逐文件校验；未签名包默认拒绝）：

```cmd
python main.py --skills-install <技能包目录>                 # 安装到项目级 skills/
python main.py --skills-install <包> --skills-target user    # 安装到用户级
python main.py --skills-install <包> --skills-overwrite      # 更新已存在的同名技能
python main.py --skills-install <包> --skills-allow-unsigned # 放行无 manifest 的包（不推荐）
```

## 七、长任务与后台命令（terminal 工具）

- 执行时传 `background=true`（模型自动使用），或交互里直接说"后台跑 XX 服务"：
  - `terminal bg list` 列出后台任务
  - `terminal bg output <任务ID> [N]` 看最近 N 行输出
  - `terminal bg kill <任务ID>` 结束任务（输出仍可查看）
- 并行工具调用：模型一轮返回多个互不依赖的调用（读文件/只读浏览器/
  低风险终端/生图）时自动并发执行，结果按原顺序回喂；混入编辑等
  有冲突风险的工具时整批保持串行。

## 八、排查工具

```cmd
my-agent --list-tools                     # 看工具和 schema
dir rollouts                              # 运行日志（JSONL，含 llm_error/reasoning_len）
dir memory\sessions                       # 对话记录文件
git log --oneline                         # 每次运行前的自动快照（可回滚点）
git diff HEAD -- <文件>                   # 看 Agent 改了什么
git revert <commit>                       # 回滚某次运行
.venv\Scripts\python -m pytest tests -q   # 跑测试（全套）
```
