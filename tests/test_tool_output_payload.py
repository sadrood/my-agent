"""内联图片负载必须搬进 metadata：被截断会变成坏图，留在 output 里还会回喂模型。"""
import base64
import os

import pytest

from tools.base import ToolResult, hoist_inline_payload, truncate_output
from tools.tool_manager import ToolManager


def _payload(size: int = 120_000):
    raw = os.urandom(size)
    return raw, base64.b64encode(raw).decode()


def _screenshot_output(b64: str) -> str:
    return f"截图已获取。\n大小: {len(b64) * 3 // 4} 字节\n[FULL_BASE64]{b64}[/FULL_BASE64]"


class TestHoistInlinePayload:
    def test_payload_moves_to_metadata_and_output_shrinks(self):
        raw, b64 = _payload()
        result = hoist_inline_payload(ToolResult(success=True, output=_screenshot_output(b64)))
        assert "[FULL_BASE64]" not in result.output
        assert "metadata.screenshot_base64" in result.output
        assert len(result.output) < 200
        assert base64.b64decode(result.metadata["screenshot_base64"]) == raw

    def test_output_survives_truncation_after_hoist(self):
        """搬完之后再截断也不会破坏负载——这正是原来坏图的地方。"""
        raw, b64 = _payload()
        result = hoist_inline_payload(ToolResult(success=True, output=_screenshot_output(b64)))
        text, truncated, original = truncate_output(result.output, 8000)
        assert not truncated, "摘要不该再触发截断"
        assert base64.b64decode(result.metadata["screenshot_base64"]) == raw

    def test_existing_metadata_wins(self):
        """调用方已经放了负载（如 vision_tool）时不要被 output 里的旧标记覆盖。"""
        _raw, b64 = _payload()
        result = ToolResult(success=True, output=_screenshot_output(b64),
                            metadata={"screenshot_base64": "QUJD"})
        hoist_inline_payload(result)
        assert result.metadata["screenshot_base64"] == "QUJD"

    def test_dangling_marker_drops_payload(self):
        """只有起始标记（无闭合）说明负载已被截断：整段丢掉并说明，不留下坏数据。"""
        _raw, b64 = _payload()
        result = hoist_inline_payload(
            ToolResult(success=True, output="截图已获取。\n[FULL_BASE64]" + b64))
        assert "[FULL_BASE64]" not in result.output
        assert "不完整" in result.output
        assert not result.metadata

    def test_text_without_payload_is_untouched(self):
        result = hoist_inline_payload(ToolResult(success=True, output="普通输出"))
        assert result.output == "普通输出" and not result.metadata


class _PayloadTool:
    """假工具：像浏览器那样把 base64 内联在 output 里。"""

    name = "fakebrowser"
    description = "test"
    risk_level = "low"
    approval = "auto"
    min_sandbox_mode = "read-only"

    def __init__(self, b64: str):
        self._b64 = b64

    def execute(self, input_str: str) -> ToolResult:
        return ToolResult(success=True, output=_screenshot_output(self._b64))

    def execute_json(self, arguments: dict) -> ToolResult:
        return self.execute("")


class TestToolManagerBoundary:
    def test_manager_hoists_before_truncating(self):
        raw, b64 = _payload()
        tm = ToolManager()
        tm.register(_PayloadTool(b64))
        result = tm.execute("fakebrowser", "screenshot_base64")
        assert not result.truncated, "负载已被搬走，文本部分不该截断"
        assert base64.b64decode(result.metadata["screenshot_base64"]) == raw
        assert "[FULL_BASE64]" not in result.output


class TestExecutorPrefersMetadata:
    """`_take_screenshot_for_vision` 要优先读 metadata，并保留旧标记的回落。"""

    def _executor(self, result: ToolResult):
        from types import SimpleNamespace
        from agent.executor import Executor

        ex = Executor.__new__(Executor)          # 不跑 __init__（会连浏览器/读配置）
        ex.tool_manager = SimpleNamespace(get_tool=lambda name: object())
        ex.call_tool_guarded = lambda name, cmd: result
        return ex

    def test_reads_metadata(self):
        ex = self._executor(ToolResult(success=True, output="截图已获取。",
                                       metadata={"screenshot_base64": "QUJD"}))
        assert ex._take_screenshot_for_vision()["screenshot_base64"] == "QUJD"

    def test_falls_back_to_inline_marker(self):
        ex = self._executor(ToolResult(success=True, output=_screenshot_output("QUJD")))
        assert ex._take_screenshot_for_vision()["screenshot_base64"] == "QUJD"

    def test_reports_failure_when_no_payload(self):
        ex = self._executor(ToolResult(success=True, output="截图已获取。"))
        got = ex._take_screenshot_for_vision()
        assert got is None, "拿不到负载时返回 None，交由上层走失败分支"
