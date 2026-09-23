"""测试套件自身的隔离：不许偷偷打真实外部 API。

实测（2026-09-23 定位）：`build_supervisor_llm` 在 `SUPERVISOR_MODEL` 留空时回退到
硬编码的 `"agnes-3.0-flash"`，key/base 再回退 `GUARDIAN_API_KEY` / `GUARDIAN_BASE_URL`。
于是**任何建 Agent 的测试**都会真去请求外部端点（12 个测试文件都没关它），后果：

- 违反 AGENTS.md「不依赖网络的测试优先（FakeLLM 脚本化）」；
- 烧用户的真实 API 额度（每次全量跑都在打）；
- 监管者的 verdict 会决定主循环要不要多跑一轮 —— 真模型回
  `{"verdict": "continue"}` 时循环继续，而 FakeLLM 的脚本已被前面耗尽，
  `test_agent_loop::test_simple_task_end_to_end` 因此拿到「（脚本耗尽）」当最终
  答案而间歇失败（~25%）。

由 `tests/conftest.py` 的 `_isolate_heavy_runtime_switches` 统一关闭；
`tests/test_supervisor.py` 自行覆盖打开（那是它的测试对象）。
"""


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
