"""
配置中心
所有模块的配置都从这里读，按模块分组。

使用方式：
    from config import LLM_CONFIG
    print(LLM_CONFIG["api_key"])

环境变量在项目根目录的 .env 文件中设置。
"""

import os
from dotenv import load_dotenv

def _drop_empty_env_values() -> None:
    """把**空值**环境变量当成"没设置"。

    `os.getenv(key, "默认")` 在变量被设成空串时返回 `""` 而不是默认值，于是
    `int(os.getenv("LLM_DEFAULT_TEMPERATURE", "0.7"))` 这类直接抛
    `ValueError: could not convert string to float: ''` —— `import config` 崩，
    `python main.py` 连欢迎界面都出不来（2026-09-22 审计实测）。

    而"留空"恰恰是这个项目里最常见的写法：`.env.example` 自己就用
    `VISION_API_KEY=` 引导用户按需填值，照抄给数字项留空太自然了；本文件 64 处
    `int(os.getenv(` 与 34 处 `float(os.getenv(` 里只有两处写了空值保护。
    与其逐个补，不如在读到配置之后统一收掉：**空 = 未设置**。

    安全性：全仓库没有一处依赖"空串 ≠ 未设置"来区分行为 —— 无默认值的
    `os.getenv(...)` 调用点要么做真值判断、要么串在 `or` 链里，而 `""` 与 `None`
    同为假值，删除后语义不变。
    """
    for key in [k for k, v in os.environ.items() if v == ""]:
        os.environ.pop(key, None)


# 加载 .env 文件中的环境变量
load_dotenv()
# 留空的项当"未设置"：否则 int()/float() 解析会抛 ValueError，整个配置导入即崩
_drop_empty_env_values()

# 项目根目录（config.py 位于仓库顶层）：默认存储路径（记忆/会话/缓存）统一
# 锚定到这里，杜绝进程 cwd 漂移导致数据写到 output/ 等子目录造成分叉。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def resolve_under_root(path) -> str:
    """把相对路径锚定到项目根（绝对路径原样返回）。

    同一个坑在这个仓库里被踩了三次，所以收成一个函数、只在配置加载时做一次：
      · 浏览器持久 profile：cwd 一变就换一份空 profile，表现为"自动化浏览器每次
        打开都没有记录"；
      · memory / session 存储：cwd 漂移会把记忆/会话写到 output/ 子目录造成分叉；
      · 经验库缓存 / rollout 目录 / 各 save_dir：云经验库看起来"凭空变空"、
        运行日志找不到、产物落到启动目录。

    实测：从 C:\\Users\\Administrator 启动时，`./memory/experience_lib` 会解析到
    `C:\\Users\\Administrator\\memory\\experience_lib`——一个不存在的新目录，
    于是 experience search 什么都搜不到。

    Args:
        path: 配置里的路径值；空值返回项目根（表示"就用项目根"）。

    Returns:
        绝对路径字符串。
    """
    expanded = os.path.expanduser(str(path if path is not None else "").strip())
    if not expanded:
        return PROJECT_ROOT
    if os.path.isabs(expanded):
        return expanded
    return os.path.abspath(os.path.join(PROJECT_ROOT, expanded))


def resolve_minimal_mode(env: dict) -> bool:
    """极简模式解析（纯函数，便于测试）：禁用一切非必要功能。"""
    return str(env.get("MY_AGENT_MINIMAL", "")).lower() in ("1", "true", "yes")


def resolve_llm_config(env: dict) -> dict:
    """LLM 连接配置解析（纯函数）：支持主流 CLI 同名环境变量别名。"""
    def get(*names, default=None):
        for name in names:
            value = env.get(name)
            if value:
                return value
        return default

    return {
        "api_key": get("MY_AGENT_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"),
        "base_url": get("MY_AGENT_BASE_URL", "ANTHROPIC_BASE_URL", "LLM_BASE_URL",
                        default="https://api.openai.com/v1"),
        "default_model": get("MY_AGENT_MODEL", "ANTHROPIC_MODEL", "LLM_DEFAULT_MODEL",
                             default="gpt-4o-mini"),
    }


# 极简模式：禁用一切非必要功能
MINIMAL_MODE = resolve_minimal_mode(os.environ)


def _env(*names, default=None):
    """按优先级读取环境变量（支持主流 CLI 同名变量的别名）。"""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default

# ============================================================
# LLM 配置
# ============================================================
# 注意：
#   api_key 和 base_url 是创建客户端用的，必须在 .env 里配置。
#   default_model / default_temperature / default_max_output_tokens
#   只是默认值，调用 chat() 时可以覆盖。
LLM_CONFIG = {
    # --- 连接配置（必须）---
    # 别名兼容 ANTHROPIC_MODEL / ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY 环境变量
    "api_key": _env("MY_AGENT_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"),
    "base_url": _env("MY_AGENT_BASE_URL", "ANTHROPIC_BASE_URL", "LLM_BASE_URL",
                     default="https://api.openai.com/v1"),
    "default_model": _env("MY_AGENT_MODEL", "ANTHROPIC_MODEL", "LLM_DEFAULT_MODEL",
                          default="gpt-4o-mini"),
    "default_temperature": float(os.getenv("LLM_DEFAULT_TEMPERATURE", "0.7")),
    "default_max_output_tokens": int(os.getenv("LLM_DEFAULT_MAX_OUTPUT_TOKENS", "4096")),
    # 固定温度：某些 thinking 模型只接受特定 temperature（如 kimi-k3 只允许 1）。
    # 设置后忽略传入的 temperature，强制用该值（None=不启用）。
    "fixed_temperature": (lambda v: float(v) if v else None)(os.getenv("LLM_FIXED_TEMPERATURE", "")),

    # --- 自动重试（OpenRouter 等供应商限流/网络抖动时自动重试） ---
    "max_retries": int(os.getenv("LLM_MAX_RETRIES", "2")),
    "retry_base_delay": float(os.getenv("LLM_RETRY_BASE_DELAY", "2.0")),
    # --- 请求超时（秒）：防止上游挂起导致无限等待 ---
    "timeout": float(os.getenv("LLM_TIMEOUT", "300")),
}

# 上下文压缩（借鉴同类实现的 compaction：token 压力时把旧历史总结成摘要）
COMPACT_CONFIG = {
    "enabled": os.getenv("COMPACT_ENABLED", "true").lower() == "true",
    # 压缩阈值（字符量，约等于 token ×3；messages 总字符数超过则压缩旧历史）。
    # 0/未设置 = 自动：按当前模型在网关报告的 context_window × window_ratio 计算，
    # 避免固定小数字在 1M 窗口模型下过早压缩（历史 4.5% 就丢细节）。
    "token_threshold": int(os.getenv("COMPACT_TOKEN_THRESHOLD", "0")),
    # 自动阈值 = 模型真实窗口 × 该比例（窗口来自网关 /models 的 context_length）
    "window_ratio": float(os.getenv("COMPACT_WINDOW_RATIO", "0.75")),
    # 保留最近多少条不压缩
    "keep_last": int(os.getenv("COMPACT_KEEP_LAST", "20")),
}

# ============================================================
# 浏览器配置
# ============================================================
BROWSER_CONFIG = {
    "headless": os.getenv("BROWSER_HEADLESS", "false").lower() == "true",
    "viewport_width": int(os.getenv("BROWSER_VIEWPORT_WIDTH", "1280")),
    "viewport_height": int(os.getenv("BROWSER_VIEWPORT_HEIGHT", "720")),
    # 持久浏览器（内置浏览器）：launch_persistent_context + 用户数据目录，
    # 登录态（cookie/localStorage）跨次启动保留——网页型分身（DeepSeek/豆包
    # 等）先手动登录一次，agent 之后复用会话。false = 每次全新上下文（旧行为）
    "persistent": os.getenv("BROWSER_PERSISTENT", "true").lower() == "true",
    # 持久 profile 目录（登录态落盘点）；memory/ 已被 gitignore，不入库
    "profile_dir": resolve_under_root(
        os.getenv("BROWSER_PROFILE_DIR", "./memory/browser_profile")),
    # 内嵌浏览器桥地址（仅桌面端模式由 Electron 主进程注入，如 http://127.0.0.1:8091/browser）。
    # 非空时 ToolManager 用 EmbeddedBrowserTool 替代独立 Playwright 浏览器：
    # Agent 操控的页面就是桌面端侧栏里内嵌的 <webview>（所见即所控）。
    "embedded_url": os.getenv("MY_AGENT_EMBEDDED_BROWSER_URL", ""),
    # 自动探测内嵌桥（默认开）：即使环境变量没注入成功，只要桌面端在运行
    # （桥 health 检查通过），browser 工具一律走内嵌浏览器，禁止弹出独立
    # Playwright 窗口；桌面端没开时（纯 CLI 场景）才回退外部浏览器。
    "embedded_auto_detect": os.getenv("BROWSER_EMBEDDED_AUTO", "true").lower() == "true",
}

# ============================================================
# 视觉模型配置
# ============================================================
# 视觉模型可使用独立端点（VISION_API_KEY / VISION_BASE_URL），
# 留空时回退到主 LLM 的 key / base_url。
# 例如主模型用 OpenRouter，视觉模型用商汤 SenseNova：
#   VISION_API_KEY=<商汤key>
#   VISION_BASE_URL=https://token.sensenova.cn/v1
#   VISION_MODEL=<商汤视觉模型名>
#
# 2026-09-23 实测（同一张答案已知的图；单图 2 次 + 多图/时序 2 次）：
#   agnes-3.0-flash 能读图（复测 2.0s/18.7s，3/3）但延迟波动大、认字精度略逊；
#   deepseek-flash（= DeepSeek V4.1 Flash）与
#   sensenova-6.8-flash-lite 都 3/3 命中、多图 4/4，1-4s。**关键：商汤
#   `GET /models` 把 deepseek-flash 的 input_modalities 标成 ["text"]，实测却
#   完全能读图** —— 判断某模型能不能读图不能只看元数据，要拿答案已知的图打一次。
VISION_CONFIG = {
    "screenshot_path": os.getenv("VISION_SCREENSHOT_PATH", "./screenshots"),
    "vision_model": os.getenv("VISION_MODEL", ""),  # 留空则自动选择
    "enabled": os.getenv("VISION_ENABLED", "true").lower() == "true",
    "api_key": os.getenv("VISION_API_KEY", "") or LLM_CONFIG["api_key"],
    "base_url": os.getenv("VISION_BASE_URL", "") or LLM_CONFIG["base_url"],
    # 单次视觉调用超时：SDK 默认 600s×3 次，远超调用方预算（工具 300s、
    # browser visionclick 仅 60s），必须显式收紧
    "timeout": float(os.getenv("VISION_TIMEOUT", "30")),
    # 备用视觉模型：主模型超时/报错/不支持图片时按顺序接着试
    # （实测商汤 sensenova-6.8-flash-lite 能读图，主模型抖动时可顶上）。
    # 注意**只有一组 base_url/api_key**：多个模型名共用同一个备用端点，
    # 所以"跨厂商兜底"只能选一家——要主备分属两家就：主走 VISION_*、备用放这里。
    # 逗号分隔多个模型；base_url/api_key 留空 = 用主 LLM 端点（商汤）。
    "fallback_models": [m.strip() for m in
                        os.getenv("VISION_FALLBACK_MODELS",
                                  "sensenova-6.8-flash-lite").split(",") if m.strip()],
    "fallback_base_url": os.getenv("VISION_FALLBACK_BASE_URL", "") or LLM_CONFIG["base_url"],
    "fallback_api_key": os.getenv("VISION_FALLBACK_API_KEY", "") or LLM_CONFIG["api_key"],
    "fallback_timeout": float(os.getenv("VISION_FALLBACK_TIMEOUT", "60")),
}

# ============================================================
# 图像生成配置（SenseNova Token Plan 文生图）
# ============================================================
# OpenAI 兼容端点：POST {base_url}/images/generations
# 模型（实测 token.sensenova.cn 可用）:
#   sensenova-u1.5-lite  文生图/信息图（构图/光影/文字渲染增强）
#   sensenova-u1-fast    信息图生成加速版
# 返回 b64_json，工具自动解码保存为本地 PNG。
IMAGE_GEN_CONFIG = {
    "enabled": os.getenv("IMAGE_GEN_ENABLED", "true").lower() == "true",
    "api_key": os.getenv("IMAGE_GEN_API_KEY", "") or LLM_CONFIG["api_key"],
    "base_url": os.getenv("IMAGE_GEN_BASE_URL", "https://token.sensenova.cn/v1"),
    "model": os.getenv("IMAGE_GEN_MODEL", "sensenova-u1.5-lite"),
    "size": os.getenv("IMAGE_GEN_SIZE", "1024x1024"),
    "save_dir": resolve_under_root(
        os.getenv("IMAGE_GEN_SAVE_DIR", "./generated_images")),
    "timeout": float(os.getenv("IMAGE_GEN_TIMEOUT", "120")),
    # 官方公测期间免费开放去水印（watermark=false）；默认关闭水印
    "watermark": os.getenv("IMAGE_GEN_WATERMARK", "false").lower() == "true",
    # 备用生图端点：主端点超时/报错时按顺序接着试（实测商汤 u1.5-lite/u1-fast/u1.5-fast
    # 都能出图，返回 b64_json）。默认指向商汤——若主端点本来就是商汤，
    # 把这里改成 Agnes（或留空关闭）才有意义；与主端点完全相同的条目会被自动跳过。
    "fallback_models": [m.strip() for m in
                        os.getenv("IMAGE_GEN_FALLBACK_MODELS",
                                  "sensenova-u1.5-lite").split(",") if m.strip()],
    "fallback_base_url": os.getenv(
        "IMAGE_GEN_FALLBACK_BASE_URL", "https://token.sensenova.cn/v1"),
    "fallback_api_key": os.getenv("IMAGE_GEN_FALLBACK_API_KEY", "") or LLM_CONFIG["api_key"],
    "fallback_timeout": float(os.getenv("IMAGE_GEN_FALLBACK_TIMEOUT", "180")),
}

# ============================================================
# 视频生成配置（OpenAI Videos 兼容：异步任务，实测 Agnes）
# ============================================================
# 契约（agnes-video-2.5-flash / 2.5 实测）：
#   创建: POST {base_url}/videos  → {"video_id": "task_xxx", "status": "queued"}
#   查询: GET  {query_base}/agnesapi?video_id=<ID>&model_name=<模型>
#         ⚠️ 查询端点在 HOST 根路径（不在 /v1 下），因此单独配 query_base
#   完成: status=completed 且 url 非空（mp4）
# 生成耗时：5 秒 / 720P 实测约 41 秒；更长/更高分辨率更久 →
#   工具默认等待 max_wait 秒，超时返回 task_id 供稍后查询（不丢任务）。
VIDEO_GEN_CONFIG = {
    "enabled": os.getenv("VIDEO_GEN_ENABLED", "true").lower() == "true",
    "api_key": os.getenv("VIDEO_GEN_API_KEY", "") or LLM_CONFIG["api_key"],
    "base_url": os.getenv("VIDEO_GEN_BASE_URL", "https://api.agnes-ai.cn/v1"),
    # 任务查询端点所在主机（默认由 base_url 去掉 /v1 推导）
    "query_base": os.getenv("VIDEO_GEN_QUERY_BASE", ""),
    "model": os.getenv("VIDEO_GEN_MODEL", "agnes-video-2.5-flash"),
    "seconds": os.getenv("VIDEO_GEN_SECONDS", "5"),        # 视频时长（秒，字符串）
    # 供应商合法区间：小于 4s 会被拒（invalid_request），超过 12s 不支持
    "seconds_min": float(os.getenv("VIDEO_GEN_SECONDS_MIN", "4")),
    "seconds_max": float(os.getenv("VIDEO_GEN_SECONDS_MAX", "12")),
    "size": os.getenv("VIDEO_GEN_SIZE", "720P"),           # Flash 仅支持 720P
    "aspect_ratio": os.getenv("VIDEO_GEN_ASPECT_RATIO", "16:9"),
    "save_dir": resolve_under_root(
        os.getenv("VIDEO_GEN_SAVE_DIR", "./generated_videos")),
    # 单次 HTTP 请求超时（创建/查询）
    "timeout": float(os.getenv("VIDEO_GEN_TIMEOUT", "120")),
    # 工具内等待上限（秒）：超过则返回 task_id 让模型稍后用 status 查询。
    # 需小于 TOOL_TIMEOUT(300)，避免撞上工具级硬超时被判定"挂起"。
    "max_wait": float(os.getenv("VIDEO_GEN_MAX_WAIT", "240")),
    "poll_interval": float(os.getenv("VIDEO_GEN_POLL_INTERVAL", "6")),
}

# ============================================================
# 语音合成配置（配音：多供应商）
# ============================================================
# 用于漫剧/短视频配音：文字 → mp3，再由 video_edit 的 add_audio 合到画面。
# 供应商：
#   edge       —— 微软 Edge 在线语音（免费、免 key、中文多音色，默认）
#   openrouter —— OpenRouter 的 /api/v1/audio/speech（OpenAI 兼容），
#                 可挂 fish-audio 等 TTS 模型；按字符计费，":free" 变体 0 元
TTS_CONFIG = {
    "enabled": os.getenv("TTS_ENABLED", "true").lower() == "true",
    "provider": os.getenv("TTS_PROVIDER", "edge").strip().lower(),
    # 默认音色（edge 的简短别名，见 models/tts.py ZH_VOICES）：
    #   xiaoxiao 女声温柔 / yunxi 男声年轻 / yunjian 男声沉稳解说
    "voice": os.getenv("TTS_VOICE", "xiaoxiao"),
    "rate": os.getenv("TTS_RATE", "+0%"),      # 语速（仅 edge 支持），如 +20%
    "volume": os.getenv("TTS_VOLUME", "+0%"),  # 音量（仅 edge 支持），如 +20%
    "save_dir": resolve_under_root(
        os.getenv("TTS_SAVE_DIR", "./generated_audio")),
    "timeout": float(os.getenv("TTS_TIMEOUT", "60")),
    # --- openrouter 供应商 ---
    # 专用 key 优先；也接受通用的 OPENROUTER_API_KEY
    "openrouter_api_key": (os.getenv("TTS_OPENROUTER_API_KEY")
                           or os.getenv("OPENROUTER_API_KEY", "")),
    "openrouter_base_url": os.getenv("TTS_OPENROUTER_BASE_URL",
                                     "https://openrouter.ai/api/v1"),
    "model": os.getenv("TTS_MODEL", "fish-audio/s2.1-pro-free:free"),
    # openrouter 的音色由模型决定：默认留空＝用模型内置默认音色。
    # 注意别把 edge 的音色别名（xiaoxiao 等）填这里——上游会报 Invalid voice。
    "openrouter_voice": os.getenv("TTS_OPENROUTER_VOICE", ""),
    # 输出格式：mp3（默认）/ pcm / wav —— 按模型支持情况填写
    "response_format": os.getenv("TTS_RESPONSE_FORMAT", "mp3"),
    # 站点归属头（可选，OpenRouter 官方示例里的 HTTP-Referer / X-OpenRouter-Title，
    # 仅用于 openrouter.ai 的排行榜统计，不影响请求结果）
    "referer": os.getenv("TTS_OPENROUTER_REFERER", ""),
    "title": os.getenv("TTS_OPENROUTER_TITLE", "my_agent"),
    # 声音克隆（fish-audio S2.1 Pro 等支持）：参考音频 + 其文字稿（可选）
    "reference_audio": os.getenv("TTS_REFERENCE_AUDIO", ""),
    "reference_text": os.getenv("TTS_REFERENCE_TEXT", ""),
    # 角色声线库（多角色配音用）：一个目录，每个文件是一个角色的克隆参考样本，
    # 命名 {角色名}.wav|mp3（如 linshen.wav / hugong.wav）。
    # 合成时传 voice=角色名，会自动带上对应参考样本 → 同角色音色恒定不偏移。
    # 优先级高于上面的全局 reference_audio；目录为空则退回全局参考。
    "reference_dir": os.getenv("TTS_REFERENCE_DIR", ""),
    # OpenRouter 失败时是否回退 edge-tts（免费档"不保证可用性"，兜底更稳）
    "fallback_edge": os.getenv("TTS_FALLBACK_EDGE", "true").lower() == "true",
}

# ============================================================
# Toonflow 对接配置（外部 AI 短剧工厂，由 agent 通过其 REST API 驱动）
# ============================================================
# Toonflow（https://github.com/HBAI-Ltd/Toonflow-app）是独立的短剧生产工具：
# 自带 Express 后端（默认 127.0.0.1:10588）与 169 个 /api 路由，覆盖
# 原文 → 事件图谱 → 剧本 → 分镜 → 出图 → 出片 全流程。agent 通过 HTTP 驱动它。
# ⚠️ 仅限本机使用：默认账号 admin/admin123、密码明文比对、token 有效期 180 天。
TOONFLOW_CONFIG = {
    "enabled": os.getenv("TOONFLOW_ENABLED", "true").lower() == "true",
    "base_url": os.getenv("TOONFLOW_BASE_URL", "http://127.0.0.1:10588").rstrip("/"),
    "username": os.getenv("TOONFLOW_USERNAME", "admin"),
    "password": os.getenv("TOONFLOW_PASSWORD", "admin123"),
    "timeout": float(os.getenv("TOONFLOW_TIMEOUT", "60")),
    # 单次回给模型的 JSON 字符上限（部分路由会返回整表数据）
    "max_chars": int(os.getenv("TOONFLOW_MAX_CHARS", "6000")),
}

# ============================================================
# 知乎数据开放平台配置（https://developer.zhihu.com/docs）
# ============================================================
# 鉴权走 Bearer：`Authorization: Bearer <access_secret>` +
# `X-Request-Timestamp: <秒级 Unix 时间戳>`（与服务端相差不得超过 10 分钟）。
# Access Secret 在开放平台「个人中心」获取：
#   ZHIHU_ACCESS_SECRET=<your_access_secret>
# 能力与每日限免额度（每项独立计数，可用 quota 命令实时查）：
#   全网搜索 / 知乎搜索 / 热榜 / 问题回答 / 用户数据 / 创作能力 /
#   直答 / 知识库 / 小工具（PDF 解析、PPT 生成）
ZHIHU_CONFIG = {
    "enabled": os.getenv("ZHIHU_ENABLED", "true").lower() == "true",
    "access_secret": os.getenv("ZHIHU_ACCESS_SECRET", "").strip(),
    "base_url": os.getenv("ZHIHU_BASE_URL", "https://developer.zhihu.com").rstrip("/"),
    # 普通读接口超时
    "timeout": float(os.getenv("ZHIHU_TIMEOUT", "30")),
    # 直答（大模型生成）与文件上传需要更久
    "llm_timeout": float(os.getenv("ZHIHU_LLM_TIMEOUT", "180")),
    "upload_timeout": float(os.getenv("ZHIHU_UPLOAD_TIMEOUT", "300")),
    # PDF 解析 / PPT 生成是异步任务：创建后轮询，这里是总等待上限
    "task_timeout": float(os.getenv("ZHIHU_TASK_TIMEOUT", "600")),
    # 异步任务轮询间隔（秒）
    "poll_interval": float(os.getenv("ZHIHU_POLL_INTERVAL", "3")),
    # 单次回给模型的字符上限（搜索/全文接口单条就可能很长）
    "max_chars": int(os.getenv("ZHIHU_MAX_CHARS", "12000")),
    # PDF 解析结果 / PPT 成品的落盘目录（项目规定产物统一放 output/）
    "save_dir": resolve_under_root(
        os.getenv("ZHIHU_SAVE_DIR", "output/zhihu")),
}

# ============================================================
# 视频剪辑配置（ffmpeg：图→运镜、拼接、配音合成、字幕）
# ============================================================
# "图 + 运镜 + 配音"路线：不消耗视频生成配额，画面可控（漫剧/图文视频）。
# 统一输出规格，保证 kenburns 产出的片段可直接无损拼接。
VIDEO_EDIT_CONFIG = {
    "enabled": os.getenv("VIDEO_EDIT_ENABLED", "true").lower() == "true",
    "width": int(os.getenv("VIDEO_EDIT_WIDTH", "1280")),
    "height": int(os.getenv("VIDEO_EDIT_HEIGHT", "720")),
    "fps": int(os.getenv("VIDEO_EDIT_FPS", "25")),
    "save_dir": resolve_under_root(
        os.getenv("VIDEO_EDIT_SAVE_DIR", "./generated_videos")),
    # ffmpeg 可执行文件所在目录（留空则用 PATH / winget 常见位置自动探测）
    "ffmpeg_path": os.getenv("FFMPEG_PATH", ""),
    # 单次 ffmpeg 处理超时（秒）：拼接/重编码可能较久
    "timeout": float(os.getenv("VIDEO_EDIT_TIMEOUT", "600")),
}

# ============================================================
# MCP 服务器配置
# ============================================================
# 示例 .env 配置:
#   MCP_SERVERS=[{"name":"filesystem","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}]
# 用户级插件（installer 工具安装）持久化在 ~/.my_agent/mcp_servers.json，
# 可用环境变量 MCP_USER_CONFIG_FILE 覆盖路径；重启后自动恢复连接。
import json
MCP_USER_CONFIG_FILE = os.path.expanduser(
    os.getenv("MCP_USER_CONFIG_FILE", "~/.my_agent/mcp_servers.json")
)
_mcp_servers_str = os.getenv("MCP_SERVERS", "[]")
try:
    _mcp_servers = json.loads(_mcp_servers_str)
except json.JSONDecodeError:
    _mcp_servers = []


def _load_user_mcp_servers() -> list:
    """读取用户级已安装 MCP 插件列表（installer 工具写入）；缺失/损坏返回空。"""
    try:
        with open(MCP_USER_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [
        dict(s) for s in data
        if isinstance(s, dict) and str(s.get("name", "")).strip() and str(s.get("command", "")).strip()
    ]


def _merge_user_mcp_servers(env_servers: list) -> list:
    """合并内置(.env) servers 与用户级已安装插件；同名时用户级覆盖。

    用户级插件打上 installed 标记（区别于 .env 静态配置），启动时一并自动连接。
    """
    merged = [dict(s) for s in env_servers if isinstance(s, dict)]
    names = {str(s.get("name")) for s in merged}
    for s in _load_user_mcp_servers():
        name = str(s.get("name"))
        if name in names:
            merged = [m for m in merged if str(m.get("name")) != name]
        s["installed"] = True
        merged.append(s)
        names.add(name)
    return merged


MCP_CONFIG = {
    # 客户端侧总开关（连接外部 MCP 服务器）。默认 true = 与**当前实际行为**一致：
    # 这个键此前从未被任何代码读取（2026-09-22 审计），也就是说不管 `.env` 里
    # 写什么，MCP 客户端都在跑。改默认值而不是直接照读 false，是为了让
    # `MCP_ENABLED=false` 这个开关真正可用，同时不悄悄关掉别人现有的 MCP。
    "enabled": os.getenv("MCP_ENABLED", "true").lower() == "true",
    "servers": _merge_user_mcp_servers(_mcp_servers),
    "user_config_file": MCP_USER_CONFIG_FILE,
}

# ============================================================
# Computer Use 配置
# ============================================================
COMPUTER_USE_CONFIG = {
    "enabled": os.getenv("COMPUTER_USE_ENABLED", "true").lower() == "true",
    "default_steps": int(os.getenv("COMPUTER_USE_DEFAULT_STEPS", "10")),
}

# ============================================================
# 记忆配置（以后用）
# ============================================================
MEMORY_CONFIG = {
    "db_path": os.getenv("MEMORY_DB_PATH", "./memory"),
}

# ============================================================
# 日志配置（以后用）
# ============================================================
LOG_CONFIG = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
}

# ============================================================
# 自我进化 / 学习配置
# ============================================================
# ⚠️ 这里区分两个容易混淆的概念（早先混在一起，导致经验库永远长不大）：
#   · 库容量（max_experiences_store）——磁盘上留多少历史
#   · 召回预算（max_experiences_recall）——每次任务往提示词里注入几条
# 之前两个值都写死在代码里（`self.experiences[-100:]` 与 `recall_experiences(n=3)`），
# 而这里定义的三个键**没有任何地方读取**：改 .env 静默无效，经验库实际是
# 「只保留最近 100 条的滑动窗口」。现在两个值都由本配置驱动。
LEARN_CONFIG = {
    "enable": os.getenv("LEARN_ENABLED", "true").lower() == "true",
    # 每次任务注入几条经验（召回预算；调大只影响提示词长度，不影响库容量）
    "max_experiences_recall": int(os.getenv("LEARN_MAX_RECALL", "3")),
    # 经验库容量：0 = 不限制（文件留全量）。
    # 默认给宽裕值而非真正的无限：experiences.json 每次保存是**整体重写**，
    # 无上限时单次写盘成本随历史线性增长（要真无限得改成 JSONL 追加）。
    "max_experiences_store": int(os.getenv("LEARN_MAX_STORE", "2000")),
    # 召回打分的候选池：同一类别各取最近 N 条参与打分（0 = 全量参与）。
    # 库变全量后由它把打分成本压住；排序仍由关键词/类别/时间打分决定。
    "recall_pool": int(os.getenv("LEARN_RECALL_POOL", "200")),
    # 相关性下限：候选经验与当前目标的关键词重叠数低于它就**不注入**
    # （宁缺毋滥）。库里大量是十几字的闲聊式目标，靠一两个通用 bigram 就能挤进
    # 前 3 条把提示词塞满噪音；设为 0 恢复"总是凑满 n 条"的旧行为。
    "min_overlap": int(os.getenv("LEARN_MIN_OVERLAP", "1")),
    # 写入侧质量门槛（策略判断在 Memory.should_record_experience，调用方记录前问一句）：
    # 目标短于 min_goal_chars **且**工具调用少于 min_tool_calls 才不记——两者都小的
    # 是闲聊/一次性问答，不是可复用经验。实测库里 159 条有 59 条属于此类，它们往往
    # 动过一两个工具（列目录/看模型）能穿过"有没有干活"的判断，召回时却只会塞噪音。
    # 只影响**新记录**，不动已有历史；**失败的经验总是记**（失败模式靠它）。0 = 关闭。
    "min_goal_chars": int(os.getenv("LEARN_MIN_GOAL_CHARS", "15")),
    "min_tool_calls": int(os.getenv("LEARN_MIN_TOOL_CALLS", "2")),
    # 经验压缩（memory distill）用的模型：留空 = 主模型。
    # 压缩是**离线维护任务**，而主模型（商汤）配额紧张时会回 429 / 空正文；
    # 换一家跑完更划算（例如 LEARN_DISTILL_BASE_URL=https://api.agnes-ai.cn/v1 +
    # LEARN_DISTILL_MODEL=agnes-3.0-flash + LEARN_DISTILL_API_KEY=<agnes key>）。
    # 只影响压缩，不动主循环的模型。
    "distill_model": os.getenv("LEARN_DISTILL_MODEL", ""),
    "distill_base_url": os.getenv("LEARN_DISTILL_BASE_URL", ""),
    "distill_api_key": os.getenv("LEARN_DISTILL_API_KEY", ""),
    # 压缩时**每组之间**等待秒数：账号 RPM 很低时连续几发大请求必被限流
    # （实测商汤 429 + 空正文），拉开间隔比换模型有效。
    "distill_pause": float(os.getenv("LEARN_DISTILL_PAUSE", "0")),
    # 失败模式按 error_type 去重，天然有界；此项只是安全阀（超限丢最久未见的）
    "max_failure_patterns": int(os.getenv("LEARN_MAX_PATTERNS", "50")),
    "inject_failure_warnings": os.getenv("LEARN_INJECT_WARNINGS", "true").lower() == "true",
}

# ============================================================
# 审批与沙箱配置（借鉴同类实现的 approval_policy / sandbox_mode）
# ============================================================
# approval_policy 取值（与主流 CLI 一致）:
#   untrusted       - 不信任模型：任何有写权限/风险操作都需人工批准
#   on-failure      - 自动执行，仅当命令失败后重跑前需要批准（默认）
#   on-request      - 仅当工具主动请求批准时才询问
#   never           - 永不询问（配合沙箱使用，适合无人值守/MCP 模式）
# sandbox_mode 取值（与上游宿主框架文件策略命名一致）:
#   read-only           - 只允许读操作，禁止一切写入/执行
#   workspace-write     - 默认：允许项目目录内读写，高风险操作需批准
#   danger-full-access  - 除硬性黑名单外全部放行
APPROVAL_CONFIG = {
    # 这两个值会被拿去和**小写**枚举比对（ApprovalPolicy 的 mode、SANDBOX_LEVELS
    # 的键），写 `Never` / `Workspace-Write` 会直接抛 ValueError、启动即 traceback
    # （2026-09-22 审计）。统一归一化，和本文件其它布尔量保持一致。
    "approval_policy": os.getenv("APPROVAL_POLICY", "on-failure").strip().lower(),
    "sandbox_mode": os.getenv("SANDBOX_MODE", "workspace-write").strip().lower(),
    "interactive": os.getenv("APPROVAL_INTERACTIVE", "true").lower() == "true",
    "default_answer_when_not_interactive": os.getenv("APPROVAL_NONINTERACTIVE_ANSWER", "deny"),
    "workspace_dir": os.getenv("APPROVAL_WORKSPACE_DIR", os.getcwd()),
    # 审批决策日志最多保留条数（长驻进程里只增不减会越用越慢；只影响报表口径）
    "decision_log_max": int(os.getenv("APPROVAL_DECISION_LOG_MAX", "200")),
    "dangerous_requires_approval": os.getenv("APPROVAL_DANGEROUS_REQUIRES", "true").lower() == "true",
    # 命令白名单（深度防御）：true = 终端命令只有命中白名单才按原策略放行，
    # 未命中的一律升级为需人工批准（never/无人值守下直接拒绝）。
    "command_whitelist": os.getenv("APPROVAL_COMMAND_WHITELIST", "false").lower() == "true",
    # 追加白名单正则（| 分隔），在内置只读白名单基础上放行项目自有安全命令
    "command_whitelist_extra": [
        p for p in os.getenv("APPROVAL_COMMAND_WHITELIST_EXTRA", "").split("|") if p.strip()
    ],
    # execpolicy DSL（结构化命令策略，白名单模式的升级）：true = 启用策略文件
    # 规则（deny 优先）。安全边界：DSL 评估永远在黑名单与沙箱等级检查之后——
    # DSL 不能豁免黑名单，也不能豁免沙箱等级不足，allow 只影响"是否需要询问"。
    "exec_policy_enabled": os.getenv("APPROVAL_EXEC_POLICY_ENABLED", "false").lower() == "true",
    # 策略文件路径（规则数组 JSON，格式见 agent/execpolicy.py；文件缺失/损坏时
    # fail-open：规则置空并告警，回到内建策略，不放大权限）
    "exec_policy_file": os.getenv("APPROVAL_EXEC_POLICY_FILE", "./execpolicy.json"),
}

# ============================================================
# AGENTS.md 项目/用户指令配置（借鉴同类实现的分层指令文件）
# ============================================================
INSTRUCTIONS_CONFIG = {
    "enabled": (not MINIMAL_MODE) and os.getenv("AGENTS_ENABLED", "true").lower() == "true",
    "project_file": os.getenv("AGENTS_PROJECT_FILE", "AGENTS.md"),
    "user_file": os.getenv("AGENTS_USER_FILE", "~/.my_agent/AGENTS.md"),
    "max_chars": int(os.getenv("AGENTS_MAX_CHARS", "20000")),
}

# ============================================================
# Guardian 安全审校配置（借鉴同类实现的 guardian）
# ============================================================
# Guardian 可使用独立端点（GUARDIAN_API_KEY / GUARDIAN_BASE_URL），
# 留空时回退到主 LLM 端点。建议用快模型（如商汤 SenseNova），
# 慢速推理模型会触发审校超时。
GUARDIAN_CONFIG = {
    "enabled": (not MINIMAL_MODE) and os.getenv("GUARDIAN_ENABLED", "true").lower() == "true",
    "min_risk": os.getenv("GUARDIAN_MIN_RISK", "medium"),  # low/medium/high：低于该风险等级不审校
    "model": os.getenv("GUARDIAN_MODEL", ""),              # 留空用默认模型
    "timeout": int(os.getenv("GUARDIAN_TIMEOUT", "20")),   # 审校调用超时（秒）
    "fail_open": os.getenv("GUARDIAN_FAIL_OPEN", "true").lower() == "true",
    "api_key": os.getenv("GUARDIAN_API_KEY", "") or LLM_CONFIG["api_key"],
    "base_url": os.getenv("GUARDIAN_BASE_URL", "") or LLM_CONFIG["base_url"],
}

# ============================================================
# Guardian 人工放行（授权）配置
# ============================================================
# 用户诉求："agent 来求助说某一步被 Guardian 拦截时，我可以跟 Guardian 说放行，
# 或者我跟 agent 说可以执行，agent 拿着这个就能让 Guardian 放行。"
#
# 防伪是这里唯一重要的事：授权**只能由人类输入产生**（REPL/CLI 的人类输入、
# 或拦截当场的人工确认），模型输出与工具结果永远不会被解析成授权——否则提示注入
# 就能自我放行，而那正是 Guardian 要拦的东西。授权默认一次性、有 TTL，且只跳过
# Guardian 盲审这一层（审批黑名单与沙箱检查在它之前，碰不到）。
GUARDIAN_CONSENT_CONFIG = {
    "enabled": os.getenv("GUARDIAN_CONSENT", "true").lower() == "true",
    # 授权/被拦记录的有效期（秒）：过期自动失效，避免长期驻留
    "ttl": float(os.getenv("GUARDIAN_CONSENT_TTL", "1800")),
    # 保留最近几次被拦记录（人话点名时用来绑定具体哪一次）
    "max_pending": int(os.getenv("GUARDIAN_CONSENT_MAX_PENDING", "5")),
    "max_grants": int(os.getenv("GUARDIAN_CONSENT_MAX_GRANTS", "20")),
    # 拦截当场就问人（仅交互式会话；无人值守时不会问，仍然拦）
    "interactive_prompt": os.getenv("GUARDIAN_CONSENT_PROMPT", "true").lower() == "true",
}

# ============================================================
# 小快模型（杂活专用）配置 —— 学 Claude Code：后台杂活不占用主模型
# ============================================================
# 主模型负责"想与做"，但有些活纯属跑腿：生成会话标题、写交接摘要、压对话历史……
# 这些活交给主模型既慢又贵，还会挤占它的上下文预算。用一个小的快模型单独干，
# 失败就退回原来的规则实现（fail-open），绝不影响主流程。
# 默认用商汤自家的轻量模型（与主模型同一把 key，实测响应快）。
# ⚠️ 坑（2026-09-23 实测踩到）：**模型名是商汤专有的，端点却默认跟随主 LLM**
# （下面 base_url/api_key 留空 = 用 LLM_CONFIG 的）。主模型一换网关（例如换到内网
# 172.16.10.242），`sensenova-6.8-flash-lite` 在那边根本不存在 → 503，而杂活是
# fail-open 的，只会**无声**退回规则实现（会话标题变成截断文本），不报错。
# 所以换主模型端点时，请显式给 SMALL_MODEL / SMALL_MODEL_BASE_URL / SMALL_MODEL_API_KEY；
# `my-agent --doctor` 的「子系统模型对账」会逐端点核对这类组合。
# 另注意：若换成思考模型（如 deepseek-flash），SMALL_MODEL_MAX_TOKENS 要给够
# （推理也占额度，默认 200 可能只剩空正文）。
SMALL_MODEL_CONFIG = {
    "enabled": os.getenv("SMALL_MODEL_ENABLED", "true").lower() == "true",
    "model": os.getenv("SMALL_MODEL", "") or "sensenova-6.8-flash-lite",
    "base_url": os.getenv("SMALL_MODEL_BASE_URL", "") or LLM_CONFIG["base_url"],
    "api_key": os.getenv("SMALL_MODEL_API_KEY", "") or LLM_CONFIG["api_key"],
    "timeout": float(os.getenv("SMALL_MODEL_TIMEOUT", "10")),
    "max_tokens": int(os.getenv("SMALL_MODEL_MAX_TOKENS", "200")),
    # 哪些杂活交给它（逗号分隔；目前支持 titles = 会话标题）
    "chores": os.getenv("SMALL_MODEL_CHORES", "titles"),
    # 会话标题长度上限（字符）
    "title_max_chars": int(os.getenv("SMALL_MODEL_TITLE_CHARS", "18")),
}

# ============================================================
# 任务监管者（Supervisor）配置
# ============================================================
# 用户痛点："我让他写小说，它每写一章就来问我一次，不应该是写完所有的然后交接
# 任务结果吗"。实测原因：单循环唯一的停止条件是"模型给出最终回答"，而唯一能拦住
# 提前收尾的完成度闸门依赖 agent 自己的清单——实测最近 8 次运行 todo_write 调用数
# 全是 0，闸门从未触发；也没有任何角色对照**目标**审"到底做完没有"。
#
# 监管者 = 一个独立模型，在 agent 想收尾时审完成度；没做完就发回**下一步指令**。
# 默认用另一家（Agnes）而不是执行任务的模型（商汤）：同源自评容易自我确认。
SUPERVISOR_CONFIG = {
    "enabled": os.getenv("SUPERVISOR_ENABLED", "true").lower() == "true",
    # 最多把任务推回去几次（有界，避免与模型无限拉锯）
    "max_rounds": int(os.getenv("SUPERVISOR_MAX_ROUNDS", "3")),
    # 监管者模型端点；留空 model 或 key/base 缺失时回退主 LLM（并记警告）
    "model": os.getenv("SUPERVISOR_MODEL", "") or os.getenv("GUARDIAN_MODEL", "") or "agnes-3.0-flash",
    "base_url": (os.getenv("SUPERVISOR_BASE_URL", "")
                 or os.getenv("GUARDIAN_BASE_URL", "")),
    "api_key": (os.getenv("SUPERVISOR_API_KEY", "")
                or os.getenv("GUARDIAN_API_KEY", "")),
    "timeout": float(os.getenv("SUPERVISOR_TIMEOUT", "30")),
    # 监管者异常/超时时是否放行（判完成）。默认 true：坏掉的裁判不该把任务卡死。
    "fail_open": os.getenv("SUPERVISOR_FAIL_OPEN", "true").lower() == "true",
    # 低于该轮数就收尾时**不惊动监管者**（简单任务没必要多花一次调用）
    "min_turns": int(os.getenv("SUPERVISOR_MIN_TURNS", "1")),
    # 目标短于这么多字视为闲聊问答，不审（真任务一律复核）
    "min_goal_chars": int(os.getenv("SUPERVISOR_MIN_GOAL_CHARS", "12")),
}

# ============================================================
# 本地 OCR 配置（截图取字，不依赖视觉大模型）
# ============================================================
# 用户痛点："agent 缺少截图识别文字的能力，总是依赖视觉模型，视觉模型无响应就废了。"
# 视觉模型能理解版面与语义，但有超时/配额/"该模型不支持图片"的坑；而这些时候
# **文字本身是能拿到的**——系统自带 OCR 就能读。所以文字提取优先走本地 OCR，
# 视觉模型不可用时也自动降级到它（见 models/ocr.py）。
OCR_CONFIG = {
    "enabled": os.getenv("OCR_ENABLED", "true").lower() == "true",
    # 指定后端（windows / rapidocr / tesseract）；留空 = 按优先级自动挑
    "backend": os.getenv("OCR_BACKEND", ""),
    # Windows OCR 的识别语言（逗号分隔，按顺序尝试）；留空用系统当前语言
    "languages": os.getenv("OCR_LANGUAGES", "zh-Hans-CN,en-US"),
    "timeout": float(os.getenv("OCR_TIMEOUT", "60")),
    # 识别前放大倍数：实测 2 倍对小字号截图明显更准（20px 图 83%→93%，
    # 34px 图 87%→89%），3 倍不再提升还会切碎英文单词。1 = 不放大。
    "scale": int(os.getenv("OCR_SCALE", "2")),
    # 视觉模型失败/不可用时自动降级到本地 OCR（"就废了"的根治）
    "auto_fallback": os.getenv("OCR_AUTO_FALLBACK", "true").lower() == "true",
    # 用户明确要"提取文字/识字"时直接用 OCR（快、离线、不花配额），不必先问视觉模型
    "prefer_for_text": os.getenv("OCR_PREFER_FOR_TEXT", "true").lower() == "true",
}

# ============================================================
# Rollout 事件追踪配置（借鉴同类实现的 rollout-trace）
# ============================================================
ROLLOUT_CONFIG = {
    "enabled": (not MINIMAL_MODE) and os.getenv("ROLLOUT_ENABLED", "true").lower() == "true",
    "dir": resolve_under_root(os.getenv("ROLLOUT_DIR", "./rollouts")),
    "max_files": int(os.getenv("ROLLOUT_MAX_FILES", "20")),     # 保留最近 N 个追踪文件
    # 旧式固定阈值已废弃（默认 0 = 不触发）：executor 传入窗口比例制阈值；
    # 仍想手动覆盖可显式设 ROLLOUT_COMPACT_TOKENS
    "compact_tokens": int(os.getenv("ROLLOUT_COMPACT_TOKENS", "0")),
    "keep_recent_messages": int(os.getenv("ROLLOUT_KEEP_RECENT", "8")),
    # 落盘文本上限。追踪文件是事后诊断的唯一依据，截得太狠等于没有记录：
    # 实测 agent 的最终报告被截到 300 字、断在半句，复盘时只能去翻会话文件。
    # 0 = 不截断（不建议：单次 run 的 JSONL 会长得很快）。
    "text_limit": int(os.getenv("ROLLOUT_TEXT_LIMIT", "4000")),      # 模型输出/最终回答
    "result_limit": int(os.getenv("ROLLOUT_RESULT_LIMIT", "2000")),  # 工具返回
}

# ============================================================
# 云经验库配置（GitHub 经验共享：私有学习库 + 公共分享库）
# - private_repo：agent 复盘条目的读写仓（私有，clone+push）
# - public_repo： 只读学习源（可选，他人分享的经验库；分享条目经 PR 合并）
# - cache_dir：本地克隆缓存（gitignored memory/ 下）
# - 学习预算：单次注入条目数与字符上限，防提示注入与上下文爆炸
# ============================================================
EXPERIENCE_CONFIG = {
    "enabled": os.getenv("EXPERIENCE_ENABLED", "true").lower() == "true",
    "private_repo": os.getenv("EXPERIENCE_PRIVATE_REPO", ""),
    "public_repo": os.getenv("EXPERIENCE_PUBLIC_REPO", ""),
    "branch": os.getenv("EXPERIENCE_BRANCH", "main"),
    "cache_dir": resolve_under_root(
        os.getenv("EXPERIENCE_CACHE_DIR", "./memory/experience_lib")),
    "learn_max_entries": int(os.getenv("EXPERIENCE_LEARN_MAX", "3")),
    "learn_max_chars": int(os.getenv("EXPERIENCE_LEARN_CHARS", "2500")),
    "entry_max_chars": int(os.getenv("EXPERIENCE_ENTRY_MAX", "8000")),
    "pull_ttl_sec": int(os.getenv("EXPERIENCE_PULL_TTL", "600")),
}

# ============================================================
# 会话持久化配置（借鉴同类实现的 thread/session）
# ============================================================
SESSION_CONFIG = {
    "enabled": os.getenv("SESSION_ENABLED", "true").lower() == "true",
    "dir": resolve_under_root(os.getenv("SESSION_DIR", "./memory/sessions")),
    "max_sessions": int(os.getenv("SESSION_MAX", "50")),
    # 单个对话文件超过该体积就提示压缩（会话每轮整份重写，且是唯一副本）
    "warn_size_mb": float(os.getenv("SESSION_WARN_SIZE_MB", "20")),
    # 继续任务时注入上下文的"之前对话"条数（含当前句；注入 recent[:-1]）
    "context_messages": int(os.getenv("SESSION_CONTEXT_MESSAGES", "12")),
    # 上轮未完成（上限/停止）时自动放宽的回忆预算
    "resume_context_messages": int(os.getenv("SESSION_RESUME_CONTEXT_MESSAGES", "30")),
    "resume_context_chars": int(os.getenv("SESSION_RESUME_CONTEXT_CHARS", "600")),
    # 每条历史消息注入的最大字符数（防超长回答撑爆上下文）
    "context_message_chars": int(os.getenv("SESSION_CONTEXT_CHARS", "400")),
}

# ============================================================
# Git 快照配置（Agent 自我修改代码前的安全网）
# ============================================================
SNAPSHOT_CONFIG = {
    "enabled": (not MINIMAL_MODE) and os.getenv("SNAPSHOT_ENABLED", "true").lower() == "true",
    # 工作目录（git init / 快照的目标；默认项目根目录）
    "work_dir": os.getenv("SNAPSHOT_WORK_DIR", os.path.dirname(os.path.abspath(__file__))),
}

# ============================================================
# 逐操作检查点（每次 edit/write 前 git 快照，可逐操作回滚）
# ============================================================
CHECKPOINT_CONFIG = {
    "per_tool": (not MINIMAL_MODE) and os.getenv("CHECKPOINT_PER_TOOL", "true").lower() == "true",
}

# ============================================================
# Repo Map（代码任务时注入仓库结构摘要）
# ============================================================
REPOMAP_CONFIG = {
    "enabled": (not MINIMAL_MODE) and os.getenv("REPOMAP_ENABLED", "true").lower() == "true",
    "max_chars": int(os.getenv("REPOMAP_MAX_CHARS", "3000")),
}

# ============================================================
# OS 级沙箱（Windows AppContainer，BEST_PRACTICES「下一步优先级」）
# ============================================================
def _normalize_sandbox_mode(raw) -> str:
    """把 SANDBOX_EXECUTION 归一化到 off / appcontainer。

    只做 `.lower()` 不够：下游判的是 `sandbox_mode() != "off"`，所以 `False` / `0` /
    `OFF` 这些"显然是想关"的写法都会被判成"开着" —— 在非 Windows 上表现为每条
    终端命令都返回"沙箱模式 OFF 在当前平台不可用"，终端工具整体瘫痪
    （2026-09-22 审计）。未知取值原样返回，交给沙箱层按 fail-closed 处理。
    """
    value = str(raw or "").strip().lower()
    if value in ("", "off", "false", "0", "no", "none", "disabled"):
        return "off"
    if value in ("appcontainer", "on", "true", "1", "yes", "enabled"):
        return "appcontainer"
    return value


SANDBOX_EXEC_CONFIG = {
    # 沙箱执行模式：off（默认，行为不变）/ appcontainer
    # appcontainer = 终端前台命令在 Windows AppContainer 内执行：
    # 默认不可访问用户文件/注册表/网络，仅可写工作区（icacls 授权）。
    # fail-closed：容器创建/启动失败时返回错误，不静默回退明文执行。
    # 归一化到规范词表（见 _normalize_sandbox_mode）：
    "mode": _normalize_sandbox_mode(os.getenv("SANDBOX_EXECUTION", "off")),
    # 沙箱内命令硬超时秒数
    "timeout": float(os.getenv("SANDBOX_EXEC_TIMEOUT", "120")),
    # AppContainer 档案名（确定性 GUID 由此派生，跨会话复用授权）
    "profile": os.getenv("SANDBOX_EXEC_PROFILE", "my_agent.sandbox"),
    # 给 Python/Node/Git 解释器授予容器只读执行权限（icacls best-effort）：
    # 不授权则沙箱内只能跑系统目录自带命令，项目工具链全部「拒绝访问」。
    "grant_tools": os.getenv("SANDBOX_GRANT_TOOLS", "true").lower() == "true",
    # 额外授权目录清单（; 分隔，icacls 只读 RX best-effort）：工作区之外的
    # 只读资源目录（如共享库、数据集）。工作区本身始终可写，不在此列。
    "grant_dirs": [d.strip() for d in os.getenv("SANDBOX_GRANT_DIRS", "").split(";") if d.strip()],
    # 容器网络放行（默认 false 保持全禁）：true = 注入 internetClient /
    # internetClientServer / privateNetworkClientServer capability。
    "allow_network": os.getenv("SANDBOX_ALLOW_NETWORK", "false").lower() == "true",
}

# ============================================================
# 工具输出截断配置（借鉴同类实现的工具输出 token 上限）
# ============================================================
TOOL_CONFIG = {
    "output_max_chars": int(os.getenv("TOOL_OUTPUT_MAX_CHARS", "8000")),
    "max_step_ops": int(os.getenv("MAX_STEP_OPS", "12")),   # 单个计划步骤内最多工具操作数
    # 单循环整次任务的轮数。注意语义已升级（见 agent/loop_budget.py）：
    # 它现在是**安全网硬上限**，不再是"跑到这个数就断"的工作限额——起步轮数由
    # loop_base_turns 决定，每出现一轮有进展就按 loop_extend_per_progress 续期，
    # 连续 loop_stall_limit 轮无进展则提前停（空转才是真正该拦的）。
    # 历史语义即"最大轮数"，所以缺省值仍取它，保证没有配置新旋钮的机器行为不劣化。
    "max_loop_ops": int(os.getenv("MAX_LOOP_OPS", "80")),
    "loop_base_turns": int(os.getenv("LOOP_BASE_TURNS", "30")),          # 起步轮数
    "loop_extend_per_progress": int(os.getenv("LOOP_EXTEND_PER_PROGRESS", "10")),
    "loop_stall_limit": int(os.getenv("LOOP_STALL_LIMIT", "5")),         # 连续无进展即停
    # 绝对值安全网：0 = 不设上限（此时只由进展/停滞与用户的停止按钮决定轮数）。
    # 留空则回退到 max_loop_ops（即升级前的行为）。
    "loop_hard_cap": (int(os.getenv("LOOP_HARD_CAP"))
                      if os.getenv("LOOP_HARD_CAP", "").strip() else None),
    # 完成度闸门（"让 agent 做完任务再结束"）：模型给出最终答案时，若它自己的任务
    # 清单里还有本次工作产生的未完成项，就把它推回去继续。有界，不会与模型僵持。
    "loop_completion_gate": os.getenv("LOOP_COMPLETION_GATE", "true").lower() == "true",
    "loop_completion_nudges": int(os.getenv("LOOP_COMPLETION_NUDGES", "2")),
    # edit 验证式应用（apply_patch preflight 思路）：修改 .py 后自动跑测试，
    # 失败自动回滚（.bak 恢复）并把测试尾部回喂模型。默认关闭，EDIT_PREFLIGHT=true 开启。
    "edit_preflight": os.getenv("EDIT_PREFLIGHT", "false").lower() == "true",
    "edit_preflight_timeout": int(os.getenv("EDIT_PREFLIGHT_TIMEOUT", "180")),
    "edit_preflight_tail": int(os.getenv("EDIT_PREFLIGHT_TAIL", "80")),   # 回喂的失败日志行数
    # preflight 测试范围：related=只跑与被改模块相关的测试（默认；全套 600+
    # 个测试会超过超时，导致文件已改却报失败）；full=始终跑全套
    "edit_preflight_scope": os.getenv("EDIT_PREFLIGHT_SCOPE", "related"),
    # 工具执行硬超时（秒）：任何工具调用超过该时间即视为挂起，返回超时错误并
    # 重置该工具实例（丢弃卡死的 playwright/子进程引用），防止整个 Agent 冻结。
    # 默认 300s；browser 因 CDP 挂起高发单独设短值。
    "tool_timeout": float(os.getenv("TOOL_TIMEOUT", "300")),
    "browser_timeout": float(os.getenv("BROWSER_TIMEOUT", "60")),
    # 单个模型轮次内并行执行的工具调用上限（超出排队）。模型一轮可发 N 个
    # 并行安全调用，每个都可能再拉子进程/HTTP 连接——无上限会把线程、句柄
    # 和上游限流同时打满（免费档尤其敏感）。
    "max_parallel_tools": int(os.getenv("MAX_PARALLEL_TOOLS", "4")),
    # python 代码工具执行超时（秒）：exec 无法中断，超时后工具立即返回明确错误
    # （此前该工具体没有任何超时，sleep 轮询/长循环会挂到 tool_timeout 才返回）
    "python_timeout": float(os.getenv("PYTHON_TOOL_TIMEOUT", "30")),
    # 本地工具目录（agent 自己新增的工具放这里：只在本机生效，已在 .gitignore
    # 中忽略，永不推送；仓库只保留产品自带的核心工具与配置）
    "local_dir": os.getenv("LOCAL_TOOLS_DIR", "./tools/local"),
    "local_enabled": os.getenv("LOCAL_TOOLS_ENABLED", "true").lower() == "true",
    # 终端前台命令超时（秒）：实战发现 60s 会掐断负载下的全量测试，
    # 默认 120s。后台命令（background=true）不受此限。
    "terminal_fg_timeout": float(os.getenv("TERMINAL_FOREGROUND_TIMEOUT", "120")),
    # 桌面操控（computer 工具）：无障碍树规模与截图保存目录
    "computer_a11y_max_elements": int(os.getenv("COMPUTER_A11Y_MAX_ELEMENTS", "120")),
    "computer_a11y_max_depth": int(os.getenv("COMPUTER_A11Y_MAX_DEPTH", "8")),
    "computer_screenshot_dir": (resolve_under_root(os.getenv("COMPUTER_SCREENSHOT_DIR", ""))
                                if os.getenv("COMPUTER_SCREENSHOT_DIR", "").strip() else ""),   # 空=generated_images/computer
}

# ============================================================
# TUI 状态栏（常驻状态行：token 计数/沙箱/审批策略）
# ============================================================
TUI_CONFIG = {
    # verbose 模式下每轮模型调用前刷新状态栏（↑输入 ↓输出 token、缓存命中、
    # 沙箱等级、审批策略）。false 关闭，只保留运行结束的统计行。
    "status_bar": os.getenv("TUI_STATUS_BAR", "true").lower() == "true",
}

# ============================================================
# Team 多Agent协作配置（Manager-Worker 模式）
# ============================================================
TEAM_CONFIG = {
    # 并行执行独立子任务（multi_agents 式）。Manager 拆解时把不依赖其他
    # 子任务结果的子任务标记为 independent；并行前有安全门（参与角色的
    # 工具集必须全部并行安全，否则自动回退串行）。默认关闭保持旧行为。
    "parallel": os.getenv("TEAM_PARALLEL", "false").lower() == "true",
    # 单个并行 Worker 的硬超时（秒）：超时标记该子任务失败，不冻结整个团队。
    "worker_timeout": float(os.getenv("TEAM_WORKER_TIMEOUT", "900")),
}

# ============================================================
# 测试命令配置（跨平台可配：循环提示词中引用的测试命令）
# ============================================================
TEST_CONFIG = {
    "command": os.getenv(
        "TEST_COMMAND",
        ".venv\\Scripts\\python -m pytest tests -q",   # Windows 默认；Linux/Mac 在 .env 覆盖
    ),
}

# ============================================================
# Hooks 配置（工具调用前后钩子，fail-open）
# ============================================================
# enabled: 是否启用钩子（默认关闭，保持旧行为不变）
# hooks_file: 钩子模块文件路径（Python 文件，可定义可选的
#   on_pre_tool_use(tool_name, arguments) 与 on_post_tool_use(tool_name, result)；
#   任何回调异常只记录警告，绝不阻断主流程）
HOOKS_CONFIG = {
    "enabled": os.getenv("HOOKS_ENABLED", "false").lower() == "true",
    "hooks_file": os.getenv("HOOKS_FILE", "./hooks.py"),
}

# ============================================================
# Skills 技能包配置（技能包式能力扩展）
# ============================================================
# 技能 = 一个包含 SKILL.md 的子目录（项目级 ./skills 与用户级 ~/.my_agent/skills）。
# SKILL.md 头部可选 frontmatter（--- 围栏内 name / description / triggers，
# triggers 为逗号分隔关键词）；缺失或解析失败时降级用目录名。
# 命中时把技能正文注入系统提示（放在 AGENTS.md 指令之后）。
SKILLS_CONFIG = {
    "enabled": os.getenv("SKILLS_ENABLED", "true").lower() == "true",
    "project_dir": os.getenv("SKILLS_DIR", "./skills"),
    "user_dir": os.getenv(
        "SKILLS_USER_DIR", os.path.expanduser("~/.my_agent/skills")
    ),
    "max_chars": int(os.getenv("SKILLS_MAX_CHARS", "6000")),
}

# ============================================================
# 文章工坊（多模型互审写作流水线）
# ============================================================
# 为什么必须**跨厂商**互审：同一个模型审自己写的稿子，盲点、偏好、知识边界完全
# 重合，"互审"会退化成自我复述；同厂不同名的模型也基本重合。所以默认让另一家的
# 模型当审阅/校对，写稿与修订留在主模型。
ARTICLE_ENDPOINTS = {
    # 端点名 → OpenAI 兼容配置。可写 "main" / "agnes"，也可 "agnes:某个模型名"。
    "main": {
        "api_key": LLM_CONFIG.get("api_key") or "",
        "base_url": LLM_CONFIG.get("base_url") or "",
        "model": LLM_CONFIG.get("default_model") or "",
    },
    "agnes": {
        "api_key": GUARDIAN_CONFIG.get("api_key") or VISION_CONFIG.get("api_key") or "",
        "base_url": GUARDIAN_CONFIG.get("base_url") or VISION_CONFIG.get("base_url") or "",
        "model": GUARDIAN_CONFIG.get("model") or VISION_CONFIG.get("vision_model") or "",
    },
}

ARTICLE_CONFIG = {
    "save_dir": resolve_under_root(os.getenv("ARTICLE_DIR", "./output/articles")),
    # 审阅→修订 最多来回几轮（到顶就带着剩余意见定稿，不会无限刷 token）
    "max_revise_rounds": int(os.getenv("ARTICLE_MAX_REVISE_ROUNDS", "2")),
    # 只有这些严重度才值得回炉重写；轻微问题攒到校对阶段一次性落定
    "revise_severities": tuple(
        s.strip() for s in os.getenv("ARTICLE_REVISE_SEVERITIES", "严重,中等").split(",") if s.strip()
    ),
    # 先出大纲再写初稿（短文/已有大纲时可关掉）
    "outline": os.getenv("ARTICLE_OUTLINE", "true").lower() == "true",
    # 目标篇幅（如 "1500字"），留空由模型按主题自行判断
    "target_length": os.getenv("ARTICLE_TARGET_LENGTH", ""),
    # 事实核查：只查审阅方点名的可疑说法（全文逐条联网太贵且慢）
    "factcheck": os.getenv("ARTICLE_FACTCHECK", "true").lower() == "true",
    "factcheck_max_claims": int(os.getenv("ARTICLE_FACTCHECK_MAX_CLAIMS", "5")),
    "lookup_limit": int(os.getenv("ARTICLE_LOOKUP_LIMIT", "3")),
    # 检索通道（按顺序凑够 lookup_limit 条即停）：zhihu=知乎开放平台全网搜索
    # （结构化、稳，需 ZHIHU_ACCESS_SECRET）；browser=抓搜索引擎结果页（无密钥依赖，
    # 但经常抓不到东西）。两条都拿不到才记"未查到"。
    "lookup_sources": os.getenv("ARTICLE_LOOKUP_SOURCES", "zhihu,browser"),
    # 限流兜底：长文一次跑 8+ 个大请求，很容易撞上游 TPM/RPM（实测商汤在 revise
    # 阶段 429 过）。同端点退避重试 stage_retries 次 → 仍失败就换另一家端点续跑
    # （fallback_endpoint=auto 表示自动选"另一家"；留空/off 关闭换端点）。
    "stage_retries": int(os.getenv("ARTICLE_STAGE_RETRIES", "1")),
    "retry_wait": float(os.getenv("ARTICLE_RETRY_WAIT", "20")),
    "fallback_endpoint": os.getenv("ARTICLE_FALLBACK_ENDPOINT", "auto"),
    # 各阶段用哪个端点（改这里就能换"谁来审谁"）
    "stages": {
        "outline": os.getenv("ARTICLE_MODEL_OUTLINE", "main"),
        "draft": os.getenv("ARTICLE_MODEL_DRAFT", "main"),
        "review": os.getenv("ARTICLE_MODEL_REVIEW", "agnes"),
        "claims": os.getenv("ARTICLE_MODEL_CLAIMS", "agnes"),
        "verify": os.getenv("ARTICLE_MODEL_VERIFY", "main"),
        "revise": os.getenv("ARTICLE_MODEL_REVISE", "main"),
        "proofread": os.getenv("ARTICLE_MODEL_PROOFREAD", "agnes"),
        "finalize": os.getenv("ARTICLE_MODEL_FINALIZE", "main"),
    },
}
