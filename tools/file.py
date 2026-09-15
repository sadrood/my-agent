"""
文件操作工具模块（v2：JSON Schema + 结构化调用）。
提供文件读写、目录浏览、文件信息查询等能力。

v2 变化：
- schema: {"operation": read|write|append|copy|move|list|exists|info, ...}
- execute_json() 把结构化参数映射到旧字符串命令，复用全部内部逻辑
- 风险分级：读操作 low，写操作 medium

v3 变化（2026-09-15）：
- 补齐 append：schema 与 executor 提示词一直在教模型「用 file append 分段写大文件」，
  但本工具从未实现该操作，模型照做必然失败。
- 补齐 copy/move：复制文件的正规入口。此前只有 write（纯文本覆盖），而 shutil 被
  python 工具黑名单正确拦下 → 模型想复制产物图片时无路可走（实测连续 3 轮瞎猜）。
"""
import os
import shutil
import json
from typing import Any, Dict

from tools.base import BaseTool, ToolResult


class FileTool(BaseTool):
    """文件系统操作工具。"""

    risk_level: str = "medium"           # 读操作为 low，写操作/复制移动为 medium（execute_json 内细化）
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"  # 读操作允许；写操作由审批策略按沙箱等级把关

    def is_parallel_safe(self, arguments: Dict[str, Any]) -> bool:
        # 只读操作可并行（read/list/exists/info）；写/复制/移动有冲突风险，保持串行
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
            "  - append <文件路径> <内容>: 追加内容到文件末尾（不存在则创建）\n"
            "  - copy <源路径> <目标路径>: 复制文件或目录（目标为已存在目录时复制到其中）\n"
            "  - move <源路径> <目标路径>: 移动/重命名文件或目录\n"
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
                    "enum": ["read", "write", "append", "copy", "move", "list", "exists", "info"],
                    "description": "要执行的文件操作",
                },
                "path": {
                    "type": "string",
                    "description": "文件或目录路径（copy/move 时为源路径）",
                },
                "destination": {
                    "type": "string",
                    "description": "目标路径（仅 operation=copy/move 时需要）",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "文件内容（仅 operation=write/append 时需要）。重要：单次写入请控制在 "
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

        if operation in ("write", "append"):
            return self.execute(f"{operation} {path} {content}")
        if operation in ("copy", "move"):
            dest = str(arguments.get("destination", "")).strip()
            return self.execute(f"{operation} {path} {dest}")
        if operation in ("read", "list", "exists", "info"):
            return self.execute(f"{operation} {path}")
        return ToolResult(
            success=False, output="",
            error=f"未知操作: '{operation}'。支持: read/write/append/copy/move/list/exists/info",
        )

    def build_approval_request(self, arguments: Dict[str, Any]):
        from agent.approval import ApprovalRequest

        operation = str(arguments.get("operation", "")).lower()
        path = str(arguments.get("path", "")).strip()
        is_write = operation in ("write", "append", "copy", "move")
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=f"file {operation} {path}",
            risk_level="medium" if is_write else "low",
            min_sandbox_mode="workspace-write" if is_write else "read-only",
        )

    def execute(self, input_str: str) -> ToolResult:
        # 这里只 lstrip、不 strip：write/append 的内容必须原样落盘。
        # 此前对整串 strip，导致「以换行结尾的文本文件」写出来丢掉末尾换行。
        # 路径类参数由各自的 handler 自行 strip，因此不受影响。
        if not input_str or not input_str.strip():
            return ToolResult(success=False, output="", error="操作指令为空。")

        parts = input_str.lstrip().split(maxsplit=1)
        action = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        action_map = {
            "read": self._read_file,
            "write": self._write_file,
            "append": self._append_file,
            "copy": self._copy,
            "move": self._move,
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

    def _append_file(self, args: str) -> ToolResult:
        """args 格式: <路径> <内容>（追加到文件末尾，文件不存在则创建）。"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return ToolResult(
                success=False, output="", error="追加操作需要: append <路径> <内容>"
            )
        path = parts[0].strip()
        content = parts[1]
        # diff 追踪：追加前留旧内容快照，便于展示增量 diff
        from tools.change_tracker import get_change_tracker
        tracker = get_change_tracker()
        old_content = tracker.snapshot(path)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(content)
            tracker.record_with_old(path, self.name, old_content)
            size = os.path.getsize(path)
            return ToolResult(
                success=True,
                output=f"已追加 {len(content)} 字符到: {path}（当前 {size} 字节）",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _copy(self, args: str) -> ToolResult:
        """args 格式: <源路径> <目标路径>。目录递归复制；目标为已存在目录时复制到其中。"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return ToolResult(
                success=False, output="", error="复制操作需要: copy <源路径> <目标路径>"
            )
        src, dst = parts[0].strip(), parts[1].strip()
        if not os.path.exists(src):
            return ToolResult(success=False, output="", error=f"源路径不存在: {src}")
        try:
            if os.path.isdir(src):
                # copytree 的 dirs_exist_ok 需要 Python 3.8+；目标是本次新增目录的常见场景
                if os.path.isdir(dst):
                    # 目标是已存在目录 → 复制到其中，与文件语义保持一致
                    target = os.path.join(dst, os.path.basename(os.path.normpath(src)))
                    shutil.copytree(src, target, dirs_exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                    shutil.copytree(src, dst)
                return ToolResult(success=True, output=f"目录已复制: {src} → {dst}")
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            # copy2 保留修改时间等元数据（视频/音频产物的时间戳有意义）
            final = shutil.copy2(src, dst)
            return ToolResult(
                success=True,
                output=f"文件已复制: {src} → {final}（{os.path.getsize(final)} 字节）",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _move(self, args: str) -> ToolResult:
        """args 格式: <源路径> <目标路径>（移动或重命名）。"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return ToolResult(
                success=False, output="", error="移动操作需要: move <源路径> <目标路径>"
            )
        src, dst = parts[0].strip(), parts[1].strip()
        if not os.path.exists(src):
            return ToolResult(success=False, output="", error=f"源路径不存在: {src}")
        try:
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            final = shutil.move(src, dst)
            return ToolResult(success=True, output=f"已移动: {src} → {final}")
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
