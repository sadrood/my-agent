# 开源 Agent 工程实践对标表（BEST PRACTICES）

> 目标：不再逐个踩坑，而是把成熟开源 Agent 项目的工程实践系统性地移植过来。
> 状态：✅ 已落地 · 🔶 部分落地 · ⬜ 待做

## 一、主流开源 Agent CLI（Rust，Apache-2.0）

| 实践 | 我们的对应 | 状态 |
|---|---|---|
| approval_policy 四级 + sandbox 分级 | `agent/approval.py` | ✅ |
| Guardian 后台安全审校 | `agent/guardian.py`（快模型审校） | ✅ |
| rollout-trace 事件流 + 对话压缩 | `agent/rollout.py`（JSONL + compaction） | ✅ |
| AGENTS.md 分层指令 | `agent/instructions.py` | ✅ |
| apply_patch 结构化编辑 | `tools/patch.py` EditTool | ✅ |
| mcp-server 化（被宿主调用） | `agent/mcp_server.py` | ✅ |
| thread/会话恢复 | 对话 ID 体系（`/sessions` `/open`） | ✅ |
| **环境自检命令（doctor）** | **`agent/doctor.py` + `my-agent --doctor`（9 项检查）** | ✅ 本次 |
| **app-server 默认绑定 localhost（安全）** | Dashboard 默认 127.0.0.1 + `--dash-host` | ✅ 本次 |
| **依赖锁定（uvicorn[standard] 等 extras）** | requirements 声明 extras | ✅ 本次 |
| OS 级沙箱（seatbelt/landlock/AppContainer） | `agent/sandbox.py`（Windows AppContainer，fail-closed） | ✅ 本次 |
| execpolicy（子代理命令策略 DSL） | `agent/execpolicy.py` 结构化规则（deny 优先 + fail-open；黑名单/沙箱边界由 ApprovalPolicy 保证不可豁免） | ✅ 本次 |
| 多代理并行（multi_agents） | Team DAG 依赖调度（depends_on 波次执行 + 并行波 + 串行化锁） | ✅ 本次 |
| **工具调用并行执行** | `executor` 批量并发（并行安全门 + 有序回喂） | ✅ |
| **后台命令 + kill** | `terminal` background=true + bg list/output/kill | ✅ |
| **apply_patch preflight 验证式应用** | `edit` 后跑测试，失败自动回滚（EDIT_PREFLIGHT） | ✅ |
| TUI 状态栏/快捷键 | 流式终端，无状态栏 | ⬜ |

## 二、主流商业 CLI

| 实践 | 我们的对应 | 状态 |
|---|---|---|
| 极简终端界面（⏺/⎿ 符号体系） | `agent/ui_theme.py` v3 | ✅ |
| **常驻状态栏（token 计数/沙箱/策略）** | `RunMetrics.render_status_bar` + `turn_start` 事件（每轮刷新） | ✅ 本次 |
| **修改 diff 展示（红-绿+上下文灰+@@ 行号）** | `print_unified_diff`（.bak 真 diff） | ✅ 本次 |
| 流式输出 + thinking 灰色展示 | `chat_with_tools_stream` + 渲染 | ✅ |
| 对话恢复（--continue / --resume） | `--session` / `/open` | ✅ |
| **会话内切换模型（/model）与配置查看（/config）** | `/model` `/config` + `--model/--base-url/--api-key` | ✅ 本次 |
| 项目级记忆文件 | AGENTS.md（同源） | ✅ |
| Hooks（PreToolUse/PostToolUse） | `agent/hooks.py` HookManager（fail-open、默认关闭、Agent 自我升级落地） | ✅ 本次 |
| 子代理（subagents） | Team Worker（旧协议） | 🔶 |

## 三、上游宿主框架

| 实践 | 我们的对应 | 状态 |
|---|---|---|
| 沙箱命名（workspace-write/danger-full-access） | 同名配置 | ✅ |
| skills 技能包 | `agent/skills.py` SkillManager（SKILL.md frontmatter + 关键词命中注入系统提示，四次自我升级落地） | ✅ 本次 |
| 运行统计状态行（轮/步/耗时/缓存命中） | `agent/metrics.py` | ✅ |

## 四、repo-map / 自动提交型工具

| 实践 | 我们的对应 | 状态 |
|---|---|---|
| **自动 git 提交（每次修改留痕）** | 运行前快照 + **逐操作 checkpoint（edit/写文件后立即提交）** | ✅ |
| **repo map（仓库结构图注入上下文）** | `agent/repomap.py`（代码任务关键词命中时注入） | ✅ 本次 |
| --doctor 自检 | 本次已落地 | ✅ |

## 五、逐操作 checkpoint 型工具

| 实践 | 我们的对应 | 状态 |
|---|---|---|
| **每次工具调用自动 checkpoint** | `executor` 修改成功后立即 git 提交（CHECKPOINT_PER_TOOL） | ✅ 本次 |
| 浏览器工具 | browser 工具 + see 视觉 | ✅ |

## 六、Goose / OpenHands

| 实践 | 我们的对应 | 状态 |
|---|---|---|
| MCP 优先扩展 | MCP 客户端 + server 双模式 | ✅ |
| 事件流架构（前端/调试共用） | rollout JSONL + dashboard hub | 🔶 |
| **意图自动路由** | `agent/mode_router.py`（--auto-mode：调研→research、团队/并行→team） | ✅ |

## 七、本次「踩坑 → 制度化」清单

| 踩过的坑 | 制度化手段 |
|---|---|
| websockets 缺失 → /ws 404 刷屏 | doctor 检查依赖 + 前端指数退避 + HTTP 轮询兜底 + uvicorn[standard] |
| localhost 解析 IPv6 → 页面打不开 | 默认绑定 127.0.0.1 + 启动信息显式地址 |
| cmd 文件中文注释被 GBK 破坏 | my-agent.cmd 纯 ASCII（规则：批处理文件禁非 ASCII） |
| 模型用 ls/head/tail 失败 | 系统提示注入平台说明 |
| 思考模式 reasoning 未回传 400 | 消息线程 reasoning 回传 + llm_error/reasoning_len 埋点 |
| 失败被记成成功污染经验库 | 成功判定保守化 + doctor 不涉及（测试覆盖） |
| 前台终端 60s 超时掐断负载下的全量测试 | `TERMINAL_FOREGROUND_TIMEOUT` 可配置（默认 120s）+ 超时报错引导转后台 |
| 沙箱内「拒绝访问」裸报错导致模型反复盲试 | 识别拒绝类失败并回喂引导文案（改用 python 工具 / 关沙箱） |
| 并行安全门整批回退，真实任务并行名存实亡 | 角色级门改为按工具名互斥锁：LLM 并发、有状态工具串行化 |
| 沙箱内项目工具链（python/node）全部被拒 | `SANDBOX_GRANT_TOOLS`：解释器目录级 AC RX 授权（继承覆盖运行时） |
| 沙箱内 stderr 中文乱码（容器 cmd 输出 GBK） | 无害已知问题：按 utf-8 replace 解码不影响退出码判断；以退出码/ASCII 标记做断言 |

## 八、桌面客户端（小悟 Desktop v0.2，与主 agent 同仓演进）

| 能力 | 我们的对应 | 状态 |
|---|---|---|
| 长会话（历史上下文延续） | `/api/run` 携带 session_id + SessionStore 恢复/逐轮自动保存 | ✅ |
| 逐操作 checkpoint 回滚 | checkpoint 事件带 commit hash + `/api/rollback`（新提交方式，不重写历史） | ✅ |
| 工具输出实时流 | terminal 增量回调 → `tool_output` 事件 → 工具行内联尾行（末 3 行） | ✅ |
| 桌面操控（Computer Use） | computer 工具：截图+视觉 / UIA 无障碍树 / OS 级鼠标键盘（高危审批门） | ✅ |
| 运行时设置覆盖 | `/api/config`：视觉模型三件套界面直配 + 测试连接（key 打码回显） | ✅ |
| 多工作区 | 后端按工作目录重启；状态栏 chip；会话按项目分组 | ✅ |
| 任务队列 | running 时入队 → run_end 自动续发；队列持久化、断连不丢 | ✅ |
| 桌面体验 | 自定义标题栏 / 原生通知 / 代码块（语言+复制+折叠）/ 审批「总是允许」/ 主题跟随系统 | ✅ |
| 打包分发 | electron-builder NSIS（图标「悟」）；winCodeSign 符号链接需提权（已知坑，见 pack 流程） | ✅ |

## 九、下一步优先级（对齐大佬们）

> 2026-08-29 复盘：原四项（Team 并行子代理 / 白名单命令模式 / OS 级沙箱
> AppContainer / TUI 状态栏）已全部落地并经真机实战验证（my_agent 首次完整
> 自我升级：实现 Hooks 机制，32 轮完成，389 tests）。实战暴露的 5 个问题
> （前台超时、沙箱引导文案、并行门过保守、解释器授权、临时文件残留）
> 当日全部修复并制度化（见第七节）。之后连续七次自我升级：Team DAG 依赖
> 调度（33 轮，412 tests，真机验证依赖链按序执行）、execpolicy DSL
> （37 轮，438 tests，真机验证 deny 拦截与 agent 改道）、Skills 技能包
> （38 轮，473 tests，真机验证技能注入与遵循）、沙箱能力清单化（目录授权
> 清单 + 网络 capability 开关，真机 A/B 验证私网可达）、脚本化 Skills
> （80 轮，486 tests，真机验证技能脚本发现→终端执行→审批门不绕过）、
> 技能包安装/更新（73 轮，498 tests，真机验证签名包安装 + 未签名拒绝 +
> agent 使用新装技能完成换算）、Dashboard 联动（80 轮，510 tests + 14
> vitest，turn_start 状态条与 skills 匹配提示上桌面端，状态文本终端/桌面
> 同源）。七次自我升级全部遵守 AGENTS.md 归因提交与文件授权范围约束。

1. **真助手路线图（异步助理愿景，按依赖顺序）**——目标形态：agent 干活时
   用户随时插话 → agent 记下 → 派分身去干 → 分身回报 → 统一汇报。差距拆解：

   | # | 缺口 | 内容 | 量级 | 依赖 |
   |---|---|---|---|---|
   | 1 | **收件箱机制** | 执行循环在轮次边界（turn_start 事件点）检查线程安全收件箱；WS/桌面把插话投进去，agent 按用户消息消化、可改派/调优先级。模式参照 stop_event | 一轮自我升级 | 无 |
   | 2 | **异步分身** | Team.run 后台模式：派活后主对话立即可继续聊；分身完成经事件流回报，经理跨轮次消化并汇总。难点：经理对话线程跨完成事件持久存活（会话持久化基建已有） | 一~两轮 | 依赖 #1 |
   | 3 | **任务实体** | 持久化任务模型（状态/依赖/结果），「记下/回报/统一汇报」的载体——与桌面会话正在做的「任务实体（任务列表）」是同一块拼图，**合流后实现，避免两套模型** | 中 | 等合流 |
   | 4 | **汇报层** | 随时问状态 → 汇总所有分身进度；关键节点主动通知（桌面原生通知已有） | 小 | 依赖 #3 |

2. Dashboard 状态栏/Hooks/Skills 事件与 UI 联动（等待并行会话的 rollback
   API 合流后开工，避免撞车）
3. 沙箱网络细粒度化（按目标地址/端口的最小放行，替代当前全有全无的开关）
4. Skills 生态：技能包分发源与签名规范（当前校验文件级 SHA-256，下一步
   引入签名者身份验证）
5. 小悟 Desktop：安装版自动更新（electron-updater + 更新服务器）
   （已完成：safeStorage 密钥加密、vitest 测试基建 + 8 个纯逻辑冒烟用例）
