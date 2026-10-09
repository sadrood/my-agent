# 第三方来源与许可声明（THIRD-PARTY NOTICES）

本项目在设计理念与部分交互约定上参考了主流开源 agent 框架、宿主平台与插件生态，
代码为独立实现；**运行时依赖**（浏览器后端、模型数据）按各自许可使用并在下方逐项列明。
按惯例保留来源与许可声明。

## 1. 运行时依赖的开源项目

### 1.1 dsh-ego-browser（浏览器 ego 后端）

- 仓库：https://github.com/Fisfzy/dsh-ego-browser ｜ 许可：**MIT**
- 用途：`BROWSER_BACKEND=ego` 时，本项目通过子进程调用它自带的 `ego-browser` CLI
  （ego-lite 运行时）驱动系统 Edge：命令集、截图、以及页面里的光标浮层均由它实现。
- 引用方式：**作为外部程序调用**（不打包、不修改其源码）；`tools/ego_browser.py`
  中"挑选真实标签页"的一小段脚本（`listTabs` → 跳过 `about:`/`chrome://` → `switchTab`）
  参照其 `ensureRealTab` 的写法改写。光标浮层的名字/开关通过其环境变量
  （`EGO_LINUX_CURSOR_NAME` / `EGO_LINUX_CURSOR`）控制。
- 未安装时：本项目自动回退到内置桥或 Playwright（见 `BROWSER_BACKEND=auto`）。

### 1.2 models.dev（模型能力目录数据）

- 仓库：https://github.com/anomalyco/models.dev ｜ 许可：**MIT**
- 用途：`models/model_catalog.py` 在网关 `/models` 不报上下文窗口/模态时，按需拉取
  其公开目录（`https://models.dev/api.json`）补齐"上下文窗口 / 输出上限 / 是否支持图片 /
  是否推理"；结果缓存在 `memory/model_catalog.json`（7 天过期，后台异步刷新）。
- 引用方式：**只读取公开数据接口**，不含任何源码；本地实测值优先于目录值
  （见 `LOCAL_OVERRIDES_BY_HOST`），目录不可用时按"未知"处理，不臆造数字。

## 2. 上游宿主框架

- 参考内容:
  - 沙箱分级命名（read-only / workspace-write / danger-full-access）
  - skills 概念（本项目通过 AGENTS.md + 工具注册表实现类似能力）
- 该框架作为宿主可与本项目通过 MCP 集成（`python main.py --mcp-server`）。

## 3. 设计借鉴（思路参考，代码独立实现）

以下项目**未引入任何源码**，仅借鉴公开的设计思路；若你希望注明更多细节，欢迎提 issue。

- **工具 schema 按需供给 + 零驻留上下文**（`agent-body` 系的 DSH 插件实践）：
  启发了本项目的 `tools load` 门控装载与 `context recall` 指针化上下文
  （`tools/tool_meta.py`、`agent/context_store.py`）。
- **按模型名补全能力字段**（`dsh-model-info-fill`）：启发了 `models/model_catalog.py`
  的取值顺序（本地实测覆盖 → 学习缓存 → 目录）。
- **插件选型调研**：`awesome-dsh-plugin`（GitHub topic `dsh-plugin` 精选清单）用于了解
  DSH 生态可借鉴的工程做法。

## 4. 其他参考

- 输入体验约定（多行粘贴整块提交、行尾 `\` 续行、欢迎面板信息排布）
  参考主流终端 agent 的通行做法，均为交互层面的对齐，未复制任何代码。
- 斜杠命令命名习惯（/model /sessions /open 等）与部分兼容参数别名
  （如 `--dangerously-skip-permissions`、`-r`）用于降低迁移成本。

## 5. 第三方 Python 依赖

`requirements.txt` 中列出的库（含 `openai`、`rich`、`playwright`、`websockets` 等）
各自遵循其原始许可，随包分发；请参见各库仓库的 LICENSE。

---

本文件随项目源码一同分发。若你对上述项目的引用方式有疑问，
请参考各项目仓库的 LICENSE 文件原文。
