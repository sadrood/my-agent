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

# 加载 .env 文件中的环境变量
load_dotenv()


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
    "profile_dir": os.getenv("BROWSER_PROFILE_DIR", "./memory/browser_profile"),
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
VISION_CONFIG = {
    "screenshot_path": os.getenv("VISION_SCREENSHOT_PATH", "./screenshots"),
    "vision_model": os.getenv("VISION_MODEL", ""),  # 留空则自动选择
    "enabled": os.getenv("VISION_ENABLED", "true").lower() == "true",
    "api_key": os.getenv("VISION_API_KEY", "") or LLM_CONFIG["api_key"],
    "base_url": os.getenv("VISION_BASE_URL", "") or LLM_CONFIG["base_url"],
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
    "save_dir": os.getenv("IMAGE_GEN_SAVE_DIR", "./generated_images"),
    "timeout": float(os.getenv("IMAGE_GEN_TIMEOUT", "120")),
    # 官方公测期间免费开放去水印（watermark=false）；默认关闭水印
    "watermark": os.getenv("IMAGE_GEN_WATERMARK", "false").lower() == "true",
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
    "enabled": os.getenv("MCP_ENABLED", "false").lower() == "true",
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
LEARN_CONFIG = {
    "enable": os.getenv("LEARN_ENABLED", "true").lower() == "true",
    "max_experiences_recall": int(os.getenv("LEARN_MAX_RECALL", "3")),
    "max_experiences_store": int(os.getenv("LEARN_MAX_STORE", "100")),
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
    "approval_policy": os.getenv("APPROVAL_POLICY", "on-failure"),
    "sandbox_mode": os.getenv("SANDBOX_MODE", "workspace-write"),
    "interactive": os.getenv("APPROVAL_INTERACTIVE", "true").lower() == "true",
    "default_answer_when_not_interactive": os.getenv("APPROVAL_NONINTERACTIVE_ANSWER", "deny"),
    "workspace_dir": os.getenv("APPROVAL_WORKSPACE_DIR", os.getcwd()),
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
# Rollout 事件追踪配置（借鉴同类实现的 rollout-trace）
# ============================================================
ROLLOUT_CONFIG = {
    "enabled": (not MINIMAL_MODE) and os.getenv("ROLLOUT_ENABLED", "true").lower() == "true",
    "dir": os.getenv("ROLLOUT_DIR", "./rollouts"),
    "max_files": int(os.getenv("ROLLOUT_MAX_FILES", "20")),     # 保留最近 N 个追踪文件
    # 旧式固定阈值已废弃（默认 0 = 不触发）：executor 传入窗口比例制阈值；
    # 仍想手动覆盖可显式设 ROLLOUT_COMPACT_TOKENS
    "compact_tokens": int(os.getenv("ROLLOUT_COMPACT_TOKENS", "0")),
    "keep_recent_messages": int(os.getenv("ROLLOUT_KEEP_RECENT", "8")),
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
    "cache_dir": os.getenv("EXPERIENCE_CACHE_DIR", "./memory/experience_lib"),
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
    "dir": os.getenv("SESSION_DIR", "./memory/sessions"),
    "max_sessions": int(os.getenv("SESSION_MAX", "50")),
    # 继续任务时注入上下文的"之前对话"条数（含当前句；注入 recent[:-1]）
    "context_messages": int(os.getenv("SESSION_CONTEXT_MESSAGES", "12")),
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
SANDBOX_EXEC_CONFIG = {
    # 沙箱执行模式：off（默认，行为不变）/ appcontainer
    # appcontainer = 终端前台命令在 Windows AppContainer 内执行：
    # 默认不可访问用户文件/注册表/网络，仅可写工作区（icacls 授权）。
    # fail-closed：容器创建/启动失败时返回错误，不静默回退明文执行。
    "mode": os.getenv("SANDBOX_EXECUTION", "off"),
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
    "max_loop_ops": int(os.getenv("MAX_LOOP_OPS", "80")),   # 单循环整次任务最大操作轮数
    # edit 验证式应用（apply_patch preflight 思路）：修改 .py 后自动跑测试，
    # 失败自动回滚（.bak 恢复）并把测试尾部回喂模型。默认关闭，EDIT_PREFLIGHT=true 开启。
    "edit_preflight": os.getenv("EDIT_PREFLIGHT", "false").lower() == "true",
    "edit_preflight_timeout": int(os.getenv("EDIT_PREFLIGHT_TIMEOUT", "180")),
    "edit_preflight_tail": int(os.getenv("EDIT_PREFLIGHT_TAIL", "40")),   # 回喂的失败日志行数
    # 工具执行硬超时（秒）：任何工具调用超过该时间即视为挂起，返回超时错误并
    # 重置该工具实例（丢弃卡死的 playwright/子进程引用），防止整个 Agent 冻结。
    # 默认 300s；browser 因 CDP 挂起高发单独设短值。
    "tool_timeout": float(os.getenv("TOOL_TIMEOUT", "300")),
    "browser_timeout": float(os.getenv("BROWSER_TIMEOUT", "60")),
    # 终端前台命令超时（秒）：实战发现 60s 会掐断负载下的全量测试，
    # 默认 120s。后台命令（background=true）不受此限。
    "terminal_fg_timeout": float(os.getenv("TERMINAL_FOREGROUND_TIMEOUT", "120")),
    # 桌面操控（computer 工具）：无障碍树规模与截图保存目录
    "computer_a11y_max_elements": int(os.getenv("COMPUTER_A11Y_MAX_ELEMENTS", "120")),
    "computer_a11y_max_depth": int(os.getenv("COMPUTER_A11Y_MAX_DEPTH", "8")),
    "computer_screenshot_dir": os.getenv("COMPUTER_SCREENSHOT_DIR", ""),   # 空=generated_images/computer
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
