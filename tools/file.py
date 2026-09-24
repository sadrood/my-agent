"""
文件操作工具模块（JSON Schema + 结构化调用）。
提供读写、追加、复制、移动、删除、列目录、存在性检查与文件信息。

约定：
- execute_json() 直接按字段取值，不回落字符串解码
- 风险分级：读操作 low，写/删除 medium，递归删除 high
- delete 是破坏性操作：目录必须显式 recursive=true；.git / 项目根 / 盘根一律拒绝
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
            "  - delete <路径> [recursive]: 删除文件；目录必须显式 recursive=true\n"
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
                    "enum": ["read", "write", "append", "copy", "move", "delete",
                             "list", "exists", "info"],
                    "description": "要执行的文件操作",
                },
                "path": {
                    "type": "string",
                    "description": "文件或目录路径（copy/move 时为源路径）",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "delete 用：删除整个目录树时必须显式传 true"
                                   "（防误删；拒绝 .git/项目根/盘根）",
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
        """结构化入口：**直接按字段取值**，不回落字符串解码。

        拼字符串再按空格切分会让含空格的路径静默写错文件，路径必须直连实现。
        """
        operation = str(arguments.get("operation", "")).lower()
        path = str(arguments.get("path", "")).strip()
        content = str(arguments.get("content", ""))

        if operation == "write":
            return self._do_write(path, content)
        if operation == "append":
            return self._do_append(path, content)
        if operation == "copy":
            return self._do_copy(path, str(arguments.get("destination", "")).strip())
        if operation == "move":
            return self._do_move(path, str(arguments.get("destination", "")).strip())
        if operation == "delete":
            return self._do_delete(path, bool(arguments.get("recursive")))
        if operation in ("read", "list", "exists", "info"):
            return self.execute(f"{operation} {path}")
        return ToolResult(
            success=False, output="",
            error=f"未知操作: '{operation}'。支持: "
                  f"read/write/append/copy/move/delete/list/exists/info",
        )

    def build_approval_request(self, arguments: Dict[str, Any]):
        from agent.approval import ApprovalRequest

        operation = str(arguments.get("operation", "")).lower()
        path = str(arguments.get("path", "")).strip()
        is_write = operation in ("write", "append", "copy", "move")
        is_delete = operation == "delete"
        recursive = bool(arguments.get("recursive"))
        if is_delete:
            risk = "high" if recursive else "medium"
        else:
            risk = "medium" if is_write else "low"
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=f"file {operation} {path}" + (" recursive" if recursive else ""),
            risk_level=risk,
            min_sandbox_mode="workspace-write" if (is_write or is_delete) else "read-only",
        )

    def execute(self, input_str: str) -> ToolResult:
        # 只 lstrip 不 strip：write/append 的内容要原样落盘（末尾换行不能丢）。
        # 路径类参数由各自的 handler 自行 strip。
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
            "delete": self._delete,
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
        """字符串入口：``<路径> <内容>``（路径含空格请用 execute_json）。"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return ToolResult(
                success=False, output="", error="写入操作需要: write <路径> <内容>"
            )
        return self._do_write(parts[0].strip(), parts[1])

    def _do_write(self, path: str, content: str) -> ToolResult:
        # diff 追踪：写入前留旧内容快照（新建文件为 None）
        from tools.change_tracker import get_change_tracker
        tracker = get_change_tracker()
        existed = os.path.exists(path)
        old_content = tracker.snapshot(path)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            # newline=""：原样写入。文本模式在 Windows 会把 LF 翻成 CRLF，与 edit
            # 工具（保留原有行尾）策略相反，交替使用会来回翻整个文件的行尾。
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            tracker.record_with_old(path, self.name, old_content, existed=existed)
            return ToolResult(success=True, output=f"文件已写入: {path}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _append_file(self, args: str) -> ToolResult:
        """字符串入口：``<路径> <内容>``（路径含空格请用 execute_json）。"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return ToolResult(
                success=False, output="", error="追加操作需要: append <路径> <内容>"
            )
        return self._do_append(parts[0].strip(), parts[1])

    def _do_append(self, path: str, content: str) -> ToolResult:
        # diff 追踪：追加前留旧内容快照，便于展示增量 diff
        from tools.change_tracker import get_change_tracker
        tracker = get_change_tracker()
        existed = os.path.exists(path)
        old_content = tracker.snapshot(path)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write(content)
            tracker.record_with_old(path, self.name, old_content, existed=existed)
            size = os.path.getsize(path)
            return ToolResult(
                success=True,
                output=f"已追加 {len(content)} 字符到: {path}（当前 {size} 字节）",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _copy(self, args: str) -> ToolResult:
        """字符串入口：``<源路径> <目标路径>``（源路径含空格请用 execute_json）。"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return ToolResult(
                success=False, output="", error="复制操作需要: copy <源路径> <目标路径>"
            )
        return self._do_copy(parts[0].strip(), parts[1].strip())

    #: 删除时必须拒绝的危险目标（.git / 项目根 / 盘根）
    _PROTECTED_NAMES = (".git", ".env")

    @staticmethod
    def _project_root() -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _delete_guard(self, path: str) -> str:
        """返回拒绝原因（"" = 允许删除）。

        比较一律走 `os.path.normcase`：Windows 大小写不敏感，敏感比较会让 .GIT /
        .ENV / 盘符大小写变体绕过守卫。POSIX 上 normcase 是恒等变换。
        """
        if not path or not path.strip():
            return "删除操作需要路径"
        target = os.path.abspath(path.strip())
        root = os.path.abspath(self._project_root())
        target_cmp = os.path.normcase(target)
        root_cmp = os.path.normcase(root)
        drive, tail = os.path.splitdrive(target)
        if tail in ("\\", "/", ""):
            return f"拒绝删除盘根/根目录: {target}"
        if target_cmp == root_cmp:
            return f"拒绝删除项目根目录: {target}"
        if root_cmp.startswith(target_cmp + os.sep):
            return f"拒绝删除项目根目录的上级: {target}"
        parts = [os.path.normcase(x) for x in target.replace("\\", "/").split("/") if x]
        for bad in self._PROTECTED_NAMES:
            if os.path.normcase(bad) in parts:
                return f"拒绝删除受保护路径（含 {bad}）: {target}"
        return ""

    def _delete(self, args: str) -> ToolResult:
        """字符串入口：``<路径> [recursive]``。"""
        text = (args or "").strip()
        recursive = text.lower().endswith(" recursive")
        if recursive:
            text = text[: -len(" recursive")].strip()
        return self._do_delete(text, recursive)

    def _do_delete(self, path: str, recursive: bool = False) -> ToolResult:
        """删除文件；目录需显式 recursive=true（破坏性操作，故加多重守卫）。"""
        reason = self._delete_guard(path)
        if reason:
            return ToolResult(success=False, output="", error=reason)
        target = os.path.abspath(path.strip())
        if not os.path.exists(target):
            return ToolResult(success=False, output="", error=f"路径不存在: {target}")
        try:
            if os.path.isdir(target):
                if not recursive:
                    n = sum(len(f) for _r, _d, f in os.walk(target))
                    return ToolResult(
                        success=False, output="",
                        error=f"{target} 是目录（含 {n} 个文件）。确认要整树删除请传 "
                              f"recursive=true。")
                n = 0
                size = 0
                for dirpath, _dirs, files in os.walk(target):
                    for f in files:
                        n += 1
                        try:
                            size += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
                shutil.rmtree(target)
                return ToolResult(
                    success=True,
                    output=f"已删除目录: {target}（{n} 个文件，{size / 1048576:.2f} MB）",
                    metadata={"deleted": target, "files": n, "bytes": size,
                              "recursive": True})
            size = os.path.getsize(target)
            os.remove(target)
            return ToolResult(
                success=True,
                output=f"已删除文件: {target}（{size / 1024:.1f} KB）",
                metadata={"deleted": target, "files": 1, "bytes": size})
        except Exception as e:                              # noqa: BLE001
            return ToolResult(success=False, output="", error=f"删除失败: {e}")

    def _do_copy(self, src: str, dst: str) -> ToolResult:
        if not src or not dst:
            return ToolResult(
                success=False, output="", error="复制操作需要: copy <源路径> <目标路径>"
            )
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
        """字符串入口：``<源路径> <目标路径>``（源路径含空格请用 execute_json）。"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return ToolResult(
                success=False, output="", error="移动操作需要: move <源路径> <目标路径>"
            )
        return self._do_move(parts[0].strip(), parts[1].strip())

    def _do_move(self, src: str, dst: str) -> ToolResult:
        if not src or not dst:
            return ToolResult(
                success=False, output="", error="移动操作需要: move <源路径> <目标路径>"
            )
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
