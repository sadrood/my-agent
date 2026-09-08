"""
精确编辑工具（EditTool，借鉴同类实现的 apply_patch / edit 工具）。

动机：Agent 自我修改代码时，整文件覆盖写（file write）有两大问题：
1. 大文件（如 agent.py 1.5 万+ token）超过单轮输出上限，根本写不出来；
2. 重写整个文件极易引入无关改动，diff 不可控。

EditTool 只发"改哪里"的增量：
    edit(file_path, old_string, new_string, replace_all=False)
- old_string 必须在文件中精确出现（默认要求恰好 1 次，多匹配报错并给出次数）
- replace_all=True 时替换全部匹配
- 换行差异自动兼容（CRLF/LF）
- 修改前自动生成 .bak 备份（可选，默认开）
"""
import os
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

# metadata.old_text 截断上限：unified diff 渲染用的旧内容快照，
# 超大文件截断（old_text_truncated=True），避免撑爆事件负载
_OLD_TEXT_META_MAX = 100 * 1024


class EditTool(BaseTool):
    """精确替换编辑工具。"""

    risk_level: str = "medium"
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "精确编辑文件工具：把文件中的一段旧文本替换为新文本（结构化补丁）。"
            "修改现有文件时优先用本工具，不要用 file write 整文件覆盖。\n"
            "参数说明：\n"
            "  - file_path: 要修改的文件路径\n"
            "  - old_string: 文件中现存的一段精确文本（需原样复制，含缩进）\n"
            "  - new_string: 替换后的新文本（可为空字符串表示删除）\n"
            "  - replace_all: 是否替换全部匹配（默认 False，要求恰好匹配 1 次）\n"
            "匹配失败（0 次）或多匹配（>1 次）时会报错，请用 file read 重新核对原文。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "要修改的文件路径"},
                "old_string": {"type": "string", "description": "文件中的旧文本（原样精确复制）"},
                "new_string": {"type": "string", "description": "替换后的新文本"},
                "replace_all": {"type": "boolean", "description": "替换全部匹配（默认 false）"},
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        file_path = str(arguments.get("file_path", "")).strip()
        old_string = arguments.get("old_string", "")
        new_string = arguments.get("new_string", "")
        replace_all = bool(arguments.get("replace_all", False))
        return self.edit(file_path, old_string, new_string, replace_all)

    def execute(self, input_str: str) -> ToolResult:
        import json as _json
        try:
            arguments = _json.loads(input_str)
        except _json.JSONDecodeError:
            return ToolResult(
                success=False, output="",
                error="edit 工具需要 JSON 参数: "
                      '{"file_path": "...", "old_string": "...", "new_string": "..."}',
            )
        if not isinstance(arguments, dict):
            return ToolResult(success=False, output="", error="参数必须是 JSON 对象。")
        return self.execute_json(arguments)

    def build_approval_request(self, arguments: Dict[str, Any]):
        from agent.approval import ApprovalRequest
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=f"edit {arguments.get('file_path', '')}",
            risk_level="medium",
            min_sandbox_mode="workspace-write",
        )

    # ================================================================
    # 核心逻辑
    # ================================================================

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        backup: bool = True,
    ) -> ToolResult:
        if not file_path:
            return ToolResult(success=False, output="", error="file_path 不能为空。")
        if not os.path.exists(file_path):
            return ToolResult(success=False, output="", error=f"文件不存在: {file_path}")
        if os.path.isdir(file_path):
            return ToolResult(success=False, output="", error=f"路径是目录，不是文件: {file_path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            return ToolResult(
                success=False, output="",
                error=f"无法以 UTF-8 文本读取: {file_path}（可能是二进制文件）",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

        # 换行归一化：统一按 LF 匹配（兼容模型传来的 CRLF 旧文本）
        content_normalized = content.replace("\r\n", "\n")
        old_normalized = old_string.replace("\r\n", "\n")
        new_normalized = new_string.replace("\r\n", "\n")

        count = content_normalized.count(old_normalized)
        if count == 0:
            return ToolResult(
                success=False, output="",
                error=(
                    f"未找到匹配的旧文本（0 次）。请用 file read 核对文件内容与缩进后重试。\n"
                    f"目标文件: {file_path}\n"
                    f"寻找的文本开头: {old_normalized[:80]!r}"
                ),
            )
        if count > 1 and not replace_all:
            return ToolResult(
                success=False, output="",
                error=(
                    f"旧文本匹配到 {count} 处（超过 1 处）。请提供更长的上下文使其唯一，"
                    f"或设置 replace_all=true 全部替换。"
                ),
            )

        new_content = content_normalized.replace(old_normalized, new_normalized)

        # 备份
        backup_path = ""
        if backup:
            try:
                backup_path = file_path + ".bak"
                with open(backup_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                backup_path = ""

        try:
            with open(file_path, "w", encoding="utf-8", newline="") as f:
                f.write(new_content)
        except Exception as e:
            return ToolResult(success=False, output="", error=f"写入失败: {e}")

        # 验证式应用（preflight）：EDIT_PREFLIGHT=true 且目标是 .py 代码时
        # 跑测试命令，失败自动回滚（.bak 恢复）并把测试尾部回喂模型。
        preflight_note = ""
        verdict = self._run_preflight(file_path, backup_path)
        if verdict is not None:
            if not verdict.success:
                return verdict
            preflight_note = " · " + verdict.output

        # .bak 生命周期：备份只在回滚时有用，修改成功（preflight 通过或未启用）
        # 即删除，避免成功路径堆积 .bak 垃圾；失败路径保留供排查/恢复。
        # 旧内容快照改由 metadata.old_text 携带（截断保护），上层 unified diff
        # 渲染不再依赖 .bak 文件。
        old_text_meta = content[:_OLD_TEXT_META_MAX]
        if backup_path:
            try:
                os.remove(backup_path)
            except OSError:
                pass
            backup_path = ""

        # diff 追踪：登记修改前快照（多次修改只保留最旧版本）
        from tools.change_tracker import get_change_tracker
        get_change_tracker().record_with_old(file_path, self.name, content)

        added_lines = new_normalized.count("\n") - old_normalized.count("\n")
        delta = f"{'+' if added_lines >= 0 else ''}{added_lines} 行"
        return ToolResult(
            success=True,
            output=(
                f"已修改 {file_path}：替换 {count} 处（{delta}）。{preflight_note}\n"
                f"旧文本开头: {old_normalized[:60]!r}\n"
                f"新文本开头: {new_normalized[:60]!r}"
            ),
            metadata={
                "matches": count,
                "line_delta": added_lines,
                "old_text": old_text_meta,       # 供上层做 unified diff 渲染
                "old_text_truncated": len(content) > _OLD_TEXT_META_MAX,
                "preflight": verdict is not None,
            },
        )

    # ================================================================
    # 验证式应用（preflight）
    # ================================================================

    def _run_preflight(self, file_path: str, backup_path: str):
        """edit 成功后验证：EDIT_PREFLIGHT=true 且目标为 .py 代码时跑测试命令。

        Returns:
            None                      —— 跳过（未开启 / 非 .py / 无测试命令 / 会递归的 pytest 场景）
            ToolResult(success=True)  —— 测试通过（output 为通过说明）
            ToolResult(success=False) —— 测试失败，已自动回滚（error 含失败尾部）
        """
        from config import TOOL_CONFIG, TEST_CONFIG
        if not TOOL_CONFIG.get("edit_preflight"):
            return None
        if not file_path.endswith(".py") or ".venv" in file_path.replace("\\", "/"):
            return None
        test_cmd = (TEST_CONFIG.get("command") or "").strip()
        if not test_cmd:
            return None
        # 递归保护（关键）：pytest 运行期间若 preflight 命令本身也是 pytest，
        # 会"测试→edit→再跑 pytest→再 edit"无限递归堆积进程。
        # 判据：在 pytest 内（PYTEST_CURRENT_TEST 由 pytest 注入）且命令含 pytest。
        # 测试里用 exit 0/1 等假命令替换时不受影响，仍可正常验证本功能。
        if "PYTEST_CURRENT_TEST" in os.environ and "pytest" in test_cmd.lower():
            return None

        import subprocess
        try:
            proc = subprocess.run(
                test_cmd, shell=True, capture_output=True, text=True,
                timeout=int(TOOL_CONFIG.get("edit_preflight_timeout", 180)),
                encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,   # 防止命令意外读取 stdin 而永久阻塞
            )
        except subprocess.TimeoutExpired:
            rolled = self._rollback_edit(file_path, backup_path)
            return ToolResult(
                success=False, output="",
                error=(f"preflight 测试超时（>{TOOL_CONFIG.get('edit_preflight_timeout')}s）。"
                       f"已{'自动回滚' if rolled else '回滚失败（请手动 git 恢复）'}本次修改。"),
                metadata={"preflight_failed": True, "rolled_back": rolled},
            )
        except Exception as e:
            rolled = self._rollback_edit(file_path, backup_path)
            return ToolResult(
                success=False, output="",
                error=(f"preflight 执行失败（{str(e)[:120]}），"
                       f"已{'自动回滚' if rolled else '回滚失败'}本次修改。"),
                metadata={"preflight_failed": True, "rolled_back": rolled},
            )

        if proc.returncode == 0:
            return ToolResult(success=True, output="preflight 测试通过")

        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        tail_lines = int(TOOL_CONFIG.get("edit_preflight_tail", 40))
        tail = "\n".join(combined.splitlines()[-tail_lines:])
        rolled = self._rollback_edit(file_path, backup_path)
        return ToolResult(
            success=False, output="",
            error=(
                f"preflight 测试未通过（返回码 {proc.returncode}），"
                f"已{'自动回滚' if rolled else '回滚失败（请手动 git 恢复）'}本次修改。测试尾部:\n{tail[:2000]}"
            ),
            metadata={"preflight_failed": True, "rolled_back": rolled,
                      "test_tail": tail[:2000]},
        )

    def _rollback_edit(self, file_path: str, backup_path: str) -> bool:
        """用 .bak 备份恢复文件内容；恢复成功后删除已用完的备份。"""
        try:
            with open(backup_path, "r", encoding="utf-8") as f:
                original = f.read()
            with open(file_path, "w", encoding="utf-8", newline="") as f:
                f.write(original)
            try:
                os.remove(backup_path)
            except OSError:
                pass
            return True
        except Exception:
            return False   # 恢复失败：保留 .bak（仅存的原始内容副本）
