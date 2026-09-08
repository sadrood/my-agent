"""
工具基类测试：截断、ToolResult、Schema 生成。
"""
import pytest

from tools.base import BaseTool, ToolResult, truncate_output


def test_truncate_short_output():
    text, truncated, original = truncate_output("hello", 100)
    assert text == "hello"
    assert truncated is False
    assert original == 5


def test_truncate_long_output():
    long_text = "x" * 10000
    text, truncated, original = truncate_output(long_text, 1000)
    assert truncated is True
    assert original == 10000
    assert len(text) <= 1100
    assert "输出过长" in text
    # 头部和尾部都保留了
    assert text.startswith("x" * 800)
    assert text.rstrip().endswith("x")


def test_tool_result_fields():
    r = ToolResult(success=True, output="ok", truncated=True, original_length=5)
    assert r.success is True
    assert r.output == "ok"
    assert r.truncated is True
    d = r.to_dict()
    assert d["success"] is True
    assert d["original_length"] == 5


def test_base_tool_default_schema():
    class Dummy(BaseTool):
        @property
        def name(self):
            return "dummy"

        @property
        def description(self):
            return "测试工具"

        def execute(self, input_str):
            return ToolResult(success=True, output=input_str)

    t = Dummy()
    s = t.schema
    assert s["type"] == "object"
    assert "input" in s["properties"]
    assert t.execute_json({"input": "abc"}).output == "abc"

    openai_schema = t.to_openai_schema()
    assert openai_schema["type"] == "function"
    assert openai_schema["function"]["name"] == "dummy"
