"""未完成交接（A）与续跑指令（B/C）的单元测试（纯函数，不联网）。"""
from agent.agent import INCOMPLETE_MARKERS, compose_handoff


def test_incomplete_markers_cover_main_cases():
    cases = [
        "已达到任务最大操作轮数（80）。任务可能比预期复杂…",
        "任务已停止",
        "已停止（用户中断）",
        "任务未完成，已完成的操作已记录",
        "中断于第 37 轮",
    ]
    for c in cases:
        assert any(k in c for k in INCOMPLETE_MARKERS), c
    # 正常完成不应误判
    assert not any(k in "任务完成，全部通过，612 passed" for k in INCOMPLETE_MARKERS)


def test_compose_handoff_structure_and_rule():
    text = compose_handoff("重构 tools 模块", "达到任务最大操作轮数（80）",
                           "原始收尾：已完成的操作已记录",
                           done_hint="修改了 tools/base.py")
    assert "未完成" in text
    assert "只执行未完成部分" in text and "不要重新执行已完成" in text
    assert "修改了 tools/base.py" in text
    assert "重构 tools 模块" in text


def test_resume_budget_keys_present():
    from config import SESSION_CONFIG
    assert int(SESSION_CONFIG.get("resume_context_messages", 0)) >= 30
    assert int(SESSION_CONFIG.get("resume_context_chars", 0)) >= 500


class TestBrowserSessionContinuity:
    """轮数上限/中断后**续跑不能丢浏览器会话**。"""

    def _agent(self):
        from agent.agent import Agent, AgentConfig
        from tools.tool_manager import ToolManager
        cfg = AgentConfig(verbose=False, guardian_enabled=False,
                          instructions_enabled=False)
        return Agent(tool_manager=ToolManager(), config=cfg)

    def test_no_note_without_session(self):
        a = self._agent()
        b = a.tool_manager.get_tool("browser")
        b._pages = []
        assert a._browser_session_note() == ""

    def test_note_reports_open_tabs(self):
        a = self._agent()
        b = a.tool_manager.get_tool("browser")
        b._pages = [object(), object(), object()]   # _pages 是纯内存，不碰 Playwright
        note = a._browser_session_note()
        assert "3 个标签页" in note
        assert "仍在运行" in note

    def test_note_does_not_launch_browser(self, monkeypatch):
        """回归：写交接备注绝不能顺手把浏览器拉起来。"""
        a = self._agent()
        b = a.tool_manager.get_tool("browser")
        b._pages = [object()]
        called = []
        monkeypatch.setattr(b, "execute",
                            lambda *args, **kw: called.append(args) or None)
        monkeypatch.setattr(b, "_is_browser_alive", lambda: False)
        note = a._browser_session_note()
        assert called == [], "浏览器未存活时不该调用 execute（会触发 launch）"
        assert "1 个标签页" in note and "仍在运行" in note

    def test_note_fetches_url_when_alive(self, monkeypatch):
        """浏览器确实存活时，仍要带上当前 URL（交接信息才有用）。"""
        from tools.base import ToolResult

        a = self._agent()
        b = a.tool_manager.get_tool("browser")
        b._pages = [object()]
        monkeypatch.setattr(b, "_is_browser_alive", lambda: True)
        monkeypatch.setattr(b, "execute", lambda *args, **kw: ToolResult(
            success=True, output="当前页面 URL: https://example.com/x"))
        note = a._browser_session_note()
        assert "example.com" in note

    def test_handoff_rule_forbids_closing_browser(self, monkeypatch):
        """交接清单里必须带"不要 close / 不要 launch"的约束。"""
        a = self._agent()
        b = a.tool_manager.get_tool("browser")
        b._pages = [object()]
        # 让 LLM 摘要走兜底路径（返回空 → compose_handoff 模板）
        monkeypatch.setattr(a.llm, "chat", lambda *args, **kw: "")
        text = a._handoff_for_incomplete("填表单", "已达到任务最大操作轮数（80）", "未完成")
        assert "浏览器" in text
        assert "不要 close" in text and "launch" in text

    def test_no_browser_constraint_when_no_session(self, monkeypatch):
        """没有浏览器会话时不应注入多余约束（避免误导模型）。"""
        a = self._agent()
        b = a.tool_manager.get_tool("browser")
        b._pages = []
        monkeypatch.setattr(a.llm, "chat", lambda *args, **kw: "")
        text = a._handoff_for_incomplete("改代码", "已达到任务最大操作轮数（80）", "未完成")
        assert "不要 close" not in text


class TestIncompleteReasonFromStopReason:
    """交接原因必须来自真实 stop_reason：上限报上限，中断报中断。"""

    def _reason(self, result):
        from agent.agent import Agent
        return Agent._incomplete_reason(result)

    def test_interrupted_reason_mentions_upstream_not_round_cap(self):
        text = self._reason({
            "stop_reason": "interrupted", "ops": 20,
            "budget": {"stop_reason": "interrupted", "used": 20, "limit": 30, "hard_cap": 0,
                       "interrupt_error": "Error code: 429 - tpm exhausted"},
        })
        assert "模型服务中断" in text
        assert "达到任务最大操作轮数" not in text
        assert "429" in text

    def test_exhausted_reason_reports_the_limit(self):
        text = self._reason({
            "stop_reason": "exhausted", "ops": 12,
            "budget": {"stop_reason": "exhausted", "used": 12, "limit": 80, "extensions": 5},
        })
        assert "达到任务最大操作轮数" in text
        assert "上限 80 轮" in text and "续期 5 次" in text

    def test_stalled_reason_reports_spinning_turns(self):
        text = self._reason({
            "stop_reason": "stalled", "ops": 7,
            "budget": {"stop_reason": "stalled", "used": 7, "spinning_turns": 5},
        })
        assert "原地打转" in text and "5 轮" in text

    def test_unknown_reason_labels_used_not_cap(self):
        text = self._reason({"ops": 9, "budget": {}})
        assert "已跑 9 轮" in text
        assert "达到任务最大操作轮数" not in text
