"""
任务中心（借鉴同类实现的想法 + 任务状态机）。

- 想法（thought）：快速速记不成熟的想法，持久化到 memory/thoughts.json。
- 任务（task）：把已确认的目标沉淀为可追踪状态机（todo → in_progress → done/failed），
  带执行日志，可"恢复"失败任务。持久化到 memory/tasks.json（全局，跨会话）。
- 会话级待办清单（todo_write）保留不动：那是模型在单个会话里的任务清单；
  这里是全局任务中心。

变更时 emit `tasks` / `thoughts` 事件，供桌面端任务中心面板实时刷新。
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

from tools.base import BaseTool, ToolResult

_MEM_DIR = "memory"
_TASKS_FILE = os.path.join(_MEM_DIR, "tasks.json")
_THOUGHTS_FILE = os.path.join(_MEM_DIR, "thoughts.json")

STATUSES = ("todo", "in_progress", "done", "failed")


def _load(path: str, default: list) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else default
    except Exception:
        return default


def _save(path: str, data: list) -> None:
    os.makedirs(_MEM_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_tasks() -> list:
    return _load(_TASKS_FILE, [])


def save_tasks(tasks: list) -> None:
    _save(_TASKS_FILE, tasks)


def load_thoughts() -> list:
    return _load(_THOUGHTS_FILE, [])


def save_thoughts(thoughts: list) -> None:
    _save(_THOUGHTS_FILE, thoughts)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


class TaskTool(BaseTool):
    """任务中心工具：创建/流转任务状态 + 记执行日志（全局，跨会话）。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"

    def __init__(self):
        self._emit_fn = None

    def set_emit(self, fn) -> None:
        self._emit_fn = fn

    @property
    def name(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return (
            "任务中心工具（全局任务状态机，跨会话）。把已确认的目标沉淀为可追踪任务：\n"
            "  task create <标题>            新建任务（todo）\n"
            "  task status <id> <状态>       流转状态（todo/in_progress/done/failed）\n"
            "  task log <id> <内容>          给任务记一条执行日志\n"
            "  task complete <id>            标记完成\n"
            "  task list                     查看全部任务\n"
            "任务带状态与日志，失败后可恢复；适合长期/跨会话的工作。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "status", "log", "complete", "list"],
                    "description": "要执行的操作",
                },
                "id": {"type": "string", "description": "status/log/complete 的目标任务 id"},
                "title": {"type": "string", "description": "create 的任务标题"},
                "status": {"type": "string", "enum": list(STATUSES),
                           "description": "status 要流转到的状态"},
                "content": {"type": "string", "description": "log 的日志内容"},
            },
            "required": ["operation"],
        }

    def _emit(self, tasks: list) -> None:
        if self._emit_fn:
            try:
                self._emit_fn("tasks", {"tasks": tasks})
            except Exception:
                pass

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        op = str(arguments.get("operation", "")).strip().lower()
        tasks = load_tasks()
        if op == "list":
            return self._render(tasks)
        if op == "create":
            title = str(arguments.get("title", "")).strip()
            if not title:
                return ToolResult(success=False, output="", error="create 需要 title。")
            t = {"id": f"t{int(time.time()*1000)}{len(tasks)}", "title": title,
                 "status": "todo", "created": _now(), "updated": _now(), "logs": []}
            tasks.append(t)
            save_tasks(tasks)
            self._emit(tasks)
            return self._render(tasks)
        if op == "complete":
            return self._set_status(tasks, arguments, "done")
        if op == "status":
            status = str(arguments.get("status", "")).strip()
            if status not in STATUSES:
                return ToolResult(success=False, output="",
                                  error=f"非法状态: {status}（可用 {', '.join(STATUSES)}）")
            return self._set_status(tasks, arguments, status)
        if op == "log":
            tid = str(arguments.get("id", "")).strip()
            content = str(arguments.get("content", "")).strip()
            hit = next((t for t in tasks if t.get("id") == tid), None)
            if not hit:
                return ToolResult(success=False, output="", error=f"找不到任务 id: {tid}")
            if not content:
                return ToolResult(success=False, output="", error="log 需要 content。")
            hit.setdefault("logs", []).append(f"[{_now()}] {content}")
            hit["updated"] = _now()
            save_tasks(tasks)
            self._emit(tasks)
            return self._render(tasks)
        return ToolResult(success=False, output="", error=f"未知操作: '{op}'")

    def _set_status(self, tasks: list, args: Dict[str, Any], status: str) -> ToolResult:
        tid = str(args.get("id", "")).strip()
        hit = next((t for t in tasks if t.get("id") == tid), None)
        if not hit:
            return ToolResult(success=False, output="", error=f"找不到任务 id: {tid}")
        hit["status"] = status
        hit["updated"] = _now()
        save_tasks(tasks)
        self._emit(tasks)
        return self._render(tasks)

    def execute(self, input_str: str) -> ToolResult:
        parts = input_str.strip().split(maxsplit=2)
        if not parts:
            return ToolResult(success=False, output="", error="用法: task create|status|log|complete|list ...")
        op = parts[0].lower()
        if op == "list":
            return self.execute_json({"operation": "list"})
        if op == "create":
            return self.execute_json({"operation": "create", "title": parts[1] if len(parts) > 1 else ""})
        if op in ("status", "complete", "log"):
            sub = parts[1].split(maxsplit=1) if len(parts) > 1 else []
            tid = sub[0] if sub else ""
            rest = sub[1] if len(sub) > 1 else ""
            if op == "complete":
                return self.execute_json({"operation": "complete", "id": tid})
            if op == "status":
                return self.execute_json({"operation": "status", "id": tid, "status": rest})
            return self.execute_json({"operation": "log", "id": tid, "content": rest})
        return ToolResult(success=False, output="", error=f"未知操作: '{op}'")

    @staticmethod
    def _render(tasks: list) -> ToolResult:
        if not tasks:
            return ToolResult(success=True, output="任务中心为空。")
        icons = {"done": "✓", "in_progress": "▶", "failed": "✗", "todo": "○"}
        lines = [f"任务中心（{len(tasks)} 个）:"]
        for t in tasks:
            icon = icons.get(t.get("status", "todo"), "○")
            lines.append(f"  {icon} [{t.get('id')}] {t.get('title', '')}（{t.get('status')}）")
            for log in (t.get("logs") or [])[-3:]:
                lines.append(f"      · {log}")
        return ToolResult(success=True, output="\n".join(lines))


class ThoughtTool(BaseTool):
    """想法速记工具：把零散念头记下来，稍后对齐成任务。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"

    def __init__(self):
        self._emit_fn = None

    def set_emit(self, fn) -> None:
        self._emit_fn = fn

    @property
    def name(self) -> str:
        return "thought"

    @property
    def description(self) -> str:
        return "想法速记：thought add <内容> —— 记录一个零散想法/灵感（可稍后对齐成任务）。"

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["add"]},
                "content": {"type": "string", "description": "想法内容"},
            },
            "required": ["operation"],
        }

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        content = str(arguments.get("content", "")).strip()
        if not content:
            return ToolResult(success=False, output="", error="thought add 需要内容。")
        thoughts = load_thoughts()
        thoughts.append({"id": f"th{int(time.time()*1000)}{len(thoughts)}",
                         "content": content[:500], "created": _now()})
        save_thoughts(thoughts)
        if self._emit_fn:
            try:
                self._emit_fn("thoughts", {"thoughts": thoughts})
            except Exception:
                pass
        return ToolResult(success=True, output=f"想法已记录（共 {len(thoughts)} 条）。")

    def execute(self, input_str: str) -> ToolResult:
        rest = input_str.strip()
        if rest.lower().startswith("add "):
            rest = rest[4:].strip()
        return self.execute_json({"operation": "add", "content": rest})
