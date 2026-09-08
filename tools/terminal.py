import re
"""
终端工具模块（v2：JSON Schema + 风险分级 + 后台任务）。

v2 变化：
- schema 提供结构化参数 {"command": string, "background": bool}
- 风险分级委托给 agent.approval.CommandSafety（low/medium/high/blocked）
- 硬性黑名单在工具层直接拒绝（blocked），其余交由上层审批策略
- execute_json() 支持 {"command": "..."} 结构化调用
- 后台任务：background=true 启动长任务/服务，bg list/output/kill 管理
"""
import os
import subprocess
import platform
import tempfile
import threading
import time
from typing import Any, Dict

from tools.base import BaseTool, ToolResult
from tools.terminal_session import TerminalSessionManager

_IS_WINDOWS = platform.system() == "Windows"

# 沙箱（AppContainer）内「程序/资源不可访问」类失败的识别特征：
# 命中即回喂引导文案，避免模型在沙箱限制下反复盲试同一类命令
_SANDBOX_DENIED_MARKERS = (
    "拒绝访问", "access is denied", "access denied", "winerror 5", "0x80070005",
)


def _foreground_timeout() -> float:
    """终端前台命令超时（秒），读 TOOL_CONFIG.terminal_fg_timeout。"""
    from config import TOOL_CONFIG
    try:
        return float(TOOL_CONFIG.get("terminal_fg_timeout", 120))
    except Exception:
        return 120.0


def _sandbox_denied(outcome) -> bool:
    """沙箱命令输出是否为「拒绝访问」类失败。"""
    combined = f"{outcome.stdout}\n{outcome.stderr}\n{outcome.error}".lower()
    return any(marker in combined for marker in _SANDBOX_DENIED_MARKERS)


def _decode_console(data):
    """控制台输出解码：UTF-8 优先，失败回退 GBK（Windows cmd 中文）。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk", errors="replace")
    except Exception:
        return data.decode("utf-8", errors="replace")

def _win_unix_shim(command: str) -> str:
    """Windows cmd 下把常见 unix 单命令翻译成 PowerShell 等价物（降低命令不存在类失败）。
    覆盖 head/tail/sleep/grep，并剥离 'bg ' 前缀与尾部 ' &'（后台意图）。
    """
    if not _IS_WINDOWS:
        return command
    cmd = command.strip()
    low = cmd.lower()
    # 剥离尾部 " &"（cmd 无后台符；前台执行原命令，避免 '&' 语法错误）
    if low.endswith(" &"):
        cmd = cmd[:-2].rstrip()
        low = cmd.lower()
    # 'bg cmd...' 前缀：脱掉后按前台命令处理（要真后台请用 background=true）
    if low.startswith("bg "):
        cmd = cmd[3:].strip()
        low = cmd.lower()
    m = re.match(r"^sleep\s+([0-9.]+)\s*$", low)
    if m:
        return 'powershell -NoProfile -Command "Start-Sleep -Seconds %s"' % float(m.group(1))
    # head -n N file / head file
    if low.startswith("head "):
        m = re.match(r"^head\s+(-n\s*|-n(\d+)\s*)?(-?(\d+)\s*)?(.+)$", low)
        if m:
            n = m.group(2) or m.group(4) or "10"
            path = m.group(5).strip()
            return "powershell -NoProfile -Command \"Get-Content '%s' -TotalCount %s\"" % (path.replace("'", "''"), n)
    # tail -f file / tail -n N file / tail -N file / tail -Nfile
    if low.startswith("tail "):
        m = re.match(r"^tail\s+(-f\s+|-n\s*|-n(\d+)\s*)?(-?(\d+)\s*)?(.+)$", low)
        if m:
            n = m.group(2) or m.group(4) or "10"
            path = m.group(5).strip()
            follow = " -Wait" if (cmd.split()[1:2] == ["-f"]) else ""
            return "powershell -NoProfile -Command \"Get-Content '%s' -Tail %s%s\"" % (path.replace("'", "''"), n, follow)
    # grep PATTERN FILE → Select-String 基础用法
    m = re.match(r"^grep\s+(-[a-zA-Z]+\s+)*['\"]?([^'\"]+)['\"]?\s+([^\s>|&]+)\s*$", low)
    if m:
        pat, path = m.group(2), m.group(3)
        return "powershell -NoProfile -Command \"Select-String -Path '%s' -Pattern '%s'\"" % (path.replace("'", "''"), pat.replace("'", "''"))
    return cmd

    return command


class TerminalTool(BaseTool):
    """终端命令执行工具（前台同步执行 + 后台任务管理）。"""

    risk_level: str = "medium"            # 具体风险在 execute 内按命令细化
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"

    def __init__(self):
        # 后台任务注册表: job_id -> {proc, command, out_path, started}
        self._jobs: Dict[str, dict] = {}
        # 前台正在运行的 Popen，供 terminate_current() 跨线程终止
        self._proc_lock = threading.Lock()
        self._active_proc = None
        # 由执行器/上层通过 set_stop_event() 注入；置位后前台命令尽快退出
        self._stop_event = None
        # 持久终端会话（session=true 时跨调用保持 cwd/env）
        self._sessions = TerminalSessionManager()
        self._session_key = ""

    def set_session_key(self, key: str) -> None:
        """注入会话 id（由 Agent 按会话调用），持久终端按会话隔离。"""
        self._session_key = key or ""

    def close_all_sessions(self) -> None:
        """关闭所有持久终端会话（Agent 清理时调用）。"""
        if self._sessions:
            self._sessions.close_all()

    def set_stop_event(self, event) -> None:
        """绑定外部停止信号（threading.Event），前台命令执行期间轮询检查。"""
        self._stop_event = event

    def terminate_current(self) -> bool:
        """终止当前正在运行的前台命令（含其子进程树）。无可终止进程时返回 False。

        先杀进程组（Windows 用 taskkill /T /F，POSIX 用 killpg），
        失败降级 proc.terminate()，超时再 proc.kill()。
        """
        with self._proc_lock:
            proc = self._active_proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            if _IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=15,
                )
            else:
                import signal
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return True

    @property
    def name(self) -> str:
        return "terminal"

    @property
    def description(self) -> str:
        return (
            "终端命令执行工具。可以在系统终端中运行命令行指令。"
            "支持查看文件、安装依赖、运行脚本、查询系统信息等。\n"
            "持久会话（session=true）：在常驻终端里执行，cd/set/激活环境跨调用保留；"
            "适合需要保持目录/环境的连续操作：\n"
            "  session start         启动持久终端\n"
            "  session status        查看当前 cwd 与状态\n"
            "  session stop          关闭持久终端\n"
            "  session clear         清空会话输出\n"
            "长任务/服务用 background=true 后台运行（立即返回任务 ID）：\n"
            "  bg list            列出后台任务\n"
            "  bg output <任务ID> [N]  查看任务最近 N 行输出（默认 20）\n"
            "  bg kill <任务ID>    结束后台任务（输出仍可查看）\n"
            "注意：删除、格式化、强制推送等危险命令会被安全策略拦截或要求人工确认；"
            "无法确定安全的命令尽量改用 python / file 工具完成。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的完整终端命令",
                },
                "background": {
                    "type": "boolean",
                    "description": "true = 后台运行（长任务/服务/测试），立即返回任务 ID，"
                                   "用 bg output / bg kill 管理",
                },
                "session": {
                    "type": "boolean",
                    "description": "true = 在持久终端会话里执行（cd/env 跨调用保留）；"
                                   "false = 一次性执行（默认）。需先 session start",
                },
                "session_op": {
                    "type": "string",
                    "enum": ["status", "start", "stop", "clear"],
                    "description": "持久终端管理：status 查看 cwd、start 启动、stop 关闭、clear 清屏",
                },
            },
            "required": ["command"],
        }

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        command = str(arguments.get("command", "")).strip()
        background = bool(arguments.get("background"))
        session = bool(arguments.get("session"))
        session_op = str(arguments.get("session_op", "")).strip()
        if session_op:
            return self._session_op(session_op)
        if session:
            return self._session_run(command)
        return self._run_command(command, background=background)

    def build_approval_request(self, arguments: Dict[str, Any]):
        from agent.approval import ApprovalRequest, CommandSafety

        command = str(arguments.get("command", "")).strip()
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=command,
            risk_level=CommandSafety.classify(command),
            min_sandbox_mode=self.min_sandbox_mode,
        )

    def is_parallel_safe(self, arguments: Dict[str, Any]) -> bool:
        # 低风险（查询/只读类）命令可并行；可能改状态的中高风险命令保持串行
        from agent.approval import CommandSafety
        return CommandSafety.classify(str(arguments.get("command", "") or "")) == "low"

    # ================================================================
    # 执行入口
    # ================================================================

    def execute(self, input_str: str) -> ToolResult:
        command = input_str.strip()
        if not command:
            return ToolResult(success=False, output="", error="命令为空。")
        if command.lower().startswith("session "):
            return self._session_op(command[len("session "):].strip())
        if command.lower().startswith("bg "):
            return self._bg_dispatch(command[3:].strip())
        return self._run_command(command, background=False)

    # ================================================================
    # 持久终端会话（session=true）
    # ================================================================

    def _session_op(self, args: str) -> ToolResult:
        parts = args.split()
        op = parts[0].lower() if parts else "status"
        key = self._session_key
        if op == "start":
            if not key:
                return ToolResult(success=False, output="", error="会话 key 未设置（Agent 未注入会话 id）。")
            s = self._sessions.start(key)
            st = s.state()
            return ToolResult(
                success=True,
                output=(f"持久终端已启动（cwd: {st.get('cwd') or '?'}）。"
                        "后续命令用 session=true 在该会话执行，cd/环境变量将保留。"),
            )
        if op == "status":
            s = self._sessions.get(key) if key else None
            if s is None:
                return ToolResult(success=True,
                                  output="持久终端未启动。用 session start 启动（cd/set/activate 将跨调用保留）。")
            st = s.state()
            return ToolResult(success=True,
                              output=f"持久终端运行中（cwd: {st.get('cwd') or '?'}）。")
        if op == "stop":
            stopped = self._sessions.stop(key) if key else False
            return ToolResult(success=True,
                              output="持久终端已关闭。" if stopped else "持久终端未运行。")
        if op == "clear":
            if key:
                self._sessions.stop(key)
                self._sessions.start(key)
            return ToolResult(success=True, output="会话输出已清空（重启会话）。")
        return ToolResult(success=False, output="",
                          error="用法: session status|start|stop|clear")

    def _session_run(self, command: str) -> ToolResult:
        if _IS_WINDOWS:
            command = _win_unix_shim(command)
        key = self._session_key
        if not key:
            return ToolResult(success=False, output="",
                              error="会话 key 未设置。请先 terminal session start。")
        s = self._sessions.start(key)
        ok, out = s.run(command, timeout=_foreground_timeout())
        if ok:
            return ToolResult(success=True, output=out)
        return ToolResult(success=False, output="", error=out)



    def _run_command(self, command: str, background: bool = False) -> ToolResult:
        # 安全检查基于原始命令（黑名单先于翻译，防止翻译产物绕过审查）
        from agent.approval import CommandSafety
        risk = CommandSafety.classify(command)
        if risk == "blocked":
            return ToolResult(
                success=False,
                output="",
                error=f"安全限制：命令 '{command[:100]}' 命中硬性黑名单，已拒绝执行。",
            )
        # Windows：unix 单命令 → PowerShell 等价翻译（head/tail/sleep/grep，
        # bg 前缀与尾 ' &' 剥离），覆盖前台/后台与会话路径，减少
        # 「不是内部或外部命令」类失败
        if _IS_WINDOWS:
            command = _win_unix_shim(command)
        if background:
            return self._start_background(command)
        # OS 级沙箱（可选，SANDBOX_EXECUTION=appcontainer）：前台命令进
        # Windows AppContainer 执行；fail-closed，沙箱失败不回退明文执行。
        from agent.sandbox import sandbox_enabled
        if sandbox_enabled():
            return self._run_in_sandbox(command)
        return self._run_foreground(command)

    def _run_in_sandbox(self, command: str) -> ToolResult:
        """在 AppContainer 沙箱内执行前台命令（仅工作区可写、默认无网络）。"""
        from agent.sandbox import run_isolated

        outcome = run_isolated(command, workspace=os.getcwd())
        if outcome.returncode < 0 and outcome.error:
            # returncode=-1：容器创建/进程启动等沙箱机制本身失败 → 明确报错
            return ToolResult(
                success=False, output="",
                error=(f"沙箱执行失败：{outcome.error}\n"
                       f"如需恢复明文执行可设置 SANDBOX_EXECUTION=off。"),
            )

        output = outcome.stdout
        if outcome.stderr:
            output += "\n[stderr]\n" + outcome.stderr
        success = outcome.returncode == 0 and not outcome.error
        if success:
            return ToolResult(success=True, output=output.strip() or "(无输出)", error="")

        error = outcome.error or f"命令返回码: {outcome.returncode}"
        if _sandbox_denied(outcome):
            error += ("\n提示：当前为 AppContainer 沙箱执行——只能运行已授权的程序、"
                      "默认无网络、仅工作区可写。运行项目脚本请改用 python 工具，"
                      "或设置 SANDBOX_EXECUTION=off 关闭沙箱。")
        return ToolResult(success=False, output=output.strip() or "(无输出)", error=error)

    def _run_foreground(self, command: str) -> ToolResult:
        try:
            # Popen + 轮询：执行期间可响应停止信号（terminate_current）。
            # 进程组隔离确保 shell=True 时子进程树能被整棵杀掉。
            popen_kwargs = dict(
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if _IS_WINDOWS:
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(command, **popen_kwargs)
            with self._proc_lock:
                self._active_proc = proc

            # 增量读取：reader 线程逐行收集输出并推给回调（实时流），
            # 主线程保留原有轮询（响应停止信号/超时），不再用结尾 communicate
            stdout_chunks: list = []
            stderr_chunks: list = []
            cb = self._output_callback

            def _pump(pipe, sink):
                try:
                    # Windows cmd 默认 GBK：先按 UTF-8 解，失败回退 GBK（消除乱码）
                    for line in iter(pipe.readline, b""):
                        text = _decode_console(line)
                        sink.append(text)
                        if cb:
                            try:
                                cb(text)
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    try:
                        pipe.close()
                    except Exception:
                        pass

            t_out = threading.Thread(target=_pump, args=(proc.stdout, stdout_chunks), daemon=True)
            t_err = threading.Thread(target=_pump, args=(proc.stderr, stderr_chunks), daemon=True)
            t_out.start()
            t_err.start()

            cancelled = False
            timed_out = False
            fg_timeout = _foreground_timeout()
            try:
                deadline = time.time() + fg_timeout
                while proc.poll() is None:
                    if self._stop_event is not None and self._stop_event.is_set():
                        cancelled = True
                        self.terminate_current()
                        break
                    if time.time() > deadline:
                        timed_out = True
                        self.terminate_current()
                        break
                    time.sleep(0.1)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            finally:
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                with self._proc_lock:
                    self._active_proc = None

            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)

            if cancelled:
                return ToolResult(
                    success=False, output=(stdout or "").strip(),
                    error="已按要求停止：命令被终止（未执行完毕）。",
                )
            if timed_out:
                return ToolResult(
                    success=False, output=(stdout or "").strip(),
                    error=f"命令执行超时（{fg_timeout:.0f}秒），进程已终止。"
                          f"长任务请用 background=true 转后台运行。",
                )

            class _R:  # 保持下方既有逻辑的返回对象形状
                pass
            result = _R()
            result.stdout = stdout or ""
            result.stderr = stderr or ""
            result.returncode = proc.returncode

            output = result.stdout
            if result.stderr:
                output += "\n[stderr]\n" + result.stderr

            success = result.returncode == 0

            # 部分命令返回非零码但实际是信息性消息（如 mkdir 目录已存在）
            if not success and result.returncode == 1:
                combined = (result.stdout + result.stderr).lower()
                harmless_patterns = [
                    "already exists", "已经存在", "已存在",
                    "already installed", "已安装", "already satisfied",
                ]
                if any(p in combined for p in harmless_patterns):
                    success = True

            return ToolResult(
                success=success,
                output=output.strip() or "(无输出)",
                error="" if success else f"命令返回码: {result.returncode}",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False, output="",
                error=f"命令执行超时（{_foreground_timeout():.0f}秒）。"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    # ================================================================
    # 后台任务：启动 / 列表 / 输出 / 结束
    # ================================================================

    def _start_background(self, command: str) -> ToolResult:
        """后台启动命令：stdout/stderr 重定向到临时日志文件，立即返回任务 ID。"""
        try:
            job_id = f"job-{int(time.time() * 1000)}-{len(self._jobs) + 1}"
            out_path = os.path.join(tempfile.gettempdir(), f"my_agent_bg_{job_id}.log")
            f_out = open(out_path, "w", encoding="utf-8", errors="replace")
            if _IS_WINDOWS:
                proc = subprocess.Popen(
                    command, shell=True, stdout=f_out, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = subprocess.Popen(
                    command, shell=True, stdout=f_out, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"后台启动失败: {str(e)[:200]}")
        self._jobs[job_id] = {
            "proc": proc, "command": command, "out_path": out_path,
            "started": time.time(),
        }
        return ToolResult(
            success=True,
            output=(f"已启动后台任务 {job_id}（PID {proc.pid}）。\n"
                    f"查看输出: bg output {job_id} · 结束: bg kill {job_id}"),
        )

    def _bg_dispatch(self, args: str) -> ToolResult:
        parts = args.split()
        sub = parts[0].lower() if parts else ""
        if sub == "list":
            return self._bg_list()
        if sub == "output":
            job_id = parts[1] if len(parts) > 1 else ""
            tail = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 20
            return self._bg_output(job_id, tail)
        if sub == "kill":
            return self._bg_kill(parts[1] if len(parts) > 1 else "")
        return ToolResult(
            success=False, output="",
            error="用法: bg list · bg output <任务ID> [行数] · bg kill <任务ID>",
        )

    def _bg_list(self) -> ToolResult:
        if not self._jobs:
            return ToolResult(success=True, output="（无后台任务）")
        lines = []
        for job_id, job in self._jobs.items():
            proc = job.get("proc")
            alive = bool(proc is not None and proc.poll() is None)
            status = "运行中" if alive else "已结束"
            lines.append(f"- {job_id}  [{status}]  {job['command'][:80]}")
        return ToolResult(success=True, output="后台任务:\n" + "\n".join(lines))

    def _bg_output(self, job_id: str, tail: int = 20) -> ToolResult:
        job = self._jobs.get(job_id or "")
        if job is None:
            return ToolResult(
                success=False, output="",
                error=f"后台任务不存在: {job_id or '（空）'}（用 bg list 查看）",
            )
        try:
            with open(job["out_path"], "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"读取任务输出失败: {str(e)[:200]}")
        shown = "".join(lines[-tail:]) or "（暂无输出）"
        proc = job.get("proc")
        alive = bool(proc is not None and proc.poll() is None)
        suffix = "（任务仍在运行）" if alive else "（任务已结束）"
        return ToolResult(
            success=True,
            output=f"{job_id} 最近 {tail} 行 {suffix}:\n{shown.rstrip()}",
        )

    def _bg_kill(self, job_id: str) -> ToolResult:
        job = self._jobs.get(job_id or "")
        if job is None:
            return ToolResult(
                success=False, output="",
                error=f"后台任务不存在: {job_id or '（空）'}（用 bg list 查看）",
            )
        proc = job.get("proc")
        if proc is None or proc.poll() is not None:
            return ToolResult(success=True, output=f"{job_id} 已结束（无需终止）。")
        try:
            if _IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            return ToolResult(
                success=True,
                output=f"{job_id} 已请求终止（{str(e)[:120]}）。",
            )
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        return ToolResult(
            success=True,
            output=f"{job_id} 已终止。输出仍可查看: bg output {job_id}",
        )
