# 项目交接提示词（给新编程工具用）

> 用法：把下面「二、提示词正文」整段复制给新工具。
> 工具会自动读取本项目的 `AGENTS.md`（强制规范在那里，不在此重复）。
> 本文件只提供工具**自己看不到的**：当前状态、已知坑、验收方式。
> 最后更新：2026-08-29（六次自我升级 + 沙箱清单化之后）。

---

## 一、使用前准备（人来做）

1. **确认测试环境干净**：

   ```cmd
   REM 1) 基线测试（健康参考：约 498~510 passed / 45~55s）
   python -m pytest tests -q

   REM 2) 环境自检（13 项，含 hooks/execpolicy/skills/sandbox 新子系统）
   python main.py --doctor
   ```

   历史坑（**已修复/制度化**，如再出现按此排查）：
   - `.pytest_cache` 与 `%TEMP%\pytest-of-Administrator` ACL 损坏曾致成片
     `PermissionError [WinError 5]`（226s）——提权 takeown+icacls /reset 后删除即可；
     临时规避可用干净 TMP 目录 + `-p no:cacheprovider`
   - `.bak` 残留已根治（edit 成功/回滚后自动清理），如再出现说明 edit 工具
     生命周期被改动

2. **确认 .env 与模型可用**：`python main.py --doctor`（13/13 通过为健康）。

3. **注意并行会话**：本项目常有多个会话并行工作（当前：桌面客户端/
   rollback API）。只提交自己授权范围内的文件（归因提交），**不要 git add -A**。

---

## 二、提示词正文（复制这段）

```
你将要接手一个 Python 实现的通用 AI Agent 项目（my_agent），位于当前目录。

## 第一步：先读规范，不要急着写代码

1. `AGENTS.md` —— 项目铁律。**第 7、8、9 条最关键**：单循环执行架构
   （Executor.execute_goal_loop，循环外禁止加规划/总结 LLM 层）；自我修改必须
   edit 精确替换；任务收尾清理临时文件。
2. `docs/BEST_PRACTICES.md` —— 对标表 + 踩坑制度化清单 + 下一步优先级。
   做架构类改动前先看这里。
3. `docs/COMMANDS.md` —— 全部命令行用法与环境变量表（新功能开关都在）。

## 项目结构速览

- `agent/`   核心引擎：agent.py（主循环）、executor.py（单循环执行，含工具
             并行批）、approval.py（审批门 + 命令白名单 + execpolicy 集成）、
             execpolicy.py（结构化命令策略 DSL）、guardian.py（安全审校）、
             hooks.py（Pre/PostToolUse 钩子，fail-open）、skills.py（Skills
             技能包 + 脚本发现 + 技能包安装校验）、sandbox.py（Windows
             AppContainer OS 级沙箱，fail-closed）、team.py（Manager-Worker
             DAG 依赖调度）、rollout.py、session.py、snapshot.py（归因提交）、
             memory.py、doctor.py（13 项自检）
- `tools/`   全部继承 tools/base.py: BaseTool，声明 risk_level /
             min_sandbox_mode 审批元数据；is_parallel_safe 决定并行批是否互斥
- `models/`  llm.py（重试/流式/function calling）、vision.py、prompts.py
- `tests/`   约 30 个测试文件 500 用例左右；新功能必须有 FakeLLM 脚本化测试

## 已落地的新能力（都有真机验证，开关见 .env.example）

1. Hooks（HOOKS_ENABLED）：on_pre_tool_use / on_post_tool_use，fail-open
2. Team 并行 + DAG 依赖（TEAM_PARALLEL）：独立/依赖子任务波次调度，
   非并行安全工具按工具名互斥
3. execpolicy DSL（APPROVAL_EXEC_POLICY_ENABLED）：JSON 规则 allow/deny/ask，
   deny 优先，不能豁免黑名单/沙箱等级
4. Skills（SKILLS_ENABLED）：SKILL.md 关键词注入系统提示；技能目录普通文件
   登记为附带脚本（只告知不自动放行）；技能包 --skills-install（SHA-256
   manifest 校验，未签名默认拒绝）
5. OS 级沙箱（SANDBOX_EXECUTION=appcontainer）：Windows AppContainer，
   fail-closed；SANDBOX_GRANT_TOOLS/SANDBOX_GRANT_DIRS 授权清单、
   SANDBOX_ALLOW_NETWORK 网络开关（已真机验证私网 401）
6. TUI 状态栏（TUI_STATUS_BAR）：每轮刷新 token 计数/沙箱/策略

## 当前已知问题

1. **并行会话**：桌面客户端（dashboard/、desktop/、snapshot.py 回滚 API、
   main.py 等）常有另一个会话在实时编辑——只动自己任务授权的文件，
   全量测试出现 tests/test_dashboard.py 等失败先判断是否并行工作所致，
   不要去修。
2. **裸跑 `pytest`（不带 tests 路径）**会收集到 xianyu_smart_reply 子项目，
   可能遇到其收集错误/慢测试——统一用 `pytest tests -q`。
3. **沙箱内 stderr 中文乱码**：容器内 cmd 输出为 GBK，按 utf-8 replace 解码
   会出现乱码（与主终端行为一致的历史问题），不影响退出码判断。
4. 编辑 main.py 等共享文件前先重读（并行会话可能在改）。

## 你要遵守的工作方式

1. **一次只改一个模块**，改完立刻跑测试，全绿再动下一个。
2. **改 .py 用 edit 精确替换**，不要用 write 整文件覆盖。
3. 新增配置放 `config.py` 并同步 `.env.example`；新增提示词放 `models/prompts.py`。
4. 涉及安全的功能必须走 `agent/approval.py` 审批门；execpolicy/白名单/沙箱
   的安全边界不可松动（DSL 不能豁免黑名单与沙箱等级）。
5. 不要硬编码任何 API key / token。
6. 保持向后兼容：BaseTool.execute(input_str)、旧文本 JSON 决策协议、
   --plan 经典计划模式不得删除。

## 验收标准

- 全部测试通过：`python -m pytest tests -q`（无关失败需在报告注明并归因）
- 新功能有 FakeLLM 测试用例；真机验证用小任务
- 改动影响文档描述的行为时，同步 docs/COMMANDS.md 或 docs/BEST_PRACTICES.md

## 本次任务

【在这里写你的具体任务。一次只给一个，写清楚：
 目标是什么 / 涉及哪个模块 / 验收标准是什么 / 明确不要动哪些文件】
```

---

## 三、按工具类型调整

| 工具类型 | 调整方式 |
|---|---|
| **CLI 类**（主流命令行 agent） | 自动读 `AGENTS.md`，提示词保留「已知问题」+「本次任务」即可 |
| **IDE 插件类** | 建 `.cursorrules` 指向 `AGENTS.md` |
| **内联补全助手** | 每次对话手动粘贴正文 |

## 四、几个让工具更听话的技巧

1. **给"禁止清单"比给"要求清单"更有效**。
2. **说清"为什么"**（如：单循环是刻意设计，规划层已废弃）。
3. **一次一个任务 + 授权文件范围**——本项目验证过的最佳实践：明确"只允许
   修改 X/Y/Z 文件"，六次自我升级全部因此零冲突。
4. **自我升级闭环**（本项目验证有效）：给 agent 有界 spec → EDIT_PREFLIGHT=true
   跑（安全敏感改动除外）→ 独立复核（全量测试 + 归因检查 + 真机端到端）。
   注意并行会话在编辑的文件会让 preflight 误报，此时改为 agent 自跑定向测试。
5. **git 兜底**：snapshot.py 归因提交已内置，失败直接 `git diff` 看改动。
