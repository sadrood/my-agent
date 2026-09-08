"""
ACP 客户端：以 Agent Client Protocol（JSON-RPC 2.0 over stdio）与外部 agent 引擎双向通信。

启动 `claude-agent-acp`（ACP 1.2 wrapper），
走 initialize → session/new → session/prompt，把 session/update 通知映射为
桌面端事件流（文本/思考/工具调用/计划/审批），支持双向（agent 主动询问权限）。

事件映射：
  agentMessageChunk  → stream_delta{kind:text}
  agentThoughtChunk  → stream_delta{kind:reasoning}
  toolCall           → tool_call（工具卡）
  toolCallUpdate     → tool_result（回填）
  plan               → plan{steps}
  state_update       → turn 结束（stopReason）
  session/request_permission → approval + 自动允许（前端可改）
"""
import json
import os
import shutil
import subprocess
import threading
import time

_NOTIFY = "session/update"


def resolve_acp_cmd() -> tuple:
    """找到 ACP wrapper 可执行命令。返回 (argv, use_shell)。"""
    resolved = shutil.which("claude-agent-acp")
    if resolved:
        if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
            return subprocess.list2cmdline([resolved]), True
        return [resolved], False
    # 回退 npx（首次拉取）；npx 在 Windows 是 .cmd，需经 cmd.exe
    npx = shutil.which("npx") or "npx"
    cmd_args = [npx, "-y", "@agentclientprotocol/claude-agent-acp"]
    if os.name == "nt" and npx.lower().endswith((".cmd", ".bat")):
        return subprocess.list2cmdline(cmd_args), True
    return cmd_args, False


class ACPClient:
    """一个 ACP wrapper 子进程 + ACP 会话。"""

    def __init__(self, cwd: str, on_event, stop_event=None, argv=None):
        self._cwd = os.path.abspath(cwd or os.getcwd())   # ACP 要求绝对路径
        self._on_event = on_event
        self._stop_event = stop_event
        self._lock = threading.Lock()
        self._pending: dict = {}      # id -> threading.Event
        self._results: dict = {}      # id -> frame
        self._next_id = 0
        self._session_id = None
        self._turn_done = threading.Event()
        self._turn_stop_reason = ""
        self._perm_auto_allow = True   # MVP：权限自动允许（前端可改为弹卡）

        argv_, use_shell = (argv if isinstance(argv, tuple) else (argv, False)) if argv else resolve_acp_cmd()
        if not isinstance(argv_, (list, str)):
            argv_, use_shell = resolve_acp_cmd()
        kwargs = dict(
            shell=use_shell,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            cwd=self._cwd, env=os.environ,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(argv_, **kwargs)
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    # ---------------- 基础通信 ----------------

    def _pump(self):
        for line in iter(self._proc.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except Exception:
                # 非 JSON 行（wrapper 日志 / 底层错误如 503）→ 流式透传，失败不丢信息
                try:
                    self._on_event("stream_delta", {"kind": "text", "text": line + "\n"})
                except Exception:
                    pass
                continue
            self._dispatch(frame)

    def _dispatch(self, frame: dict):
        method = frame.get("method")
        if method == _NOTIFY:
            self._handle_update(frame.get("params") or {})
            return
        if method == "session/request_permission":
            self._handle_permission(frame)
            return
        rid = frame.get("id")
        if rid is not None:
            with self._lock:
                if rid in self._pending:
                    self._results[rid] = frame
                    self._pending[rid].set()

    def _request(self, method: str, params: dict, timeout: float = 30.0) -> dict | None:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            ev = threading.Event()
            self._pending[rid] = ev
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        ev.wait(timeout=timeout)
        with self._lock:
            frame = self._results.pop(rid, None)
            self._pending.pop(rid, None)
        return frame

    def _notify(self, method: str, params: dict):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, frame: dict):
        try:
            self._proc.stdin.write(json.dumps(frame) + "\n")
            self._proc.stdin.flush()
        except Exception:
            pass

    # ---------------- 事件处理 ----------------

    def _handle_update(self, params: dict):
        update = params.get("update") or {}
        kind = update.get("sessionUpdate") or update.get("variant")
        if kind in ("agent_message_chunk", "agentMessageChunk"):
            text = (update.get("content") or {}).get("text") or ""
            if text:
                self._on_event("stream_delta", {"kind": "text", "text": text})
        elif kind in ("agent_thought_chunk", "agentThoughtChunk"):
            text = (update.get("content") or {}).get("text") or ""
            if text:
                self._on_event("stream_delta", {"kind": "reasoning", "text": text})
        elif kind in ("tool_call", "toolCall"):
            call = update.get("toolCall") or update.get("tool_call") or {}
            title = str(call.get("title") or call.get("toolCallId") or "tool")
            self._on_event("tool_call", {"tool": title, "args": call.get("input") or {}})
        elif kind in ("tool_call_update", "toolCallUpdate"):
            cu = update.get("toolCallUpdate") or update.get("tool_call_update") or {}
            title = str(cu.get("title") or cu.get("toolCallId") or "tool")
            content = cu.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else str(content or "")
            status = str(cu.get("status") or "pending")
            self._on_event("tool_result", {
                "tool": title,
                "success": status in ("completed", "ok", "done"),
                "output": str(text or "")[:2000],
            })
        elif kind == "plan":
            items = update.get("items") or []
            steps = [str(i.get("content", "")) for i in items if i.get("content")]
            if steps:
                self._on_event("plan", {"steps": steps})
        elif kind in ("state_update", "turn_finished"):
            reason = str(update.get("stopReason") or update.get("reason") or "endTurn")
            self._turn_stop_reason = reason
            self._turn_done.set()

    def _handle_permission(self, frame: dict):
        params = frame.get("params") or {}
        desc = str(params.get("description") or params.get("title") or "ACP 请求权限")
        self._on_event("approval", {"tool": "ACP", "command": desc[:300], "risk_level": "medium"})
        if self._perm_auto_allow:
            # 自动允许并回传
            self._notify("session/response_permission", {
                "sessionId": self._session_id or "",
                "permissionId": params.get("permissionId") or "",
                "allow": True,
            })

    # ---------------- 会话生命周期 ----------------

    def initialize(self) -> bool:
        r = self._request("initialize", {
            "protocolVersion": 1,   # 该 wrapper 用数字版本
            "clientInfo": {"name": "my-agent-desktop", "version": "0.1.0"},
            "capabilities": {},
        }, timeout=40)
        return bool(r and r.get("result"))

    def new_session(self) -> bool:
        r = self._request("session/new", {"cwd": self._cwd, "mcpServers": []}, timeout=40)
        if r and r.get("result"):
            self._session_id = r["result"].get("sessionId")
            return bool(self._session_id)
        return False

    def prompt(self, text: str):
        """发送 prompt（request，带 id）。turn 结束时收到 result（stopReason）。
        事件经 _handle_update 流式返回；wait_turn 等 result 或 turn_finished 更新。"""
        if not self._session_id:
            return
        self._turn_done.clear()
        self._turn_stop_reason = ""
        with self._lock:
            self._next_id += 1
            self._prompt_rid = self._next_id
            ev = threading.Event()
            self._pending[self._next_id] = ev
        self._write({"jsonrpc": "2.0", "id": self._next_id, "method": "session/prompt",
                     "params": {"sessionId": self._session_id,
                                "prompt": [{"type": "text", "text": text}]}})

    def wait_turn(self, timeout: float = 600.0) -> str:
        """等 turn 结束：prompt 的 result（stopReason）或 turn_finished 更新。返回 stopReason。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._turn_done.is_set():
                return self._turn_stop_reason or "endTurn"
            with self._lock:
                rid = getattr(self, "_prompt_rid", None)
                frame = self._results.get(rid) if rid else None
                if frame is not None:
                    self._results.pop(rid, None)
                    self._pending.pop(rid, None)
            if frame is not None:
                if frame.get("error"):
                    return "failed"
                res = frame.get("result") or {}
                return str(res.get("stopReason") or "endTurn")
            if self._stop_event is not None and self._stop_event.is_set():
                self.cancel()
                return "cancelled"
            time.sleep(0.1)
        return "timeout"

    def cancel(self):
        if self._session_id:
            self._notify("session/cancel", {"sessionId": self._session_id})

    def close(self):
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        if self._proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                                   capture_output=True, timeout=15)
                else:
                    self._proc.kill()
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass


def run_acp_session(goal: str, cwd: str, on_event, stop_event=None, argv=None,
                    model: str | None = None) -> str:
    """跑一次 ACP 任务：initialize → session/new → prompt → 等 turn 结束。返回最终文本。

    model: 覆盖 ACP wrapper 使用的模型（ANTHROPIC_MODEL）。默认继承环境；若你的
    ~/.claude/settings.json 指向的网关缺少默认模型会 503——此时传网关
    实际服务的模型（如 deepseek-v4-pro / glm-5.2）。
    """
    if model:
        os.environ.setdefault("ANTHROPIC_MODEL", model)
    client = ACPClient(cwd, on_event, stop_event, argv=argv)
    on_event("run_start", {"goal": goal, "runtime": "claude-acp", "mode": "acp"})
    try:
        if not client.initialize():
            on_event("answer", {"output": "ACP 初始化失败（claude-agent-acp 不可用？）"})
            on_event("run_end", {"status": "failed"})
            return "init failed"
        if not client.new_session():
            on_event("answer", {"output": "ACP 创建会话失败"})
            on_event("run_end", {"status": "failed"})
            return "session failed"
        client.prompt(goal)
        reason = client.wait_turn()
        status = ("completed" if reason in ("endTurn", "end_turn", "completed")
                  else "stopped" if reason == "cancelled" else "failed")
        on_event("answer", {"output": ""})
        on_event("run_end", {"status": status, "stopReason": reason})
        return reason
    finally:
        client.close()
