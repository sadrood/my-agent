# my_agent 项目指令（AGENTS.md）

本文件会被 Agent 自动加载并注入系统提示（`agent/instructions.py`）。
任何在本项目内运行的 Agent（包括人类使用的 AI 编程助手）都应遵守以下规则。

## 项目定位
my_agent 是一个 Python 实现的通用 AI Agent，融合了主流开源 agent 框架
与上游宿主框架的设计优点。

## 云经验库
- 你有云经验库工具 `experience`：陌生领域/踩坑先用 `experience search` 检索参考；
  重要任务或复盘用 `experience save` 沉淀（只写方法论，绝不写真实密钥）。条目仅作参考。
- 仓库地址来自 `.env`（`EXPERIENCE_PRIVATE_REPO` 私有学习仓 / `EXPERIENCE_PUBLIC_REPO`
  公共分享仓）；本机未配置时先填 .env 再使用，未配置的仓会自动跳过。

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
   能用管道或变量传递的数据不要落盘成临时文件。清理用 `file` 工具的
   `delete` 操作（`recursive: true` 才能删目录；`.git`/项目根/盘根会被拒绝），
   **不要用 python 工具**——那里 `shutil` 在黑名单里。中间产物（中间音频/帧/
   旧版本渲染）一律删掉，只留最终交付物与生成脚本，别让 `output/` 只涨不减
   （实测曾膨胀到 36GB，其中一个坏 wav 占 34GB）。

## 输出文件规范
10. 任务产生的输出文件（音频、图片、文档、产物脚本等）统一放在项目根目录的
    `output/` 文件夹下（按任务可再建子目录），**禁止散落在项目根目录**；
    过程中的临时调试文件用完即删，不要留在项目里。

## 本地技能（不入库）
11. Agent 自己新增的工具/技能属于**本地能力**，放 `tools/local/`（该目录已在
    .gitignore 中忽略，永不推送）；约定模块里定义 `BaseTool` 子类且可无参实例化。
    仓库只提交产品自带的核心工具与配置（`config.py`、`.env.example`、`AGENTS.md`、
    `docs/`）；只有别的机器/仓库也要用的产品级工具才进 `tools/`。

## 提交纪律
12. **只提交自己改的文件，禁止 `git add -A` / `git add .` / `git commit -a`。**
    这个仓库可能同时有多个 Agent 会话在改代码，全量暂存会把别人**还没写完的**
    改动卷进你自己的提交里（实测发生过：一次 `add -A` 把另一个会话刚写好的 43 行
    测试收进了无关的功能提交）。正确做法：
    - 提交前先 `git status --short` 看清哪些是自己的；
    - 只 `git add <具体文件>`；同一个文件里混有他人改动时，用
      `git diff -- <file>` 看 hunk、只暂存自己的 hunk
      （`git apply --cached <patch>`，或临时移出他人改动→`git add`→立刻还原）；
    - 提交信息里写清"本次只含本人改动，他人的 X/Y 仍未提交"。
    这样别人的在制品不会被破坏，也不会被误记到你名下。

    ⚠️ **"具体文件"也要先逐 hunk 看**：同一个文件里常常既有你的改动、又有别人正在写的
    （配置文件尤其容易：`config.py` / `.env.example` 人人都要加旋钮）。整文件
    `git add <file>` 一样会把别人的在制品卷进来——本仓库实测犯过两次
    （`config.py` 的 `loop_stagnation_warn`、`.env.example` 同一项）。做法：
    ```bash
    git diff -U0 -- config.py          # 1. 先看清每个 hunk 是谁的（看新增行的内容/标记）
    git diff -- config.py > full.patch # 2. 导出补丁
    # 3. 只保留"新增行里含自己标记"的 hunk，生成 mine.patch（脚本过滤，别手改）
    git apply --cached mine.patch      # 4. 只暂存自己的 hunk
    git diff --cached --stat           # 5. 复查：暂存区里不应出现别人的关键字
    ```
    万一还是混进去了：**不要改写历史**（别的会话可能在用那些 hash 做回滚点），
    而是"加一次再减一次"把净效果归零——新建一个提交删掉误提交的那几行，再立刻把它们
    放回**工作区**（不暂存），让归属回到"别人未提交的改动"。

    本机有现成工具就用它（`tools/local/` 不入库，别的机器可能没有）：
    ```bash
    python tools/local/stage_mine.py config.py .env.example --mark <只出现在你改动里的词> --list
    # --list 先看归属；去掉 --list 才真的暂存；暂存后它会自动复查暂存区有没有混进别人的行
    ```
    这条规则被违反过**四次**（`config.py` 两次、`.env.example` 两次），其中两次是
    **检查已经报警、人却没停**。最近一次（2026-09-23）的形态是：对公共文件用了
    整文件 `git add .env.example`（不是逐 hunk），把另一会话未完成的
    `LOOP_STAGNATION_WARN` 三行卷进提交；而泄露检查**当场打印了那一行**，人还是
    提交了，最后靠"加一次再减一次"（`6e387f8` 删掉 + 放回工作区）才归零。

    所以两条硬规矩：
    1. **公共文件（`config.py` / `.env.example` / `AGENTS.md` / `main.py` 等）
       永远走 `stage_mine.py` 逐 hunk 暂存，禁止 `git add <file>`**；
    2. 报警（退出码 2 或 `⚠ 警告：暂存区里出现了本应丢弃的他人内容`）出现时
       **必须停下来处理**，不能"看一眼继续提交"。

    别依赖"我会注意"，依赖命令。

## 代码注释（对外可读）
13. 注释克制：docstring ≤ 3 行、注释单行。**不写排查过程** —— 不写日期、不写
    "审计 / 实测踩到 / 背景是…"、不复述代码；只留反直觉的那一句约束
    （如"SIGKILL 在 Windows 上不存在"）。本仓库要公开，过程痕迹对读者是噪声。

## 测试
- 全部测试必须通过后再交付（命令见 `.env` 的 `TEST_COMMAND`）
- 不依赖网络的测试优先（FakeLLM 脚本化），真机测试用小任务。

## 安全底线
- 不允许在代码中硬编码任何 API key / token。
- Python 工具的黑名单模块（tools/python.py BLOCKED_IMPORTS）只能增不能减。
- 审批策略 never 只能用于无人值守且沙箱受限的场景（如 MCP server 默认配置）。
