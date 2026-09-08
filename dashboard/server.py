"""
Dashboard FastAPI 服务器。

启动方式:
    python -m dashboard.server
    或
    uvicorn dashboard.server:app --reload --port 8080
"""
import os
import sys
import json
import uuid
import time
import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Optional

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import HTMLResponse, FileResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from dashboard.hub import get_dashboard_hub

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
hub = get_dashboard_hub()


class _LivePermission:
    """运行中实时权限模式：桌面端任务进行中切换 auto/ask/block 立即生效
    （下次工具审批询问即按新模式；正在等待中的审批卡也会被新模式打断裁决）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._modes: dict = {}

    def set(self, sid: str, mode: str):
        with self._lock:
            self._modes[str(sid or "default")] = str(mode or "ask")

    def get(self, sid: str, default: str = "ask") -> str:
        with self._lock:
            return self._modes.get(str(sid or "default"), default)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._modes)


permission_modes = _LivePermission()


def _mode_to_policy(mode: str) -> str:
    """用户权限模式 → ApprovalPolicy 策略（运行中经可调用 mode 每次实时解析）。"""
    return {
        "auto": "never",        # 自动执行：放行（黑名单/沙箱仍生效）
        "ask": "on-request",    # 每次询问：需要确认的工具弹卡
        "block": "untrusted",   # 禁止：非低风险全部拒绝（broker 拦）
    }.get(str(mode or "").lower(), "on-request")


class ApprovalBroker:
    """交互式审批桥：把工作线程里的审批请求转成 hub 事件推给前端，
    阻塞等待前端经 WebSocket（approval_response）或 /api/approve 返回决定。

    用于 dashboard / desktop 等无 TTY 场景——ApprovalPolicy 内置的
    input() 交互在服务器线程里会 EOF 直接拒绝，导致 ask 模式形同虚设。
    """

    def __init__(self, timeout: float = 300.0):
        self.timeout = timeout
        self._pending: dict = {}          # id -> threading.Event
        self._decisions: dict = {}        # id -> bool
        self._lock = threading.Lock()

    def approver(self, request) -> bool:
        """ApprovalPolicy 的 approver 回调（默认会话）：发事件 → 等前端决定。"""
        return self._ask_card(request, "")

    def approver_for(self, sid: str):
        """按会话生成实时 approver：每次询问前先看该会话当前的权限模式——
        已切 auto → 直接放行（不再弹卡）；已切 block → 直接拒绝；
        仍是 ask → 弹卡等前端决定（等待期间用户切走也会即时生效）。"""

        def _approver(request) -> bool:
            mode = str(permission_modes.get(sid, "ask") or "ask").lower()
            if mode == "auto":
                return True          # 运行中切到自动执行：正在等待的审批也放行
            if mode == "block":
                return False         # 运行中切到禁止：拒绝
            return self._ask_card(request, sid)

        return _approver

    def _ask_card(self, request, sid: str) -> bool:
        """发审批卡并等前端决定；等待期间该会话权限模式被切换会立即按新模式裁决。"""
        req_id = uuid.uuid4().hex[:10]
        ev = threading.Event()
        with self._lock:
            self._pending[req_id] = ev
        hub.emit("approval", {
            "id": req_id,
            "tool": getattr(request, "tool_name", "") or "",
            "command": (getattr(request, "command", "") or "")[:400],
            "risk_level": getattr(request, "risk_level", "") or "medium",
            "reason": getattr(request, "reason", "") or "",
        })
        try:
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                # 卡等待中用户把该会话切走 ask → auto/block：立即按新模式裁决
                if str(permission_modes.get(sid, "ask") or "ask").lower() != "ask":
                    mode = str(permission_modes.get(sid, "ask") or "ask").lower()
                    hub.emit("approval_resolved", {
                        "id": req_id, "allow": mode == "auto",
                        "reason": f"权限模式已切换为 {mode}",
                    })
                    return mode == "auto"
                if ev.wait(min(1.0, max(0.1, deadline - time.time()))):
                    break
            else:
                hub.emit("approval_resolved", {"id": req_id, "allow": False, "reason": "timeout"})
                return False
            with self._lock:
                allow = bool(self._decisions.get(req_id, False))
            hub.emit("approval_resolved", {"id": req_id, "allow": allow})
            return allow
        finally:
            with self._lock:
                self._pending.pop(req_id, None)
                self._decisions.pop(req_id, None)

    def resolve(self, req_id: str, allow: bool) -> bool:
        """前端提交审批决定。返回是否命中了一个待审批请求。"""
        with self._lock:
            ev = self._pending.get(req_id)
            if ev is None:
                return False
            self._decisions[req_id] = bool(allow)
        ev.set()
        return True

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


approval_broker = ApprovalBroker()


class RunControl:
    """按会话的运行停止控制：前端按"停止"→ 置位对应会话的 stop_event，
    Agent/Executor 在下一个检查点优雅退出，TerminalTool 会强杀正在跑的子进程。

    多标签 Agent：按 session_id 隔离，每个会话可独立运行、独立停止（互不干扰）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict = {}   # session_id -> threading.Event

    def begin(self, session_id: str = "") -> threading.Event:
        """任务开始时为指定会话注册一个 stop_event。"""
        ev = threading.Event()
        with self._lock:
            self._events[session_id or ""] = ev
        return ev

    def stop(self, session_id: str = "") -> bool:
        """请求停止指定会话的任务；缺省停最近一个。返回是否命中可停任务。"""
        with self._lock:
            if session_id:
                ev = self._events.get(session_id)
            else:
                ev = next(reversed(self._events.values()), None) if self._events else None
        if ev is None or ev.is_set():
            return False
        ev.set()
        return True

    def end(self, session_id: str = "") -> None:
        with self._lock:
            self._events.pop(session_id or "", None)

    def running_count(self) -> int:
        with self._lock:
            return len(self._events)


run_control = RunControl()

# 辅助 Agent 独立的停止控制器（与主任务互不干扰：主任务跑着也能让辅助 Agent 并行）
side_run_control = RunControl()


# ---------------- 侧栏辅助对话的浏览器通道 ----------------
# 侧栏（/api/ask）设计上不带工具；这里给前端提供一个极简只读浏览器端点：
# 前端识别到消息里的 URL 后经 /api/browser 抓取页面文本，作为上下文随 ask 注入。
# 说明：
# - Playwright sync API 不能跑在 asyncio 事件循环里，且线程敏感（须固定线程），
#   故用单线程 executor 串行执行。
# - 独立实例 + 非持久 profile + 无头模式：与主任务的 BrowserTool 完全隔离，
#   不争用 user-data-dir 锁，也不弹窗口打扰用户。
# - 命令白名单只有导航 + 内容读取，无点击/输入/JS 执行（安全边界）。
_SIDE_BROWSER_COMMANDS = {
    "status", "goto", "text", "snapshot", "title", "url", "refresh", "close",
}
_SIDE_BROWSER_TIMEOUT = 60.0
_SIDE_BROWSER_LOCK = threading.Lock()
_SIDE_BROWSER = None
_SIDE_BROWSER_EXECUTOR = None


def _get_side_browser_executor():
    global _SIDE_BROWSER_EXECUTOR
    if _SIDE_BROWSER_EXECUTOR is None:
        from concurrent.futures import ThreadPoolExecutor
        _SIDE_BROWSER_EXECUTOR = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="side-browser")
    return _SIDE_BROWSER_EXECUTOR


def _get_side_browser():
    global _SIDE_BROWSER
    with _SIDE_BROWSER_LOCK:
        if _SIDE_BROWSER is None:
            from tools.browser import BrowserTool
            _SIDE_BROWSER = BrowserTool(headless=True, persistent=False)
        return _SIDE_BROWSER


def _side_browser_execute(command: str, args: str = "") -> dict:
    """在固定线程里执行一条白名单浏览器命令，返回 {ok, output/error}。"""
    try:
        tool = _get_side_browser()
        result = tool.execute(f"{command} {args}".strip() if args else command)
        if result.success:
            return {"ok": True, "output": result.output}
        return {"ok": False, "error": result.error or "浏览器命令执行失败"}
    except Exception as e:
        return {"ok": False, "error": f"浏览器通道异常: {str(e)[:200]}"}


if HAS_FASTAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        print("[Dashboard] 服务启动")
        # 启动定时任务调度器（到期的周期任务自动跑 agent）
        try:
            from agent.scheduler import start as _start_sched
            _start_sched(_run_scheduled_task)
        except Exception as e:
            print(f"[Dashboard] 调度器启动失败: {e}")
        yield
        print("[Dashboard] 服务关闭")

    app = FastAPI(
        title="AI Agent Dashboard",
        version="1.0.0",
        lifespan=lifespan,
    )

    # 桌面端前端跨域访问后端（dev 用 Vite localhost:5173，打包用 file://）。
    # 后端默认只监听 127.0.0.1，不对外暴露，故放开本地来源是安全的。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        client_id = str(uuid.uuid4())[:8]
        queue = hub.subscribe(client_id)
        try:
            while True:
                # 同时等待：新事件（推送）或客户端消息（get_state 请求）
                get_event = asyncio.ensure_future(queue.get())
                get_msg = asyncio.ensure_future(websocket.receive_text())
                done, pending = await asyncio.wait(
                    {get_event, get_msg}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_event in done:
                    event = get_event.result()
                    await websocket.send_text(json.dumps(event, ensure_ascii=False))
                if get_msg in done:
                    try:
                        msg = json.loads(get_msg.result())
                        if msg.get("type") == "get_state":
                            await websocket.send_text(json.dumps({
                                "type": "state_snapshot",
                                "data": hub.get_current_state(),
                                "timestamp": __import__('time').time(),
                            }))
                        elif msg.get("type") == "approval_response":
                            ok = approval_broker.resolve(
                                str(msg.get("id", "")), bool(msg.get("allow"))
                            )
                            await websocket.send_text(json.dumps({
                                "type": "approval_ack",
                                "data": {"id": msg.get("id"), "matched": ok},
                                "timestamp": __import__('time').time(),
                            }))
                        elif msg.get("type") == "stop":
                            stopped = run_control.stop()
                            await websocket.send_text(json.dumps({
                                "type": "stop_ack",
                                "data": {"stopped": stopped},
                                "timestamp": __import__('time').time(),
                            }))
                    except WebSocketDisconnect:
                        raise
                    except Exception:
                        pass
                for f in pending:
                    f.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(client_id)

    @app.get("/api/state")
    async def get_state():
        return hub.get_current_state()

    @app.get("/api/running")
    async def api_running():
        """当前活动运行（供前端刷新/重连后恢复 running 状态与停止按钮）。"""
        with run_control._lock:
            sessions = list(run_control._events.keys())
        return {"ok": True, "running": len(sessions) > 0, "sessions": sessions}

    @app.get("/api/runtimes")
    async def api_runtimes():
        """可用 Agent 运行时（内置 + 外部 CLI 是否安装）。"""
        from agent.runtime import RUNTIMES, detect_available
        return {"ok": True, "runtimes": RUNTIMES, "available": detect_available()}

    @app.get("/api/history")
    async def get_history(limit: int = 100):
        events = hub._history[-limit:]
        return {"count": len(events), "events": [e.to_dict() for e in events]}

    @app.get("/api/screenshots")
    async def get_screenshots():
        return {"count": len(hub._screenshots), "screenshots": hub._screenshots[-30:]}

    # ---- 文件树（只读，绑定工作目录，不暴露系统盘）----
    # 安全约束：所有路径必须解析到 WORKSPACE_ROOT 之内（realpath 防符号链接逃逸）。
    WORKSPACE_ROOT = os.path.realpath(os.getcwd())
    _TREE_SKIP_DIRS = {
        ".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
        ".pytest_cache", ".mypy_cache", ".idea", ".vscode",
    }
    _TREE_MAX_DEPTH = 6
    _TREE_MAX_ENTRIES = 3000
    _FILE_READ_MAX_BYTES = 200 * 1024  # 预览上限 200KB

    def _safe_resolve(rel: str):
        """把相对路径解析为工作区内绝对路径；越界返回 None。"""
        abs_path = os.path.realpath(os.path.join(WORKSPACE_ROOT, rel or ""))
        if abs_path != WORKSPACE_ROOT and not abs_path.startswith(WORKSPACE_ROOT + os.sep):
            return None
        return abs_path

    def _build_tree(dir_abs: str, rel: str, depth: int, counter: list):
        node = {"name": os.path.basename(dir_abs) or rel, "path": rel, "type": "dir", "children": []}
        if depth >= _TREE_MAX_DEPTH:
            return node
        try:
            entries = sorted(os.listdir(dir_abs))
        except OSError:
            return node
        dirs, files = [], []
        for name in entries:
            if counter[0] >= _TREE_MAX_ENTRIES:
                break
            if name in _TREE_SKIP_DIRS or (name.startswith(".") and name not in (".env.example",)):
                continue
            child_abs = os.path.join(dir_abs, name)
            child_rel = f"{rel}/{name}" if rel else name
            if os.path.isdir(child_abs):
                counter[0] += 1
                dirs.append(_build_tree(child_abs, child_rel, depth + 1, counter))
            else:
                counter[0] += 1
                files.append({"name": name, "path": child_rel, "type": "file"})
        node["children"] = dirs + files
        return node

    @app.get("/api/files")
    async def api_files():
        """工作目录文件树（递归、跳过噪声目录、有深度/数量上限）。"""
        counter = [0]
        tree = _build_tree(WORKSPACE_ROOT, "", 0, counter)
        return {"root": WORKSPACE_ROOT, "tree": tree}

    @app.get("/api/skills")
    async def api_skills():
        """可用技能包列表（SKILL.md），供桌面端能力面板展示。

        只读枚举（agent.skills.SkillManager.discover），不做任何执行；
        scripts 仅作「告知」渲染，执行仍走终端工具 → 审批门原样生效。
        """
        try:
            from agent.skills import SkillManager
            mgr = SkillManager()
            skills = mgr.discover()
            return {
                "ok": True,
                "skills": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "triggers": s.triggers,
                        "scripts": s.scripts,
                        "path": s.path,
                    }
                    for s in skills
                ],
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "skills": []}

    @app.get("/api/file")
    async def api_file(path: str = ""):
        """读取工作区内单个文件内容（只读预览，200KB 上限）。"""
        abs_path = _safe_resolve(path)
        if abs_path is None:
            return {"ok": False, "error": "路径越界：仅允许访问工作目录内文件"}
        if not os.path.isfile(abs_path):
            return {"ok": False, "error": "文件不存在"}
        try:
            with open(abs_path, "rb") as f:
                raw = f.read(_FILE_READ_MAX_BYTES + 1)
        except OSError as e:
            return {"ok": False, "error": f"读取失败: {e}"}
        truncated = len(raw) > _FILE_READ_MAX_BYTES
        raw = raw[:_FILE_READ_MAX_BYTES]
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "二进制文件不支持预览"}
        return {"ok": True, "path": path, "content": content, "truncated": truncated}

    # ---- 文件变更 diff（透明可控：从"知道被改"到"看到改了什么"）----
    # 数据源：tools/change_tracker（file/edit 工具写入前自动留旧内容快照）。
    # 安全约束与 /api/file 一致：路径必须解析到工作区内。
    import difflib as _difflib
    from tools.change_tracker import get_change_tracker as _get_tracker

    @app.get("/api/changes")
    async def api_changes():
        """本次会话被 Agent 修改过的文件清单（按时间排序）。"""
        changes = _get_tracker().list_changes(WORKSPACE_ROOT)
        return {"ok": True, "count": len(changes), "changes": changes}

    @app.get("/api/diff")
    async def api_diff(path: str = ""):
        """返回指定文件的修改前后对比（unified diff + 行级统计）。

        多次修改展示"会话累计变更"（快照保留最旧版本）。
        """
        abs_path = _safe_resolve(path)
        if abs_path is None:
            return {"ok": False, "error": "路径越界：仅允许访问工作目录内文件"}
        tracker = _get_tracker()
        rec = tracker.get(abs_path)
        if rec is None:
            # change_tracker 只追踪 edit/file 工具的写入；python/terminal 改的文件
            # 没有快照 → 回退到 git diff（文件在 git 仓库内才能对比）
            import subprocess as _sub
            try:
                rel = os.path.relpath(abs_path, WORKSPACE_ROOT)
                proc = _sub.run(
                    ["git", "diff", "--no-color", "--unified=3", "HEAD", "--", rel],
                    cwd=WORKSPACE_ROOT, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=15,
                )
            except Exception as e:
                return {"ok": False, "error": f"git diff 不可用: {str(e)[:120]}"}
            diff_text = proc.stdout or ""
            if not diff_text.strip():
                try:
                    proc2 = _sub.run(["git", "rev-parse", "--is-inside-work-tree"],
                                     cwd=WORKSPACE_ROOT, capture_output=True,
                                     text=True, timeout=10)
                    in_repo = proc2.returncode == 0 and proc2.stdout.strip() == "true"
                except Exception:
                    in_repo = False
                if not in_repo:
                    return {"ok": False, "error": "当前目录不在 git 仓库内，无法生成 diff。"}
                return {"ok": False,
                        "error": "该文件没有可对比的 diff（未跟踪的新文件或 git 中无改动）。"}
            added = sum(1 for l in diff_text.splitlines()
                        if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff_text.splitlines()
                          if l.startswith("-") and not l.startswith("---"))
            return {
                "ok": True, "path": path, "tool": "git", "writes": 1,
                "is_new": False, "unified_diff": diff_text,
                "added": added, "removed": removed,
            }
        # 读当前内容作为"新内容"
        new_content = ""
        current_exists = os.path.isfile(abs_path)
        if current_exists:
            try:
                with open(abs_path, "rb") as f:
                    raw = f.read(_FILE_READ_MAX_BYTES + 1)
                if len(raw) > _FILE_READ_MAX_BYTES:
                    return {"ok": False, "error": "当前文件过大（>200KB），无法生成 diff"}
                new_content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return {"ok": False, "error": "二进制文件不支持 diff"}
            except OSError as e:
                return {"ok": False, "error": f"读取失败: {e}"}
        if rec.old_content is None and not rec.is_new:
            return {"ok": False, "error": "修改前的文件过大或为二进制，未留存快照"}
        # 换行归一化（对齐 EditTool 的 CRLF/LF 兼容策略），避免 Windows 上
        # 快照(LF)与当前内容(CRLF)因行尾差异产生"全文替换"的假 diff
        old_lines = (rec.old_content or "").replace("\r\n", "\n").splitlines(keepends=True)
        new_lines = new_content.replace("\r\n", "\n").splitlines(keepends=True)
        diff_text = "".join(_difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
        ))
        added = sum(1 for l in diff_text.splitlines()
                    if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_text.splitlines()
                      if l.startswith("-") and not l.startswith("---"))
        return {
            "ok": True,
            "path": path,
            "tool": rec.tool,
            "writes": rec.writes,
            "is_new": rec.is_new,
            "deleted": not current_exists,
            "old": rec.old_content or "",
            "new": new_content,
            "unified_diff": diff_text,
            "added": added,
            "removed": removed,
            "timestamp": rec.timestamp,
        }

    @app.post("/api/run")
    async def api_run(payload: dict = Body(...)):
        """提交一个任务目标，后台线程执行；事件经 WebSocket 实时推送。

        payload 支持（均可选，缺省用 .env / 默认值）：
          goal: str                        任务目标（必填）
          model / base_url / api_key: str  LLM 配置（覆盖 .env）
          temperature / top_p / max_tokens: number  模型参数
          permission_mode: str            auto / ask / block
          sandbox_mode: str               read-only / workspace-write / danger-full-access
        """
        goal = str((payload or {}).get("goal", "")).strip()
        if not goal:
            return {"ok": False, "error": "goal 不能为空"}
        # 注意：run_start 由 worker 线程里的 Agent/demo 流程统一 emit，
        # 这里不再重复发送，避免前端收到两条 run_start。
        kwargs = {
            "model": (payload or {}).get("model"),
            "base_url": (payload or {}).get("base_url"),
            "api_key": (payload or {}).get("api_key"),
            "agent_name": (payload or {}).get("agent_name"),
            "max_ops": (payload or {}).get("max_ops"),
            "temperature": (payload or {}).get("temperature"),
            "top_p": (payload or {}).get("top_p"),
            "max_tokens": (payload or {}).get("max_tokens"),
            "permission_mode": (payload or {}).get("permission_mode"),
            "sandbox_mode": (payload or {}).get("sandbox_mode"),
            "session_id": (payload or {}).get("session_id"),
            "runtime": (payload or {}).get("runtime"),
            "runtime_config": (payload or {}).get("runtime_config"),
        }
        threading.Thread(target=_run_agent_worker, args=(goal, kwargs), daemon=True).start()
        return {"ok": True}

    @app.post("/api/permission-mode")
    async def api_permission_mode(payload: dict = Body(None)):
        """运行中切换某会话的权限模式（auto/ask/block）：对该会话后续工具审批
        （含正在等待中的审批卡）立即生效；同时作为该会话下一次运行的默认模式。"""
        mode = str((payload or {}).get("mode") or "").lower()
        session_id = str((payload or {}).get("session_id") or "default")
        if mode not in ("auto", "ask", "block"):
            return {"ok": False, "error": f"未知权限模式: {mode or '(空)'}"}
        permission_modes.set(session_id, mode)
        return {"ok": True, "session_id": session_id, "mode": mode}

    @app.post("/api/stop")
    async def api_stop(payload: dict = Body(None)):
        """停止指定会话的运行任务（缺省停最近一个）：置位 stop_event，
        执行器在下一个检查点退出，正在运行的前台子进程会被强杀。"""
        session_id = str((payload or {}).get("session_id", "") or "").strip()
        stopped = run_control.stop(session_id)
        return {"ok": True, "stopped": stopped, "running": run_control.running_count()}

    @app.post("/api/side-run")
    async def api_side_run(payload: dict = Body(...)):
        """辅助 Agent：独立完整 Agent（可调工具），会话按主会话隔离（side-<main_id>）。

        与 /api/run 的区别：事件带 panel="side" 由前端路由到辅助面板；
        完成后把摘要写入主会话记录并 emit side_complete。
        """
        goal = str((payload or {}).get("goal", "")).strip()
        main_session_id = str((payload or {}).get("main_session_id", "")).strip()
        if not goal:
            return {"ok": False, "error": "goal 不能为空"}
        kwargs = {k: (payload or {}).get(k) for k in
                  ("model", "base_url", "api_key", "agent_name", "permission_mode",
                   "sandbox_mode", "max_ops")}
        threading.Thread(
            target=_run_agent_worker, args=(goal, kwargs, main_session_id), daemon=True,
        ).start()
        return {"ok": True}

    @app.post("/api/side-stop")
    async def api_side_stop(payload: dict = Body(None)):
        """停止辅助 Agent 任务（与主任务独立）。"""
        session_id = str((payload or {}).get("session_id", "") or "").strip()
        stopped = side_run_control.stop(session_id)
        return {"ok": True, "stopped": stopped}

    # ---------------- 任务中心（想法 + 任务状态机） ----------------

    @app.get("/api/tasks")
    async def api_tasks_get():
        from agent.tasks import load_tasks
        return {"ok": True, "tasks": load_tasks()}

    @app.post("/api/tasks")
    async def api_tasks_post(payload: dict = Body(...)):
        """前端任务中心操作：status / log / complete（create 走 agent 的 task 工具）。"""
        import time as _t
        from agent.tasks import load_tasks, save_tasks
        from dashboard.hub import get_dashboard_hub
        op = str((payload or {}).get("operation", "")).strip()
        tid = str((payload or {}).get("id", "") or "").strip()
        tasks = load_tasks()
        hit = next((t for t in tasks if t.get("id") == tid), None)
        if hit is None:
            return {"ok": False, "error": f"找不到任务: {tid}"}
        now = _t.strftime("%Y-%m-%d %H:%M")
        if op == "status":
            status = str((payload or {}).get("status", "")).strip()
            if status not in ("todo", "in_progress", "done", "failed"):
                return {"ok": False, "error": f"非法状态: {status}"}
            hit["status"] = status; hit["updated"] = now
        elif op == "log":
            content = str((payload or {}).get("content", "") or "").strip()
            if not content:
                return {"ok": False, "error": "log 需要 content"}
            hit.setdefault("logs", []).append(f"[{now}] {content}")
            hit["updated"] = now
        elif op == "complete":
            hit["status"] = "done"; hit["updated"] = now
        else:
            return {"ok": False, "error": f"未知操作: {op}"}
        save_tasks(tasks)
        try:
            get_dashboard_hub().emit("tasks", {"tasks": tasks})
        except Exception:
            pass
        return {"ok": True, "tasks": tasks}

    @app.get("/api/thoughts")
    async def api_thoughts_get():
        from agent.tasks import load_thoughts
        return {"ok": True, "thoughts": load_thoughts()}

    @app.post("/api/thoughts")
    async def api_thoughts_post(payload: dict = Body(...)):
        import time as _t
        from agent.tasks import load_thoughts, save_thoughts
        from dashboard.hub import get_dashboard_hub
        content = str((payload or {}).get("content", "") or "").strip()
        if not content:
            return {"ok": False, "error": "content 不能为空"}
        thoughts = load_thoughts()
        thoughts.append({"id": f"th{int(_t.time()*1000)}{len(thoughts)}", "content": content[:500],
                         "created": _t.strftime("%Y-%m-%d %H:%M")})
        save_thoughts(thoughts)
        try:
            get_dashboard_hub().emit("thoughts", {"thoughts": thoughts})
        except Exception:
            pass
        return {"ok": True, "thoughts": thoughts}

    # ---------------- 定时任务（周期调度） ----------------

    @app.get("/api/scheduled")
    async def api_scheduled_get():
        from agent.scheduler import list_tasks
        return {"ok": True, "tasks": list_tasks()}

    @app.post("/api/scheduled")
    async def api_scheduled_add(payload: dict = Body(...)):
        from agent.scheduler import add_task
        goal = str((payload or {}).get("goal", "") or "").strip()
        interval = int((payload or {}).get("interval_minutes", 10) or 10)
        runtime = str((payload or {}).get("runtime", "myagent") or "myagent")
        if not goal:
            return {"ok": False, "error": "goal 不能为空"}
        task = add_task(goal, interval, runtime)
        return {"ok": True, "task": task}

    @app.post("/api/scheduled/remove")
    async def api_scheduled_remove(payload: dict = Body(...)):
        from agent.scheduler import remove_task
        removed = remove_task(str((payload or {}).get("id", "") or ""))
        return {"ok": True, "removed": removed}

    # ---------------- 消息反馈（👍👎） ----------------

    @app.post("/api/feedback")
    async def api_feedback(payload: dict = Body(...)):
        import json as _json, time as _t
        msg_id = str((payload or {}).get("message_id", "") or "")
        rating = str((payload or {}).get("rating", "") or "").strip()
        note = str((payload or {}).get("note", "") or "").strip()[:500]
        if rating not in ("up", "down"):
            return {"ok": False, "error": "rating 需要 up/down"}
        path = os.path.join("memory", "feedback.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if not isinstance(data, list):
                data = []
        except Exception:
            data = []
        data.append({"message_id": msg_id, "rating": rating, "note": note,
                     "time": _t.strftime("%Y-%m-%d %H:%M")})
        os.makedirs("memory", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        return {"ok": True, "count": len(data)}

    @app.get("/api/search")
    async def api_search(q: str = "", scope: str = "all", limit: int = 30):
        """本地全文搜索：会话历史 + 工作区文件（scope=conversations|files|all）。"""
        from tools.search import search_sessions, search_files
        q = (q or "").strip()
        out = {"conversations": [], "files": []}
        if not q:
            return {"ok": True, **out}
        try:
            if scope in ("all", "conversations"):
                out["conversations"] = search_sessions(q, limit)
            if scope in ("all", "files"):
                out["files"] = search_files(os.getcwd(), q, limit)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], **out}
        return {"ok": True, **out}

    @app.get("/api/git")
    async def api_git():
        """当前工作区 Git 分支与状态（modified/untracked）。非 git 仓库返回 in_repo=False。"""
        import subprocess as _sub
        try:
            branch = _sub.run(["git", "branch", "--show-current"], cwd=os.getcwd(),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
            status = _sub.run(["git", "status", "--porcelain"], cwd=os.getcwd(),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        except Exception:
            return {"ok": False, "in_repo": False, "branch": "", "files": []}
        files = []
        for line in (status.stdout or "").splitlines():
            line = line.rstrip("\n")
            if not line.strip():
                continue
            code = line[:2].strip()
            path = line[3:].strip()
            state = "D" if code == "D" else "?" if "?" in code else "M"
            files.append({"path": path, "state": state})
        return {"ok": True, "in_repo": bool(branch.stdout.strip() or status.stdout),
                "branch": branch.stdout.strip(), "files": files[:100]}

    @app.get("/api/file-search")
    async def api_file_search(q: str = "", limit: int = 30):
        """工作区文件按文件名/内容搜索。"""
        from tools.search import search_files
        q = (q or "").strip()
        if not q:
            return {"ok": True, "files": []}
        return {"ok": True, "files": search_files(os.getcwd(), q, limit)}

    @app.post("/api/approve")
    async def api_approve(payload: dict = Body(...)):
        """审批决定回传（HTTP 兜底通道，WebSocket approval_response 之外）。"""
        req_id = str((payload or {}).get("id", ""))
        allow = bool((payload or {}).get("allow"))
        if not req_id:
            return {"ok": False, "error": "缺少审批 id"}
        matched = approval_broker.resolve(req_id, allow)
        return {"ok": True, "matched": matched}

    @app.post("/api/browser")
    async def api_side_browser(payload: dict = Body(...)):
        """侧栏辅助对话的只读浏览器通道。

        payload: {"command": "goto|text|snapshot|title|url|status|refresh|close", "args": "..."}
        白名单外的命令（click/type/js 等改动性操作）直接拒绝——侧栏只用来读网页。
        """
        command = str((payload or {}).get("command", "")).strip().lower()
        args = str((payload or {}).get("args", "") or "").strip()
        if command not in _SIDE_BROWSER_COMMANDS:
            return {
                "ok": False,
                "error": (
                    f"不允许的浏览器命令: '{command or '(空)'}'。"
                    f"允许: {', '.join(sorted(_SIDE_BROWSER_COMMANDS))}"
                ),
            }
        import concurrent.futures
        fut = _get_side_browser_executor().submit(_side_browser_execute, command, args)
        try:
            return fut.result(timeout=_SIDE_BROWSER_TIMEOUT)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            return {"ok": False, "error": f"浏览器操作超时（{_SIDE_BROWSER_TIMEOUT:.0f}s）"}

    @app.post("/api/ask")
    async def api_ask(payload: dict = Body(...)):
        """辅助对话：一次性 LLM 问答（不带工具、不占任务通道、不发事件）。

        payload.messages: [{"role": "user"|"assistant", "content": str}, ...]
        payload.browser_context: 可选，前端经 /api/browser 抓取的网页文本，
        会作为 system 上下文注入（由前端维护历史，后端无状态）。
        """
        import re as _re
        from openai import OpenAI
        from config import LLM_CONFIG
        from models.prompts import SIDE_CHAT_SYSTEM

        msgs_in = (payload or {}).get("messages") or []
        clean = [{"role": "system", "content": SIDE_CHAT_SYSTEM}]
        browser_context = _re.sub(
            r"\s+", " ", str((payload or {}).get("browser_context", ""))
        ).strip()[:12000]
        if browser_context:
            clean.append({
                "role": "system",
                "content": f"以下是用户刚让侧栏浏览器抓取的网页内容，回答相关问题时以此为准（可能被截断）：\n{browser_context}",
            })
        for m in msgs_in[-20:]:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "user"))
            if role not in ("user", "assistant"):
                role = "user"
            content = _re.sub(r"\s+", " ", str(m.get("content", "")))[:6000]
            if content.strip():
                clean.append({"role": role, "content": content})
        if len(clean) <= 1:
            return {"ok": False, "error": "messages 为空"}
        try:
            client = OpenAI(api_key=LLM_CONFIG["api_key"], base_url=LLM_CONFIG["base_url"])
            resp = client.chat.completions.create(
                model=LLM_CONFIG.get("default_model") or "gpt-4o-mini",
                messages=clean,
                max_tokens=2000,
                temperature=0.6,
            )
        except Exception as e:
            return {"ok": False, "error": f"辅助对话调用失败: {str(e)[:200]}"}
        choices = getattr(resp, "choices", None)
        content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        if not content:
            return {"ok": False, "error": "模型返回空内容"}
        return {"ok": True, "answer": content}

    @app.post("/api/test-llm")
    async def api_test_llm(payload: Optional[dict] = Body(None)):
        """用给定（或当前）主 LLM 配置做一次最小真实调用验证连通性。

        payload: {model, base_url, api_key} 均可选——提供即用（测试未保存的
        表单值）；缺省回退 .env。用于设置页「测试连接」按钮诊断。
        """
        import time as _t
        from models.llm import LLM
        p = payload or {}
        model = str(p.get("model") or "").strip() or None
        base_url = str(p.get("base_url") or "").strip() or None
        api_key = str(p.get("api_key") or "").strip() or None
        t0 = _t.time()
        try:
            llm = LLM(api_key=api_key, base_url=base_url, model=model)
            # 极小调用（max_tokens=8）：只验证配置可用与网络/鉴权
            text = llm.chat([{"role": "user", "content": "ping"}], max_tokens=8)
            return {
                "ok": True,
                "latency_ms": int((_t.time() - t0) * 1000),
                "reply": str(text or "")[:80],
                "endpoint": base_url or "(.env 默认)",
                "model": model or "(.env 默认)",
            }
        except Exception as e:
            return {
                "ok": False,
                "latency_ms": int((_t.time() - t0) * 1000),
                "error": str(e)[:300],
                "endpoint": base_url or "(.env 默认)",
                "model": model or "(.env 默认)",
            }

    @app.get("/api/config")
    async def api_config_get():
        """当前生效的运行时配置（视觉模型三件套，key 打码只回尾部）。"""
        return {"ok": True, "vision": _effective_vision()}

    @app.post("/api/config")
    async def api_config_set(payload: dict = Body(...)):
        """设置运行时覆盖（视觉模型三件套；空值=清除该项覆盖回退 .env）。"""
        vision = (payload or {}).get("vision") or {}
        for k in ("model", "base_url", "api_key"):
            if k in vision:
                v = str(vision.get(k) or "").strip()
                if v:
                    _VISION_OVERRIDES[k] = v
                else:
                    _VISION_OVERRIDES.pop(k, None)
        return {"ok": True, "vision": _effective_vision()}

    @app.post("/api/config/test-vision")
    async def api_config_test_vision(payload: Optional[dict] = Body(None)):
        """用视觉配置做一次真实调用验证连通性。

        payload.vision 可选：提供 model/base_url/api_key 时优先用它们（测试未保存的
        表单值）；否则用当前生效（含桌面端已下发覆盖）的配置。
        """
        import base64 as _b64
        from io import BytesIO

        from models.vision import VisionModel
        override = (payload or {}).get("vision") or {}
        model = str(override.get("model") or "").strip() or _VISION_OVERRIDES.get("model")
        base_url = str(override.get("base_url") or "").strip() or _VISION_OVERRIDES.get("base_url")
        api_key = str(override.get("api_key") or "").strip() or _VISION_OVERRIDES.get("api_key")
        try:
            vm = VisionModel(vision_model=model or None, base_url=base_url or None, api_key=api_key or None)
        except Exception as e:
            return {"ok": False, "error": f"构造视觉模型失败: {str(e)[:150]}"}
        try:
            # 造一张 64x64 测试图（红底白方块），不依赖屏幕状态
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (64, 64), (200, 30, 30))
            draw = ImageDraw.Draw(img)
            draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))
            buf = BytesIO()
            img.save(buf, format="PNG")
            b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
            answer = vm.analyze(
                b64,
                "这张图片的背景是什么颜色？中间是什么形状？用一句话回答。",
                max_tokens=80,
            )
            return {"ok": True, "model": vm.vision_model, "answer": answer[:300]}
        except Exception as e:
            return {"ok": False, "model": getattr(vm, "vision_model", ""),
                    "error": f"调用失败: {str(e)[:200]}"}

    @app.post("/api/rollback")
    async def api_rollback(payload: dict = Body(...)):
        """回滚工作区到某个 checkpoint 提交的树状态。

        不重写历史：差异文件恢复/移除后作为新提交落库，旧提交全部保留，
        之后仍可回滚到更早的检查点。commit 必须是 HEAD 的祖先。
        """
        from agent.snapshot import rollback_to
        commit = str((payload or {}).get("commit", "")).strip()
        if not commit:
            return {"ok": False, "error": "缺少 commit"}
        new_head = rollback_to(WORKSPACE_ROOT, commit)
        if not new_head:
            return {"ok": False, "error": "回滚失败：提交不存在、不是当前分支祖先，或 git 操作出错"}
        return {"ok": True, "head": new_head}

    @app.get("/api/sessions")
    async def api_sessions():
        """列出后端持久化的对话（agent/session.py SessionStore，conv-*.json）。"""
        from agent.session import SessionStore
        return {"ok": True, "sessions": SessionStore().list_conversations()}

    @app.get("/api/sessions/{session_id}")
    async def api_session_detail(session_id: str):
        """读取单个对话（含全部消息记录，供前端恢复 transcript）。"""
        from agent.session import SessionStore
        conv = SessionStore().load_conversation(session_id)
        if not conv:
            return {"ok": False, "error": "对话不存在"}
        return {"ok": True, "conversation": conv}

    @app.delete("/api/sessions/{session_id}")
    async def api_session_delete(session_id: str):
        """删除后端持久化的对话（前端删会话时同步调用，404 也算删除成功）。"""
        from agent.session import SessionStore
        deleted = SessionStore().delete_conversation(session_id)
        return {"ok": True, "deleted": deleted}

    @app.post("/api/sessions")
    async def api_session_create():
        """创建一个新会话（id 由后端生成 conv-YYYYMMDD-hex），落盘返回元信息。

        桌面端「新对话」走后端生成 id：保证 id 格式统一、重启后能从后端恢复，
        与 cmd 交互模式的会话命名空间一致。前端离线时可用本地兜底 id。
        """
        from agent.session import SessionStore, generate_conversation_id
        conv_id = generate_conversation_id()
        store = SessionStore()
        store.save_conversation(conv_id, messages=[], title="新对话")
        conv = store.load_conversation(conv_id) or {}
        return {"ok": True, "session": {
            "id": conv_id,
            "title": conv.get("title", "新对话"),
            "created_at": conv.get("created_at", ""),
            "updated_at": conv.get("updated_at", ""),
            "count": len(conv.get("messages", [])),
        }}

    @app.post("/api/demo")
    async def api_demo(payload: dict = Body(...)):
        """演示模式：不调用真实 Agent/API，模拟一次完整任务的事件流，
        让前端（聊天流 + 工作台）在无 LLM key 时也能完整演示。"""
        goal = str((payload or {}).get("goal", "")).strip()
        if not goal:
            return {"ok": False, "error": "goal 不能为空"}
        # run_start 由 _run_demo_worker 统一 emit，此处不重复发送
        threading.Thread(target=_run_demo_worker, args=(goal,), daemon=True).start()
        return {"ok": True, "mode": "demo"}

    # ---- Web 浏览器版控制面板（默认禁用）----
    # 桌面端（Electron）只使用 /api、/ws 做后端，浏览器版页面（GET / 与 /static）
    # 默认关闭。需要恢复浏览器版控制面板时，启动前设置 MY_AGENT_WEB_UI=1。
    _WEB_UI_ENABLED = os.getenv("MY_AGENT_WEB_UI", "") == "1"

    @app.get("/")
    async def root():
        if not _WEB_UI_ENABLED:
            return HTMLResponse(
                "<h1>Web 控制台已禁用</h1>"
                "<p>本服务仅供桌面端客户端（/api、/ws）使用。</p>"
                "<p>如需浏览器版控制面板，设置环境变量 <code>MY_AGENT_WEB_UI=1</code> 后重启。</p>"
            )
        index_path = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return HTMLResponse("<h1>Dashboard</h1><p>index.html not found</p>")

    if os.path.exists(STATIC_DIR) and _WEB_UI_ENABLED:
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

else:
    app = None


# ---------------- 运行时设置覆盖（桌面端「设置」下发） ----------------
# 视觉模型三件套：桌面端保存后经 /api/config 下发；只存内存不落盘（敏感信息
# 的持久化职责在桌面端 localStorage），后端重启后由前端自动重新同步。
_VISION_OVERRIDES: dict = {}


def _effective_vision() -> dict:
    """当前生效的视觉配置（key 打码，只回尾部 4 位）。"""
    from config import VISION_CONFIG, LLM_CONFIG
    model = _VISION_OVERRIDES.get("model") or VISION_CONFIG.get("vision_model") or ""
    base_url = _VISION_OVERRIDES.get("base_url") or VISION_CONFIG.get("base_url") or LLM_CONFIG.get("base_url", "")
    key = _VISION_OVERRIDES.get("api_key") or VISION_CONFIG.get("api_key") or LLM_CONFIG.get("api_key", "")
    return {
        "model": model,
        "base_url": base_url,
        "key_set": bool(key),
        "key_tail": ("***" + key[-4:]) if len(key) >= 4 else "",
    }


def _apply_vision_overrides(tool_manager) -> None:
    """把视觉覆盖应用到工具管理器里的 see / computer 工具（worker 构建后调用）。"""
    if not _VISION_OVERRIDES:
        return
    try:
        from models.vision import VisionModel
        vm = VisionModel(
            vision_model=_VISION_OVERRIDES.get("model") or None,
            base_url=_VISION_OVERRIDES.get("base_url") or None,
            api_key=_VISION_OVERRIDES.get("api_key") or None,
        )
    except Exception:
        return
    for name in ("see", "computer"):
        try:
            t = tool_manager.get_tool(name)
            if t is not None and hasattr(t, "_vision_model"):
                t._vision_model = vm
        except Exception:
            pass


def _run_agent_worker(goal: str, kwargs: dict = None, side_of: str = ""):
    """后台线程执行一次 Agent 任务（供 /api/run 与 /api/side-run 调用）。

    side_of 非空 = 辅助 Agent：会话命名空间 side-<main_session_id>、事件打
    panel="side"、完成时把摘要写入主会话记录并 emit side_complete（主 Agent
    下次运行恢复历史时能看到辅助 Agent 干了什么）。

    kwargs: 前端传入的 LLM 配置 / 模型参数 / 权限模式（均可选）。
    """
    kwargs = kwargs or {}
    is_side = bool(side_of)

    # 外部 Runtime 分派：外部 CLI / custom → 子进程桥接（不走内置 Agent）
    runtime = str(kwargs.get("runtime") or "").strip() or "myagent"
    if runtime != "myagent":
        from agent.runtime import run_external
        from dashboard.hub import get_dashboard_hub as _hub_get
        conv_id_x = str(kwargs.get("session_id") or "").strip()
        if is_side:
            main_id = str(side_of).strip()
            conv_id_x = f"side-{main_id}" if main_id else (conv_id_x or "")
        if not conv_id_x:
            from agent.session import generate_conversation_id
            conv_id_x = generate_conversation_id()

        def _on_ev(event_type, data):
            try:
                d = dict(data)
                if is_side:
                    d["panel"] = "side"
                    d["side_of"] = str(side_of).strip()
                _hub_get().emit(event_type, d)
            except Exception:
                pass

        stop_ev = (side_run_control if is_side else run_control).begin(conv_id_x)
        try:
            run_external(runtime, goal, cwd=os.getcwd(), on_event=_on_ev,
                         stop_event=stop_ev, cfg=kwargs.get("runtime_config"))
        finally:
            (side_run_control if is_side else run_control).end(conv_id_x)
        return

    try:
        from agent import Agent, AgentConfig
        from agent.memory import Memory
        from tools import ToolManager

        # 权限模式：运行时实时可切换——policy 用可调用对象，每次工具审批都
        # 重新读取该会话的当前模式（auto→never / ask→on-request / block→untrusted）；
        # approver 恒定走审批桥，broker 在询问前也按当前模式裁决（auto 放行不打扰）。
        perm_mode = str(kwargs.get("permission_mode") or "").lower() or "ask"
        conv_id_y = str(kwargs.get("session_id") or "default")
        permission_modes.set(conv_id_y, perm_mode)

        config = AgentConfig(verbose=False)
        if kwargs.get("agent_name"):
            # 用户自定义名字：写进系统提示词（agent.py _agent_name）
            config.agent_name = str(kwargs["agent_name"]).strip()
        if kwargs.get("model") or kwargs.get("base_url") or kwargs.get("api_key"):
            config.llm_model = kwargs.get("model") or config.llm_model
            config.llm_base_url = kwargs.get("base_url") or config.llm_base_url
            config.llm_api_key = kwargs.get("api_key") or config.llm_api_key
        config.approval_policy = lambda: _mode_to_policy(permission_modes.get(conv_id_y))
        config.approver = approval_broker.approver_for(conv_id_y)
        if perm_mode == "block":
            config.sandbox_mode = "read-only"
        elif kwargs.get("sandbox_mode"):
            config.sandbox_mode = kwargs["sandbox_mode"]
        # 模型参数（temperature/top_p/max_tokens）经 AgentConfig 下发
        if kwargs.get("temperature") is not None:
            config.temperature = float(kwargs["temperature"])
        if kwargs.get("top_p") is not None:
            config.top_p = float(kwargs["top_p"])
        if kwargs.get("max_tokens") is not None:
            config.max_tokens = int(kwargs["max_tokens"])
        # 任务最大操作轮数：>0 覆盖（0/缺省 → TOOL_CONFIG.max_loop_ops 或 .env MAX_LOOP_OPS）
        if kwargs.get("max_ops"):
            try:
                config.max_ops = max(1, int(kwargs["max_ops"]))
            except (TypeError, ValueError):
                pass

        tool_manager = ToolManager()
        # 桌面端「设置」下发的视觉模型覆盖：应用到 see / computer 工具
        _apply_vision_overrides(tool_manager)
        # 会话隔离（重要）：Memory 按 chat_id 分目录存储长期记忆/经验/失败模式。
        # 辅助 Agent 用独立命名空间 side-<main_id>，与主对话互不污染。
        conv_id = str(kwargs.get("session_id") or "").strip()
        if is_side:
            main_id = str(side_of).strip()
            conv_id = f"side-{main_id}" if main_id else (conv_id or "")
        if not conv_id:
            from agent.session import generate_conversation_id
            conv_id = generate_conversation_id()
        import re as _re_conv
        mem_chat_id = _re_conv.sub(r"[^A-Za-z0-9._-]", "_", conv_id) or "conv_default"
        agent = Agent(
            tool_manager=tool_manager,
            memory=Memory(chat_id=mem_chat_id),
            config=config,
        )
        if is_side:
            agent._events_panel = "side"
            agent._events_side_of = main_id

        # 会话连续性：绑定 session_name（Agent 每轮结束自动全量保存对话）
        restored_conv = False
        try:
            from agent.session import SessionStore
            if conv_id:
                config.session_name = conv_id
                conv = SessionStore().load_conversation(conv_id)
                if conv:
                    restored_conv = True
                    for m in conv.get("messages", []):
                        agent.memory.add_message(m.get("role", "user"), m.get("content", ""))
                    agent.last_execution_summary = conv.get("last_summary", "")
                    try:
                        agent._restore_conversation_model(conv)
                    except Exception:
                        pass
        except Exception as e:
            # 会话恢复失败不阻塞任务执行（等同无会话运行），但必须留下线索
            try:
                hub.emit("error", {"message": f"会话恢复失败（忽略，不影响执行）: {str(e)[:200]}"})
            except Exception:
                pass

        stop_ev = (side_run_control if is_side else run_control).begin(conv_id)
        try:
            # 恢复过会话历史 → 打开 keep_session：_run_loop 才会把之前的对话记录
            # 注入上下文（否则 agent 每次都是裸奔上下文，对"没做完的任务"一无所知）
            result_text = agent.run(goal, keep_session=restored_conv, stop_event=stop_ev)
        finally:
            (side_run_control if is_side else run_control).end(conv_id)

        # 侧任务完成：摘要写入主会话记录 + 通知前端（主 Agent 下次运行恢复历史时能看到）
        if is_side:
            try:
                from agent.session import SessionStore
                summary = (result_text or "")[:500]
                store = SessionStore()
                main_conv = store.load_conversation(main_id) or {}
                msgs = list(main_conv.get("messages", []))
                msgs.append({
                    "role": "system",
                    "content": f"[辅助Agent] 完成子任务「{goal[:60]}」：\n{summary}",
                })
                store.save_conversation(
                    main_id, messages=msgs,
                    title=main_conv.get("title", ""),
                    last_summary=main_conv.get("last_summary", ""),
                )
                hub.emit("side_complete", {
                    "panel": "side",
                    "main_session_id": main_id,
                    "goal": goal[:120],
                    "summary": summary,
                })
            except Exception as e:
                try:
                    hub.emit("error", {"message": f"侧任务结果写入主会话失败: {str(e)[:150]}"})
                except Exception:
                    pass
    except Exception as e:
        hub.emit("error", {"message": f"运行失败: {str(e)[:300]}",
                           **({"panel": "side"} if is_side else {})})
        hub.emit("run_end", {"status": "failed",
                             **({"panel": "side"} if is_side else {})})


def _run_scheduled_task(t):
    """定时任务到期：复用 _run_agent_worker 跑一次任务，事件推给前端。"""
    try:
        goal = str(t.get("goal") or "").strip()
        if not goal:
            return
        threading.Thread(
            target=_run_agent_worker,
            args=(goal, {"runtime": t.get("runtime") or "myagent"}),
            daemon=True,
        ).start()
    except Exception as e:
        try:
            hub.emit("error", {"message": f"定时任务启动失败: {str(e)[:200]}"})
        except Exception:
            pass


def _run_demo_worker(goal: str):
    """演示模式：不调用真实 Agent/API，模拟一次完整任务的事件流，
    让前端（聊天流 + 工作台）在无 LLM key 时也能完整演示。

    事件序列与真实 Agent 一致（run_start → plan → step_start →
    tool_call → tool_result ×N → answer → run_end），前端无需改动。
    """
    import time
    sleep = time.sleep
    try:
        hub.emit("run_start", {"goal": goal, "mode": "demo"})
        sleep(0.4)

        steps = [
            "拆解目标：明确任务范围与验收标准",
            "收集信息：检索资料与代码上下文",
            "制定方案：确定实现路径与风险点",
            "执行落地：调用工具逐步完成任务",
            "检查收尾：核对结果并输出总结",
        ]
        hub.emit("plan", {"steps": steps})
        sleep(0.8)

        # 每步 (步骤描述, [(工具, 输入, 输出), ...])
        script = [
            (steps[0], [
                ("think", "用户目标：{goal}。先拆解为可执行子任务，明确输入与验收标准。",
                 "已拆解为 5 个子任务：信息收集 → 方案 → 实现 → 验证 → 总结"),
            ]),
            (steps[1], [
                ("python", "扫描工作区文件，统计模块规模与依赖关系",
                 "共 128 个文件，核心模块 6 个，依赖关系已整理成表格"),
                ("browser", "打开相关资料页，抓取关键说明",
                 "抓取 3 个页面，提炼要点 18 条"),
            ]),
            (steps[2], [
                ("think", "对比两条候选路径的成本与风险，择优执行",
                 "选定路径 A（改动小、风险低），路径 B 留作备选"),
            ]),
            (steps[3], [
                ("edit", "修改 dashboard/server.py，补全演示模式端点实现",
                 "已添加 /api/demo 路由与 _run_demo_worker 事件流"),
                ("terminal", "运行测试：.venv\\Scripts\\python -m pytest tests -q",
                 "全部 42 个测试通过"),
            ]),
            (steps[4], [
                ("think", "复核各步骤输出是否与目标对齐，整理最终总结",
                 "目标全部达成，输出总结"),
            ]),
        ]

        for i, (desc, calls) in enumerate(script, 1):
            hub.emit("step_start", {"step": i, "total": len(script), "description": desc})
            sleep(0.5)
            for tool, inp, out in calls:
                hub.emit("tool_call", {"tool": tool, "input": inp.format(goal=goal)})
                sleep(0.6)
                hub.emit("tool_result", {"tool": tool, "success": True, "output": out})
                sleep(0.3)

        summary = (
            f"演示任务《{goal}》已完成（演示模式，未调用真实 API）。\n\n"
            "执行过程：共 5 个步骤、8 次工具调用，全部成功。\n"
            "右侧工作台可查看计划、工具调用日志与统计；"
            "配置好 API key 后，这里展示的就是真实执行过程。"
        )
        hub.emit("answer", {"output": summary})
        sleep(0.4)
        hub.emit("run_end", {"status": "completed", "summary": summary})
    except Exception as e:
        hub.emit("error", {"message": f"演示失败: {str(e)[:200]}"})
        hub.emit("run_end", {"status": "failed", "error": str(e)[:200]})


def _get_lan_ip() -> str:
    """获取本机局域网 IP（无网络时返回空串）。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def start_server(host: str = "127.0.0.1", port: int = 8080):
    """启动 Dashboard 服务器（默认仅本机可达，安全对齐本地服务）。"""
    if not HAS_FASTAPI:
        print("[Dashboard] FastAPI/uvicorn 未安装，请运行: pip install fastapi uvicorn")
        return
    # 用 127.0.0.1 而非 localhost：uvicorn 仅监听 IPv4，
    # 浏览器解析 localhost 可能优先走 IPv6 (::1) 导致"打不开"
    print(f"\n  Dashboard: http://127.0.0.1:{port}")
    if host not in ("127.0.0.1", "localhost"):
        lan_ip = _get_lan_ip()
        if lan_ip:
            print(f"  局域网访问: http://{lan_ip}:{port}（远程浏览器访问需防火墙放行 {port} 端口）")
    print(f"  API 文档:  http://127.0.0.1:{port}/docs\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    # 支持 python -m dashboard.server [--port N] [--host H]
    port = 8080
    host = "127.0.0.1"
    argv = sys.argv[1:]
    try:
        if "--port" in argv:
            port = int(argv[argv.index("--port") + 1])
        if "--host" in argv:
            host = argv[argv.index("--host") + 1]
    except (ValueError, IndexError):
        pass
    start_server(host=host, port=port)
