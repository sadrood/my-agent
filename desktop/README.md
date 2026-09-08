# 小悟 Desktop — 图形化 AI Agent 桌面客户端

融合主流 CLI 与桌面客户端优点的三栏式桌面应用。

![stack](https://img.shields.io/badge/Electron-33-blue) ![stack](https://img.shields.io/badge/React-18-61dafb) ![stack](https://img.shields.io/badge/TypeScript-5-3178c6) ![stack](https://img.shields.io/badge/Zustand-4-orange)

## 功能一览

- **三栏布局（可折叠）**：左侧会话列表 + 文件树，中央对话流，右侧辅助面板（工具轨迹时间线 / 模型参数 / 提示词模板）
- **工具轨迹可视化**：tool_call 与 tool_result 自动配对，展示状态（进行中/成功/失败）、耗时、时间戳与参数摘要，失败可 hover 查看错误
- **顶部状态栏**：当前模型、权限模式（自动/询问/禁止，点击切换）、会话 ID、后端连接状态、主题切换
- **对话流**：Markdown 渲染 + 代码高亮 + 表格 + 图片；特殊卡片——Diff 卡片（接受/拒绝）、命令执行卡片（成功/失败/等待）、任务步骤卡片
- **输入区**：多行输入（Shift+Enter 换行）、大段粘贴自动成块、拖拽文件/图片、`@` 引用文件选择器
- **权限管理**：状态栏快速切换权限模式；受限操作在对话流弹出确认卡片
- **首次启动引导**：检测 API Key/模型/工作目录缺失 → 三步模态向导
- **会话管理**：搜索、双击重命名、置顶、删除；完整操作历史
- **亮/暗主题**：CSS 变量设计系统，一键切换，自动持久化

## 快速开始

```bash
cd desktop
npm install
npm start          # 构建 + 启动（自动拉起 Python 后端，端口 8090）
```

开发模式（热更新）：

```bash
npm run desktop:dev       # Vite dev server + Electron
```

> 注意：Electron 二进制在国内下载需镜像。若 `npm install` 失败，设置：
> ```powershell
> $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
> $env:ELECTRON_CUSTOM_DIR = "{{ version }}"
> npm install
> ```

## 架构

```
desktop/
├── package.json
├── vite.config.ts              # Vite 构建配置
├── tsconfig.*.json             # renderer / main / preload 三份 TS 配置
├── index.html
└── src/
    ├── main/index.ts           # Electron 主进程：窗口 + 自动拉起 Python 后端 + IPC
    ├── preload/index.ts        # contextBridge 安全桥
    └── renderer/
        ├── main.tsx            # React 入口
        ├── App.tsx             # 三栏布局装配 + 首次引导 + 事件接线
        ├── styles/global.css   # 设计系统（CSS 变量双主题）
        ├── lib/
        │   ├── types.ts        # 事件/会话/权限类型（对齐后端 hub 事件）
        │   └── backend.ts      # WebSocket 事件流 + /api/run 提交
        ├── store/index.ts      # Zustand：ui / session / permission / params / backend
        └── components/
            ├── StatusBar.tsx   # 顶部状态栏
            ├── LeftPanel.tsx   # 会话列表 + 文件树
            ├── RightPanel.tsx  # 操作日志 / 模型参数 / 提示词模板
            ├── ChatFlow.tsx    # 对话流 + Diff/命令/步骤/权限卡片
            ├── InputArea.tsx   # 输入区（粘贴/拖拽/@）
            └── Onboarding.tsx  # 首次启动引导
```

## 后端集成

桌面应用复用项目现有的 FastAPI 后端（`dashboard/server.py`），Electron 主进程启动时**自动拉起**：

```
Electron 主进程
  └─ spawn python -m dashboard.server --port 8090
        ├─ /api/run    POST 提交任务目标（后台线程执行 Agent）
        ├─ /ws         WebSocket 事件流（run_start → tool_call → answer → run_end）
        └─ /api/state  状态快照
```

渲染进程通过 WebSocket 订阅事件，映射为对话消息（事件模型对齐 `dashboard/hub.py`）。
无 API Key 时自动使用 `/api/demo` 演示模式，全流程可跑通。

## 状态管理（Zustand）

| Store | 职责 | 持久化 |
|---|---|---|
| `useUIStore` | 主题、左右面板折叠 | ✅ localStorage |
| `useSessionStore` | 会话列表、消息、增删改置顶 | ✅ |
| `usePermissionStore` | 权限模式、待确认操作 | ✅ |
| `useParamsStore` | temperature/top_p/max_tokens | ✅ |
| `useBackendStore` | 连接状态、事件流、运行状态 | ❌（内存） |

## 权限模型

- 模式：`auto`（自动执行）/ `ask`（每次询问）/ `block`（只读沙箱，拒绝一切写入/执行）
- 状态栏点击循环切换；受限操作在对话流弹出确认卡片（允许/拒绝）
- **审批闭环已打通**：`ask` 模式下后端工作线程通过 `ApprovalBroker` 发出 `approval` 事件并阻塞等待，
  前端卡片点击后经 WebSocket `approval_response`（或 HTTP `POST /api/approve` 兜底）回传决定；
  超时（默认 300s）自动拒绝并同步卡片状态（`approval_resolved`）
- 待扩展：按类别（文件写入/命令执行/网络访问）分别配置默认策略

## 打包发布

```bash
npm run dist        # electron-builder：Windows NSIS / macOS DMG / Linux AppImage
```

## 与 Web Dashboard 的关系

- `dashboard/`：浏览器版控制面板（FastAPI 直接服务静态页）
- `desktop/`：本桌面客户端（Electron 壳 + 同一后端）
- 两者共享 `/ws` 事件流与 `/api/run`，前端实现独立

## v0.2 升级记录

- **会话连续性**：`/api/run` 携带 `session_id`，后端恢复全部历史上下文并逐轮自动保存；删会话同步清理后端记录
- **任务队列**：任务运行中回车 = 排队（可移除），本轮结束自动下发下一条；队列持久化，重启后重连自动续发
- **Checkpoint 回滚**：`checkpoint` 事件携带 commit hash，工具行「回滚到此处」一键恢复（新提交方式，不重写历史）
- **工具输出实时流**：terminal 执行期间输出逐行推送（`tool_output` 事件），工具行内联显示末尾 3 行
- **桌面操控（computer 工具）**：截图+视觉分析 / UIA 无障碍树 / OS 级鼠标键盘（支持中文输入），高危审批门
- **设置中心**：单页分区（对话模型/视觉模型/工作目录/生成参数）；视觉模型三件套界面直配（`/api/config` 运行时覆盖，带测试连接）
- **多工作区**：设置工作目录后后端自动以该目录重启；状态栏显示当前工作区；会话按项目分组
- **体验**：自定义标题栏（拖拽区+原生按钮）、原生完成通知+任务栏闪烁、工具行内联输出、代码块（语言标签/复制/超长折叠）、思考折叠手动记忆、消息复制、跟随系统主题、权限/模型 chips 入 composer、回到底部、懒渲染
- **打包**：`npm run dist` 出 NSIS 安装包（应用图标：蓝紫渐变「悟」）；非管理员 shell 需先手动解压 winCodeSign 缓存或提权运行
