"""
Agent 核心模块（Computer Use + MCP 增强版）。
完整的主循环：Think → Act → Observe → Compare(帧对比) → Anomaly(异常检测) → RePlan。
支持：精确鼠标键盘操作、MCP 外部工具集成、视觉帧对比、异常检测。

v3.1 起默认使用单循环执行模式（主循环式：一次对话完成目标），
经典计划模式通过 exec_mode="plan" / --plan 使用。
"""
import sys
import os

from agent.state import AgentState
from agent.planner import Planner
from agent.executor import Executor
from agent.memory import Memory
from models.llm import LLM
from models.prompts import SUMMARY_SYSTEM_PROMPT, SUMMARY_USER_PROMPT_TEMPLATE
from tools.tool_manager import ToolManager
from tools.intent_detector import get_intent_detector
from config import (
    VISION_CONFIG, MCP_CONFIG, APPROVAL_CONFIG,
    GUARDIAN_CONFIG, ROLLOUT_CONFIG, INSTRUCTIONS_CONFIG,
    SNAPSHOT_CONFIG, TEST_CONFIG, CHECKPOINT_CONFIG, REPOMAP_CONFIG,
    SESSION_CONFIG, SKILLS_CONFIG,
)
from agent.ui_theme import (
    get_console, print_header, print_stage, print_step_header, print_step_result,
    print_final_result, print_plan, print_warning, print_error, print_evolution,
    print_info, PERSONALITY,
)


def _resolve_compact_threshold(llm=None) -> int:
    """压缩触发阈值解析（供实例方法/无 llm 场景共用）。

    显式 COMPACT_TOKEN_THRESHOLD >0 优先；否则委托 LLM 按网关报告窗口 ×
    COMPACT_WINDOW_RATIO；llm 缺失/窗口不可知时兜底 240_000。
    概览刻度与真实压缩点共用此值——显示与实际不再脱节。
    """
    try:
        from config import COMPACT_CONFIG
        explicit = int(COMPACT_CONFIG.get("token_threshold") or 0)
        if explicit > 0:
            return explicit
        if llm is not None and hasattr(llm, "compact_threshold_tokens"):
            return llm.compact_threshold_tokens()
    except Exception:
        pass
    return 240_000


class AgentConfig:
    """Agent 运行时配置。"""

    def __init__(
        self,
        max_steps: int = 20,
        max_replans: int = 3,
        max_step_retries: int = 15,
        verbose: bool = True,
        enable_vision: bool = True,
        enable_frame_compare: bool = True,
        enable_anomaly_detect: bool = True,
        mcp_servers: list = None,
        # ---- v2：审批 / 沙箱 / 安全 ----
        approval_policy: str = None,
        sandbox_mode: str = None,
        approval_interactive: bool = None,
        approver=None,
        agent_name: str = None,       # 用户自定义名字（None=默认人格名「小悟」）
        guardian_enabled: bool = None,
        # ---- v2：指令 / 追踪 / 会话 ----
        instructions_enabled: bool = None,
        rollout_enabled: bool = None,
        max_step_ops: int = None,
        session_name: str = "",
        # ---- v3.1：执行模式 ----
        exec_mode: str = "loop",   # loop = 单循环（默认）；plan = 经典计划模式
        max_ops: int = None,       # loop 模式整次任务最大操作轮数
        stream_enabled: bool = True,  # 流式输出（逐字渲染）
        # ---- v3.4：自我升级安全网 ----
        snapshot_enabled: bool = None,  # 运行前 git 快照（默认取 SNAPSHOT_CONFIG）
        checkpoint_per_tool: bool = None,  # 逐操作式检查点（edit/write 前快照）
        repomap_enabled: bool = None,      # 仓库结构摘要注入（代码任务时）
        # ---- v3.5：临时模型覆盖（不写入 .env）----
        llm_model: str = None,             # 临时切换主模型
        llm_base_url: str = None,          # 临时切换 API 地址
        llm_api_key: str = None,           # 临时切换 API key
        # ---- v3.6：模型参数覆盖（桌面端滑块 / API 下发）----
        temperature: float = None,         # 温度（None=用 .env 默认）
        top_p: float = None,               # top_p（None=用 .env 默认）
        max_tokens: int = None,            # 最大输出 token（None=用 .env 默认）
        # ---- v3.7：Skills 技能包（技能包式能力扩展）----
        skills_enabled: bool = None,       # 技能包开关（None=取 SKILLS_CONFIG）
        skills_project_dir: str = None,    # 覆盖项目级技能目录
        skills_user_dir: str = None,       # 覆盖用户级技能目录
        skills_max_chars: int = None,      # 覆盖注入文本上限
    ):
        self.max_steps = max_steps
        self.max_replans = max_replans
        self.max_step_retries = max_step_retries
        self.verbose = verbose
        self.enable_vision = enable_vision
        self.enable_frame_compare = enable_frame_compare
        self.enable_anomaly_detect = enable_anomaly_detect
        self.mcp_servers = mcp_servers or MCP_CONFIG.get("servers", [])

        # 审批与沙箱
        self.approval_policy = approval_policy or APPROVAL_CONFIG.get("approval_policy", "on-failure")
        self.sandbox_mode = sandbox_mode or APPROVAL_CONFIG.get("sandbox_mode", "workspace-write")
        self.approval_interactive = (
            approval_interactive
            if approval_interactive is not None
            else APPROVAL_CONFIG.get("interactive", True)
        )
        self.approver = approver

        # 用户自定义名字（桌面端首次初始化填写；None=默认人格名「小悟」）
        self.agent_name = agent_name

        # Guardian 安全审校
        self.guardian_enabled = (
            guardian_enabled
            if guardian_enabled is not None
            else GUARDIAN_CONFIG.get("enabled", True)
        )

        # AGENTS.md / Rollout / 步骤内操作上限 / 会话
        self.instructions_enabled = (
            instructions_enabled
            if instructions_enabled is not None
            else INSTRUCTIONS_CONFIG.get("enabled", True)
        )
        self.rollout_enabled = (
            rollout_enabled
            if rollout_enabled is not None
            else ROLLOUT_CONFIG.get("enabled", True)
        )
        self.max_step_ops = max_step_ops
        self.session_name = session_name or ""

        # v3.1：执行模式（loop = 单循环，plan = 经典计划模式）
        if exec_mode not in ("loop", "plan"):
            raise ValueError(f"未知 exec_mode: {exec_mode}")
        self.exec_mode = exec_mode
        self.max_ops = max_ops
        self.stream_enabled = stream_enabled

        # 自我升级安全网：git 快照
        self.snapshot_enabled = (
            snapshot_enabled
            if snapshot_enabled is not None
            else SNAPSHOT_CONFIG.get("enabled", True)
        )
        # 逐操作式检查点
        self.checkpoint_per_tool = (
            checkpoint_per_tool
            if checkpoint_per_tool is not None
            else CHECKPOINT_CONFIG.get("per_tool", True)
        )
        # Repo Map（仓库结构摘要）
        self.repomap_enabled = (
            repomap_enabled
            if repomap_enabled is not None
            else REPOMAP_CONFIG.get("enabled", True)
        )
        # 临时模型覆盖（只对本次会话生效，不写入 .env）
        self.llm_model = llm_model
        self.llm_base_url = llm_base_url
        self.llm_api_key = llm_api_key
        # 模型参数覆盖（None 表示沿用 .env / LLM 默认）
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens

        # Skills 技能包（技能包式能力扩展）
        self.skills_enabled = (
            skills_enabled
            if skills_enabled is not None
            else SKILLS_CONFIG.get("enabled", True)
        )
        self.skills_project_dir = (
            skills_project_dir
            if skills_project_dir is not None
            else SKILLS_CONFIG.get("project_dir", "./skills")
        )
        self.skills_user_dir = (
            skills_user_dir
            if skills_user_dir is not None
            else SKILLS_CONFIG.get("user_dir", os.path.expanduser("~/.my_agent/skills"))
        )
        self.skills_max_chars = (
            skills_max_chars
            if skills_max_chars is not None
            else SKILLS_CONFIG.get("max_chars", 6000)
        )


class Agent:
    """
    AI Agent 主类（Computer Use + MCP 增强版）。

    增强的核心流程：
    1. Think    → Planner 制定计划
    2. Act      → Executor LLM 决策 → 工具调用
    3. Compare  → 操作前后截图帧对比（验证效果）
    4. Anomaly  → 异常检测（弹窗/错误/验证码）
    5. Observe  → 判断成功/失败/继续
    6. RePlan   → 失败时重规划
    """

    def __init__(
        self,
        llm: LLM = None,
        tool_manager: ToolManager = None,
        memory: Memory = None,
        config: AgentConfig = None,
    ):
        self.config = config or AgentConfig()
        self.llm = llm or self._build_llm()
        self.tool_manager = tool_manager or ToolManager()
        self.memory = memory or Memory()

        # ================================================================
        # v2：审批策略 + Guardian + 指令 + 会话
        # ================================================================
        from agent.approval import ApprovalPolicy
        from agent.instructions import get_instructions_loader
        from agent.session import SessionStore

        self.approval = ApprovalPolicy(
            mode=self.config.approval_policy,
            sandbox_mode=self.config.sandbox_mode,
            interactive=self.config.approval_interactive,
            approver=self.config.approver,
        )
        self.guardian = self._build_guardian()

        self.instructions_text = ""
        if self.config.instructions_enabled:
            try:
                self.instructions_text = get_instructions_loader().load()
            except Exception:
                self.instructions_text = ""

        self.session_store = SessionStore()
        self.rollout = None     # 每次 run() 创建

        self.planner = Planner(llm=self.llm)
        self.executor = Executor(
            tool_manager=self.tool_manager,
            llm=self.llm,
            enable_vision=self.config.enable_vision,
            approval_policy=self.approval,
            guardian=self.guardian,
            instructions_text=self.instructions_text,
            max_step_ops=self.config.max_step_ops,
            snapshot_checkpoints=self.config.checkpoint_per_tool,
            snapshot_dir=SNAPSHOT_CONFIG.get("work_dir") or os.getcwd(),
        )
        self.state = AgentState()
        self._mcp_connected = False
        self._browser_launched = False     # 本轮是否启动了浏览器
        self._browser_launch_reason = ""    # 启动浏览器的原因
        self._tools_used_this_run = set()   # 本轮用到的工具集合
        self.last_execution_summary = ""    # 上轮执行摘要（注入到下一轮上下文）
        self._turn_spinner = None           # 思考中转圈状态（流式模式）
        self._session_loaded = False        # 会话只加载一次（防指数膨胀）

        # Dashboard 集成
        try:
            from dashboard.hub import get_dashboard_hub
            self._dashboard = get_dashboard_hub()
        except Exception:
            self._dashboard = None
        # 事件面板标记：空=主对话；"side"=辅助 Agent（前端据此路由事件，不污染主会话）
        self._events_panel = ""
        # 辅助 Agent 归属的主会话 id（前端据此把 side 事件路由到对应的辅助面板）
        self._events_side_of = ""

    def _agent_name(self) -> str:
        """当前生效的 Agent 名字：用户自定义优先，缺省回人格名。"""
        name = (getattr(self.config, "agent_name", "") or "").strip()
        return name or str(PERSONALITY.get("name", "小悟"))

    def _build_llm(self):
        """按临时覆盖配置构建主 LLM（无覆盖时走 .env 配置）。"""
        if self.config.llm_model or self.config.llm_base_url or self.config.llm_api_key:
            return LLM(
                model=self.config.llm_model,
                base_url=self.config.llm_base_url,
                api_key=self.config.llm_api_key,
            )
        return LLM()

    def switch_model(self, model: str = None, base_url: str = None, api_key: str = None):
        """
        会话内临时切换主模型（/model 风格）：
        未提供的参数沿用当前值；影响本次会话，不写入 .env。

        Returns:
            切换后的配置摘要
        """
        self.config.llm_model = model or self.config.llm_model
        self.config.llm_base_url = base_url or self.config.llm_base_url
        self.config.llm_api_key = api_key or self.config.llm_api_key
        self.llm = self._build_llm()
        # 重建 LLM 后重接统计：否则 _record_usage 写进旧实例（或 None），
        # token 统计/上下文水位全部归零（概览"运行 tokens 0"的根因）
        if getattr(self, "metrics", None) is not None:
            self.llm.metrics = self.metrics
        # 同步给执行器 / 规划器 / Guardian（端点变化时重建）
        if self.executor is not None:
            self.executor.llm = self.llm
        if self.planner is not None:
            self.planner.llm = self.llm
        try:
            self.guardian = self._build_guardian()
        except Exception:
            pass
        return {
            "model": self.llm.default_model,
            "base_url": str(self.llm.client.base_url),
        }

    def _build_guardian(self):
        """
        构建 Guardian（支持独立端点：GUARDIAN_API_KEY / GUARDIAN_BASE_URL）。

        审校建议用快模型：主模型是慢速推理模型（如 stealth/ox-alpha）时，
        若 Guardian 端点与主 LLM 相同则共用实例，否则用独立端点建新客户端。
        """
        if not self.config.guardian_enabled:
            return None
        from agent.guardian import Guardian
        from config import GUARDIAN_CONFIG, LLM_CONFIG

        g_key = GUARDIAN_CONFIG.get("api_key") or LLM_CONFIG["api_key"]
        g_base = GUARDIAN_CONFIG.get("base_url") or LLM_CONFIG["base_url"]
        if g_key == LLM_CONFIG["api_key"] and g_base == LLM_CONFIG["base_url"]:
            return Guardian(llm=self.llm)
        return Guardian(llm=LLM(api_key=g_key, base_url=g_base))

    # ================================================================
    # 核心主循环
    # ================================================================

    def run(self, goal: str, keep_session: bool = False, event_sink=None, stop_event=None) -> str:
        # 按会话给会话级工具注入 key + 事件（todo 清单、terminal 持久会话）
        self._wire_session_tools()
        # 持久目标：把本次任务目标落盘（失败/重启后可续跑）
        try:
            if self.config.session_name:
                from agent.goal import GoalStore
                GoalStore().set(self.config.session_name, goal)
        except Exception:
            pass

        # 防御：清理可能存在的孤立代理字符（管道输入编码损坏场景），
        # 避免后续 JSON 序列化崩溃
        try:
            goal = goal.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        except Exception:
            pass

        # v3.3 统计：每次运行新建 RunMetrics，注入 LLM 与 Executor
        from agent.metrics import RunMetrics
        self.metrics = RunMetrics()
        if self.llm is not None:
            self.llm.metrics = self.metrics
        if self.executor is not None:
            self.executor.metrics = self.metrics

        # v3.4 安全网：运行前 git 快照（git init + 提交当前状态，形成回滚点；
        # 静默失败，绝不影响正常执行）
        if self.config.snapshot_enabled:
            try:
                from agent.snapshot import ensure_repo, snapshot
                work_dir = SNAPSHOT_CONFIG.get("work_dir") or os.getcwd()
                if ensure_repo(work_dir):
                    # 逐工具 checkpoint 开启时用选择性快照（只提交暂存区）：
                    # Agent 的每次修改已被 checkpoint 独立提交，全量 add -A
                    # 只会把用户的并行未提交工作卷进 Agent 提交。
                    snapshot(work_dir, goal,
                             full=not self.config.checkpoint_per_tool)
            except Exception:
                pass

        # v3.1：单循环模式（默认，主循环式）
        if self.config.exec_mode == "loop":
            return self._run_loop(goal, keep_session, event_sink, stop_event)

        # ================================================================
        # 以下为经典计划模式（--plan 强制使用）
        # ================================================================
        # Rich 美化启动头
        print_header(title="", goal=goal, use_rich=self.config.verbose)

        # 0. Dashboard 事件
        self._emit("run_start", {"goal": goal})

        # 0. 初始化 MCP 服务器
        self._init_mcp_servers()

        # 0. v2：创建 Rollout 事件追踪 + 加载会话
        from agent.rollout import Rollout
        self.rollout = Rollout(
            summarizer=self._summarize_for_compaction,
            config={**ROLLOUT_CONFIG, "compact_tokens": self._compact_threshold()},
        ) if self.config.rollout_enabled else None
        if self.executor is not None:
            self.executor.rollout = self.rollout
        self._load_session_if_requested()
        if self.rollout is not None:
            self.rollout.emit("run_start", {"goal": goal})
            print_info(
                f"Rollout 追踪已开启: {self.rollout.log_path}",
                style="info", use_rich=self.config.verbose,
            )

        # 1. 初始化
        self.state.goal = goal
        self.state.current_step = 0
        self.state.finished = False
        if not keep_session:
            self.memory.clear_session()
        else:
            # 保留对话历史，只清空步骤记录（每次执行是新的）
            self.memory.step_history.clear()
        self.memory.add_message("user", goal)

        # 对话上下文：让 Planner 知道之前聊了什么 + 实际做了什么
        conversation_context = ""
        if keep_session:
            ctx_count = int(SESSION_CONFIG.get("context_messages", 12))
            ctx_chars = int(SESSION_CONFIG.get("context_message_chars", 400))
            recent = self.memory.get_recent_messages(ctx_count)
            if len(recent) > 1:  # 有历史对话
                ctx_lines = ["\n## 之前的对话记录\n"]
                for m in recent[:-1]:  # 排除刚加入的当前消息
                    role_label = "用户" if m["role"] == "user" else self._agent_name()
                    ctx_lines.append(f"- {role_label}: {m['content'][:ctx_chars]}")
                # 追加上一轮的操作摘要（工具使用、浏览器等）
                if self.last_execution_summary:
                    ctx_lines.append(f"\n上一轮执行摘要: {self.last_execution_summary}")
                conversation_context = "\n".join(ctx_lines)

        # ================================================================
        # 自我进化：召回历史经验 + 失败模式 + 策略建议
        # ================================================================
        experience_context = self.memory.recall_experiences(goal, n=3, llm=self.llm)
        failure_warnings = self.memory.get_failure_warnings(goal)

        # 意图检测：识别中文网站名并翻译为 URL
        intent_detector = get_intent_detector()
        intent_result = intent_detector.detect(goal)
        if intent_result.detected_sites:
            site_names = [s['name'] for s in intent_result.detected_sites]
            site_urls = [s['url'] for s in intent_result.detected_sites]
            print_info(f"意图识别: {', '.join(f'{n}→{u}' for n, u in zip(site_names, site_urls))}", style="accent", use_rich=self.config.verbose)
            planning_goal = intent_result.enriched_goal
            self.state.goal = planning_goal
        else:
            planning_goal = goal

        # 策略建议
        task_category = self.memory._classify_task(goal)
        strategy_hints = self.memory.get_best_strategies(task_category)
        if strategy_hints:
            print_evolution("召回策略建议", use_rich=self.config.verbose)

        # 2. 制定计划（注入历史经验）
        print_stage("制定计划", use_rich=self.config.verbose)
        if self.memory.experiences:
            print_evolution(f"已召回 {len(self.memory.experiences)} 条相关经验", use_rich=self.config.verbose)
        if failure_warnings:
            print_evolution("失败模式警告已注入", use_rich=self.config.verbose)

        self.state.experience_context = experience_context
        self.state.failure_warnings = failure_warnings
        self.state.plan = self._think_and_plan(
            planning_goal, experience_context, strategy_hints, conversation_context
        )
        if not self.state.plan:
            self._cleanup()
            msg = "无法为目标生成执行计划，请尝试更具体地描述您的需求。"
            print_error(msg, use_rich=self.config.verbose)
            self.memory.add_message("assistant", msg)
            self._emit("run_end", {"status": "error", "error": msg})
            self._finalize_run("error", msg)
            return msg

        print_plan(self.state.plan, use_rich=self.config.verbose)

        # 发送计划到 Dashboard
        self._emit("plan", {"steps": self.state.plan})

        # 3. 预启动浏览器
        # 只检查用户输入 goal 是否涉及浏览器操作，不检查 LLM 生成的 plan 文本
        # （plan 文本可能包含"网页浏览"等能力描述词，会导致误判）
        self._browser_launched = False
        self._browser_launch_reason = ""
        self._tools_used_this_run = set()
        is_browser = self.executor._is_browser_task(goal, "")
        if is_browser:
            self._browser_launched = True
            self._browser_launch_reason = f"目标关键词匹配: {goal[:50]}"
            self._log("\n[预启动] 检测到浏览器任务，自动启动浏览器...")
            self._auto_launch_browser()

        # 4. 执行主循环
        print_stage("执行计划", use_rich=self.config.verbose)
        replan_count = 0
        step_retry_count = 0
        page_screenshot_b64 = ""
        vision_feedback = ""
        tool_usage = {}          # 工具使用统计（用于经验记录）
        errors_collected = []    # 收集所有错误（用于失败模式学习）

        while self.state.current_step < len(self.state.plan):
            step_idx = self.state.current_step
            current_step = self.state.plan[step_idx]

            self._log(f"\n{'─'*40}")
            print_step_header(step_idx + 1, len(self.state.plan), current_step, use_rich=self.config.verbose)

            self._emit("step_start", {
                "step": step_idx + 1,
                "total": len(self.state.plan),
                "description": current_step,
            })

            step_context_parts = []
            step_success = True

            while step_retry_count < self.config.max_step_retries:
                context_summary = "\n".join(step_context_parts[-5:]) if step_context_parts else ""

                result = self._act(
                    current_step,
                    step_context=context_summary,
                    vision_feedback=vision_feedback,
                )

                # 追踪工具使用（v2：支持多工具步骤 tools_used）
                tool_name = result.get("tool", "")
                if tool_name:
                    tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1
                    self._tools_used_this_run.add(tool_name)
                for tn in result.get("tools_used", []) or []:
                    if tn and tn != tool_name:
                        tool_usage[tn] = tool_usage.get(tn, 0) + 1
                        self._tools_used_this_run.add(tn)

                # Dashboard 事件
                self._emit("tool_call", {
                    "tool": result.get("tool", ""),
                    "input": result.get("tool_input", ""),
                    "action": result.get("action", ""),
                })
                self._emit("tool_result", {
                    "tool": result.get("tool", ""),
                    "success": result.get("success", False),
                    "output": result.get("output", "")[:500],
                    "error": result.get("error", ""),
                })

                self._observe(result, step_idx)

                # 帧对比：对于浏览器工具操作，对比前后变化
                if (self.config.enable_frame_compare
                        and (result.get("tool") == "browser" or "browser" in (result.get("tools_used") or []))
                        and result.get("success")
                        and result.get("output")):
                    self._frame_compare_feedback(result, current_step)

                # 异常检测：浏览器操作后检测弹窗/错误
                if (self.config.enable_anomaly_detect
                        and (result.get("tool") == "browser" or "browser" in (result.get("tools_used") or []))
                        and self.executor.vision_model):
                    anomaly = self._detect_anomaly(result, current_step)
                    if anomaly:
                        print_warning(f"异常检测: {anomaly[:150]}", use_rich=self.config.verbose)
                        step_context_parts.append(f"  异常: {anomaly[:100]}")

                # 重置视觉反馈（已消费）
                vision_feedback = ""

                if result.get("status") == "continue":
                    step_context_parts.append(
                        f"  [{result.get('tool', result.get('action', ''))}] "
                        f"{result.get('output', '')[:200]}"
                    )
                    step_retry_count += 1

                    # 如果是 see 操作，保存截图 base64 和分析结果供下一轮使用
                    if result.get("action") == "see":
                        page_screenshot_b64 = result.get("screenshot_base64", "")
                        vision_feedback = result.get("vision_analysis", "")
                        print_info(f"视觉分析: {vision_feedback[:200]}", style="info", use_rich=self.config.verbose)

                    continue

                elif result.get("action") == "finish":
                    self.state.finished = True
                    step_success = True
                    break

                elif result.get("status") == "failed":
                    # 记录错误
                    error_msg = result.get("error", result.get("output", "未知错误"))
                    errors_collected.append(error_msg[:200])
                    # 自我进化：从失败中学习
                    self.memory.learn_from_failure(current_step, error_msg)
                    # 如果被视觉模型辅助过，尝试一次普通重试
                    if vision_feedback and step_retry_count < self.config.max_step_retries - 1:
                        print_info("重试: 基于视觉反馈重新尝试...", style="accent", use_rich=self.config.verbose)
                        step_context_parts.append(f"  失败: {result.get('error', '')[:100]}")
                        vision_feedback = ""  # 不重复使用旧的分析
                        step_retry_count += 1
                        continue
                    step_success = False
                    break

                else:
                    # completed
                    step_success = True
                    break

            # 检查整体完成
            if self.state.finished:
                self.state.current_step += 1
                break

            # 成功 → 下一步
            if step_success:
                step_retry_count = 0
                page_screenshot_b64 = ""
                vision_feedback = ""
                self.state.current_step += 1
                continue

            # 失败 → 重规划
            if replan_count < self.config.max_replans:
                print_warning(f"重规划 (第 {replan_count + 1} 次)...", use_rich=self.config.verbose)
                new_plan = self._replan(
                    failed_step=current_step,
                    error_message=result.get("error", result.get("output", "未知错误")),
                )
                if new_plan:
                    completed_count = self.state.current_step
                    self.state.plan = self.state.plan[:completed_count] + new_plan
                    print_plan(new_plan, use_rich=self.config.verbose)
                    replan_count += 1
                    step_retry_count = 0
                    vision_feedback = ""
                    continue
                else:
                    print_error("重规划失败，终止执行。", use_rich=self.config.verbose)
                    break
            else:
                print_warning(f"已达最大重规划次数 ({self.config.max_replans})，终止。", use_rich=self.config.verbose)
                break

            if self.state.current_step >= self.config.max_steps:
                print_warning(f"已达最大步骤数 ({self.config.max_steps})，终止。", use_rich=self.config.verbose)
                break

        # 5. 清理
        self._cleanup()

        # 6. 总结
        print_stage("生成总结", use_rich=self.config.verbose)
        final_summary = self._generate_summary()

        print_stage("自我进化", use_rich=self.config.verbose)

        self.memory.add_message("assistant", final_summary)

        # ================================================================
        # 自我进化：保存经验 + 更新策略库
        # ================================================================
        failed_steps = [s for s in self.memory.step_history if s.get("status") == "failed"]
        task_success = len(self.memory.get_failed_steps()) == 0

        self.memory.save_experience(
            goal=goal,
            plan_steps=self.state.plan,
            success=task_success,
            summary=(final_summary or "无输出")[:500],
            tool_usage=tool_usage,
            errors=errors_collected,
        )

        # 记录策略结果
        task_category = self.memory._classify_task(goal)
        approach = self._infer_approach()
        if approach:
            self.memory.record_strategy(task_category, approach, task_success)

        print_evolution(f"经验已保存 (类别: {task_category}, 成功: {task_success})", use_rich=self.config.verbose)
        if errors_collected:
            print_evolution(f"已记录 {len(errors_collected)} 个错误模式，累计 {len(self.memory.failure_patterns)} 个模式", use_rich=self.config.verbose)

        # 生成本轮执行摘要（供下一轮对话使用）
        summary_parts = []
        if self._browser_launched:
            summary_parts.append(f"本轮自动启动了浏览器（原因: {self._browser_launch_reason}）")
        if self._tools_used_this_run:
            tools_str = ", ".join(sorted(self._tools_used_this_run))
            summary_parts.append(f"使用了工具: {tools_str}")
        if summary_parts:
            self.last_execution_summary = "；".join(summary_parts)
        else:
            self.last_execution_summary = "本轮为纯文本问答，未使用任何工具"

        # 最终结果打印
        print_stage("完成", use_rich=self.config.verbose)
        print_final_result(final_summary, use_rich=self.config.verbose)

        self._emit("run_end", {"status": "completed", "summary": (final_summary or "")[:500]})

        self._finalize_run("completed", final_summary)

        return final_summary

    # ================================================================
    # 核心方法
    # ================================================================

    def _think_and_plan(self, planning_goal: str = None,
                         experience_context: str = "",
                         strategy_hints: str = "",
                         conversation_context: str = "") -> list[str]:
        goal_to_plan = planning_goal if planning_goal is not None else self.state.goal
        return self.planner.create_plan(
            goal_to_plan, experience_context, strategy_hints, conversation_context
        )

    def _act(
        self,
        current_step: str,
        step_context: str = "",
        vision_feedback: str = "",
    ) -> dict:
        history = self.memory.get_step_history_summary()
        result = self.executor.execute_step(
            goal=self.state.goal,
            current_step=current_step,
            history_summary=history,
            step_context=step_context,
            vision_feedback=vision_feedback,
            failure_warnings=getattr(self.state, 'failure_warnings', ''),
        )
        # 追踪工具使用
        tool_used = result.get("tool", "")
        if tool_used:
            tool_usage_key = f"_act_tool_{tool_used}"  # dummy，实际在调用处统计
        return result

    def _observe(self, result: dict, step_idx: int):
        status = result.get("status", "unknown")
        output = result.get("output", "")
        error = result.get("error", "")

        print_step_result(status, output, error, use_rich=self.config.verbose)

        self.state.history.append(result)
        self.memory.add_step_result(result)

        if status in ("completed", "failed"):
            self.memory.add_message(
                "tool",
                f"步骤: {result.get('step', '')}\n"
                f"状态: {status}\n"
                f"结果: {(output or error)[:300]}",
            )

    def _replan(self, failed_step: str, error_message: str) -> list[str]:
        completed = self.memory.get_completed_steps()
        return self.planner.replan(
            goal=self.state.goal,
            original_plan=self.state.plan,
            completed_steps=completed,
            failed_step=failed_step,
            error_message=error_message,
            failure_warnings=getattr(self.state, 'failure_warnings', ''),
        )

    def _generate_summary(self) -> str:
        if not self.state.history:
            return "任务未执行任何步骤。"

        history_lines = []
        for i, h in enumerate(self.state.history, 1):
            status = "✓" if h.get("success") else "✗"
            history_lines.append(
                f"{i}. [{status}] {h.get('step', '')}\n"
                f"   结果: {h.get('output', h.get('error', '无输出'))[:300]}"
            )
        history_text = "\n".join(history_lines)

        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT.format(
                personality_style=PERSONALITY.get("speaking_style", "")
            )},
            {"role": "user", "content": SUMMARY_USER_PROMPT_TEMPLATE.format(
                goal=self.state.goal, history_text=history_text
            )},
        ]
        try:
            result = self.llm.chat(messages)
            if not result:
                raise ValueError("LLM 返回空内容")
            return result
        except Exception:
            completed = sum(1 for h in self.state.history if h.get("success"))
            failed = sum(1 for h in self.state.history if not h.get("success"))
            return (
                f"任务执行完毕。共执行 {len(self.state.history)} 个操作，"
                f"成功 {completed} 个，失败 {failed} 个。\n\n详细:\n{history_text}"
            )

    # ================================================================
    # MCP 服务器管理
    # ================================================================

    def _init_mcp_servers(self):
        """根据配置初始化 MCP 服务器连接。"""
        servers = self.config.mcp_servers
        if not servers:
            return

        self._log(f"\n[MCP] 正在连接 {len(servers)} 个 MCP 服务器...")
        for server_cfg in servers:
            name = server_cfg.get("name", "unknown")
            command = server_cfg.get("command", "")
            args = server_cfg.get("args", [])
            env = server_cfg.get("env")

            if not command:
                self._log(f"  [警告] 服务器 '{name}' 缺少 command 配置，跳过。")
                continue

            ok = self.tool_manager.connect_mcp_server(name, command, args, env)
            if ok:
                self._mcp_connected = True
                self._log(f"  [成功] MCP 服务器 '{name}' 已连接。")
            else:
                self._log(f"  [失败] MCP 服务器 '{name}' 连接失败。")

    # ================================================================
    # 帧对比与异常检测
    # ================================================================

    def _frame_compare_feedback(self, result: dict, current_step: str):
        """
        对工具执行进行前后帧对比，验证操作效果。
        通过捕获操作前的截图（如果有）和操作后的截图进行对比。
        """
        # 尝试获取操作后截图进行快速异常检查
        try:
            screenshot_result = self.executor._take_screenshot_for_vision()
            if screenshot_result and screenshot_result.get("success"):
                after_b64 = screenshot_result.get("screenshot_base64", "")
                if after_b64:
                    compare = self.executor.detect_page_anomaly(
                        after_b64,
                        context=f"步骤: {current_step}, 工具: {result.get('tool', '')}, 输入: {result.get('tool_input', '')}"
                    )
                    if compare.get("has_anomaly"):
                        print_warning(f"帧对比异常: {compare.get('type', '')} - {compare.get('description', '')[:100]}", use_rich=self.config.verbose)
        except Exception:
            pass

    def _detect_anomaly(self, result: dict, current_step: str) -> str:
        """检测浏览器操作后的页面异常。"""
        try:
            screenshot_result = self.executor._take_screenshot_for_vision()
            if not screenshot_result or not screenshot_result.get("success"):
                return ""
            anomaly = self.executor.detect_page_anomaly(
                screenshot_result.get("screenshot_base64", ""),
                context=f"{current_step} | {result.get('tool_input', '')}"
            )
            if anomaly.get("has_anomaly"):
                return f"{anomaly.get('type', '未知')}: {anomaly.get('description', '')}"
        except Exception:
            pass
        return ""

    # ================================================================
    # 浏览器自动管理
    # ================================================================

    def _auto_launch_browser(self):
        result = self.executor.execute_tool_directly("browser", "launch")
        self.memory.add_step_result(result)
        if result["success"]:
            self._log("  浏览器已自动启动。")
        else:
            self._log(f"  浏览器启动失败: {result.get('error', '')}")

    def _cleanup(self):
        # 关闭浏览器
        try:
            browser = self.tool_manager.get_tool("browser")
            if browser and browser._pages:
                self._log("\n[清理] 关闭浏览器...")
                result = self.executor.execute_tool_directly("browser", "close")
                if result.get("success"):
                    self._log("  浏览器已关闭。")
        except Exception:
            pass

        # 断开 MCP 服务器
        if self._mcp_connected:
            self._log("[清理] 断开 MCP 服务器...")
            self.tool_manager.disconnect_mcp_server()
            self._mcp_connected = False

    # ================================================================
    # v2：Rollout / 会话 / 压缩 / 收尾
    # ================================================================

    def _summarize_for_compaction(self, messages: list) -> str:
        """供 Rollout 压缩调用的摘要回调（用主 LLM 生成摘要）。"""
        try:
            return self.llm.chat(messages, temperature=0.3, max_tokens=1200)
        except Exception:
            return ""

    def _load_session_if_requested(self):
        """会话持久化：启动时恢复历史对话（全量记录），**只加载一次**。

        幂等性：加载前先清空 memory，且每个 Agent 实例只加载一次
        （_session_loaded 标记），防止每轮 run 反复 append 历史消息
        导致 conversation_history 指数膨胀（曾出现 540 万条消息/7.6GB
        内存的雪崩事故）。main.py 交互模式已自行加载过时，这里会跳过。
        """
        if self._session_loaded:
            return
        self._session_loaded = True
        name = self.config.session_name
        if not name:
            return
        # 优先按对话（conversation）加载；兼容旧名字格式
        data = self.session_store.load_conversation(name)
        if data is None:
            data = self.session_store.load(name)
        if not data:
            return
        try:
            messages = data.get("messages", [])
            # 清空 memory 后再加载：避免与调用方（main.py 启动加载）重复叠加
            self.memory.clear_session()
            for m in messages:
                self.memory.add_message(m.get("role", "user"), m.get("content", ""))
            self.last_execution_summary = data.get("last_summary", "")
            print_evolution(f"已恢复对话 '{name}'（{len(messages)} 条记录）", use_rich=self.config.verbose)
            self._restore_conversation_model(data)
        except Exception as e:
            print_warning(f"会话恢复失败: {e}", use_rich=self.config.verbose)

    def _save_session_if_requested(self, final_summary: str):
        """会话持久化：结束时保存**全部**对话记录（不截断）+ 绑定当前模型。"""
        name = self.config.session_name
        if not name:
            return
        try:
            messages = [
                {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}
                for m in self.memory.conversation_history
            ]
            self.session_store.save_conversation(
                name,
                messages=messages,          # 全量记录
                last_summary=self.last_execution_summary,
                model=self.llm.default_model,
                base_url=str(self.llm.client.base_url),
            )
            print_evolution(f"对话已保存: {name}（{len(messages)} 条记录）", use_rich=self.config.verbose)
        except Exception as e:
            print_warning(f"对话保存失败: {e}", use_rich=self.config.verbose)

    def _restore_conversation_model(self, data: dict):
        """恢复对话时自动切回其绑定的模型（一个对话一个模型）。

        尊重用户的显式配置：若用户通过环境变量/.env 显式指定了默认模型
        （MY_AGENT_MODEL / ANTHROPIC_MODEL / LLM_DEFAULT_MODEL），
        说明用户有全局模型偏好，恢复对话**不**覆盖当前模型（只提示）；
        仅当用户未显式配置（用程序默认值）时，才自动切回对话绑定模型。
        """
        model = data.get("model") or ""
        if not model or model == self.llm.default_model:
            return
        # 用户显式配置了默认模型 → 尊重用户选择，不覆盖
        if self._user_explicitly_set_default_model():
            print_evolution(
                f"该对话绑定模型 {model}，但你已显式配置默认模型 "
                f"{self.llm.default_model}，保持当前模型（/model 可随时切换）。",
                use_rich=self.config.verbose,
            )
            return
        try:
            self.switch_model(
                model=model,
                base_url=data.get("base_url") or None,
            )
            print_evolution(
                f"对话绑定模型 {model}，已自动切换。",
                use_rich=self.config.verbose,
            )
        except Exception as e:
            print_warning(f"恢复绑定模型失败: {str(e)[:100]}", use_rich=self.config.verbose)

    @staticmethod
    def _user_explicitly_set_default_model() -> bool:
        """用户是否显式配置过默认模型（环境变量/.env 里写了模型名）。"""
        return any(
            os.getenv(k)
            for k in ("MY_AGENT_MODEL", "ANTHROPIC_MODEL", "LLM_DEFAULT_MODEL")
        )

    def _finalize_run(self, status: str, result_text: str = ""):
        """运行收尾：关闭 Rollout、保存会话、输出统计与审批信息。"""
        if self.rollout is not None:
            try:
                self.rollout.emit("run_end", {"status": status, "result": (result_text or "")[:300]})
                self.rollout.close()
                self.rollout.cleanup_old_logs()
            except Exception:
                pass
        if self.executor is not None:
            self.executor.rollout = None

        self._save_session_if_requested(result_text)

        # 统计行（轮数/步数/LLM 耗时/工具耗时/首 token/速率/缓存命中/token 用量 + 策略）
        # 桌面端统计卡：无论 verbose 与否都发 metrics 事件（hub 广播给 WS）；verbose 时同时打印。
        metrics = getattr(self, "metrics", None)
        if metrics is not None and metrics.has_data():
            try:
                line = metrics.render_line()
                if self.approval is not None:
                    line += f" · 策略 {self.approval.mode}/{self.approval.sandbox_mode}"
                payload = {
                    "line": line,
                    "turns": metrics.turns,
                    "steps": metrics.steps,
                    "input_tokens": metrics.input_tokens,
                    "output_tokens": metrics.output_tokens,
                    "cached_tokens": metrics.cached_tokens,
                    "cache_hit_rate": round(metrics.cache_hit_rate * 100.0, 2),
                    "llm_seconds": round(metrics.llm_seconds, 1),
                    "tool_seconds": round(metrics.tool_seconds, 1),
                    "first_token_avg": round(metrics.first_token_avg, 2) if metrics.first_token_seconds else 0,
                    "tokens_per_sec": round(metrics.tokens_per_sec, 1),
                    "model": getattr(getattr(self, "llm", None), "default_model", None)
                    or getattr(self.config, "model", None) or "",
                    "context_tokens": getattr(metrics, "last_context_tokens", 0),
                    "compact_threshold_tokens": self._compact_threshold(),
                }
                try:
                    self._emit("metrics", payload)
                except Exception:
                    pass
                if self.config.verbose:
                    print_info(line, style="info", use_rich=True)
            except Exception:
                pass

        if self.approval is not None and self.approval.decision_log and self.config.verbose:
            denied = sum(1 for d in self.approval.decision_log if d["decision"] == "deny")
            if denied:
                print_warning(
                    f"安全审批: {len(self.approval.decision_log)} 次判定，拒绝 {denied} 次"
                    f"（策略: {self.approval.mode}, 沙箱: {self.approval.sandbox_mode}）",
                    use_rich=True,
                )

    # ================================================================
    # v3.1：单循环执行（主循环式）
    # ================================================================

    def _render_skills_prompt(self, goal: str) -> str:
        """渲染 Skills 技能包注入文本（技能包式能力扩展）。

        未启用 / 无技能 / 任何异常时返回空串（静默，不影响主流程）。
        独立成方法便于测试直接调用渲染路径。
        """
        if not self.config.skills_enabled:
            return ""
        try:
            from agent.skills import SkillManager
            mgr = SkillManager(
                project_dir=self.config.skills_project_dir,
                user_dir=self.config.skills_user_dir,
                max_chars=self.config.skills_max_chars,
            )
            matched = mgr.match(goal)
            if matched:
                # Dashboard 桌面端 skills 匹配提示：仅命中时推送技能名列表
                self._emit("skills_matched", {
                    "skills": [s.name for s in matched],
                    "goal": (goal or "")[:200],
                })
            return mgr.render_for_prompt(goal)
        except Exception:
            return ""

    def _run_loop(self, goal: str, keep_session: bool = False,
                  event_sink=None, stop_event=None) -> str:
        """
        单循环模式：一轮持续对话完成整个目标。

        与计划模式的区别：
        - 没有独立的规划 / 逐步执行 / 总结三层 LLM 调用
        - 整个任务共享一条消息线程，模型能看到此前每一步真实的工具调用与结果
        - 最终回答直接来自循环的最后一轮输出
        """
        from agent.rollout import Rollout
        from models.prompts import LOOP_SYSTEM_PROMPT, APPROVAL_NOTICE_TEMPLATE

        # 0. 极简启动：
        #    - 交互模式（keep_session）：零头部——用户消息已在 "> " 提示行可见
        #    - 单次执行：仅回显一行 "> 目标"，不重复状态行/分隔线
        from agent.ui_theme import print_goal_echo
        if not keep_session:
            print_goal_echo(goal, use_rich=self.config.verbose)
        elif self.config.verbose:
            get_console(True).print()

        # 0. Dashboard / MCP / Rollout / 会话
        self._emit("run_start", {"goal": goal, "mode": "loop"})
        self._init_mcp_servers()
        self.rollout = Rollout(
            summarizer=self._summarize_for_compaction,
            config={**ROLLOUT_CONFIG, "compact_tokens": self._compact_threshold()},
        ) if self.config.rollout_enabled else None
        if self.executor is not None:
            self.executor.rollout = self.rollout
        self._load_session_if_requested()
        if self.rollout is not None:
            self.rollout.emit("run_start", {"goal": goal, "mode": "loop"})

        self._tools_used_this_run = set()

        # 1. 记忆召回（经验 + 失败模式 + 策略建议）
        # 轻量相似度排序（不调 LLM，避免主模型慢速拖累每次启动）；
        # 用 use_llm_rank=True 可显式开启语义排序。
        experience_context = self.memory.recall_experiences(goal, n=3, llm=self.llm)
        failure_warnings = self.memory.get_failure_warnings(goal)
        task_category = self.memory._classify_task(goal)
        strategy_hints = self.memory.get_best_strategies(task_category)
        # 可视反馈：让用户看到记忆在起作用（只读一行，不刷屏）
        # 注意：print_info 用模块级导入；此处禁止再局部 import——
        # 局部导入会把 print_info 变成整个函数的局部名，
        # 本分支不执行时下面的意图识别 print_info 会触发 UnboundLocalError
        if experience_context and self.config.verbose:
            try:
                n_exp = experience_context.count("### 经验")
                print_info(
                    f"记忆召回: {n_exp} 条相关经验 / {len(self.memory.experiences)} 条总经验"
                    + (f"（类别: {task_category}）" if task_category != "general" else ""),
                    style="dim",
                )
            except Exception:
                pass

        # 2. 意图检测（中文网站名 → URL）
        intent_detector = get_intent_detector()
        intent_result = intent_detector.detect(goal)
        if intent_result.detected_sites:
            site_names = [s['name'] for s in intent_result.detected_sites]
            site_urls = [s['url'] for s in intent_result.detected_sites]
            print_info(
                f"意图识别: {', '.join(f'{n}→{u}' for n, u in zip(site_names, site_urls))}",
                style="accent", use_rich=self.config.verbose,
            )
            planning_goal = intent_result.enriched_goal
        else:
            planning_goal = goal

        # 3. 上下文：会话历史 + 经验 + 策略 + 失败警告
        context_parts = []
        if keep_session:
            ctx_count = int(SESSION_CONFIG.get("context_messages", 12))
            ctx_chars = int(SESSION_CONFIG.get("context_message_chars", 400))
            recent = self.memory.get_recent_messages(ctx_count)
            if len(recent) > 1:
                ctx_lines = ["\n## 之前的对话记录"]
                for m in recent[:-1]:
                    role_label = "用户" if m["role"] == "user" else self._agent_name()
                    ctx_lines.append(f"- {role_label}: {m['content'][:ctx_chars]}")
                if self.last_execution_summary:
                    ctx_lines.append(f"上一轮执行摘要: {self.last_execution_summary}")
                context_parts.append("\n".join(ctx_lines))
        if experience_context:
            context_parts.append(f"\n## 历史经验（可参考的成功做法）\n{experience_context}")
        if strategy_hints:
            context_parts.append(f"\n## 策略建议\n{strategy_hints}")
        if failure_warnings:
            context_parts.append(
                f"\n【警告：已知失败模式】\n{failure_warnings}\n请避开以上错误模式。"
            )
        # Repo Map：代码相关任务注入仓库结构摘要，帮助直接定位文件
        if self.config.repomap_enabled:
            try:
                from agent.repomap import get_repo_map_for_goal
                repo_map = get_repo_map_for_goal(
                    goal, SNAPSHOT_CONFIG.get("work_dir") or os.getcwd()
                )
                if repo_map:
                    context_parts.append(f"\n## 项目结构（Repo Map）\n{repo_map}")
            except Exception:
                pass
        context_text = "\n".join(context_parts)

        # 4. 系统提示：人格 + 循环规则 + 审批提示 + 运行环境 + AGENTS.md
        import platform as _platform
        from models.prompts import PLATFORM_NOTICE_TEMPLATE
        system_prompt = LOOP_SYSTEM_PROMPT.format(
            test_command=TEST_CONFIG.get("command", "pytest tests -q"),
            agent_name=self._agent_name(),
        )
        system_prompt += PLATFORM_NOTICE_TEMPLATE.format(
            system=_platform.system(),
            shell="cmd.exe" if _platform.system() == "Windows" else "sh",
        )
        # 注册了桌面操控工具时，附带使用指南（先感知再操作，避免盲点坐标）
        if self.tool_manager.get_tool("computer") is not None:
            from models.prompts import COMPUTER_USE_PROMPT_GUIDE
            system_prompt += "\n\n" + COMPUTER_USE_PROMPT_GUIDE
        # 多引擎协作：内置 Agent 主动委托外部 CLI 引擎（delegate 工具存在时）
        if self.tool_manager.get_tool("delegate") is not None:
            from models.prompts import DELEGATE_GUIDE_PROMPT
            system_prompt += "\n\n" + DELEGATE_GUIDE_PROMPT
        # 云经验库：学习/沉淀跨 Agent 经验（experience 工具存在时）
        if self.tool_manager.get_tool("experience") is not None:
            from models.prompts import EXPERIENCE_GUIDE_PROMPT
            system_prompt += "\n\n" + EXPERIENCE_GUIDE_PROMPT
        if self.approval is not None:
            system_prompt += APPROVAL_NOTICE_TEMPLATE.format(
                approval_policy=self.approval.mode,
                sandbox_mode=self.approval.sandbox_mode,
            )
        if self.instructions_text:
            system_prompt += f"\n\n## 项目指令（必须遵守）\n{self.instructions_text}"
        # Skills 技能包（技能包式能力扩展）：放在 AGENTS.md 指令之后；
        # 未启用 / 无技能 / 异常时静默，不影响主流程。
        try:
            skills_prompt = self._render_skills_prompt(planning_goal)
        except Exception:
            skills_prompt = ""
        if skills_prompt:
            system_prompt += "\n\n" + skills_prompt

        # 5. 单循环执行（流式渲染：思考转圈 → 逐字输出答案，加粗实时生效）
        self._turn_spinner = None
        self._stream_reasoning_started = False
        self._stream_answer_started = False
        from agent.ui_theme import StreamingMarkdown
        self._md_renderer = StreamingMarkdown(use_rich=self.config.verbose)
        try:
            result = self.executor.execute_goal_loop(
                goal=planning_goal,
                system_prompt=system_prompt,
                context_text=context_text,
                max_ops=self.config.max_ops,
                event_sink=event_sink or self._loop_tool_event,
                stream=self.config.stream_enabled,
                temperature=self.config.temperature,
                top_p=self.config.top_p,
                max_tokens=self.config.max_tokens,
                on_turn_start=self._on_stream_turn_start,
                on_text_delta=self._on_stream_delta,
                stop_event=stop_event,
            )
        except KeyboardInterrupt:
            # Ctrl+C：停转圈、给出提示后向上传播（交互模式会回到提示符）
            self._stop_turn_spinner()
            print_warning("已中断（Ctrl+C）。", use_rich=self.config.verbose)
            raise
        except Exception as e:
            self._stop_turn_spinner()
            # 供应商不支持 function calling → 回退经典计划模式（旧协议兼容）
            if self.executor._looks_like_unsupported(e):
                self.executor._fc_supported = False
                print_warning(
                    "当前模型不支持 function calling，回退到经典计划模式。",
                    use_rich=self.config.verbose,
                )
                self.config.exec_mode = "plan"
                return self.run(goal, keep_session, event_sink, stop_event)
            final = f"执行过程中出现错误：{e}"
            result = {"success": False, "output": final, "tool_calls": [], "errors": [str(e)]}
        self._stop_turn_spinner()

        final = result.get("output", "").strip() or "任务执行完毕（无文字总结）。"
        # 空回复：明确记为失败并补充原因（供失败模式学习，防止污染经验库）
        if not result.get("output", "").strip():
            result["errors"] = list(result.get("errors") or []) + ["模型返回空回复（上游服务可能降级）"]
            result["success"] = False
        tool_usage = {}
        for tc in result.get("tool_calls", []):
            name = tc.get("name", "")
            if name:
                tool_usage[name] = tool_usage.get(name, 0) + 1
                self._tools_used_this_run.add(name)

        # 6. 记忆与自我进化（与计划模式一致）
        self.memory.add_message("user", goal)
        self.memory.add_message("assistant", final)
        if not result.get("stopped"):
            self.memory.save_experience(
                goal=goal,
                plan_steps=[goal],
                success=result.get("success", False),
                summary=final[:500],
                tool_usage=tool_usage,
                errors=result.get("errors", []),
            )
        approach = self._infer_approach()
        if approach:
            self.memory.record_strategy(task_category, approach, result.get("success", False))
        self.last_execution_summary = (
            f"使用了工具: {', '.join(sorted(self._tools_used_this_run))}"
            if self._tools_used_this_run else "本轮未使用工具"
        )

        # 7. 展示与收尾
        if self._stream_answer_started and self.config.verbose:
            # 答案已流式逐字输出：冲刷 Markdown 缓冲并补空行，不再重复打印
            try:
                self._md_renderer.flush()
            except Exception:
                pass
            get_console(True).print()
            get_console(True).print()
        else:
            print_final_result(final, use_rich=self.config.verbose)
        if result.get("stopped"):
            status = "stopped"
        else:
            status = "completed" if result.get("success") else "failed"
        # 完整最终答案以 answer 事件推送（供桌面/Web 前端渲染，不截断），
        # run_end 仍带 summary 供日志/状态用
        self._emit("answer", {"output": final, "status": status})
        self._emit("run_end", {"status": status, "summary": final[:500]})
        self._finalize_run(status, final)
        return final

    def _on_stream_turn_start(self):
        """每轮模型调用开始：显示"思考中"转圈（仅真实 TTY）。"""
        self._stream_reasoning_started = False   # 每轮重置推理块标记
        if not self.config.verbose:
            return
        try:
            self._stop_turn_spinner()
            if sys.stdout.isatty():
                console = get_console(True)
                self._turn_spinner = console.status("✻ 思考中…", spinner="dots")
                self._turn_spinner.start()
        except Exception:
            self._turn_spinner = None

    def _stop_turn_spinner(self):
        """停止思考中转圈。"""
        if self._turn_spinner is not None:
            try:
                self._turn_spinner.stop()
            except Exception:
                pass
            self._turn_spinner = None

    def _on_stream_delta(self, kind: str, text: str):
        """
        流式增量渲染（主流风格）：
        - reasoning → 灰色"✻ 思考"块逐字输出（推理过程）
        - text     → "✻ 小悟"前缀后经流式 Markdown 渲染（**加粗**实时生效）

        同时把增量推送到 Dashboard（stream_delta 事件），供桌面/Web 前端
        实时渲染回答正文（终端 verbose 关闭时也推送）。
        """
        # 推送到前端（即使非 verbose / 非 TTY 也发）
        self._emit("stream_delta", {"kind": kind, "text": text})
        if not self.config.verbose:
            return
        self._stop_turn_spinner()
        console = get_console(True)
        if kind == "reasoning":
            if not self._stream_reasoning_started:
                self._stream_reasoning_started = True
                console.print()
                console.print("[dim]✻ 思考[/dim]")
            console.print(text, end="", markup=False, style="dim", soft_wrap=True)
        else:
            if not self._stream_answer_started:
                self._stream_answer_started = True
                console.print()
                console.print(f"[primary]✻[/primary] [bold white]{self._agent_name()}[/bold white]")
            try:
                self._md_renderer.feed(text)
            except Exception:
                console.print(text, end="", markup=False, soft_wrap=True)

    def _status_text(self) -> str:
        """构造常驻状态栏聚合文本（token 计数/沙箱等级/审批策略）。

        终端与 Dashboard turn_start 事件共用同一份状态文本：
        终端直接打印，事件侧放进 data["status_text"] 供桌面端状态条展示。
        任何异常 / metrics 缺失时返回空串（静默）。
        """
        try:
            from agent.sandbox import sandbox_enabled
            metrics = getattr(self, "metrics", None)
            if metrics is None:
                return ""
            policy = self.approval.mode if self.approval is not None else ""
            sandbox_label = self.approval.sandbox_mode if self.approval is not None else ""
            if sandbox_enabled():
                # OS 级沙箱（AppContainer）开启时叠加显示
                sandbox_label = (
                    f"{sandbox_label}+AppContainer" if sandbox_label else "AppContainer"
                )
            return metrics.render_status_bar(policy=policy, sandbox_mode=sandbox_label)
        except Exception:
            return ""

    def _render_status_bar(self):
        """常驻状态栏：token 计数/沙箱等级/审批策略（verbose 且开关开启时）。"""
        from config import TUI_CONFIG
        if not TUI_CONFIG.get("status_bar", True):
            return
        try:
            from agent.ui_theme import print_status_bar
            text = self._status_text()
            if not text:
                return
            print_status_bar(
                text,
                use_rich=self.config.verbose,
            )
        except Exception:
            pass

    def _loop_tool_event(self, event_type: str, data: dict):
        """
        单循环模式事件回调：转发 Dashboard + 主流风格终端渲染。

        turn_start  → 附带 status_text（token/沙箱/策略聚合文本）转发 Dashboard，
                      并刷新常驻状态栏（verbose 时）
        tool_call   → 打印 `⏺ tool(args)`
        tool_result → 打印 `⎿ 结果`（失败红色）
        """
        if event_type == "turn_start":
            # 桌面端状态条联动：把 token 计数/沙箱/策略聚合文本挂到事件上再转发。
            # 同时携带结构化累计值（每轮实时），供概览面板精确动态更新
            # （不再依赖文本解析）。
            try:
                data = dict(data)
                data["status_text"] = self._status_text()
                m = getattr(self, "metrics", None)
                if m is not None:
                    data["input_tokens"] = int(m.input_tokens)
                    data["output_tokens"] = int(m.output_tokens)
                    data["cached_tokens"] = int(m.cached_tokens)
                    # 真实窗口水位 = 最近一次请求的输入规模（≠累计输入，前端概览用）
                    data["context_tokens"] = int(getattr(m, "last_context_tokens", 0))
                    data["cache_hit_rate"] = round(m.cache_hit_rate * 100.0, 2)
                    data["turns"] = int(m.turns)
                    data["steps"] = int(m.steps)
                    data["llm_seconds"] = round(m.llm_seconds, 1)
                    data["compact_threshold_tokens"] = self._compact_threshold()
            except Exception:
                pass
            self._emit(event_type, data)
            if self.config.verbose:
                self._render_status_bar()
            return

        if event_type == "run_end" and data.get("status") != "completed":
            # 失败/中断：带上持久目标，供桌面端渲染「重试」按钮（续跑）
            try:
                d = dict(data)
                if self.config.session_name:
                    from agent.goal import GoalStore
                    g = GoalStore().get(self.config.session_name)
                    if g:
                        d["goal"] = g
                data = d
            except Exception:
                pass

        self._emit(event_type, data)
        if not self.config.verbose:
            return

        from agent.ui_theme import (
            print_tool_call, print_tool_result, print_edit_call,
            print_edit_diff, print_unified_diff, print_file_write_call,
        )

        if event_type == "tool_call":
            tool = data.get("tool", "")
            if tool == "think":
                return
            # 推理块刚流式结束 → 换行后再打印工具调用行
            if self._stream_reasoning_started:
                self._stream_reasoning_started = False
                get_console(True).print()
            args = data.get("arguments") or {}
            if tool == "edit":
                print_edit_call(args.get("file_path", ""))
            elif tool == "file" and str(args.get("operation", "") or "").lower() == "write":
                print_file_write_call(args.get("path", ""))
            else:
                print_tool_call(tool, args)
        elif event_type == "tool_result":
            tool = data.get("tool", "")
            if tool == "think":
                return
            args = data.get("arguments") or {}
            if tool == "edit" and data.get("success"):
                # unified diff（红-绿+上下文灰+@@ 行号）：
                # 优先读 .bak 备份；成功路径 .bak 已即时清理，退回 metadata.old_text；
                # 都没有时回退参数内 old/new 简式
                meta = data.get("metadata") or {}
                backup = meta.get("backup_path", "")
                old_text = new_text = None
                if backup:
                    try:
                        with open(backup, "r", encoding="utf-8") as f:
                            old_text = f.read()
                        with open(args.get("file_path", ""), "r", encoding="utf-8") as f:
                            new_text = f.read()
                    except Exception:
                        old_text = new_text = None
                if old_text is None and meta.get("old_text"):
                    old_text = meta["old_text"]
                    try:
                        with open(args.get("file_path", ""), "r", encoding="utf-8") as f:
                            new_text = f.read()
                    except Exception:
                        new_text = None
                if old_text is not None and new_text is not None:
                    print_unified_diff(old_text, new_text)
                else:
                    print_edit_diff(args.get("old_string", ""), args.get("new_string", ""))
            print_tool_result(data.get("success", False), data.get("output", ""))

    # ================================================================
    # 向后兼容
    # ================================================================

    def think(self):
        self.state.plan = self._think_and_plan()

    def act(self):
        if self.state.current_step < len(self.state.plan):
            return self._act(self.state.plan[self.state.current_step])
        return None

    def observe(self):
        if self.state.current_step >= len(self.state.plan) - 1:
            self.state.finished = True

    def _log(self, msg: str, style: str = ""):
        """统一日志输出，默认使用 Rich 美化输出。"""
        if not self.config.verbose:
            return
        c = get_console(True)
        if style:
            c.print(msg, style=style)
        else:
            c.print(msg)

    def _compact_threshold(self) -> int:
        """压缩触发阈值（与执行器同一来源，概览刻度用它）。"""
        return _resolve_compact_threshold(getattr(self, "llm", None))

    def _emit(self, event_type: str, data: dict):
        """向 Dashboard 发送事件。辅助 Agent（panel="side"）时给事件打标记，前端据此路由。"""
        if self._events_panel:
            try:
                data["panel"] = self._events_panel
                if self._events_side_of:
                    data["side_of"] = self._events_side_of
            except Exception:
                pass
        # 主会话归属：事件带上会话 id，桌面端按会话隔离统计（概览不串台）
        if not data.get("session_id"):
            try:
                sid = getattr(getattr(self, "config", None), "session_name", None)
                if sid:
                    data["session_id"] = sid
            except Exception:
                pass
        if self._dashboard:
            try:
                self._dashboard.emit(event_type, data)
            except Exception:
                pass

    def _compact_history(self) -> int:
        """手动压缩当前对话历史（/compact 用）：旧部分交给 LLM 总结成摘要。返回移除条数。"""
        try:
            msgs = [{"role": m.role, "content": m.content} for m in self.memory.conversation_history]
            ex = getattr(self, "executor", None)
            if ex is None or not msgs:
                return 0
            compacted = ex._maybe_compact(msgs)
            if len(compacted) == len(msgs):
                return 0
            from agent.memory import MemoryEntry
            self.memory.conversation_history = [
                MemoryEntry(role=m["role"], content=m["content"]) for m in compacted
            ]
            return len(msgs) - len(compacted)
        except Exception:
            return 0

    def _wire_session_tools(self):
        """按会话给会话级工具注入 key + 事件回调（todo/task/thought/terminal）。"""
        try:
            session_key = getattr(self.config, "session_name", "") or ""
            for name in ("todo_write", "terminal", "task", "thought"):
                try:
                    t = self.tool_manager.get_tool(name)
                except Exception:
                    t = None
                if t is None:
                    continue
                if hasattr(t, "set_session_key"):
                    try:
                        t.set_session_key(session_key)
                    except Exception:
                        pass
                if name in ("todo_write", "task", "thought") and hasattr(t, "set_emit"):
                    try:
                        t.set_emit(self._emit)
                    except Exception:
                        pass
        except Exception:
            pass

    def _infer_approach(self) -> str:
        """推断本次任务使用的整体策略方案，用于策略库记录。"""
        plan_text = " ".join(self.state.plan).lower() if self.state.plan else ""
        if "openpyxl" in plan_text or "pandas" in plan_text:
            return "python库生成文件"
        if "browser" in plan_text or "浏览器" in plan_text:
            return "浏览器工具"
        if "terminal" in plan_text or "终端" in plan_text:
            return "终端命令"
        if "python" in plan_text:
            return "python脚本"
        # v3.1：单循环模式没有计划文本，按本轮工具使用推断
        tools = self._tools_used_this_run
        if "browser" in tools or "see" in tools:
            return "浏览器工具"
        if "terminal" in tools:
            return "终端命令"
        if "python" in tools:
            return "python脚本"
        return "通用方案"
