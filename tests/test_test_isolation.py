"""测试套件自身的隔离：不许偷偷打真实外部 API。
硬编码的 `"agnes-3.0-flash"`，key/base 再回退 `GUARDIAN_API_KEY` / `GUARDIAN_BASE_URL`。
答案而间歇失败（~25%）。"""


class TestNoAccidentalNetworkCalls:
    def test_supervisor_disabled_by_default(self):
        from config import SUPERVISOR_CONFIG
        assert SUPERVISOR_CONFIG.get("enabled") is False,             "测试里监管者开着 —— 会真打外部 API，且让循环时序不确定"

    def test_agent_without_explicit_flag_gets_no_supervisor(self):
        """不显式传 supervisor_enabled 时，Agent 不该建出监管者。"""
        import tempfile

        from agent import Agent, AgentConfig
        from agent.memory import Memory
        from models.llm import LLMToolResponse
        from tools.tool_manager import ToolManager

        class _LLM:
            def chat_with_tools(self, *a, **k):
                return LLMToolResponse(content="完成")

            def chat(self, *a, **k):
                return "完成"

        with tempfile.TemporaryDirectory() as d:
            ag = Agent(llm=_LLM(), tool_manager=ToolManager(),
                       memory=Memory(db_path=d),
                       config=AgentConfig(verbose=False, rollout_enabled=False,
                                          snapshot_enabled=False))
            assert ag.supervisor is None, "监管者被建出来了（测试会打真实 API）"
