"""
my_agent 入口程序（Team + Research + Dashboard + 安全审批 + MCP Server 增强版）。

用法:
    python main.py                           # 交互模式
    python main.py "你的任务"                 # 单Agent执行
    python main.py --team "复杂任务"          # 多Agent团队协作
    python main.py --research "研究主题"      # 深度研究模式
    python main.py --dashboard                # 启动Web监控面板
    python main.py --dashboard --team "任务"  # Dashboard + 团队模式
    python main.py --mcp-server               # 以 MCP server 运行（供上游宿主平台调用）
    python main.py --list-tools               # 列出全部工具（含 JSON Schema）

安全参数:
    --approval {untrusted,on-failure,on-request,never}   审批策略
    --sandbox {read-only,workspace-write,danger-full-access}  沙箱等级
    --guardian / --no-guardian                  Guardian 安全审校
    --no-rollout                                关闭 Rollout 事件追踪
    --max-step-ops N                            单步骤内最大工具操作数
    --session NAME                              会话持久化（保存/恢复对话历史）
"""
import sys
import os
import argparse
import threading

from agent import Agent, AgentConfig, Team
from tools import ToolManager
from agent.memory import Memory
from config import LOG_CONFIG


def build_config(args) -> AgentConfig:
    """根据命令行参数构建 AgentConfig。"""
    return AgentConfig(
        max_steps=args.max_steps,
        verbose=not args.quiet,
        enable_vision=not args.no_vision,
        approval_policy=args.approval,
        sandbox_mode=args.sandbox,
        approval_interactive=True,   # 终端交互模式下允许人工确认
        guardian_enabled=args.guardian,
        rollout_enabled=not args.no_rollout,
        max_step_ops=args.max_step_ops,
        session_name=args.session or "",
        exec_mode=args.exec_mode,
        max_ops=args.max_ops,
        stream_enabled=not args.no_stream,
        llm_model=args.model,
        llm_base_url=args.base_url,
        llm_api_key=args.api_key,
    )


def run_once(goal: str, verbose: bool = True, enable_vision: bool = True,
             config: AgentConfig = None):
    """单 Agent 模式。"""
    agent = Agent(
        tool_manager=ToolManager(),
        memory=Memory(),
        config=config or AgentConfig(
            verbose=verbose,
            enable_vision=enable_vision,
        ),
    )
    return agent.run(goal)


def run_team(task: str):
    """多 Agent 团队协作模式。"""
    from agent.ui_theme import print_info, print_final_result
    from config import TEAM_CONFIG

    print_info(f"Team 协作模式 · {task[:80]}", style="primary")

    tool_manager = ToolManager()
    team = Team(tool_manager=tool_manager)
    result = team.run(task, parallel=TEAM_CONFIG.get("parallel", False))

    for st in result.subtasks:
        icon = "✓" if st.status == "completed" else "✗"
        print_info(
            f"{icon} [{st.worker_name}] {st.description[:60]}",
            style="success" if st.status == "completed" else "error",
        )
    if result.review_feedback:
        print_info(f"审校建议: {result.review_feedback[:300]}")

    print_final_result(result.final_answer)
    return result.final_answer


def run_research(topic: str):
    """深度研究模式。"""
    from agent.ui_theme import print_info, print_final_result

    print_info(f"Deep Research · {topic[:80]}", style="primary")

    from models import LLM
    from tools.research import DeepResearcher

    tool_manager = ToolManager()
    llm = LLM()
    researcher = DeepResearcher(tool_manager=tool_manager, llm=llm)
    report = researcher.research(topic, depth=3)

    report_text = f"置信度: {report.confidence:.0%}\n\n{report.summary[:800]}"
    for s in report.sections:
        report_text += f"\n\n## {s.get('title', '')}\n{s.get('content', '')[:1500]}"
    if report.sources:
        report_text += "\n\n---\n来源:\n" + "\n".join(
            f"- [{s.title or '未知'}]({s.url})" for s in report.sources[:10]
        )

    print_final_result(report_text)
    return report


def start_dashboard(port: int = 8080, host: str = "127.0.0.1"):
    """
    启动 dashboard 后端服务（/api + /ws）。

    注意：浏览器版控制面板（GET / 与 /static）默认已禁用（web 端停用），
    本服务仅供桌面端客户端 / 手动 API 使用；需要浏览器版页面时先设置
    MY_AGENT_WEB_UI=1 再调用。桌面端由 Electron 主进程自行拉起（端口 8090），
    main.py 的 --dashboard 参数不再走这里。
    """
    try:
        from dashboard.server import start_server
        t = threading.Thread(target=start_server, kwargs={
            "host": host, "port": port,
        }, daemon=True)
        t.start()
        # 明确用 IPv4 地址（浏览器对 localhost 可能优先解析 IPv6 而连不上）
        print(f"  [Dashboard] 服务已启动: http://127.0.0.1:{port}")
    except Exception as e:
        print(f"  [Dashboard] 启动失败: {e}")


def start_desktop_app():
    """原生桌面 GUI（Tkinter 窗口，非 Web/浏览器壳）。"""
    from agent.desktop_gui import launch_desktop
    launch_desktop()


def _parse_command(goal: str):
    """
    解析斜杠命令（容忍 `/team任务` 这种命令与参数连写的输入）。

    Returns:
        (命令名, 参数) 或 (None, None)。
        命令名 ∈ {"team", "research", "tools", "sessions", "open", "new"}
    """
    for cmd in ("team", "research", "tools", "sessions", "open", "new", "model", "config", "image", "memory", "compact", "goal", "help"):
        prefix = "/" + cmd
        if goal == prefix:
            return cmd, ""
        if goal.startswith(prefix):
            rest = goal[len(prefix):]
            nxt = rest[0]
            # 空格分隔：/team 任务
            if nxt.isspace():
                return cmd, rest.strip()
            # 命令+参数连写：/team任务（中文等非 ASCII 字符）
            # 排除 /teamwork 这类英文连写（ASCII 字母数字）
            if not nxt.isascii():
                return cmd, rest.strip()
    return None, None


def run_interactive(enable_team: bool = False, auto_mode: bool = False,
                    config: AgentConfig = None):
    """
    交互模式：`>` 提示符 + 轮次间弱分割线 + 对话 ID 管理。

    对话管理：
    - 每次交互自动生成对话 ID（conv-日期-随机码），欢迎区显示
    - 每轮结束自动全量保存该对话的所有记录
    - /sessions 列出全部对话；/open <ID> 打开并恢复某对话的全部记录；/new 新开对话
    - 启动参数 --session <ID> 直接恢复指定对话
    """
    from agent.ui_theme import (
        print_welcome, print_goodbye, print_final_result, print_info,
        print_warning, get_console,
    )
    from agent.session import SessionStore, generate_conversation_id
    from agent import input_reader
    from rich.prompt import Prompt

    class _AgentPrompt(Prompt):
        """主流风格提示符：去掉 rich 默认的 ': ' 后缀。"""
        prompt_suffix = ""

    c = get_console(True)
    store = SessionStore()

    # 对话 ID：--session 指定则沿用（不存在就新建），否则生成新对话
    agent = Agent(config=config or AgentConfig(verbose=True))
    conv_id = agent.config.session_name or generate_conversation_id()
    agent.config.session_name = conv_id

    conv = store.load_conversation(conv_id)

    # 欢迎面板（单面板启动框：状态/会话/快速开始/模型信息；终端过窄自动回退极简版）
    _api_key = (agent.llm.client.api_key or "")[:6]
    print_welcome(use_rich=True, info={
        "model": agent.llm.default_model,
        "base_url": agent.llm.client.base_url,
        "workspace": os.getcwd(),
        "session_id": conv_id,
        "sandbox": agent.config.sandbox_mode,
        "approval": agent.config.approval_policy,
        "api_key": f"{_api_key}…" if _api_key else "（未设置）",
        "restored": bool(conv),
    })
    if conv:
        # 恢复对话的全部记录
        for m in conv.get("messages", []):
            agent.memory.add_message(m.get("role", "user"), m.get("content", ""))
        agent.last_execution_summary = conv.get("last_summary", "")
        agent._restore_conversation_model(conv)   # 自动切回该对话绑定的模型
        print_info(
            f"已恢复对话 {conv_id}（{len(conv.get('messages', []))} 条记录，"
            f"标题: {conv.get('title', '')[:40]}）",
            style="accent",
        )
    else:
        print_info(
            f"对话 ID: {conv_id}（下次用 --session {conv_id} 或 /open {conv_id} 恢复全部记录）",
            style="info",
        )

    team = Team(tool_manager=agent.tool_manager) if enable_team else None

    first_turn = True
    while True:
        # 上一轮回答与下一轮提问之间的弱分割线
        if not first_turn:
            c.print()
            c.print("[dim]┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄[/dim]")
            c.print()

        def _ask(prompt_text: str) -> str:
            return _AgentPrompt(prompt_text, console=c)()

        try:
            # 粘贴感知的多行读取：粘贴整块合并为一条；行尾 \ 续行
            goal = input_reader.read_goal(
                prompt_primary=lambda: _ask("[bold white]>[/bold white] "),
                prompt_continuation=lambda: _ask("[dim]… [/dim]"),
            )
        except (EOFError, KeyboardInterrupt):
            print_goodbye(use_rich=True)
            break
        if goal is None:
            print_goodbye(use_rich=True)
            break
        if "\n" in goal:
            print_info(f"已接收多行输入（{goal.count(chr(10)) + 1} 行）", style="info")

        if not goal:
            continue
        first_turn = False

        if goal.lower() in ("exit", "quit", "q"):
            print_goodbye(use_rich=True)
            break

        # 斜杠命令解析（容忍 /team任务 连写）
        cmd, cmd_args = _parse_command(goal)
        if cmd == "tools":
            from agent.ui_theme import print_step_header
            c.print()
            print_step_header(0, 1, "工具列表", use_rich=True)
            c.print(agent.tool_manager.get_tools_description())
            continue
        if cmd == "sessions":
            convs = store.list_conversations()
            if not convs:
                print_info("暂无历史对话。", style="info")
            else:
                print_info(f"历史对话（{len(convs)} 个）:", style="primary")
                for cv in convs:
                    mark = "●" if cv["id"] == conv_id else " "
                    c.print(
                        f"[dim]  {mark} {cv['id']}  {cv['title'][:36]}  "
                        f"{cv['count']} 条 · {cv['updated_at'][:16].replace('T', ' ')}[/dim]"
                    )
            continue
        if cmd == "open":
            if not cmd_args:
                print_warning("用法: /open <对话ID>", use_rich=True)
                continue
            target = store.load_conversation(cmd_args.strip())
            if target is None:
                print_warning(f"对话不存在: {cmd_args.strip()}（用 /sessions 查看全部）", use_rich=True)
                continue
            # 切换：当前对话已在每轮自动保存；载入目标对话的全部记录
            conv_id = cmd_args.strip()
            agent.config.session_name = conv_id
            agent.memory.clear_session()
            for m in target.get("messages", []):
                agent.memory.add_message(m.get("role", "user"), m.get("content", ""))
            agent.last_execution_summary = target.get("last_summary", "")
            agent._restore_conversation_model(target)   # 恢复该对话绑定的模型
            print_info(
                f"已打开对话 {conv_id}（{len(target.get('messages', []))} 条记录）",
                style="accent",
            )
            for m in target.get("messages", [])[-4:]:
                who = "你" if m.get("role") == "user" else "小悟"
                c.print(f"[dim]  {who}: {m.get('content', '')[:80]}[/dim]")
            continue
        if cmd == "new":
            conv_id = generate_conversation_id()
            agent.config.session_name = conv_id
            agent.memory.clear_session()
            agent.last_execution_summary = ""
            print_info(f"新对话已开始: {conv_id}", style="accent")
            continue
        if cmd == "model":
            if not cmd_args:
                print_info(
                    f"当前模型: {agent.llm.default_model} @ {agent.llm.client.base_url}",
                    style="accent",
                )
                print_info("切换: /model <模型名> [@<API地址>]（本次会话生效，不写入 .env）", style="info")
                continue
            # 支持 /model name 或 /model name@base_url
            target = cmd_args.strip()
            model_name = target
            base_url = None
            if "@" in target:
                model_name, base_url = target.split("@", 1)
                model_name, base_url = model_name.strip(), base_url.strip()
            try:
                info = agent.switch_model(model=model_name or None, base_url=base_url)
                # 持久化绑定：该对话从此记住这个模型（恢复时自动切回）
                try:
                    msgs = [
                        {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}
                        for m in agent.memory.conversation_history
                    ]
                    store.save_conversation(
                        conv_id, messages=msgs,
                        last_summary=agent.last_execution_summary,
                        model=info["model"], base_url=info["base_url"],
                    )
                except Exception:
                    pass
                print_info(
                    f"已切换模型: {info['model']} @ {info['base_url']}（临时生效，已绑定当前对话）",
                    style="accent",
                )
            except Exception as e:
                print_warning(f"切换失败: {str(e)[:120]}", use_rich=True)
            continue
        if cmd == "config":
            from config import LLM_CONFIG, APPROVAL_CONFIG, GUARDIAN_CONFIG, VISION_CONFIG
            key = (agent.llm.client.api_key or "")[:6]
            masked = f"{key}…" if key else "（未设置）"
            print_info("当前生效配置:", style="primary")
            c.print(f"[dim]  主模型    {agent.llm.default_model} @ {agent.llm.client.base_url}[/dim]")
            c.print(f"[dim]  主 key     {masked}[/dim]")
            c.print(f"[dim]  视觉      {VISION_CONFIG.get('vision_model') or LLM_CONFIG.get('default_model')} @ {VISION_CONFIG.get('base_url')}[/dim]")
            c.print(f"[dim]  Guardian  {'开' if GUARDIAN_CONFIG.get('enabled') else '关'}"
                    f"{' · ' + (GUARDIAN_CONFIG.get('model') or '') if GUARDIAN_CONFIG.get('model') else ''}[/dim]")
            c.print(f"[dim]  审批/沙箱 {APPROVAL_CONFIG.get('approval_policy')} / {APPROVAL_CONFIG.get('sandbox_mode')}[/dim]")
            c.print(f"[dim]  对话 ID   {conv_id}[/dim]")
            c.print("[dim]  临时覆盖方式: --model/--base-url/--api-key 参数，或 /model 命令[/dim]")
            continue
        if cmd == "team":
            c.print()
            if not cmd_args:
                print_warning("用法: /team <任务>", use_rich=True)
            else:
                run_team(cmd_args)
            continue
        if cmd == "research":
            c.print()
            if not cmd_args:
                print_warning("用法: /research <主题>", use_rich=True)
            else:
                run_research(cmd_args)
            continue
        if cmd == "image":
            # 文生图（SenseNova U1.5 Lite）：生成图片并保存到本地
            c.print()
            if not cmd_args:
                print_warning("用法: /image <图片描述>（构图/风格/光影写清楚）", use_rich=True)
                continue
            from tools.image_gen import ImageGenTool
            _r = ImageGenTool().execute(cmd_args)
            if _r.success:
                c.print(f"[success]✓[/success] [muted]{_r.output}[/muted]")
            else:
                print_warning(_r.error, use_rich=True)
            continue
        if cmd == "memory":
            # 记忆总览与清理（summary / prune_long_term）
            c.print()
            if cmd_args.strip().lower().startswith("prune"):
                parts = cmd_args.strip().split()
                try:
                    keep = int(parts[1]) if len(parts) > 1 else 100
                except ValueError:
                    print_warning("用法: /memory prune <保留条数>（默认 100）", use_rich=True)
                    continue
                removed = agent.memory.prune_long_term(keep=keep)
                print_info(f"已清理长期记忆 {removed} 条（保留最新 {keep} 条）", style="success")
                continue
            s = agent.memory.summary()
            print_info("记忆总览:", style="primary")
            for label, key in [
                ("对话历史", "conversation_history"),
                ("步骤历史", "step_history"),
                ("长期记忆", "long_term_memory"),
                ("经验库", "experiences"),
                ("失败模式", "failure_patterns"),
                ("策略库", "strategies"),
            ]:
                c.print(f"[dim]  {label:<6} {s[key]} 条[/dim]")
            c.print(f"[dim]  完成步骤 {s['completed_steps']} · 失败步骤 {s['failed_steps']}[/dim]")
            c.print("[dim]  清理长期记忆: /memory prune 100[/dim]")
            continue
        if cmd == "compact":
            # 手动压缩对话历史（旧部分 LLM 总结成摘要）
            removed = agent._compact_history()
            if removed:
                print_info(f"已压缩 {removed} 条历史为摘要。", style="success")
            else:
                print_info("对话历史未超阈值或压缩失败，保持原样。", style="info")
            continue
        if cmd == "goal":
            # 查看 / 设置当前会话持久目标
            from agent.goal import GoalStore
            store = GoalStore()
            if not cmd_args:
                g = store.get(agent.config.session_name or "")
                if g:
                    print_info(f"当前目标: {g}", style="accent")
                else:
                    print_info("当前会话未设置目标。用法: /goal <目标>", style="info")
            else:
                store.set(agent.config.session_name or "", cmd_args.strip())
                print_info(f"目标已保存: {cmd_args.strip()[:80]}", style="success")
            continue
        if cmd == "help":
            c.print()
            print_info("可用命令:", style="primary")
            for line in [
                "/team <任务>        团队协作模式",
                "/research <主题>     深度研究",
                "/image <描述>        文生图（SenseNova U1.5 Lite）",
                "/memory [prune N]    记忆总览 / 清理长期记忆",
                "/tools              查看全部工具（含 JSON Schema）",
                "/sessions           历史对话列表",
                "/open <对话ID>       打开并恢复历史对话",
                "/new                新开一个对话",
                "/model [名@地址]     查看 / 切换模型（绑定当前对话）",
                "/config             查看当前生效配置",
                "/compact            手动压缩对话历史（超长时减少上下文占用）",
                "/goal [内容]         查看 / 设置当前会话持久目标",
                "/help               本帮助",
                "exit / q            退出",
            ]:
                c.print(f"[dim]  {line}[/dim]")
            c.print("[dim]  多行输入: 直接粘贴整块提交；行尾 [white]\\\\[/white] + 回车 = 手工换行[/dim]")
            continue
        if goal.startswith("/"):
            # 打错的斜杠命令（如 /resrarch）给出提示与最近命令建议
            import difflib
            word = goal.split()[0]
            matches = difflib.get_close_matches(word, ["/team", "/research", "/tools", "/sessions", "/open", "/new", "/model", "/config", "/image", "/memory", "/help"], n=1, cutoff=0.6)
            hint = f"你是不是想输入 {matches[0]}？" if matches else ""
            print_warning(
                f"未知命令: {goal[:40]}。{hint}可用命令: /team <任务> · /research <主题> · /image <描述> · /memory · /help · /tools · /sessions · /open <ID> · /new · exit"
            )
            continue

        c.print()
        try:
            if auto_mode and not enable_team:
                # 意图自动路由：调研/报告类 → 深度研究；团队/并行类 → 团队协作
                from agent.mode_router import route_goal
                routed = route_goal(goal)
                if routed == "research":
                    print_info("自动路由: 深度研究模式", style="accent")
                    run_research(goal)
                    continue
                if routed == "team":
                    print_info("自动路由: 团队协作模式", style="accent")
                    run_team(goal)
                    continue
            if enable_team and team:
                result = team.run(goal)
                print_final_result(result.final_answer)
            else:
                agent.run(goal, keep_session=True)
        except KeyboardInterrupt:
            # Ctrl+C 中断当前任务：回到提示符继续会话，而不是整个程序崩溃
            c.print()
            print_warning("已中断当前任务，回到输入。", use_rich=True)


def list_tools():
    """列出全部工具（含 JSON Schema 摘要）。"""
    import json
    tm = ToolManager()
    print(f"已注册 {len(tm.list_tools())} 个工具:\n")
    for schema in tm.list_openai_schemas():
        fn = schema["function"]
        required = fn["parameters"].get("required", [])
        props = fn["parameters"].get("properties", {})
        param_summary = ", ".join(
            f"{k}{'*' if k in required else ''}:{v.get('type', '?')}"
            for k, v in props.items()
        )
        print(f"■ {fn['name']}  ({param_summary})")
        print(f"  {fn['description'][:200]}")
        print()
    mcp_servers = tm.list_mcp_servers()
    if mcp_servers:
        print(f"已连接 MCP 服务器: {', '.join(mcp_servers)}")


def run_mcp_server():
    """以 MCP server 模式运行。"""
    from agent.mcp_server import run_server
    run_server(transport="stdio")


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器（独立函数便于测试）。"""
    parser = argparse.ArgumentParser(
        description="my_agent - AI Agent with Team/Research/Dashboard/Safety/MCP",
    )
    parser.add_argument("goal", nargs="?", help="任务目标描述")
    parser.add_argument("-i", "--interactive", action="store_true", help="交互模式")
    parser.add_argument("-r", "--resume", action="store_true",
                        help="恢复最近一次对话")
    parser.add_argument("-q", "--quiet", action="store_true", help="安静模式")
    parser.add_argument("--max-steps", type=int, default=20, help="最大步骤数")
    parser.add_argument("--max-replans", type=int, default=3, help="最大重规划次数")
    parser.add_argument("--team", action="store_true", help="团队协作模式")
    parser.add_argument("--research", action="store_true", help="深度研究模式")
    parser.add_argument("--auto-mode", action="store_true",
                        help="按意图自动路由：调研/报告类 → 深度研究；团队/并行类 → 团队模式；其余单循环")
    parser.add_argument("--dashboard", action="store_true", help="启动Web监控面板")
    parser.add_argument("--desktop", action="store_true",
                        help="原生桌面 GUI（Tkinter 窗口，非 Web）")
    parser.add_argument("--dash-port", type=int, default=8080, help="Dashboard端口")
    parser.add_argument("--dash-host", default="127.0.0.1",
                        help="Dashboard 绑定地址（默认 127.0.0.1 仅本机；局域网访问用 0.0.0.0）")
    parser.add_argument("--doctor", action="store_true",
                        help="依赖/浏览器/git/.env/模型连通等环境自检")
    parser.add_argument("--no-vision", action="store_true", help="关闭视觉感知")

    # ---- v2：安全 / 追踪 / 会话 ----
    parser.add_argument(
        "--approval", default=None,
        choices=["untrusted", "on-failure", "on-request", "never"],
        help="审批策略（默认取 APPROVAL_POLICY 环境变量 / on-failure）",
    )
    parser.add_argument(
        "--dangerously-skip-permissions", dest="approval",
        action="store_const", const="never",
        help="等价于 --approval never（主流 CLI 同名参数的兼容别名）",
    )
    parser.add_argument(
        "--sandbox", default=None,
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="沙箱等级（默认取 SANDBOX_MODE 环境变量 / workspace-write）",
    )
    parser.add_argument("--guardian", dest="guardian", action="store_true",
                        default=None, help="启用 Guardian 安全审校")
    parser.add_argument("--no-guardian", dest="guardian", action="store_false",
                        help="关闭 Guardian 安全审校")
    parser.add_argument("--no-rollout", action="store_true", help="关闭 Rollout 事件追踪")
    parser.add_argument("--max-step-ops", type=int, default=None,
                        help="计划模式下单步骤内最大工具操作数（默认 12）")
    parser.add_argument("--session", default="", help="会话名（保存/恢复对话历史）")

    # ---- v3.1：执行模式 ----
    exec_group = parser.add_mutually_exclusive_group()
    exec_group.add_argument("--loop", dest="exec_mode", action="store_const",
                            const="loop", help="单循环模式（默认）：一次对话完成整个目标")
    exec_group.add_argument("--plan", dest="exec_mode", action="store_const",
                            const="plan", help="经典计划模式：规划→逐步执行→总结")
    parser.set_defaults(exec_mode="loop")
    parser.add_argument("--max-ops", type=int, default=None,
                        help="单循环模式整次任务最大操作轮数（默认 40）")
    parser.add_argument("--no-stream", action="store_true",
                        help="关闭流式输出（答案整段返回而非逐字渲染）")

    # ---- v3.5：临时模型覆盖（仅本次运行生效，不写入 .env）----
    parser.add_argument("--model", default=None, help="临时切换主模型")
    parser.add_argument("--base-url", default=None, help="临时切换 API 地址")
    parser.add_argument("--api-key", default=None, help="临时切换 API key")

    # ---- MCP ----
    parser.add_argument("--mcp-server", action="store_true",
                        help="以 MCP server 运行（stdio）")
    parser.add_argument("--list-tools", action="store_true", help="列出全部工具")

    # ---- Skills 技能包安装 / 更新（带完整性校验） ----
    parser.add_argument("--skills-install", metavar="SOURCE_DIR", default=None,
                        help="安装/更新技能包：SOURCE_DIR 为技能包目录（含 SKILL.md 的技能目录 + 可选 MANIFEST.sha256）")
    parser.add_argument("--skills-target", choices=["project", "user"], default="project",
                        help="技能安装目标（默认 project=项目级 SKILLS_DIR；user=用户级 ~/.my_agent/skills）")
    parser.add_argument("--skills-overwrite", action="store_true",
                        help="覆盖已存在的同名技能")
    parser.add_argument("--skills-allow-unsigned", action="store_true",
                        help="允许安装未签名（无 MANIFEST.sha256）的技能包")
    return parser


def run_skills_install(args) -> int:
    """安装/更新技能包（CLI 入口）。

    打印每个技能的安装结果；全部安装成功返回 0，部分失败 / 整体失败返回 1。
    """
    from agent.skills import SkillPackManager
    from config import SKILLS_CONFIG

    target = (SKILLS_CONFIG["user_dir"] if args.skills_target == "user"
              else SKILLS_CONFIG["project_dir"])
    mgr = SkillPackManager()
    result = mgr.install_pack(
        args.skills_install, target,
        overwrite=args.skills_overwrite,
        allow_unsigned=args.skills_allow_unsigned,
    )

    for r in result.results:
        line = f"  [{r.status}] {r.name}"
        if r.reason:
            line += f" — {r.reason}"
        print(line)
    for reason in result.reasons:
        print(f"  [failed] {reason}")

    label = {"success": "成功", "partial": "部分失败", "failed": "失败"}[result.status]
    unsigned_note = result.unsigned and any("未签名" in r for r in result.reasons)
    note = "（未签名包，已拒绝）" if unsigned_note else ""
    print(f"技能包安装结果: {label}{note}")
    return 0 if result.ok else 1


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Windows conhost 防卡：QuickEdit 选区会永久阻塞 stdout（看 MY_AGENT_DISABLE_QUICKEDIT）
    try:
        from agent.console_guard import disable_quickedit_if_enabled
        disable_quickedit_if_enabled()
    except Exception:
        pass

    # -r / --resume：恢复最近一次对话
    if args.resume and not args.session:
        try:
            from agent.session import SessionStore
            convs = SessionStore().list_conversations()
            if convs:
                args.session = convs[0]["id"]
                print(f"恢复最近对话: {args.session}（{convs[0].get('title', '')[:30]}）")
            else:
                print("暂无历史对话，开启新对话。")
        except Exception:
            pass

    # 环境自检（my-agent --doctor）
    if args.doctor:
        from agent.doctor import run_doctor, render_report
        print(render_report(run_doctor(include_llm=True)))
        return

    # 启动轻量预警（只查快检项：.env / git / websockets，静默通过，失败给一行修复提示）
    try:
        from agent.doctor import _check_env, _check_git, _check_ws_support
        from agent.ui_theme import print_warning
        for _fn in (_check_env, _check_git, _check_ws_support):
            _r = _fn()
            if not _r["ok"]:
                print_warning(f"{_r['name']}: {_r['message']} → {_r.get('hint', '')}")
    except Exception:
        pass

    # MCP server 模式（优先级最高）
    if args.mcp_server:
        run_mcp_server()
        return

    if args.list_tools:
        list_tools()
        return

    # Skills 技能包安装 / 更新（带完整性校验）
    if args.skills_install:
        return run_skills_install(args)

    # 安全参数默认值
    if args.approval is None or args.sandbox is None:
        from config import APPROVAL_CONFIG
        if args.approval is None:
            args.approval = APPROVAL_CONFIG["approval_policy"]
        if args.sandbox is None:
            args.sandbox = APPROVAL_CONFIG["sandbox_mode"]
    if args.guardian is None:
        from config import GUARDIAN_CONFIG
        args.guardian = GUARDIAN_CONFIG["enabled"]

    config = build_config(args)

    # 桌面 GUI（原生 Tkinter 窗口）
    if args.desktop:
        start_desktop_app()
        return

    # 自动路由（--auto-mode / AUTO_MODE=true）：按意图把目标路由到 research/team
    if not args.auto_mode:
        args.auto_mode = os.getenv("AUTO_MODE", "false").lower() == "true"
    if args.auto_mode and args.goal and not args.team and not args.research:
        from agent.mode_router import route_goal
        routed = route_goal(args.goal)
        if routed == "research":
            args.research = True
            print("· 自动路由: 深度研究模式")
        elif routed == "team":
            args.team = True
            print("· 自动路由: 团队协作模式")

    # Dashboard：浏览器版控制面板已禁用（web 端停用）。
    # 桌面端（Electron）由主进程自行 spawn `python -m dashboard.server` 做后端，
    # 与这里的 --dashboard 无关；--dashboard 仅保留参数兼容，不再拉起服务。
    if args.dashboard:
        print("  [Dashboard] 浏览器版控制面板已禁用（web 端已停用）。")
        print("  [Dashboard] 如需浏览器版页面，设置 MY_AGENT_WEB_UI=1 后启动服务。")

    if args.research and args.goal:
        run_research(args.goal)
    elif args.team and args.goal:
        run_team(args.goal)
    elif args.interactive or not args.goal:
        run_interactive(enable_team=args.team, auto_mode=args.auto_mode, config=config)
    elif args.goal:
        if args.team:
            run_team(args.goal)
        elif args.research:
            run_research(args.goal)
        else:
            run_once(
                goal=args.goal,
                verbose=not args.quiet,
                enable_vision=not args.no_vision,
                config=config,
            )


if __name__ == "__main__":
    sys.exit(main())
