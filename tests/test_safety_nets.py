"""
批次1 修复的回归测试：安全网失效与静默错误。

覆盖：
1. MCP 调用失败（超时/崩/断管）不得再伪装成 success=True, output="None"
2. 工具超时不得冒充"被安全策略拦截"（否则不计入 errors → 任务被判成功）
3. 所有工具分发路径都必须经过硬超时兜底
4. 视觉模型必须带显式超时（SDK 默认 600s×3 远超调用方预算）
"""
import pytest

from tools.base import ToolResult
from tools.mcp_client import MCPTool


# ----------------------------------------------------------------------
# 1) MCP 失败不再伪装成功
# ----------------------------------------------------------------------

def make_mcp_tool(call_fn):
    """call_fn 的真实签名是 (tool_name, arguments)。"""
    return MCPTool(name="do_thing", description="测试工具",
                   input_schema={"type": "object", "properties": {}},
                   call_fn=call_fn, server_name="srv")


class TestMCPFailureIsVisible:
    def test_none_result_is_an_error_not_success(self):
        """回归：旧代码 `return ToolResult(success=True, output=str(result))`
        把超时/崩溃/断管全变成 success=True + 字面量 "None"。"""
        tool = make_mcp_tool(lambda name, args: None)
        r = tool.execute_json({})
        assert r.success is False, "MCP 无响应必须报失败"
        assert "None" not in (r.output or "")
        assert "无响应" in r.error or "超时" in r.error

    def test_error_message_is_actionable(self):
        r = make_mcp_tool(lambda name, args: None).execute_json({})
        assert "超时" in r.error and ("崩溃" in r.error or "未连接" in r.error)

    def test_normal_result_still_success(self):
        tool = make_mcp_tool(
            lambda name, args: {"content": [{"type": "text", "text": "ok"}]})
        r = tool.execute_json({})
        assert r.success is True and "ok" in r.output

    def test_iserror_still_failure(self):
        tool = make_mcp_tool(lambda name, args: {
            "isError": True, "content": [{"text": "服务器内部错误"}]})
        r = tool.execute_json({})
        assert r.success is False and "服务器内部错误" in r.error

    def test_plain_value_result_still_success(self):
        r = make_mcp_tool(lambda name, args: 42).execute_json({})
        assert r.success is True and "42" in r.output


# ----------------------------------------------------------------------
# 2) 超时不得冒充"被拦截"
# ----------------------------------------------------------------------

class HungTool:
    """调用即挂起的假工具。"""

    name = "hung"
    description = "挂起"
    schema = {"type": "object", "properties": {}}
    risk_level = "low"
    approval = "auto"
    min_sandbox_mode = "workspace-write"
    parallel_safe = False

    def execute(self, input_str):
        import time
        time.sleep(5)
        return ToolResult(success=True, output="不该到达")

    def execute_json(self, arguments):
        import time
        time.sleep(5)
        return ToolResult(success=True, output="不该到达")

    def is_parallel_safe(self, arguments):
        return False


class TestTimeoutIsNotABlock:
    def _executor(self, monkeypatch):
        from agent.approval import ApprovalPolicy
        from agent.executor import Executor
        from config import TOOL_CONFIG
        from tools.tool_manager import ToolManager

        class NoopLLM:
            def chat(self, *a, **k):
                return ""

        monkeypatch.setitem(TOOL_CONFIG, "tool_timeout", 0.4)
        tm = ToolManager()
        tm.register(HungTool())
        return Executor(llm=NoopLLM(), tool_manager=tm, approval_policy=ApprovalPolicy(
            mode="never", sandbox_mode="workspace-write", interactive=False))

    def test_timeout_returns_empty_blocked_reason(self, monkeypatch):
        """回归：超时曾把超时文案当 blocked_reason 返回，调用方于是把它当
        "被安全策略拦截"——不计入 errors、不推进 last_failure_idx，最终
        success 判成 True，卡死的调用被当成任务成功写进经验库。"""
        ex = self._executor(monkeypatch)
        result, blocked_reason = ex._dispatch_tool_call("hung", {}, "目标")
        assert result.success is False
        assert "超时" in result.error
        assert blocked_reason == "", "超时不是安全拦截，blocked_reason 必须为空"

    def test_timeout_message_is_honest(self, monkeypatch):
        ex = self._executor(monkeypatch)
        result, _ = ex._dispatch_tool_call("hung", {}, "目标")
        assert "超时" in result.error and "重置" in result.error


# ----------------------------------------------------------------------
# 3) 视觉模型超时
# ----------------------------------------------------------------------

class TestVisionTimeout:
    def test_client_gets_explicit_timeout(self, monkeypatch):
        """SDK 默认读超时 600s + 重试 2 次 ≈ 1800s，而工具预算只有 300s、
        browser visionclick 只有 60s —— 必须显式收紧且不自动重试。"""
        import models.vision as vision

        captured = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(vision, "OpenAI", FakeOpenAI)
        vision.VisionModel()
        assert "timeout" in captured, "VisionModel 未设置超时"
        assert captured["timeout"] <= 60
        assert captured.get("max_retries") == 0, "重试应交给上层统一管理"

    def test_config_has_timeout_key(self):
        from config import VISION_CONFIG
        assert "timeout" in VISION_CONFIG
        assert 0 < float(VISION_CONFIG["timeout"]) <= 60
