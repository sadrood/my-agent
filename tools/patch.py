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
import re
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

    @staticmethod
    def _find_repo_root(file_path: str) -> str:
        """从被改文件向上找仓库根（第一个含 tests/ 目录的祖先），
        避免 preflight 在 cwd 漂移时找不到测试而误判失败。"""
        d = os.path.dirname(os.path.abspath(file_path))
        while True:
            if os.path.isdir(os.path.join(d, "tests")):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                return os.getcwd()
            d = parent

    def _related_test_command(self, file_path: str, test_cmd: str) -> str:
        """按被改文件推断**相关**测试目标，避免每次 edit 都跑全套测试。

        背景：全套测试（600+ 个）耗时可达 3 分钟以上，超过 preflight 与
        工具级超时，导致 edit 被误判失败（文件已改却报错）。改为只跑与
        被改模块相关的测试文件，通常几秒到几十秒完成。

        选择顺序（命中即用）：
        1. tests/test_<模块名>.py      精确同名   如 agent/memory.py → test_memory.py
        2. tests/test_<模块名>_*.py    同名前缀   如 models/llm.py    → test_llm_retry.py
        3. tests/test_<所属目录>.py    同目录     如 tools/browser.py → test_tools.py
        4. tests/test_<所属目录>_*.py  同目录前缀
        5. tests/*_<模块名>.py         词级包含（最后手段）
        6. 无匹配 → 返回原命令（保守：跑全套）

        注：优先"同目录测试文件"而非宽泛子串匹配——后者易错配到无关模块
        （如 tools/browser.py 曾被匹配到 dashboard 的 test_embedded_browser.py）。

        配置 EDIT_PREFLIGHT_SCOPE=full 可强制始终跑全套。
        """
        from config import TOOL_CONFIG
        if str(TOOL_CONFIG.get("edit_preflight_scope", "related")).lower() == "full":
            return test_cmd

        norm = file_path.replace("\\", "/")
        stem = os.path.splitext(os.path.basename(norm))[0]
        parent = os.path.basename(os.path.dirname(norm))

        repo_root = self._find_repo_root(file_path)
        tests_dir = os.path.join(repo_root, "tests")
        if not os.path.isdir(tests_dir):
            return test_cmd

        def _pick(pred) -> list:
            try:
                names = sorted(os.listdir(tests_dir))
            except OSError:
                return []
            return [f for f in names
                    if f.startswith("test_") and f.endswith(".py") and pred(f)]

        candidates = (
            _pick(lambda f: f == f"test_{stem}.py")
            or _pick(lambda f: f.startswith(f"test_{stem}_"))
            or (_pick(lambda f: f == f"test_{parent}.py") if parent else [])
            or (_pick(lambda f: f.startswith(f"test_{parent}_")) if parent else [])
            or _pick(lambda f: f.endswith(f"_{stem}.py"))
        )
        # 兜底：匹配过宽（超过 8 个）说明规则失效，退回全套更稳妥
        if not candidates or len(candidates) > 8:
            return test_cmd

        targets = " ".join(os.path.join("tests", c) for c in candidates)
        # 保留原命令的解释器前缀，只替换测试目标（兼容自定义 TEST_COMMAND）
        base = test_cmd
        for token in ("tests", "./tests", "tests/"):
            idx = base.find(token)
            if idx > 0:
                base = base[:idx].rstrip()
                break
        return f"{base} {targets} -q" if base else f"pytest {targets} -q"

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

        # 智能缩小测试范围：只跑与被改模块相关的测试（全套 600+ 个会超过
        # preflight/工具超时，导致"文件已改却报失败"）
        scoped_cmd = self._related_test_command(file_path, test_cmd)
        scope_note = ""
        if scoped_cmd != test_cmd:
            # 提取目标文件名用于回喂提示（让模型知道验证覆盖到哪）
            names = [t for t in scoped_cmd.split() if t.endswith(".py")]
            if names:
                scope_note = "（相关测试: " + ", ".join(
                    os.path.basename(n) for n in names) + "）"

        import subprocess
        try:
            proc = subprocess.run(
                scoped_cmd, shell=True, capture_output=True, text=True,
                timeout=int(TOOL_CONFIG.get("edit_preflight_timeout", 180)),
                encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,   # 防止命令意外读取 stdin 而永久阻塞
                cwd=self._find_repo_root(file_path),
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
            return ToolResult(success=True, output=f"preflight 测试通过{scope_note}")

        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        tail_lines = int(TOOL_CONFIG.get("edit_preflight_tail", 80))
        summary = self._summarize_test_failure(combined, file_path, tail_lines)
        rolled = self._rollback_edit(file_path, backup_path)
        return ToolResult(
            success=False, output="",
            error=(
                f"preflight 测试未通过（返回码 {proc.returncode}），"
                f"已{'自动回滚' if rolled else '回滚失败（请手动 git 恢复）'}本次修改。\n{summary}"
            ),
            metadata={"preflight_failed": True, "rolled_back": rolled,
                      "test_summary": summary},
        )

    #: 失败摘要里最多列几个用例（其余折叠成计数，别把提示词刷屏）
    _FAILURE_LIST_MAX = 8

    @staticmethod
    def _summarize_test_failure(combined: str, edited_file: str = "",
                                tail_lines: int = 80) -> str:
        """把 pytest 输出压成"能判断该怪谁"的失败摘要。

        为什么不只回喂尾部 N 行（早先做法，也是 agent 明确反馈过的坑）：
        全套 pytest 失败时 FAILURES 段很长，固定行数窗口经常**只截到断言片段、
        丢掉"哪个用例失败"**。实测后果是 agent 拿着 `assert 110 == 100` 全项目
        搜不到对应测试名，把（并发改动引起的）失败误判成自己改坏了代码。

        这里显式抽出四样东西：
          1. 失败用例清单——取自 pytest 的 short test summary，不受窗口影响；
          2. 每个用例的首个断言行——让模型不用猜是哪个断言；
          3. 归因提示——失败用例与被改文件无关时明说，避免误回滚；
          4. 截断告知——输出行数超过窗口时给出总行数，避免"没看到"当成"没有"。
        """
        lines = combined.splitlines()

        # 1) 失败用例（去重保序）。pytest 的 short test summary 形如：
        #      FAILED tests/test_x.py::test_y - AssertionError: ...
        #    注意：按绝对路径跑时文件部分会是空的（`FAILED ::test_y`），
        #    这时不能据此判断"与本次改动无关"（会误报），要从断言处的文件行补。
        failed, seen = [], set()
        for ln in lines:
            m = re.match(r"^(?:FAILED|ERROR)\s+(\S+)", ln.strip())
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                failed.append(m.group(1))

        # 2) 每个用例的断言行与所在文件（pytest 的 FAILURES 段）
        #    分隔符随版本不同（下划线/横线/破折号），都认
        _bar = r"[_\-\u2500\u2501]{3,}"
        _head = re.compile(r"^%s\s+(\S+)\s+%s$" % (_bar, _bar))
        info, headers, cur = {}, [], None
        for ln in lines:
            s = ln.strip()
            m = _head.match(s)
            if m:
                cur = m.group(1)
                headers.append(cur)
                info.setdefault(cur, {"e": [], "file": ""})
                continue
            if cur is None:
                continue
            rec = info.setdefault(cur, {"e": [], "file": ""})
            if s.startswith("E ") and len(rec["e"]) < 3:
                rec["e"].append(s[2:].strip()[:200])
            fm = re.match(r"^([A-Za-z]:\\[^\s:]+|\S+\.py):\d+:", s)
            if fm and not rec["file"]:
                rec["file"] = fm.group(1)

        def _best_assert(name):
            """优先给带 assert 的那行——它才是判断失败原因的关键。"""
            es = info.get(name, {}).get("e") or []
            for e in es:
                if "assert" in e:
                    return e
            return es[0] if es else ""

        if not failed:                      # 没有 short summary（如收集阶段就失败）
            failed = headers[:EditTool._FAILURE_LIST_MAX]

        parts = []
        if failed:
            parts.append("失败用例（%d 个）:" % len(failed))
            for node in failed[:EditTool._FAILURE_LIST_MAX]:
                short = node.split("::")[-1]
                msg = _best_assert(short) or _best_assert(node)
                parts.append("  - %s" % node)
                if msg:
                    parts.append("      %s" % msg)
            if len(failed) > EditTool._FAILURE_LIST_MAX:
                parts.append("  …还有 %d 个未列出" % (len(failed) - EditTool._FAILURE_LIST_MAX))
        else:
            parts.append("未能从输出解析出失败用例（pytest 输出可能被参数裁剪）；"
                         "请看下方原始尾部自行判断。")

        # 3) 归因：失败用例是否与被改文件相关。只有在**确实知道失败文件**时才下结论，
        #    否则宁可不提示，也不误导模型去回滚无关改动。
        base = os.path.basename(edited_file or "")
        stem = base[:-3] if base.endswith(".py") else base
        if failed and stem:
            known, related = 0, 0
            for node in failed:
                path = node.split("::")[0]
                if not path or path.startswith("::"):
                    path = info.get(node.split("::")[-1], {}).get("file", "")
                if not path:
                    continue
                known += 1
                if os.path.basename(path) == base or stem in path:
                    related += 1
            if known and not related:
                parts.append(
                    "⚠️ 能定位到文件的 %d 个失败用例都跟本次改动的 %s 无关，可能是并发改动或"
                    "既有失败——不要急着认定是自己改坏的，更不要为此回滚无关改动。"
                    % (known, base))
            elif related:
                parts.append("其中 %d 个失败用例与被改文件 %s 相关。"
                             % (related, base))

        # 4) 原始尾部 + 截断告知
        if len(lines) > tail_lines:
            parts.append("（pytest 输出共 %d 行，下面只是末尾 %d 行）"
                         % (len(lines), tail_lines))
        parts.append("\n".join(lines[-tail_lines:])[:2000])
        return "\n".join(parts)[:3000]

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
