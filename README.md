<div align="center">

# my_agent

**Python 实现的通用 AI Agent** —— 一条消息线程完成整个目标

浏览器 · 桌面 · 终端 · 文档 · 多 Agent 团队，都在一个循环里

[![Python](https://img.shields.io/badge/Python-3.10%2B%20(建议%203.11%2B)-3776AB?logo=python&logoColor=white)](#-快速开始)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows&logoColor=white)](#-快速开始)
[![Tests](https://img.shields.io/badge/tests-2177%20passed-brightgreen)](#-开发约定)
[![Tools](https://img.shields.io/badge/tools-26-4B8BBE)](#-能力总览)
[![Browser](https://img.shields.io/badge/browser-ego%20%7C%20bridge%20%7C%20Playwright-9cf)](#-浏览器三种后端)

</div>

---

## ✨ 亮点

- **单循环执行** —— 不再"规划层 → 执行层 → 总结层"三段接力；一轮对话里 function calling + 流式输出把目标做完，上下文只有一条线。
- **省 token 是设计目标，不是副作用** —— 工具 schema 按需装载、大输出零驻留、旧历史指针化压缩：每轮提示词从约 **9.7K token 降到约 3.7K**。
- **浏览器有三条路** —— `ego`（驱动你系统里的 Edge，带可见光标浮层）、桌面端内置桥、Playwright；默认自动挑可用的那条。
- **崩溃不丢活** —— 会话**增量落盘**、关窗 ~5 秒窗口兜底刷盘、进程级运行日志；被强杀后还能用 `python -m agent.recover` 从事件流把那一轮还原回会话。
- **安全是门槛不是装饰** —— 四级审批 + OS 级沙箱（AppContainer，fail-closed）+ 命令黑名单 + execpolicy DSL；安全边界不可被任何扩展层豁免。
- **自我升级** —— Hooks、Team DAG、execpolicy、Skills 等子系统由 Agent 自己实现并通过独立验收；改动前自动 git 快照，改坏可回滚。

## 🚀 快速开始

```cmd
:: 1) 环境
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
copy .env.example .env          :: 填入 LLM_API_KEY（任何兼容 OpenAI 协议的端点均可）

:: 2) 浏览器
::    ego 后端：直接用系统已装的 Edge，无需额外安装（推荐）
::    Playwright 后端：需要先装内核
playwright install chromium

:: 3) 自检（15 项：依赖 / 端点 / 工具 / 沙箱 / 浏览器 / 技能…）
python main.py --doctor

:: 4) 开始用
python main.py "你的任务目标"      :: 单次任务（单循环）
python main.py --team "复杂任务"   :: 多 Agent 团队协作（可 DAG 依赖）
python main.py                     :: 交互模式（/help 看命令）

:: 5) 升级到最新版本（只快进，不做破坏性动作；完事重启进程）
python main.py --upgrade-check     :: 只看有没有新版本
python main.py --upgrade           :: 升级代码 + 依赖（工作区脏会拒绝）
```

> 浏览器默认 `BROWSER_BACKEND=auto`：**内置桥 > ego > Playwright**。
> 想固定用哪条，在 `.env` 里写死即可（见 [浏览器三种后端](#-浏览器三种后端)）。

### 🐧 Ubuntu / Linux 安装

需要 **Python 3.10+**（代码用了 `str | None` 注解；22.04 自带 3.10 ✓，24.04 自带 3.12 ✓，
20.04 需 deadsnakes PPA）。一键脚本（幂等，可重复跑；`--dry-run` 只打印不改动）：

```bash
git clone https://github.com/sadrood/my-agent.git ~/my_agent
cd ~/my_agent
bash scripts/install-ubuntu.sh          # 系统包 + venv + 依赖 + chromium 内核 + .env + 自检
$EDITOR .env                            # 填 LLM_API_KEY（至少这一项）
.venv/bin/python main.py --doctor
.venv/bin/python main.py "你的任务"
```

常驻服务（可选）：`examples/systemd/my-agent.service` → 拷到 `/etc/systemd/system/` 后
`systemctl daemon-reload && systemctl enable --now my-agent`（记得改里面的路径与 User）。

> Linux 差异：桌面操控工具 `computer`（Windows UIA）在 Linux 上不可用，浏览器后端建议用 Playwright
> （ego 后端需要另装 `dsh-ego-browser` 运行时）。`skills/` 与 `tools/local/` 不入库，新机是干净的产品本体。

## 🧩 能力总览

<table>
<tr><th align="left">分类</th><th align="left">能力</th><th align="left">开关</th></tr>
<tr><td rowspan="3"><b>执行与协作</b></td>
    <td>单循环执行：一条消息线程完成整个目标，function calling + 流式输出</td><td>默认</td></tr>
<tr><td>多 Agent 团队：Manager 拆解 → Worker DAG 依赖调度 → 汇总 → 审校</td><td><code>TEAM_PARALLEL</code></td></tr>
<tr><td>工具并行：一轮里互不依赖的只读调用自动并发，结果按原顺序回喂</td><td>默认</td></tr>
<tr><td rowspan="4"><b>安全与审批</b></td>
    <td>四级审批 + 命令白名单 + execpolicy DSL（JSON 规则 allow / deny / ask）</td><td><code>APPROVAL_*</code></td></tr>
<tr><td>OS 级沙箱：Windows AppContainer，仅工作区可写、网络可开关、fail-closed</td><td><code>SANDBOX_EXECUTION</code></td></tr>
<tr><td>Guardian：快模型独立审校高风险操作</td><td><code>GUARDIAN_ENABLED</code></td></tr>
<tr><td>Hooks：工具调用前后回调（on_pre/post_tool_use），fail-open</td><td><code>HOOKS_ENABLED</code></td></tr>
<tr><td rowspan="3"><b>能力扩展</b></td>
    <td>Skills：SKILL.md 按关键词注入系统提示，可附带脚本；安装带完整性校验</td><td><code>SKILLS_ENABLED</code></td></tr>
<tr><td>浏览器自动化：ego / 内置桥 / Playwright 三后端</td><td><code>BROWSER_BACKEND</code></td></tr>
<tr><td>桌面操控、图像/视频生成、剪辑、配音、OCR、文档工坊等 26 个工具</td><td>见工具表</td></tr>
<tr><td rowspan="4"><b>上下文与成本</b></td>
    <td>工具 schema 按需装载（<code>tools load</code>），低频大工具不进提示词</td><td><code>CONTEXT_*</code></td></tr>
<tr><td>零驻留上下文：大输出只留指针，<code>context recall</code> 逐字取回</td><td><code>CONTEXT_STORE_*</code></td></tr>
<tr><td>指针化压缩：旧历史折成确定性指针，<b>不调用模型</b></td><td><code>CONTEXT_COMPACT_MODE</code></td></tr>
<tr><td>模型能力目录：窗口/输出上限/视觉能力 = 实测覆盖 + models.dev + 调用后回填</td><td>默认</td></tr>
<tr><td rowspan="5"><b>运维与可观测</b></td>
    <td>会话增量落盘：每完成一次工具调用就写盘，被强杀最多丢最后一条</td><td><code>SESSION_INCREMENTAL_SAVE</code></td></tr>
<tr><td>关窗兜底：Windows 控制台关闭/注销/关机时抢 ~5 秒刷盘（Ctrl+C 不受影响）</td><td>默认</td></tr>
<tr><td>运行日志：启动环境、未捕获异常、子线程崩溃、退出原因</td><td><code>RUN_LOG_*</code></td></tr>
<tr><td>会话恢复：<code>python -m agent.recover</code> 从事件流还原被中断的一轮</td><td>默认</td></tr>
<tr><td>记忆与经验：四层记忆 + 经验库 + 失败模式预警；逐操作 git checkpoint 可回滚</td><td><code>MEMORY_DB_PATH</code></td></tr>
<tr><td>QQ 机器人桥：官方 API 私聊驱动（白名单 + 独立会话 + 审批推到 QQ，聊天通道不可提权）</td><td><code>QQBOT_*</code></td></tr>
</table>

## 📉 省 token：实测数字

| 项目 | 改造前 | 改造后 | 说明 |
|---|---|---|---|
| 每轮工具 schema | ≈9,678 token | ≈3,565 token | 10 个低频大工具改为按需装载 |
| 按需清单开销 | — | ≈160 token | 一行式清单进系统提示 |
| 单个大工具输出 | 7,988 字符常驻上下文 | 106 字符指针 | 原文落盘，`context recall` 逐字取回 |
| 旧历史压缩 | 1 次 LLM 摘要（有损） | **0 次调用** | 指针化，原文可还原 |

```cmd
> tools list                     :: 看哪些工具在按需区（模型也能自己调）
> tools load zhihu video_edit    :: 装载，下一轮起可直接调用
> context recall run-1759-0007   :: 取回被折叠的原文（消息里会看到 [已折叠 #句柄 12.3KB]）
> context stats                  :: 账本：折叠条数 / 体积 / 估算省下的 token
```

## 🌐 浏览器三种后端

| 后端 | 依赖 | 特点 |
|---|---|---|
| **`ego`** | 系统已装的 Edge + ego CLI | 共享浏览器窗口、页面里有**可见光标浮层**（会被画进截图，可关）、任务空间隔离；不支持的命令少，日常首选 |
| `embedded` | 桌面端（桥） | 由宿主桌面端托管浏览器，桥接调用 |
| `playwright` | `playwright install chromium` | 独立浏览器实例，最"干净"，适合无人值守 |

```ini
BROWSER_BACKEND=auto                    # auto / ego / embedded / playwright
BROWSER_EGO_CURSOR=true                 # 页面里的 agent 光标浮层
BROWSER_EGO_CURSOR_NAME=my_agent        # 浮层上显示的名字
BROWSER_EGO_TIMEOUT=120
BROWSER_EGO_ISOLATE=true                # 每个会话自己的任务空间（互不抢页面）
BROWSER_EGO_HEADLESS=false              # true = 无窗口运行，完全不弹窗打扰你
```

> 光标只在"读过页面 / 点过 / 打过字 / 该进程接住页面加载"时出现 —— 纯 `js`、`text`、`screenshot` 的步骤不显示，这是 ego 的显隐规则，不是故障。
> 两个进程同时驱动同一浏览器会互相抢标签页：各自用不同任务空间才互不干扰。
> **不想被浏览器弹窗打扰**：会话隔离 + 快路径让它只在"真要换页面"时抢前台；`BROWSER_EGO_HEADLESS=true` 则完全不弹（代价是看不到页面）。

## 🧰 工具（26 个）

**常驻 15** —— 每轮都在提示词里，拿来就用：

`terminal` · `file` · `edit` · `python` · `browser` · `see` · `ocr` · `image_gen` · `todo_write` · `task` · `thought` · `memory` · `experience` · `delegate` · `context`

**按需装载 10** —— schema 不进提示词，用时先装载（也支持目标关键词自动装载）：

`zhihu` · `zhihu_draft` · `toonflow` · `video_edit` · `video_gen` · `tts` · `article` · `computer` · `installer` · 示例工具

**元工具** —— `tools`（`list` / `load` / `unload`）就是按需区的入口。

```cmd
python main.py --list-tools      :: 查看全部工具与 JSON Schema
```

## 🛠 出问题时先看这三样

```cmd
python main.py --doctor                          :: 环境/依赖/端点/工具/沙箱/浏览器 15 项自检
type memory\logs\run-*.log                       :: 进程级日志：启动环境、未捕获异常、退出原因
python -m agent.recover --list                   :: 找出被中断（没有 run_end）的运行
python -m agent.recover --run <run-id> --dry-run :: 先从事件流还原，确认后再写回会话
```

## 📚 文档

| 文档 | 内容 |
|---|---|
| [`docs/COMMANDS.md`](docs/COMMANDS.md) | 全部命令行用法、会话内命令、环境变量表 |
| [`docs/BEST_PRACTICES.md`](docs/BEST_PRACTICES.md) | 对标主流开源 Agent 的实践清单 + 踩坑制度化 |
| [`docs/HANDOFF_PROMPT.md`](docs/HANDOFF_PROMPT.md) | 交接提示词（给下一个开发会话/工具） |
| [`AGENTS.md`](AGENTS.md) | Agent 强制规范（运行时自动注入系统提示） |
| [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) | 第三方来源与许可声明 |

## 🤝 开发约定

- 一次只改一个模块，改完立刻跑相关用例；**提交前跑全量**（当前 2177 passed / 6 skipped）。
- 改 `.py` 用精确替换；新配置进 `config.py` 并同步 `.env.example`；新提示词进 `models/prompts.py`。
- 安全功能必须走 `agent/approval.py` 审批门；黑名单与沙箱等级不可被任何扩展层豁免。
- 公共文件（`config.py` / `.env.example` / `AGENTS.md` / `main.py`）提交时**逐 hunk 暂存**，避免把别人未完成的改动卷进来。
- **当天改动汇总成一笔提交（一天一条）**。用钩子把它变成命令级约束：

```bash
bash scripts/install-hooks.sh      # 启用仓库自带钩子（core.hooksPath=.githooks）
# 同一天已在远端推过提交时，再次 git push 会被拦下并列出当天已推的提交；
# 紧急例外：ALLOW_MULTI_PUSH_TODAY=1 git push ...
# 关闭：bash scripts/install-hooks.sh --uninstall
```

详见 [`AGENTS.md`](AGENTS.md)。

## 🙏 致谢

用到了这些开源成果（逐项许可与引用方式见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)）：

| 项目 | 许可 | 用在哪 |
|---|---|---|
| [`dsh-ego-browser`](https://github.com/Fisfzy/dsh-ego-browser) | MIT | `BROWSER_BACKEND=ego` 的浏览器运行时：以其 `ego-browser` CLI 驱动系统 Edge，页面里的光标浮层也是它实现的 |
| [`models.dev`](https://github.com/anomalyco/models.dev) | MIT | 模型能力目录的数据源（网关不报上下文窗口/模态时按需拉取并缓存） |
| DeepSeek Harness（宿主框架） | — | 沙箱分级命名、skills 概念；可作为宿主经 MCP 集成（`--mcp-server`） |

另有几处**思路层面**的借鉴：工具 schema 按需供给与零驻留上下文、按模型名补全能力字段（来自 DSH 插件生态的公开实践与 `awesome-dsh-plugin` 清单）——均为独立实现，未复制源码。

## 📄 许可与第三方

第三方来源与许可声明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)；
运行时依赖（`dsh-ego-browser`、`models.dev`）按各自 MIT 许可使用，`requirements.txt` 中的库遵循各自原始许可。

<div align="center"><sub>单循环 · 可回滚 · 可恢复 · 省 token</sub></div>
