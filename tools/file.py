"""
文件操作工具模块（v2：JSON Schema + 结构化调用）。
提供文件读写、目录浏览、文件信息查询等能力。

v2 变化：
- schema: {"operation": read|write|list|exists|info, "path": str, "content": str}
- execute_json() 把结构化参数映射到旧字符串命令，复用全部内部逻辑
- 风险分级：读操作 low，写操作 medium
"""
import os
import json
from typing import Any, Dict

from tools.base import BaseTool, ToolResult


class FileTool(BaseTool):
    """文件系统操作工具。"""

    risk_level: str = "medium"           # 读操作为 low，写操作为 medium（execute_json 内细化）
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"  # 读操作允许；写操作由审批策略按沙箱等级把关

    def is_parallel_safe(self, arguments: Dict[str, Any]) -> bool:
        # 只读操作可并行（read/list/exists/info）；write 有写冲突风险，保持串行
        return str(arguments.get("operation", "")).lower() in ("read", "list", "exists", "info")

    @property
    def name(self) -> str:
        return "file"

    @property
    def description(self) -> str:
        return (
            "文件操作工具。支持以下操作：\n"
            "  - read <文件路径>: 读取文件内容\n"
            "  - write <文件路径> <内容>: 写入文件（覆盖）\n"
            "  - list <目录路径>: 列出目录中的文件\n"
            "  - exists <路径>: 检查文件或目录是否存在\n"
            "  - info <文件路径>: 获取文件信息（大小、修改时间等）\n"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "write", "list", "exists", "info"],
                    "description": "要执行的文件操作",
                },
                "path": {"type": "string", "description": "文件或目录路径"},
                "content": {
                    "type": "string",
                    "description": (
                        "文件内容（仅 operation=write 时需要）。重要：单次写入请控制在 "
                        "4K 字符以内——过长内容会被模型输出上限截断导致参数损坏。"
                        "写大文件请先 write 最短骨架，再用 file append 逐段追加。"
                    ),
                },
            },
            "required": ["operation", "path"],
        }

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        operation = str(arguments.get("operation", "")).lower()
        path = str(arguments.get("path", "")).strip()
        content = arguments.get("content", "")

        if operation == "write":
            return self.execute(f"write {path} {content}")
        if operation in ("read", "list", "exists", "info"):
            return self.execute(f"{operation} {path}")
        return ToolResult(
            success=False, output="",
            error=f"未知操作: '{operation}'。支持: read/write/list/exists/info",
        )

    def build_approval_request(self, arguments: Dict[str, Any]):
        from agent.approval import ApprovalRequest

        operation = str(arguments.get("operation", "")).lower()
        path = str(arguments.get("path", "")).strip()
        is_write = operation in ("write",)
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=f"file {operation} {path}",
            risk_level="medium" if is_write else "low",
            min_sandbox_mode="workspace-write" if is_write else "read-only",
        )

    def execute(self, input_str: str) -> ToolResult:
        parts = input_str.strip().split(maxsplit=1)
        if not parts:
            return ToolResult(success=False, output="", error="操作指令为空。")

        action = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        action_map = {
            "read": self._read_file,
            "write": self._write_file,
            "list": self._list_dir,
            "exists": self._check_exists,
            "info": self._file_info,
        }

        handler = action_map.get(action)
        if handler is None:
            return ToolResult(
                success=False,
                output="",
                error=f"未知操作: '{action}'。支持的操作: {', '.join(action_map.keys())}",
            )
        return handler(args)

    def _read_file(self, path: str) -> ToolResult:
        path = path.strip()
        if not os.path.exists(path):
            return ToolResult(success=False, output="", error=f"文件不存在: {path}")
        if os.path.isdir(path):
            return ToolResult(success=False, output="", error=f"路径是目录，不是文件: {path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return ToolResult(success=True, output=content)
        except UnicodeDecodeError:
            return ToolResult(
                success=False, output="", error=f"无法以文本方式读取: {path}（可能是二进制文件）"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _write_file(self, args: str) -> ToolResult:
        """args 格式: <路径> <内容>"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return ToolResult(
                success=False, output="", error="写入操作需要: write <路径> <内容>"
            )
        path = parts[0].strip()
        content = parts[1]
        # diff 追踪：写入前留旧内容快照（新建文件为 None）
        from tools.change_tracker import get_change_tracker
        tracker = get_change_tracker()
        old_content = tracker.snapshot(path)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            tracker.record_with_old(path, self.name, old_content)
            return ToolResult(success=True, output=f"文件已写入: {path}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _list_dir(self, path: str) -> ToolResult:
        path = path.strip() or "."
        if not os.path.exists(path):
            return ToolResult(success=False, output="", error=f"目录不存在: {path}")
        if not os.path.isdir(path):
            return ToolResult(success=False, output="", error=f"路径不是目录: {path}")
        try:
            items = os.listdir(path)
            result_lines = [f"目录 '{path}' 的内容 ({len(items)} 项):"]
            for item in sorted(items):
                item_path = os.path.join(path, item)
                tag = "[目录]" if os.path.isdir(item_path) else "[文件]"
                result_lines.append(f"  {tag} {item}")
            return ToolResult(success=True, output="\n".join(result_lines))
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _check_exists(self, path: str) -> ToolResult:
        path = path.strip()
        exists = os.path.exists(path)
        kind = "目录" if os.path.isdir(path) else "文件" if exists else "不存在"
        return ToolResult(success=True, output=f"路径 '{path}' {'存在' if exists else '不存在'}，类型: {kind}")

    def _file_info(self, path: str) -> ToolResult:
        path = path.strip()
        if not os.path.exists(path):
            return ToolResult(success=False, output="", error=f"文件不存在: {path}")
        try:
            stat = os.stat(path)
            import datetime
            info = {
                "路径": path,
                "大小(字节)": stat.st_size,
                "最后修改": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "最后访问": datetime.datetime.fromtimestamp(stat.st_atime).isoformat(),
                "是目录": os.path.isdir(path),
            }
            return ToolResult(success=True, output=json.dumps(info, ensure_ascii=False, indent=2))
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
