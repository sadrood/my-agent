# my_agent

Python 实现的通用 AI Agent，融合主流开源 Agent 框架
与上游宿主框架的工程实践。单循环执行架构（一轮对话完成整个目标），具备自我升级能力——
本仓库的 Hooks、Team DAG、execpolicy、Skills 等子系统均由 Agent 自己实现并通过
独立验收。

## 快速开始

```cmd
:: 1. 环境
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
playwright install chromium
copy .env.example .env        :: 填入 LLM_API_KEY（OpenAI 兼容端点均可）

:: 2. 环境自检（13 项）
python main.py --doctor

:: 3. 运行
python main.py "你的任务目标"            :: 单循环模式
python main.py --team "复杂任务"         :: 多 Agent 团队协作（可 DAG 依赖）
python main.py                           :: 交互模式（/help 查看命令）
```

## 能力总览

| 能力 | 说明 | 开关 |
|---|---|---|
| 单循环执行 | 一条消息线程完成整个目标，function calling + 流式输出 | 默认 |
| 多 Agent 团队 | Manager 拆解 → Worker DAG 依赖调度 → 汇总 → 审校 | `TEAM_PARALLEL` |
| 审批与策略 | 四级审批 + 沙箱分级 + 命令白名单 + execpolicy DSL（JSON 规则 allow/deny/ask） | `APPROVAL_*` |
| OS 级沙箱 | Windows AppContainer：仅工作区可写、网络可开关、fail-closed | `SANDBOX_EXECUTION` |
| Hooks | 工具调用前后回调（on_pre_tool_use / on_post_tool_use），fail-open | `HOOKS_ENABLED` |
| Skills | SKILL.md 技能包按关键词注入系统提示，支持附带脚本；`--skills-install` 带完整性校验 | `SKILLS_ENABLED` |
| 安全审校 | Guardian 快模型审校高风险操作 | `GUARDIAN_ENABLED` |
| 记忆与经验 | 四层记忆 + 经验库 + 失败模式预警 | `MEMORY_DB_PATH` |
| 会话与回滚 | 会话持久化 + 逐操作 git checkpoint（归因提交，可逐操作回滚） | `SESSION_*` |
| 自我升级 | 运行前快照 + edit 验证式应用（改坏自动回滚）+ git 安全网 | `SNAPSHOT_*` |
| Dashboard | Web/桌面客户端（Electron），事件流实时推送 | `--dashboard` |

## 文档

- `docs/COMMANDS.md` —— 全部命令行用法与环境变量表
- `docs/BEST_PRACTICES.md` —— 对标主流开源 Agent 的实践清单 + 踩坑制度化
- `docs/HANDOFF_PROMPT.md` —— 交接提示词（给下一个开发会话/工具）
- `AGENTS.md` —— Agent 强制规范（自动注入）

## 开发约定

- 一次只改一个模块，改完立刻 `python -m pytest tests -q`，全绿再动下一个
- 改 `.py` 用精确替换；新配置进 `config.py`、新提示词进 `models/prompts.py`
- 安全功能必须走 `agent/approval.py` 审批门，安全边界（黑名单/沙箱等级）
  不可被任何扩展层豁免
- 详见 `AGENTS.md`

## 许可与第三方

见 `THIRD_PARTY_NOTICES.md`。
