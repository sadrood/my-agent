# my_agent 快捷命令手册

> 全局启动：任何目录直接敲 `my-agent`（自动切换项目目录，无需 python main.py）
> 全套测试覆盖（pytest）；本文档随代码更新，以 `main.py --help` 为准。

## 一、全局启动命令（任何目录）

```cmd
my-agent                          # 交互模式
my-agent "任务描述"                # 单次任务
my-agent -q "任务"                 # 安静模式（无渲染输出）
my-agent --list-tools             # 查看全部工具（含 JSON Schema）
my-agent --team "复杂任务"         # 多 Agent 团队协作
my-agent --research "主题"         # 深度研究模式
my-agent --dashboard              # 已禁用（web 端停用；参数保留兼容，不再拉起服务）
my-agent --mcp-server             # 以 MCP server 运行（供上游宿主平台调用）
```

## 二、临时改配置（主流 CLI 风格，不写入 .env）

```cmd
# 1. 命令行参数
my-agent --model deepseek-v4-pro "任务"
my-agent --model <名> --base-url <API地址> --api-key <key> "任务"
my-agent --dangerously-skip-permissions "任务"   # = --approval never（主流 CLI 同名别名）
my-agent -r                                     # 恢复最近一次对话（-r 风格）

# 2. 会话内命令
> /model                          # 查看当前模型
> /model deepseek-v4-pro          # 切换模型（临时，并绑定到当前对话）
> /model <名>@<API地址>            # 连地址一起换
> /config                         # 查看当前生效配置（key 打码）

# 3. 环境变量（支持 ANTHROPIC_* 同名变量）
$env:ANTHROPIC_MODEL="deepseek-v4-pro"; my-agent     # 别名：MY_AGENT_MODEL / LLM_DEFAULT_MODEL
$env:ANTHROPIC_BASE_URL="https://..."; my-agent      # 别名：MY_AGENT_BASE_URL / LLM_BASE_URL
$env:ANTHROPIC_API_KEY="sk-..."; my-agent            # 别名：MY_AGENT_API_KEY / LLM_API_KEY
$env:MY_AGENT_MINIMAL="1"; my-agent                  # 极简模式（关掉 Guardian/追踪/快照/RepoMap 等非必要功能）
```

**对话绑定模型**：每个对话记录它当时用的模型；`--session <ID>` / `/open <ID>` / `-r` 恢复时自动切回。切换只影响主模型；视觉与 Guardian 各自独立配置。

## 三、执行模式与安全参数

```cmd
my-agent --loop "任务"             # 单循环模式（默认，主循环式）
my-agent --plan "任务"             # 经典计划模式（规划→逐步执行→总结）
my-agent --research "主题"         # 深度研究（单模型：搜索→抓页→提事实→综合报告）
my-agent --article "主题"          # 文章工坊（跨厂商双模型互审写作流水线）
my-agent --auto-mode "任务"        # 意图自动路由：调研/报告类→研究；团队/并行类→团队
my-agent --approval never "任务"   # 审批策略: untrusted | on-failure | on-request | never
my-agent --sandbox read-only "任务" # 沙箱: read-only | workspace-write | danger-full-access
my-agent --no-guardian             # 关闭 Guardian 审校
my-agent --no-rollout              # 关闭事件追踪日志
my-agent --no-stream               # 关闭流式输出
my-agent --no-vision               # 关闭视觉能力
my-agent --max-ops 40              # 单循环整次任务最大操作轮数（默认 24）
my-agent --max-step-ops 15         # 计划模式单步骤最大操作数（默认 12）
my-agent --max-steps 30            # 计划模式最大步骤数（默认 20）
my-agent --session <对话ID>        # 恢复指定对话（见下方对话管理）
```

## 三、交互模式命令（`>` 提示符下输入）

| 命令 | 说明 |
|---|---|
| `exit` / `quit` / `q` | 退出 |
| `/tools` | 查看工具列表 |
| `/team <任务>` | 团队协作（可连写：`/team任务`） |
| `/research <主题>` | 深度研究（可连写：`/research主题`） |
| `/article <主题> [\| 要求]` | **文章工坊**：多模型互审写作（一家写、另一家审 + 事实核查 + 逐条修订 + 校对定稿） |
| `/image <描述>` | 文生图（SenseNova U1.5 Lite，生成并保存到 `generated_images/`） |
| `/memory` | 记忆总览（各层条数 + 完成/失败步骤）；`/memory prune 100` 清理长期记忆 |
| `/consent` | **Guardian 放行台账**：被拦下待放行的调用 + 已生效的人工授权 |
| `/help` | 查看全部可用命令 |
| `/sessions` | **查看全部对话 ID**（● 标记当前对话） |
| `/open <对话ID>` | 打开并恢复某对话的全部记录，继续聊 |
| `/new` | 新开一个对话 |
| `Ctrl+C` | 中断当前任务，回到提示符（不会退出程序） |
| 打错命令 | 自动提示最近命令（如 `/reasearch` → 建议 /research） |

## 三·五、多行输入（对齐主流 agent 的粘贴体验）
- **直接粘贴多行文本**：回车一次提交整块，内部换行完整保留，不会像单行
  读取那样只剩第一行。收到后会提示"已接收多行输入（N 行）"。
- **手工换行**：行尾输入 `\` 再回车（主流 CLI 同款续行约定），续行提示符 `…`：
  ```
  > 写一个脚本：\
  > … 读取 data.json \
  > … 输出统计结果
  ```
- 注意：Windows 路径行尾 `C:\path\`（单个反斜杠结尾）也会触发续行，
  如要输入字面反斜杠请写两个 `\\`。
- 管道/重定向（非 TTY）模式保持逐行一条消息，不自动合并。

## 三·六、文生图 / 文生视频

### 文生图（OpenAI 兼容 `/images/generations`）

- 交互命令：`/image <图片描述>`，例如：
  ```
  > /image 一张信息图海报：标题「AI Agent 架构」，蓝色扁平风格，留白充足
  ```
- Agent 任务中：直接让它"生成一张 XX 的图片"即可，会自动调用 `image_gen` 工具。
- 生成结果保存到 `./generated_images/`（已在 .gitignore），返回本地文件路径。
- **两种返回形式都会落盘**：提供方给 `b64_json`（商汤）→ 解码保存；
  给 `url`（Agnes）→ **自动下载**后保存（下载失败则回退给出 URL）。
- 配置（.env）：
  ```
  IMAGE_GEN_API_KEY=sk-...                 # 留空回退主 LLM key
  IMAGE_GEN_BASE_URL=https://api.agnes-ai.cn/v1
  IMAGE_GEN_MODEL=agnes-image-2.5-flash    # 或 agnes-image-2.1-flash
  IMAGE_GEN_SIZE=1024x1024                 # 1024x1024 / 768x1024 / 1280x720 ...
  IMAGE_GEN_SAVE_DIR=./generated_images
  IMAGE_GEN_WATERMARK=false                # 商汤专有字段；不支持的提供方会自动跳过
  ```
- 实测可用的模型：`agnes-image-2.5-flash`、`agnes-image-2.1-flash`（Agnes，返回 url）、
  `sensenova-u1.5-lite`、`sensenova-u1-fast`（商汤，返回 b64）。
  注意 `agnes-image-2.0-flash` 已下线（503 无可用渠道）。

### 文生视频（OpenAI Videos 兼容，**异步任务**）

- Agent 任务中：让它"生成一段 XX 的视频"即会调用 `video_gen` 工具。
- 两步式契约（实测 Agnes）：
  ```
  创建: POST {BASE_URL}/videos
        {"model": "agnes-video-2.5-flash", "prompt": "...", "seconds": "5",
         "mode": "text", "size": "720P", "aspect_ratio": "16:9"}
        → {"video_id": "task_xxx", "status": "queued"}
  查询: GET {HOST}/agnesapi?video_id=<ID>&model_name=<模型>
        ⚠️ 查询端点在 **HOST 根路径**（不在 /v1 下）
        → {"status": "queued|in_progress|completed|failed", "progress": 0-100,
           "url": "<mp4>" | null, "error": null}
  ```
- 工具命令：
  ```
  video_gen(command="generate", prompt="...", seconds="5", size="720P")
  video_gen(command="status", task_id="task_xxx")     # 超时后取结果
  ```
- **超时不丢任务**：5 秒/720P 实测约 40-70 秒；等待上限 `VIDEO_GEN_MAX_WAIT`（默认 240s，
  须小于工具级 `TOOL_TIMEOUT` 300s）。超时返回 `task_id` 并提示稍后用 `status` 取回
  ——**不要重新生成**。
- 完成后自动下载 mp4 到 `./generated_videos/`。
- 配置（.env）：
  ```
  VIDEO_GEN_API_KEY=sk-...                 # 留空回退主 LLM key
  VIDEO_GEN_BASE_URL=https://api.agnes-ai.cn/v1
  VIDEO_GEN_QUERY_BASE=                    # 留空由 BASE_URL 去掉 /v1 推导
  VIDEO_GEN_MODEL=agnes-video-2.5-flash    # 或 agnes-video-2.5 / agnes-video-v2.0
  VIDEO_GEN_SECONDS=5
  VIDEO_GEN_SIZE=720P                      # Flash 仅支持 720P
  VIDEO_GEN_ASPECT_RATIO=16:9
  VIDEO_GEN_SAVE_DIR=./generated_videos
  VIDEO_GEN_MAX_WAIT=240
  ```

## 三·七、语音合成（配音）

- 工具：`tts`，两种供应商由 `TTS_PROVIDER` 切换：

### 供应商 A：`edge`（默认，免费、免 key）

```
tts(command="speak", text="从前有座山", voice="yunxi", rate="+10%")
tts(command="voices")            # 列出可用中文音色
```
- 音色别名：`xiaoxiao`(女·温柔) `xiaoyi`(女·活泼) `yunxi`(男·年轻)
  `yunjian`(男·沉稳解说) `yunyang`(男·新闻) `yunxia`(男·少年)
  `liaoning`(东北话) `shaanxi`(陕西话)；也可传完整名 `zh-CN-YunxiNeural`

### 供应商 B：`openrouter`（可挂 fish-audio 等 TTS 模型）

```
tts(command="speak", text="有些告别，要重复七次。")
tts(command="voices")            # 显示当前模型与克隆参考样本状态
```
- 走 OpenRouter 的 `/api/v1/audio/speech`（OpenAI 兼容），按字符计费；
  `:free` 变体 0 元（如 `fish-audio/s2.1-pro-free:free`，多语言、有表现力）。
- **音色**：由模型决定；fish-audio 没有预设音色目录，不传 `voice` 即用内置默认音色。
  ⚠️ 别把 edge 的音色别名填进 openrouter——上游会报 `Invalid voice`（代码会自动
  忽略并留痕，但显式配置更清楚，用 `TTS_OPENROUTER_VOICE`）。
- **声音克隆**（模型支持时，如付费版 `fish-audio/s2.1-pro`）：在 `.env` 填
  `TTS_REFERENCE_AUDIO=<参考音频路径>` 与 `TTS_REFERENCE_TEXT=<该音频的文字稿>`，
  即可用参考音色合成。免费版实测也能受理克隆请求。
- **兜底**：`TTS_FALLBACK_EDGE=true`（默认）时，OpenRouter 失败会自动回退
  edge-tts，并在工具输出里标注「已降级」——免费档不保证可用性。

### 配置

```
# 通用
TTS_ENABLED=true
TTS_PROVIDER=edge               # edge | openrouter
TTS_SAVE_DIR=./generated_audio

# TTS_PROVIDER=edge 时
TTS_VOICE=xiaoxiao
TTS_RATE=+0%

# TTS_PROVIDER=openrouter 时
TTS_OPENROUTER_API_KEY=         # 留空则读 OPENROUTER_API_KEY
TTS_MODEL=fish-audio/s2.1-pro-free:free
TTS_RESPONSE_FORMAT=mp3         # mp3（默认）/ pcm
TTS_OPENROUTER_VOICE=           # 留空＝模型默认音色
TTS_OPENROUTER_REFERER=         # 可选，OpenRouter 榜单归属
TTS_OPENROUTER_TITLE=my_agent
TTS_REFERENCE_AUDIO=            # 声音克隆参考样本（可选）
TTS_FALLBACK_EDGE=true
```
- 产物落在 `./generated_audio/`（mp3/wav），返回路径与时长，供 `video_edit` 合成。

## 三·八、外部短剧工厂 Toonflow（可选）

[Toonflow](https://github.com/HBAI-Ltd/Toonflow-app) 是独立的开源 AI 短剧工具。
它的 Electron 只是外壳，**后端是独立的 Express 服务**（默认 `127.0.0.1:10588`），
169 个 `/api` 路由覆盖 `原文 → 事件图谱 → 剧本 → 分镜 → 出图 → 出片` 全流程 ——
所以 agent 可以完全不碰它的界面，直接用 `toonflow` 工具编排它。

```
toonflow(command="health")      # 连通性/登录自检（不触发模型调用，不花钱）
toonflow(command="styles")      # 画风列表（建项目要填 artStyle）
toonflow(command="models")      # 模型列表（建项目要填 imageModel/videoModel）
toonflow(command="projects")    # 项目列表
toonflow(command="create_project", name="第七次葬礼", art_style="3",
         image_model="...", video_model="...", video_ratio="9:16")
toonflow(command="add_novel", project_id=1, text="第一章 …\n…")   # 自动切章
toonflow(command="events", project_id=1)
toonflow(command="storyboard", project_id=1)
toonflow(command="videos", project_id=1)
toonflow(command="call", method="POST", path="/api/xxx", body={...})  # 直连其余路由
```

- **登录**：`POST /api/login/login` → JWT（有效期 180 天），客户端自动缓存并在 401 时重登。
- **参数发现**：上游用 zod 校验，字段不对会返回 400 + 具体字段名，错误原样回传，
  按提示补齐即可；主干路由清单见工具 `description`（来自上游 `router.ts` 核实）。
- **配置**：
  ```
  TOONFLOW_ENABLED=true
  TOONFLOW_BASE_URL=http://127.0.0.1:10588
  TOONFLOW_USERNAME=admin
  TOONFLOW_PASSWORD=admin123
  TOONFLOW_MAX_CHARS=6000        # 单次回给模型的 JSON 上限
  ```
- ⚠️ **仅限本机**：Toonflow 默认账号 `admin/admin123`、密码明文比对、token 180 天有效。
  要暴露到局域网请先在它里面改密码。
- ⚠️ **它要自己的模型供应商**（设置中心 → 模型服务），生成走它配置的厂商；
  官方 Demo 做 2 分钟短剧约 ¥130（大头是视频）。可以把本项目的 Agnes/OpenRouter
  key 填进它的供应商配置。
- 它的 API 无公开文档、路由是代码生成的，版本间字段可能变；**升级后先跑 `health`**。

## 三·九、视频剪辑与合成（漫剧路线）
工具：`video_edit`（底层 ffmpeg）。**"图 + 运镜 + 配音"**是漫剧/图文视频的推荐路线
——不消耗 `video_gen` 的视频生成配额（免费额度有严格 RPM 限制），画面完全由图像模型控制。

| 命令 | 作用 |
|---|---|
| `kenburns` | 静态图 → 带推拉摇移的镜头（`zoom_in`/`zoom_out`/`pan_*`/`static`） |
| `concat` | 按顺序拼接多段（先试无损 copy，规格不一致自动回退重编码） |
| `add_audio` | 给视频叠配音/BGM（音轨短于画面时**自动补静音**，不截断画面） |
| `trim` | 裁剪片段 |
| `probe` | 读时长/分辨率/有无音轨（用于对齐画面与配音） |
| `subtitle` | 烧录 SRT 字幕 |

**完整漫剧流程**（实测 2 镜样片耗时 27.5 秒）：
```
1. image_gen              逐镜出图（同角色在 prompt 里固定外观描述保持一致）
2. video_edit kenburns    图片 → 运镜镜头（可传 images 批量，运镜自动轮换）
3. tts                    逐镜台词配音
4. video_edit add_audio   视频 + 配音 合成
5. video_edit concat      拼接成完整视频
```

- **统一输出规格** `1280x720 @ 25fps`（保证片段可无损拼接，无需重编码）。
- 依赖 **ffmpeg/ffprobe**：自动探测顺序为 `FFMPEG_PATH` → PATH → `static-ffmpeg`(pip) → winget 目录。
  安装方式（任选）：
  ```
  .venv\Scripts\python -m pip install static-ffmpeg      # 推荐：自带二进制，国内可下
  winget install --id Gyan.FFmpeg -e --source winget     # 备选（受限网络可能卡住）
  ```
- 配置：
  ```
  VIDEO_EDIT_ENABLED=true
  VIDEO_EDIT_WIDTH=1280
  VIDEO_EDIT_HEIGHT=720
  VIDEO_EDIT_FPS=25
  VIDEO_EDIT_SAVE_DIR=./generated_videos
  FFMPEG_PATH=                    # 留空自动探测
  ```

## 三·十、文章工坊（写作 / 审阅 / 校对）

**为什么要两个模型**：同一个模型审自己写的稿子，知识边界、行文偏好、盲点完全重合，
"互审"会退化成自我复述（同厂不同名的模型也基本重合）。所以默认**商汤写、Agnes 审**。

### 在对话里直接用（agent 会自己判断）

| 你说 | agent 会做 |
|---|---|
| "帮我校对一下 xxx.md，看看有没有错别字/标点问题" | `article(operation="proofread", file=...)`（另一家模型只挑错 + 出修正稿） |
| "审阅一下这篇，提提意见" / "帮我看看逻辑和事实有没有问题" | `article(operation="review", file=...)`（提意见 + 对可疑事实联网核查） |
| "写一篇关于 X 的文章" / "写份报告" | `article(operation="write", topic=...)`（大纲→初稿→审阅→核查→修订→校对→定稿） |

> 校对/审阅**默认不改原文件**（修正稿落在产物目录）。要让 agent 就地替换原文件，
> 明确说一句"直接改原文件"，它会带 `apply=true`（写回前自动留 `.bak` 备份）。
> 只是"顺手改几个字"的小事不必走这条流水线，agent 直接用 `edit` 更快。

### 命令行入口

```cmd
my-agent --article "AI Agent 的记忆机制"        # 写一篇
/article 为什么需要长期记忆 | 给非技术读者,400字  # 交互模式（| 后是写作要求）
article models                                   # 看各阶段用了哪个模型（谁写、谁审）
```

### 产物与配置

产物在 `output/articles/<名字>-<时间>/`：

- `write`：`outline.md` / `draft.md` / `review-rN.md`+`json` / `factcheck-rN.md` /
  `revise-rN.md` / `proofread.md`+`json` / `final.md` / `changes.md`（逐条修改台账）/ `meta.json`
- `review`：`review.md`+`json`（+ 有事实问题时 `factcheck.md`，带来源）
- `proofread`：`proofread.md`+`json` / `final.md`（修正稿）

```cmd
ARTICLE_MODEL_DRAFT=main            # main=主模型（LLM_*）；agnes=Agnes；也可写 agnes:模型名
ARTICLE_MODEL_REVIEW=agnes          # 审阅：默认另一家
ARTICLE_MODEL_PROOFREAD=agnes       # 校对：默认另一家
ARTICLE_MAX_REVISE_ROUNDS=2         # 审阅→修订最多几轮（到顶带着剩余意见定稿）
ARTICLE_FACTCHECK=true              # 只核查审阅方点名的可疑说法
ARTICLE_LOOKUP_SOURCES=zhihu,browser # 核查检索通道（zhihu 需配 ZHIHU_ACCESS_SECRET）
ARTICLE_FALLBACK_ENDPOINT=auto      # 某阶段被限流时自动换另一家把这一步跑完
```

> 机械类问题（半角标点、引号不配对、叠字、省略号写法）由**规则**直接判定，不花 token、
> 可复现；语义与措辞问题才交给审阅模型——实测模型会漏掉"全文都用半角逗号"这种惯例问题。

## 三·十一、被 Guardian 拦了怎么放行

Guardian 是**独立于审批门的第二道盲审**（`审批门 → Guardian → 执行`）。它只看到
「任务目标 + 工具名 + 参数 + 风险等级」，**看不到对话、也看不到你在审批门点的 y**，
所以"跟它讲道理"是没用的——能生效的是下面这几条：

| 做法 | 说明 |
|---|---|
| **拦截当场答 `y`** | 交互式会话里 Guardian 拦下时会弹 `仍要执行? [y/N/always]`：`y`=只放行这一次，`always`=本会话内**完全相同的那次调用**都放行 |
| **跟 agent 说"这个我允许"** | agent 求助后你回一句「那个删除我允许 / 放行 / 可以执行」，宿主把它绑定到刚被拦的那次调用；agent 重试时直接跳过盲审 |
| `/consent` | 查看台账：哪些调用被拦下待放行、哪些已授权 |
| `GUARDIAN_MIN_RISK=high` | 只有 high 风险才送审（medium 的写文件/删文件不再过 Guardian） |
| `--no-guardian` / `GUARDIAN_ENABLED=false` | 整层关掉（启动横幅与 `--doctor` 会显示开关状态） |
| **把授权写进任务目标** | Guardian 拿得到 `goal`：直接写「清理 output/tmp（**我已授权删除**）」。注意中途在对话里补一句没用，它只拿运行开始时的 goal |

**为什么"跟 agent 说可以"能生效、而模型自己说"用户已授权"不行**：授权只能由
**人类输入**产生（你敲的那句话 / 弹窗里你的 y），模型输出、工具结果、网页内容
永远不会被解析成授权——否则提示注入就能给自己发通行证，而那正是 Guardian 要拦的。

**边界（安全语义没被削弱）**：
- 授权默认**一次性**、有 TTL（默认 30 分钟），`always` 也只对**完全相同**的调用指纹
  有效（同工具 + 同参数），参数一变就要重新问；
- 无人值守（`--approval never` / 非交互）**不弹窗、不询问**，拦截保持生效；
- 授权只跳过 Guardian 盲审这一层：**审批硬黑名单与沙箱等级检查在它之前**，碰不到。

> 实测：真 Guardian 对同一个命令的裁决会**随机波动**（同一个删除命令 3 次里 2 次拦、
> 1 次放），这是 LLM 审校的固有特性——所以确定性的手段（配置旋钮 + 人工授权）才重要。

相关配置：`GUARDIAN_CONSENT` / `GUARDIAN_CONSENT_TTL` / `GUARDIAN_CONSENT_PROMPT`。

## 三·十二、截图识字（本地 OCR，不依赖视觉模型）

视觉模型（多模态 LLM）负责"看懂版面、找元素坐标"，但**识字**这件事本地引擎就能做：
快、离线、免费，也不会因为上游超时/"该模型不支持图片"而整个卡死。

```cmd
ocr D:\path\shot.png      # 识别图片里的文字（也可写相对路径）
ocr                       # 识别最近一张截图（自动找 screenshots/ 等目录下最新的图）
ocr engines               # 看本机可用的后端
ocr lang                  # Windows OCR 支持的语言
```

**后端优先级（自动挑，谁可用用谁）**：

| 后端 | 说明 |
|---|---|
| `windows` | Windows.Media.Ocr，Win10/11 **自带、零安装**，实测本机支持 `zh-Hans-CN`（中英混排可读） |
| `rapidocr` | `pip install rapidocr-onnxruntime` 后自动启用（跨平台，精度通常更好） |
| `tesseract` | 需装 tesseract 可执行文件 + `pip install pytesseract` |

**agent 什么时候会自动用它**：
- 你让它"读图里的字/看看屏幕上报什么错" → `see` 只想要文字时**直接走 OCR**，不调用视觉模型；
- **视觉模型超时/不可用/不支持图片时自动降级到 OCR**（`see`、`computer screenshot`、
  浏览器视觉分析三处都接了兜底）——以前这种时候是整个失败；
- 桌面应用截图（`computer screenshot`）在视觉不可用时也会用 OCR 把屏幕文字读出来。

**准确率（本机实测，同一张现造的图）**：中文 20px 字号约 96%、34px 约 92-97%；
英文混排基本正确。已知的错法：个别汉字被拆（"无法"→"无氵去"）、下划线/点号可能被读成
「·」「．」（数字里的小数点已归一成 `.`）。要更准就装 `rapidocr`。

```cmd
OCR_ENABLED=true                 # 总开关
OCR_BACKEND=                     # windows / rapidocr / tesseract；留空自动挑
OCR_LANGUAGES=zh-Hans-CN,en-US   # Windows OCR 识别语言（按顺序尝试）
OCR_SCALE=2                      # 识别前放大倍数（实测 2 倍更准；1=不放大）
OCR_AUTO_FALLBACK=true           # 视觉失败自动降级到 OCR
OCR_PREFER_FOR_TEXT=true         # 要文字时直接用 OCR，不先问视觉模型
```

## 三·十三、备用模型（主模型超时就换一家）

上游抖动很常见：视觉模型超时、生图端点 502、某个模型"rpm exhausted"。所以视觉与生图
都接了**跨提供方备用链**——主模型失败就按顺序换备用的，全失败才报错（错误里列出每次
尝试的原因）。

**本机实测（商汤 token.sensenova.cn，用现有 key）**：

| 用途 | 可用的模型 | 备注 |
|---|---|---|
| 视觉（能读图） | `sensenova-6.8-flash-lite` | 实测答对了测试图里的暗号；`sensenova-6.7-flash-lite` 对多模态返回 404 |
| 生图 | `sensenova-u1.5-lite`、`sensenova-u1.5-fast`、`sensenova-u1-fast` | `/images/generations` 均可用，返回 b64_json |
| 纯文本 | `deepseek-v4-pro`、`glm-5.2`、`kimi-k3` 等 | 见下方"列出可用模型" |

```cmd
# 列出这把 key 能用的全部模型（权威，直接问端点）
python -c "import json,urllib.request;from config import LLM_CONFIG as c;req=urllib.request.Request(c['base_url'].rstrip('/')+'/models',headers={'Authorization':'Bearer '+c['api_key']});print([m['id'] for m in json.load(urllib.request.urlopen(req))['data']])"
```

```cmd
# ---- 视觉备用（默认：Agnes 主 → 商汤备）
VISION_FALLBACK_MODELS=sensenova-6.8-flash-lite
VISION_FALLBACK_BASE_URL=https://token.sensenova.cn/v1
VISION_FALLBACK_API_KEY=            # 留空 = 用主 LLM(商汤) 的 key
VISION_FALLBACK_TIMEOUT=60

# ---- 生图备用（默认：Agnes 主 → 商汤备）
IMAGE_GEN_FALLBACK_MODELS=sensenova-u1.5-lite
IMAGE_GEN_FALLBACK_BASE_URL=https://token.sensenova.cn/v1
IMAGE_GEN_FALLBACK_TIMEOUT=180
```

要点：
- 备用链**跳过与主端点+主模型完全相同的条目**（重试同一个挂掉的东西没意义）；
- 换了备用时，工具输出会**明确标注**（"本次由**备用模型** X 应答"），生图产物也标实际
  出图的模型——不会把备用的功劳记在主模型头上；
- 视觉还叠了一层**本地 OCR** 兜底（见上一节）：模型链全挂时，至少把字读出来。

## 三·十五、任务监管者（做完再交付，别每章来问一次）

**症状**：让它写小说/多集视频/一批文件，它做一部分就回来问一次"要不要继续"。

**为什么**（查代码 + 实测）：单循环唯一的停止条件是"模型给出最终回答"；唯一能拦住
提前收尾的**完成度闸门**依赖 agent 自己的清单，而**最近 8 次运行里 `todo_write`/`task`
调用数全是 0**——闸门从来没有触发条件。也没有任何角色对照**目标**审"到底做完没有"。

**现在有两道保险**：

1. **提示词要求先列全清单**：多部分交付物（三章小说/多集视频/一批修改）必须开头就用
   `todo_write` 把全部部分列出来，做完一条勾一条；只有三种情况允许中途停下问人
   （需要你本人操作、不可逆高风险、目标本身要求先请示）。
2. **独立监管者复核**（`agent/supervisor.py`）：模型想收尾时，由一个**另一家的模型**
   对照原始目标审完成度；发现只做了一部分（或结尾又在反问你）就把**下一步指令**
   发回循环继续做。

```
【监管裁决】verdict=continue
  理由：执行者结尾出现"想调整…随时说一声"的交互式反问，违反"不要中途问我"的指令；
       且交付物未展示具体内容，无法验证每章字数是否真正达标。
→ 模型继续干活，第二轮复核通过：verdict=done
```

```cmd
SUPERVISOR_ENABLED=true          # 总开关
SUPERVISOR_MAX_ROUNDS=3          # 最多推回去几次（有界，不会无限拉锯）
SUPERVISOR_MODEL=agnes-3.0-flash # 监管者模型（默认另一家，避免同源自评）
SUPERVISOR_FAIL_OPEN=true        # 监管者挂了就放行——坏掉的裁判不能把任务卡死
SUPERVISOR_MIN_TURNS=1           # 轮次门槛
SUPERVISOR_MIN_GOAL_CHARS=12     # 目标短于这个字数视为闲聊，不审
```

监管者**只给指令不动手**；它看到的证据是**事实**（工具调用/涉及文件/失败次数），
不是模型的自述。`rollouts/*.jsonl` 里有 `supervisor` 事件可复盘每次裁决。

## 三·十六、运行中卡住的排查

**症状**：程序跑着跑着不动了，但进程还在（CPU 为 0）。

**第一反应（Windows conhost 输出阻塞，最常见）**：
1. 点一下窗口按 `Esc`（取消 QuickEdit 文本选区）或右键；
2. 如果按过 `Ctrl+S`（暂停输出），按 `Ctrl+Q` 恢复；
3. 仍不行按 `Ctrl+C` 中断当前任务回到 `>` 提示符（对话不丢）。

**预防**：`.env` 设 `MY_AGENT_DISABLE_QUICKEDIT=true`——启动时自动关闭
QuickEdit 选区（退出时恢复），代价是该窗口失去鼠标框选复制
（Windows Terminal 不受影响）。

**查日志**：每次运行的事件流在 `rollouts/run-*.jsonl`，文件 mtime 停在
哪个事件、进程有无子进程/网络连接，可以定位卡点（卡在输出写 vs 卡在
等待模型 vs 卡在命令执行，现象各不相同）。

## 四、对话 ID（会话）管理——重点

**查看对话 ID 的方式：**

1. **启动时**：欢迎信息下方显示当前对话 ID
   ```
   · 对话 ID: conv-20260825-a1b2c3（下次用 --session conv-... 恢复全部记录）
   ```
2. **交互中输入 `/sessions`**：列出全部历史对话
   ```
   · 历史对话（2 个）:
     ● conv-20260825-a1b2c3  帮我写报告  12 条 · 2026-08-25 09:30
       conv-20260824-x9y8z7  计算任务    4 条 · 2026-08-24 23:47
   ```
3. **直接看文件**：`memory/sessions/` 目录下的 JSON 文件名就是对话 ID

**恢复对话（打开全部记录）：**
```cmd
my-agent --session conv-20260825-a1b2c3     # 启动时恢复
> /open conv-20260825-a1b2c3                # 交互中切换
```

**特点**：每轮结束自动全量保存（不截断）；标题取第一条用户消息；最多保留 50 个对话（SESSION_MAX）。

## 五、运行时快捷键

| 键 | 作用 |
|---|---|
| `Ctrl+C` | 任务执行中：中断并回到 `>` 提示符；提示符下：退出程序 |
| 审批提示时 | `y` 批准 / 回车或 `n` 拒绝 |

## 六、环境变量（.env 常用项）

| 变量 | 默认 | 说明 |
|---|---|---|
| LLM_DEFAULT_MODEL | — | 主模型（当前 deepseek-v4-flash） |
| LLM_BASE_URL / LLM_API_KEY | — | 主模型端点 |
| LLM_TIMEOUT | 300 | 请求超时秒数（防挂死） |
| LLM_MAX_RETRIES | 2 | 限流自动重试次数 |
| APPROVAL_POLICY | on-failure | 审批策略（当前 .env 已设 never） |
| SANDBOX_MODE | workspace-write | 沙箱等级 |
| VISION_MODEL / VISION_API_KEY / VISION_BASE_URL | 回退主 LLM | 视觉模型（可走独立端点） |
| GUARDIAN_ENABLED | true | Guardian 审校开关 |
| GUARDIAN_MODEL | 主模型 | 审校模型（当前 deepseek-v4-flash） |
| ROLLOUT_ENABLED / ROLLOUT_DIR | true / ./rollouts | 事件追踪（JSONL 日志） |
| SESSION_DIR / SESSION_MAX | ./memory/sessions / 50 | 对话存储 |
| SNAPSHOT_ENABLED | true | 运行前 git 快照安全网 |
| TEST_COMMAND | .venv\Scripts\python -m pytest tests -q | Agent 自测命令 |
| EDIT_PREFLIGHT | false | edit 改 .py 后自动跑测试，失败自动回滚（true 开启） |
| AUTO_MODE | false | 意图自动路由（等同 --auto-mode） |
| TUI_STATUS_BAR | true | 常驻状态栏：每轮刷新 token 计数/沙箱/策略（verbose 下） |
| TEAM_PARALLEL | false | Team 并行执行独立子任务（并行安全门 + 自动回退串行） |
| TEAM_WORKER_TIMEOUT | 900 | 并行 Worker 硬超时秒数（超时标记该子任务失败） |
| APPROVAL_COMMAND_WHITELIST | false | 命令白名单深度防御：终端命令未命中白名单需人工批准（never 下直接拒绝） |
| APPROVAL_COMMAND_WHITELIST_EXTRA | 空 | 追加白名单正则（用 \| 分隔） |
| SANDBOX_EXECUTION | off | OS 级沙箱：appcontainer = 终端前台命令进 Windows AppContainer（仅工作区可写、无网络、fail-closed） |
| SANDBOX_EXEC_TIMEOUT | 120 | 沙箱内命令硬超时秒数 |
| SANDBOX_GRANT_TOOLS | true | 沙箱启动时给 Python/Node/Git 解释器目录授予容器只读执行权限（icacls best-effort，false 关闭） |
| SANDBOX_GRANT_DIRS | 空 | 额外只读授权目录清单（; 分隔，工作区之外的共享库/数据集等） |
| SANDBOX_ALLOW_NETWORK | false | 容器网络放行：true 注入 internetClient 等三个 capability（实测私网可达；外网另受本机网络出口限制） |
| TERMINAL_FOREGROUND_TIMEOUT | 120 | 终端前台命令超时秒数（60s 会掐断负载下的全量测试；后台命令不受限） |
| HOOKS_ENABLED | false | 工具钩子开关：加载 HOOKS_FILE 中的 on_pre_tool_use / on_post_tool_use 回调（fail-open） |
| HOOKS_FILE | ./hooks.py | 钩子模块路径 |
| APPROVAL_EXEC_POLICY_ENABLED | false | execpolicy 结构化命令策略开关（deny 优先 + fail-open；不能豁免黑名单/沙箱等级） |
| APPROVAL_EXEC_POLICY_FILE | ./execpolicy.json | 策略规则文件（JSON 数组：match 的 tool/command_prefix/pattern + decision 的 allow/deny/ask） |
| SKILLS_ENABLED | true | Skills 技能包开关：扫描技能目录，goal 命中关键词时把 SKILL.md 正文注入系统提示 |
| SKILLS_DIR | ./skills | 项目级技能目录（每个含 SKILL.md 的子目录即一个技能） |
| SKILLS_USER_DIR | ~/.my_agent/skills | 用户级技能目录 |
| SKILLS_MAX_CHARS | 6000 | 注入系统提示的技能文本总长上限 |

**技能目录结构**：`skills/<技能名>/SKILL.md`（frontmatter：name/description/triggers）+ 同目录普通文件自动登记为「附带脚本」。脚本仅被**告知**给 agent（渲染绝对路径清单），执行仍走终端 → 审批门原样生效；需免询问请在 execpolicy.json 显式加 allow 规则。

**技能包安装/更新**（完整性校验：MANIFEST.sha256 逐文件校验；未签名包默认拒绝）：

```cmd
python main.py --skills-install <技能包目录>                 # 安装到项目级 skills/
python main.py --skills-install <包> --skills-target user    # 安装到用户级
python main.py --skills-install <包> --skills-overwrite      # 更新已存在的同名技能
python main.py --skills-install <包> --skills-allow-unsigned # 放行无 manifest 的包（不推荐）
```

## 七、长任务与后台命令（terminal 工具）

- 执行时传 `background=true`（模型自动使用），或交互里直接说"后台跑 XX 服务"：
  - `terminal bg list` 列出后台任务
  - `terminal bg output <任务ID> [N]` 看最近 N 行输出
  - `terminal bg kill <任务ID>` 结束任务（输出仍可查看）
- 并行工具调用：模型一轮返回多个互不依赖的调用（读文件/只读浏览器/
  低风险终端/生图）时自动并发执行，结果按原顺序回喂；混入编辑等
  有冲突风险的工具时整批保持串行。

## 八、排查工具

```cmd
my-agent --list-tools                     # 看工具和 schema
dir rollouts                              # 运行日志（JSONL，含 llm_error/reasoning_len）
dir memory\sessions                       # 对话记录文件
git log --oneline                         # 每次运行前的自动快照（可回滚点）
git diff HEAD -- <文件>                   # 看 Agent 改了什么
git revert <commit>                       # 回滚某次运行
.venv\Scripts\python -m pytest tests -q   # 跑测试（全套）
```
