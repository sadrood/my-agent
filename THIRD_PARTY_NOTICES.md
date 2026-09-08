# 第三方来源与许可声明（THIRD-PARTY NOTICES）

本项目在设计理念与部分交互约定上参考了主流开源 agent 框架与宿主平台，
代码为独立实现，无第三方源码复制。按惯例保留来源与许可声明。

## 1. 上游宿主框架

- 参考内容:
  - 沙箱分级命名（read-only / workspace-write / danger-full-access）
  - skills 概念（本项目通过 AGENTS.md + 工具注册表实现类似能力）
- 该框架作为宿主可与本项目通过 MCP 集成（`python main.py --mcp-server`）。

## 2. 其他参考

- 输入体验约定（多行粘贴整块提交、行尾 `\` 续行、欢迎面板信息排布）
  参考主流终端 agent 的通行做法，均为交互层面的对齐，未复制任何代码。
- 斜杠命令命名习惯（/model /sessions /open 等）与部分兼容参数别名
  （如 `--dangerously-skip-permissions`、`-r`）用于降低迁移成本。

---

本文件随项目源码一同分发。若你对上述项目的引用方式有疑问，
请参考各项目仓库的 LICENSE 文件原文。
