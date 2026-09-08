> 【历史存档】本文记录 v2 架构升级（当时 64 个测试）。现状以 docs/BEST_PRACTICES.md 与 docs/COMMANDS.md 为准。

# my_agent v2 升级说明：融合主流开源 agent 框架优点

本次升级把 my_agent 从"文本 JSON 决策 + 字符串工具"的早期架构，
升级为对齐主流开源 agent 框架
设计理念的现代 Agent。全部 64 个测试通过（`.venv\Scripts\python -m pytest tests -q`）。

## 一、新能力总览

| 能力 | 模块 | 借鉴来源 | 说明 |
|---|---|---|---|
| 原生 function calling | `models/llm.py`, `agent/executor.py` | 主流工具协议 | 模型以 JSON Schema 参数调用工具，不再正则解析文本 |
| JSON Schema 工具 | `tools/base.py`, 全部工具 | 主流工具设计 | 每个工具声明 schema + execute_json；字符串接口保留兼容 |
| 审批策略 | `agent/approval.py` | 主流 approval_policy | untrusted / on-failure / on-request / never 四级 |
| 沙箱分级 | `agent/approval.py` | 上游框架文件策略命名 | read-only / workspace-write / danger-full-access |
| 命令安全分级 | `agent/approval.py` CommandSafety | 主流 exec_policy | 终端命令分 low/medium/high/blocked 四级 |
| Guardian 审校 | `agent/guardian.py` | 同类 guardian | 中高风险调用由审校模型二次把关 |
| AGENTS.md 指令 | `agent/instructions.py` | 同类 AGENTS.md | 项目/用户分层指令自动注入系统提示 |
| Rollout 追踪 | `agent/rollout.py` | 同类 rollout-trace | JSONL 事件流 + token 阈值对话压缩 |
| 会话持久化 | `agent/session.py` | 同类 thread | `--session NAME` 保存/恢复对话历史 |
| MCP server | `agent/mcp_server.py` | 同类 mcp-server | my_agent 可作为 MCP server 被上游宿主平台调用 |
| 工具输出截断 | `tools/tool_manager.py` | 同类输出上限 | 超长输出头部 80% + 尾部保留 |
| Python 沙箱收紧 | `tools/python.py` | 同类沙箱思路 | 黑名单 import（subprocess/socket/ctypes 等） |
| 视觉工具化 | `tools/vision_tool.py` | 同类 computer-use | see 工具：截图 + 视觉分析结构化调用 |

## 二、执行协议变化

### 旧（v1）
```
LLM 输出 JSON {"action":"use_tool","tool":"terminal","tool_input":"dir"}
→ 正则解析 → 执行 → 每步一次决策，靠 continue 循环
```

### 新（v3）
```
LLM 原生 tool_calls → 审批门 → Guardian → 执行 → 工具结果回喂
→ 模型继续决策（同一步骤内最多 max_step_ops 轮）→ 输出文字收尾 = 步骤完成
```

- 每个计划步骤内自动多轮工具调用，不再需要"continue"协议
- 供应商不支持 tools 时自动回退旧协议（`execute_step_legacy`），行为不变
- 新增 `think` 伪工具：模型可显式记录推理（输出不参与后续决策）

## 三、安全模型

决策顺序（每次工具调用）：
1. 硬性黑名单（fork bomb、mkfs、rm -rf /、del /f /s 等）→ 一律拒绝
2. 沙箱等级不足 → 拒绝
3. 工具主动请求批准（approval="on-request"）→ 按策略询问
4. 按 approval_policy 决策（untrusted 询问一切非低风险；on-failure 仅高风险询问；never 全放行）
5. Guardian 审校（medium 及以上风险，独立提示词，超时 fail-open）

## 四、命令行用法

```bash
python main.py "任务"                                  # 单 Agent
python main.py --approval untrusted "任务"             # 不信任模式
python main.py --sandbox read-only "任务"              # 只读沙箱
python main.py --session demo "任务"                   # 会话持久化
python main.py --list-tools                           # 查看工具与 schema
python main.py --mcp-server                           # MCP server（stdio）

# MCP server 接入主流宿主平台后，宿主可调用:
#   run_agent(goal, mode="single|team|research", approval_policy="never", ...)
#   list_agent_tools()
```

## 五、环境变量（.env / .env.example）

```bash
APPROVAL_POLICY=on-failure          # untrusted|on-failure|on-request|never
SANDBOX_MODE=workspace-write        # read-only|workspace-write|danger-full-access
GUARDIAN_ENABLED=true               # Guardian 安全审校
ROLLOUT_ENABLED=true                # 事件追踪（输出到 ./rollouts/*.jsonl）
ROLLOUT_COMPACT_TOKENS=24000        # 步骤内对话压缩阈值
SESSION_DIR=./memory/sessions       # 会话存储目录
TOOL_OUTPUT_MAX_CHARS=8000          # 工具输出截断上限
MAX_STEP_OPS=12                     # 单步骤最大工具操作数
AGENTS_PROJECT_FILE=AGENTS.md       # 项目指令文件
AGENTS_USER_FILE=~/.my_agent/AGENTS.md
```

## 六、兼容性说明

- `BaseTool.execute(input_str)`、`ToolResult(success/output/error)`、`Executor.execute_step` 返回字段、
  `run_once / run_team / run_research / dashboard / 交互模式` 全部保持兼容。
- Team / Research 模式暂未接入审批门（后续版本可加），执行协议仍为文本。
- Python 工具不再允许 `import subprocess`（安全收紧）；系统命令请走 terminal（受审批管理）。

## 七、测试

```bash
.venv\Scripts\python -m pytest tests -q     # 82 个测试
```
覆盖：工具 schema/执行/截断、命令安全分级、审批决策矩阵、Guardian 拦截、
function calling 主循环（FakeLLM）、旧协议回退、Rollout 压缩、AGENTS.md 加载、
会话、MCP server、LLM 自动重试、单循环模式（executor 层 + Agent 层）。

## 八、接入 OpenRouter stealth/ox-alpha（2026-08）

默认模型已切换为 OpenRouter 的 OpenAI stealth 推理模型：

```bash
LLM_API_KEY=sk-or-v1-xxxx
LLM_BASE_URL=https://openrouter.ai/api/v1      # 注意不是模型页面地址
LLM_DEFAULT_MODEL=stealth/ox-alpha
LLM_DEFAULT_MAX_OUTPUT_TOKENS=8192
LLM_MAX_RETRIES=2                              # 限流自动重试
```

实测结论（2026-08-24，直接探测 API）：
- 原生 function calling + 多轮工具循环（tool_calls → 结果回喂 → 文字收尾）完全兼容本执行器
- temperature 参数可用；JSON 输出模式可用；**图片输入可用**（1×1 PNG 识别为"红"）
- 上游限流（429）频繁 → `models/llm.py` 新增指数退避自动重试（429/5xx/网络错误）

模型端点分离（新增）：视觉与 Guardian 支持独立端点，主模型用 OpenRouter、
辅助模型用商汤 SenseNova 等快模型：

```bash
# 视觉模型（独立端点，留空回退主 LLM）
VISION_API_KEY=          VISION_BASE_URL=          VISION_MODEL=stealth/ox-alpha
# Guardian 审校（建议快模型；当前 ox-alpha 慢推理默认关闭 GUARDIAN_ENABLED=false）
GUARDIAN_API_KEY=        GUARDIAN_BASE_URL=        GUARDIAN_MODEL=
```

注意：
- 该 OpenRouter key 无付费额度（402 Insufficient credits），付费模型（如
  openai/gpt-4o-mini）不可用；stealth/ox-alpha 免费可用。
- ox-alpha 是推理模型，单个步骤可能耗时较长（数十秒），
  `ROLLOUT_COMPACT_TOKENS` 与 `GUARDIAN_TIMEOUT` 可按需调整。

## 九、v3.1：单循环执行模式（2026-08）

经典"规划 → 逐步执行 → 总结"三层架构存在三个问题：
1. 三层各调一次 LLM，同一信息被加工三遍（推理模型时代每个来回都很贵）
2. 步骤之间上下文断裂：每个步骤重建消息，模型看不到此前真实的工具调用与结果
3. 产生冗余步骤（如"执行计算"与"汇报结果"被拆成两步，第二步纯复述）

v3.1 引入 **单循环模式（默认）**，对齐主流 Agent Loop：
- 一轮持续对话完成整个目标：思考 → 工具调用 → 看结果 → 继续 → 最终回答
- 最终回答直接来自循环最后一轮输出，无独立总结层
- 整条消息线程贯穿任务，步骤间上下文零断裂
- 成功判定：最后一次工具操作成功（中间失败已补救仍算成功，失败留档供学习）

```bash
python main.py "任务"            # 默认单循环
python main.py --plan "任务"     # 经典计划模式（旧行为，保留）
python main.py --max-ops 40      # 单循环整次任务最大操作轮数
```

- 供应商不支持 function calling 时自动回退计划模式（旧文本协议）
- Team / Research 模式不受影响
- 计划模式提示词同步优化：执行与汇报合并为一步，禁止确认型冗余步骤
- UI：循环模式启动头显示模式/审批/沙箱徽章，不再显示无关的视觉徽章

## 十、v3.2：主流 CLI 风格终端界面（2026-08）

旧的 Rich 重型边框面板（HEAVY box + 阶段仪式 + 大标题框）替换为极简风格：

- 启动头：一行 Agent 名 + 目标 + 弱分隔线，无边框
- 状态行：一行灰字（`单循环 · 审批 never · 沙箱 workspace-write`），
  无 emoji 徽章；AGENTS.md 加载、Rollout 路径等日志不再刷屏
- 安全审批统计仅在发生拒绝时显示（警告样式）
- 交互模式：`> ` 提示符（去掉 rich 默认的 `: ` 后缀）+ 轮次间 `┄┄` 弱分割线，
  上一轮回答与下一轮提问清晰分离
- Team / Research 模式输出统一为新风格（`Team · ...` 灰字 + ✻ 小悟 总结）
- 工具调用：`⏺ tool(args)` 灰色内联 + `⎿ 结果`（失败红色，多行截断）——主流符号体系
- 最终回答：`✻ 小悟` 后直接输出 Markdown 文本，无"最终结果"大框
- 思考等待：真实 TTY 下显示 `✻ 思考中…` 原地转圈动画；
  管道重定向时自动禁用（避免日志里出现动画帧垃圾）
- **流式输出（v3.3）**：答案逐字渲染、o 系列模型的 reasoning 增量以灰色
  `✻ 思考` 块流式显示、工具调用在生成完成后立即渲染；供应商不支持流式时
  自动回退一次性调用；`--no-stream` 可强制关闭。实现位于
  `models/llm.py chat_with_tools_stream` + `agent/executor.py _stream_turn`
- **流式 Markdown**：`**加粗**` 在流式中实时渲染为粗体（`StreamingMarkdown`
  增量配对，跨 token 边界、未闭合标记兜底），不再显示字面星号
- **运行统计行**：每次运行结束输出 `· 2 轮 · 1 步 · LLM 27.4s · 工具 0.0s
  · 首 token 平均 0.43s · 1.8 tok/s · 缓存命中 21% · 输入 301 · 输出 50`；
  数据来自 `agent/metrics.py`（RunMetrics），LLM 层记录耗时/usage/首 token，
  流式调用通过 `stream_options.include_usage` 获取 usage（不支持的供应商自动降级）
- **空回复防御**：上游偶发只回 reasoning 就 stop（空回复）时自动追加提示重试，
  最多 2 次，避免"无文字总结"的假完成

## 十一、v3.4：自我升级前置（edit 工具 + git 快照安全网，2026-08）

为了让 Agent 能够安全地修改自己的代码（自我升级），补齐两个前置：

1. **`edit` 工具**（`tools/patch.py`，借鉴主流 apply_patch / edit）：
   `edit(file_path, old_string, new_string, replace_all)` 精确替换——
   只发送改动增量（几十 token），任何大小的文件都能改；
   old_string 必须精确匹配（默认恰好 1 次，多匹配报错），CRLF/LF 自动归一化，
   修改前自动生成 `.bak` 备份。解决了"整文件覆盖写超过 8K 输出上限"的死结。
   循环模式提示词已强制要求：改代码用 edit、禁止整文件覆盖、改完跑 pytest。
2. **git 快照安全网**（`agent/snapshot.py`）：每次 `run()` 开始前自动
   `git init`（如未初始化）+ 提交当前状态（`snapshot: 运行前快照 <时间> — <目标>`），
   任何一次自我修改的破坏都能 `git log` / `git diff` / `git revert` 回滚。
   静默失败不影响执行；身份未配置时用 my-agent 兜底；`SNAPSHOT_ENABLED=false`
   可关闭；测试环境已禁用（防污染真实仓库）。

自我升级的标准流程（已写入 AGENTS.md 与系统提示）：
读文件 → edit 精确改 → terminal 跑 pytest → 不通过就继续 edit → 全部通过收尾；
每次运行前的快照保证任何一步都可回滚。
- 审批提示：一行 `⚠ 需要批准 · tool` + `$ 命令` + `[y/N]`
- 输入消毒：goal 中的孤立代理字符（管道编码损坏场景）自动替换，防止 JSON 崩溃
- 全部渲染函数位于 `agent/ui_theme.py`，`tests/test_ui.py` 覆盖冒烟测试

注意：`agent/agent.py` 在 2026-08-24 曾因 PowerShell 双编码损坏，已按会话记录
完整重建（等价内容）；后续编辑文件请使用 UTF-8 安全的工具。

## 十二、切换主模型到内网 deepseek-v4-pro（2026-08-24 晚）

用户提供的内网模型服务（`http://172.16.10.242:3000/v1`，需在该内网可达）实测：

- `BAAI/bge-m3` 是**嵌入模型**，chat 端点直接 400——文档模板的占位模型，不能当对话用
- 服务器上有 17 个模型，其中 **deepseek-v4-pro** 实测最可用：
  纯文本 1.6s、function calling 正常、流式 + include_usage 完整、max_tokens 8192 OK；
  **deepseek-v4-flash** 更快（约 1s），适合 Guardian 审校；
  glm-5.2 / qwen3.7-max 欠费（429/Arrearage）、kimi-k2.7-code 502
- 不支持图片输入 → 视觉模型保留 OpenRouter 的 ox-alpha（独立端点）
- 配置：主模型 **deepseek-v4-flash**（约 1s 响应；此前为 pro，按用户要求
  统一用 flash）、GUARDIAN 同用 flash；OpenRouter 配置保留在 `.env` 注释中可切回

**踩坑修复（思考模式 reasoning 回传）**：deepseek 思考模式要求每轮响应里的
`reasoning_content` 在下一轮请求中原样回传，否则 400
"The reasoning_content in the thinking mode must be passed back to the API"。
修复：`LLMToolResponse` 新增 `reasoning`/`reasoning_field` 字段，流式与非流式
均捕获推理内容，assistant 消息构造时原样回传
（`models/llm.py`、`agent/executor.py`）。此前该 400 还会被误判为
"不支持 function calling"而触发计划模式回退——顺带说明 `_looks_like_unsupported`
对 `invalid_request_error` 的匹配过于宽松，仍待收紧。

效果对比（同一个 7×8 任务）：ox-alpha 27-120s → deepseek-v4-pro **LLM 4.8s ·
首 token 0.08s · 29 tok/s**，且不再有 429/空回复/流中断。
