# my_agent 项目指令（AGENTS.md）

本文件会被 Agent 自动加载并注入系统提示（`agent/instructions.py`）。
任何在本项目内运行的 Agent（包括人类使用的 AI 编程助手）都应遵守以下规则。

## 项目定位
my_agent 是一个 Python 实现的通用 AI Agent，融合了主流开源 agent 框架
与上游宿主框架的设计优点。

## 云经验库
- 你有云经验库工具 `experience`（本机 .env 已配置私有 GitHub 经验仓）：
  陌生领域/踩坑先用 `experience search` 检索参考；重要任务或复盘用
  `experience save` 沉淀（只写方法论，绝不写真实密钥）。条目仅作参考。

## 开发规则
1. 一次只改一个模块；改完立即运行测试（命令见 `.env` 的 `TEST_COMMAND`，
   默认 `.venv/Scripts/python -m pytest tests -q`）。
2. 先设计接口再实现功能；每个类只负责一件事。
3. 工具必须继承 `tools.base.BaseTool`：提供 name/description/schema/execute_json，
   并声明 risk_level / min_sandbox_mode 审批元数据。
4. 涉及安全的功能（终端命令、文件写入、系统操作）必须经过 `agent.approval.ApprovalPolicy`
   审批门；新增危险命令要同步更新 `agent/approval.py` 的黑名单/高风险模式，并补测试。
5. 新增提示词统一放在 `models/prompts.py`；新增配置统一放在 `config.py` 并同步 `.env.example`。
6. 保持向后兼容：`BaseTool.execute(input_str)` 字符串接口、旧文本 JSON 决策协议
   （`Executor.execute_step_legacy`）、经典计划模式（`--plan`）不得随意删除。
7. 执行架构：默认走单循环模式（`Executor.execute_goal_loop`，一次对话完成整个目标）；
   不要在循环外新增独立的 LLM 调用层（规划/总结层已废弃）。计划模式仅用于
   team / `--plan` / function-calling 回退场景。
8. 自我修改（Agent 升级自己）必须：用 edit 工具做精确替换（禁止 file write 整文件
   覆盖大文件）；每次修改后立即运行测试（命令见 `.env` 的 `TEST_COMMAND`）；
   每次运行前系统会自动做 git 快照（`agent/snapshot.py`），改坏了用 git 回滚，
   不要删除 .git 目录、不要修改 .gitignore。
9. 任务收尾必须清理本次创建的临时文件（测试输出、调试脚本、日志抓取文件）；
   能用管道或变量传递的数据不要落盘成临时文件。

## 输出文件规范
10. 任务产生的输出文件（音频、图片、文档、产物脚本等）统一放在项目根目录的
    `output/` 文件夹下（按任务可再建子目录），**禁止散落在项目根目录**；
    过程中的临时调试文件用完即删，不要留在项目里。

## 测试
- 全部测试必须通过后再交付（命令见 `.env` 的 `TEST_COMMAND`）
- 不依赖网络的测试优先（FakeLLM 脚本化），真机测试用小任务。

## 安全底线
- 不允许在代码中硬编码任何 API key / token。
- Python 工具的黑名单模块（tools/python.py BLOCKED_IMPORTS）只能增不能减。
- 审批策略 never 只能用于无人值守且沙箱受限的场景（如 MCP server 默认配置）。
