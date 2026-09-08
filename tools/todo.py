"""
待办清单工具（参照上游同类 `todo`：会话级任务清单，跨轮持久）。

模型用 todo_write 维护当前会话的任务清单（增 / 进行中 / 勾掉），
清单按会话 id 持久化到 memory/todos/<session>.json，同一会话的后续轮次
可继续看到与更新；桌面端收到 `todo` 事件后常驻显示进度。

- 会话隔离：key（会话 id）由 Agent 注入，缺失时落到 default（不跨会话污染）。
- 每次变更 emit `todo` 事件（{todos:[...]}），供 UI 实时刷新。
"""
import os
import re
import time
from typing import Any, Dict, List, Optional

from tools.base import BaseTool, ToolResult

_TODO_DIR = os.path.join("memory", "todos")


def _safe_name(key: str) -> str:
    """会话 key → 安全文件名（防路径穿越）。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", key or "")
    return cleaned[:80] or "default"


class TodoTool(BaseTool):
    """会话级待办清单（todo_write）。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"

    def __init__(self):
        self._key = ""
        self._emit_fn = None

    def set_session_key(self, key: str) -> None:
        self._key = key or ""

    def set_emit(self, fn) -> None:
        """注入事件回调（Agent 的 _emit），清单变更时通知前端。"""
        self._emit_fn = fn

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "待办清单工具（会话级任务列表，跨轮持久）。规划任务时用它建清单，\n"
            "  todo_write add <标题>            新增待办\n"
            "  todo_write update <id> <新标题>  改标题\n"
            "  todo_write done <id>             标记完成\n"
            "  todo_write delete <id>           删除一项\n"
            "  todo_write clear                 清空清单\n"
            "  todo_write list                  查看当前清单\n"
            "清单会随会话保存并在界面展示，方便追踪任务进度。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["add", "update", "done", "delete", "clear", "list"],
                    "description": "要执行的操作",
                },
                "id": {"type": "string", "description": "update/done/delete 的目标待办 id"},
                "title": {"type": "string", "description": "add/update 的标题"},
            },
            "required": ["operation"],
        }

    # ---------------------------------------------------------------
    # 存储
    # ---------------------------------------------------------------

    def _path(self) -> str:
        return os.path.join(_TODO_DIR, f"{_safe_name(self._key)}.json")

    def _load(self) -> List[dict]:
        try:
            import json
            with open(self._path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self, todos: List[dict]) -> None:
        import json
        os.makedirs(_TODO_DIR, exist_ok=True)
        with open(self._path(), "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)
        self._emit(todos)

    def _emit(self, todos: List[dict]) -> None:
        if self._emit_fn:
            try:
                self._emit_fn("todo", {"todos": todos})
            except Exception:
                pass

    # ---------------------------------------------------------------
    # 入口
    # ---------------------------------------------------------------

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        op = str(arguments.get("operation", "")).strip().lower()
        if op == "list":
            todos = self._load()
            return self._render(todos)

        todos = self._load()
        if op == "add":
            title = str(arguments.get("title", "")).strip()
            if not title:
                return ToolResult(success=False, output="", error="add 需要 title。")
            todo = {"id": f"t{int(time.time()*1000)}{len(todos)}", "title": title, "status": "todo"}
            todos.append(todo)
            self._save(todos)
            return self._render(todos)
        if op == "done":
            todo_id = str(arguments.get("id", "")).strip()
            hit = next((t for t in todos if t.get("id") == todo_id), None)
            if not hit:
                return ToolResult(success=False, output="", error=f"找不到待办 id: {todo_id}")
            hit["status"] = "done"
            self._save(todos)
            return self._render(todos)
        if op == "delete":
            todo_id = str(arguments.get("id", "")).strip()
            before = len(todos)
            todos = [t for t in todos if t.get("id") != todo_id]
            if len(todos) == before:
                return ToolResult(success=False, output="", error=f"找不到待办 id: {todo_id}")
            self._save(todos)
            return self._render(todos)
        if op == "update":
            todo_id = str(arguments.get("id", "")).strip()
            title = str(arguments.get("title", "")).strip()
            hit = next((t for t in todos if t.get("id") == todo_id), None)
            if not hit:
                return ToolResult(success=False, output="", error=f"找不到待办 id: {todo_id}")
            if title:
                hit["title"] = title
            self._save(todos)
            return self._render(todos)
        if op == "clear":
            self._save([])
            return ToolResult(success=True, output="待办清单已清空。")
        return ToolResult(success=False, output="", error=f"未知操作: '{op}'（add/update/done/delete/clear/list）")

    def execute(self, input_str: str) -> ToolResult:
        """旧文本协议：todo_write add 标题 / todo_write done id / ..."""
        parts = input_str.strip().split(maxsplit=1)
        if not parts:
            return ToolResult(success=False, output="", error="用法: todo_write <操作> [参数]")
        op = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if op == "list":
            return self.execute_json({"operation": "list"})
        if op == "add":
            return self.execute_json({"operation": "add", "title": rest})
        if op == "clear":
            return self.execute_json({"operation": "clear"})
        if op in ("done", "delete", "update"):
            sub = rest.split(maxsplit=1)
            todo_id = sub[0] if sub else ""
            title = sub[1] if len(sub) > 1 else ""
            return self.execute_json({"operation": op, "id": todo_id, "title": title})
        return ToolResult(success=False, output="", error=f"未知操作: '{op}'")

    @staticmethod
    def _render(todos: List[dict]) -> ToolResult:
        if not todos:
            return ToolResult(success=True, output="待办清单为空。")
        lines = [f"待办清单（{len(todos)} 项）:"]
        icons = {"done": "✓", "in_progress": "▶", "todo": "○"}
        for t in todos:
            icon = icons.get(t.get("status", "todo"), "○")
            lines.append(f"  {icon} [{t.get('id')}] {t.get('title', '')}")
        return ToolResult(success=True, output="\n".join(lines))
