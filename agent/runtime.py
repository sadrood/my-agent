"""
外部 Agent Runtime：把外部 CLI / 自定义命令当作执行引擎（多 Runtime 思路借鉴同类实现）。

桌面端 /api/run 派发到所选 runtime；外部 CLI 以子进程启动，
stdout 逐行桥接为 stream_delta 事件，结束时 emit answer + run_end。
内置 Agent（myagent）不在这里——那是 Python 单循环，走原有路径。
"""
import os
import shutil
import subprocess
import threading
import time

RUNTIMES = {
    "myagent": "内置 Agent（Python 单循环）",
    "claude": "外部 CLI（文本输出）",
    "claude-acp": "外部 CLI · ACP（结构化事件）",
    "codex": "外部 CLI（JSON 事件流）",
    "custom": "自定义命令模板",
}


def detect_available() -> dict:
    """检测外部 CLI 是否安装（前端据此禁用不可用的引擎）。"""
    return {
        "claude": shutil.which("claude") is not None,
        "claude-acp": shutil.which("claude-agent-acp") is not None or shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None,
        "dsh": shutil.which("dsh") is not None,
    }


def _build_argv(runtime: str, goal: str, cfg: dict | None) -> tuple:
    """构建 (argv, use_shell)。外部 CLI 用参数数组；custom 用命令模板字符串。"""
    if runtime == "claude":
        return ["claude", "-p", goal, "--output-format", "json", "--verbose"], False
    if runtime == "codex":
        return ["codex", "exec", "--json", goal], False
    if runtime == "custom":
        cmd = (cfg or {}).get("command") or ""
        if not cmd:
            raise ValueError("自定义运行时需要配置 command 模板（含 {goal}）")
        return cmd.format(goal=goal), True   # 用户模板按 shell 执行
    raise ValueError(f"未知运行时: {runtime}")


def _resolve_cmd(argv: list) -> tuple:
    """Windows 下 .cmd/.bat 须经 cmd.exe 执行（CreateProcess 不认外部 CLI 的 .cmd 包装之类）。"""
    if os.name != "nt" or not argv:
        return argv, False
    resolved = shutil.which(argv[0]) or argv[0]
    if resolved.lower().endswith((".cmd", ".bat")):
        cmdline = subprocess.list2cmdline([resolved] + argv[1:])
        return cmdline, True
    return [resolved] + argv[1:], False


def _kill_tree(proc) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_external(runtime: str, goal: str, cwd: str | None, on_event,
                 stop_event=None, cfg: dict | None = None) -> str:
    """启动外部 CLI 执行任务，把输出桥接为事件。返回最终文本。"""
    if runtime == "claude-acp":
        # 完整 ACP 协议：与外部 agent 双向结构化通信（文本/思考/工具/审批）。
        # 模型默认用网关实际服务的 deepseek-v4-flash（避免网关未知模型 503）；
        # 可用 MY_AGENT_ACP_MODEL 或 runtime_config.model 覆盖。
        from agent.acp import run_acp_session
        model = (cfg or {}).get("model") or os.getenv("MY_AGENT_ACP_MODEL") or "deepseek-v4-flash"
        return run_acp_session(goal, cwd or os.getcwd(), on_event, stop_event, model=model)

    try:
        argv, use_shell = _build_argv(runtime, goal, cfg)
        if not use_shell:
            # 外部 CLI：Windows 下可能解析成 .cmd，需经 cmd.exe
            argv, use_shell = _resolve_cmd(argv)
    except Exception as e:
        on_event("run_start", {"goal": goal, "runtime": runtime, "mode": "external"})
        on_event("answer", {"output": f"运行时配置错误: {str(e)[:200]}"})
        on_event("run_end", {"status": "failed", "error": str(e)[:200]})
        return str(e)

    on_event("run_start", {"goal": goal, "runtime": runtime, "mode": "external"})
    try:
        kwargs = dict(
            shell=use_shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            cwd=cwd or os.getcwd(),
            env=os.environ,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(argv, **kwargs)
        # 外部 CLI 以参数传 prompt，不读 stdin——关掉避免它等 stdin 3s 出告警
        if runtime != "custom":
            try:
                proc.stdin.close()
            except Exception:
                pass
    except Exception as e:
        on_event("answer", {"output": f"启动失败: {str(e)[:200]}"})
        on_event("run_end", {"status": "failed", "error": str(e)[:200]})
        return str(e)

    chunks: list = []
    cancelled = False

    def _pump():
        try:
            for line in iter(proc.stdout.readline, ""):
                chunks.append(line)
                try:
                    on_event("stream_delta", {"kind": "text", "text": line})
                except Exception:
                    pass
        except Exception:
            pass

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    while proc.poll() is None:
        if stop_event is not None and stop_event.is_set():
            cancelled = True
            _kill_tree(proc)
            break
        time.sleep(0.1)
    t.join(timeout=5)
    output = "".join(chunks).strip()

    if cancelled:
        on_event("answer", {"output": output or "(已停止)"})
        on_event("run_end", {"status": "stopped"})
        return output
    ok = proc.returncode == 0
    on_event("answer", {"output": output or "(无输出)"})
    on_event("run_end", {"status": "completed" if ok else "failed",
                         "error": "" if ok else f"退出码 {proc.returncode}"})
    return output
