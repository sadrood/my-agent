# -*- coding: utf-8 -*-
"""任务监管者（Supervisor）测试。"""
import json
from types import SimpleNamespace

import pytest

from agent.executor import Executor
from agent.supervisor import Supervisor, SupervisorVerdict, build_supervisor_llm
from models.llm import LLMToolResponse, ToolCall
from tools.tool_manager import ToolManager



@pytest.fixture(autouse=True)
def _supervisor_enabled(monkeypatch):
    """本文件专门验证监管者本身 —— 覆盖 conftest 的默认关闭。"""
    from config import SUPERVISOR_CONFIG
    monkeypatch.setitem(SUPERVISOR_CONFIG, "enabled", True)

class FakeJudge:
    """脚本化监管模型：按顺序返回，可抛错/返回非 JSON。"""

    def __init__(self, *outs):
        self.outs = list(outs)
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        if not self.outs:
            return '{"verdict": "done", "reason": "脚本耗尽"}'
        item = self.outs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _v(verdict, reason="", nxt=""):
    return json.dumps({"verdict": verdict, "reason": reason, "next_instruction": nxt},
                      ensure_ascii=False)


class TestSupervisorParsing:
    def test_continue_with_instruction(self):
        # 判"未完成"后会再做一轮独立复核：脚本要给出复核结论（这里复核也确认缺口成立）
        s = Supervisor(llm=FakeJudge(_v("continue", "只写了第 1 章", "继续写第 2、3 章"),
                                     _v("continue", "交付里确实只有第 1 章")))
        v = s.review("写三章小说", "第 1 章写完了")
        assert v.verdict == "continue" and v.next_instruction == "继续写第 2、3 章"
        assert v.used is True and s.continue_count == 1
        assert s.verify_count == 1, "复核轮应真的发生过"

    def test_done(self):
        s = Supervisor(llm=FakeJudge(_v("done", "三章齐全")))
        assert s.review("写三章", "三章都写完了").verdict == "done"

    def test_fenced_json_tolerated(self):
        s = Supervisor(llm=FakeJudge("好的：\n```json\n" + _v("continue", "缺第3章", "写第3章")
                                     + "\n```",
                                     _v("continue", "确认缺第 3 章")))
        assert s.review("写三章", "两章").verdict == "continue"

    @pytest.mark.parametrize("raw", ["胡说八道", "", "[1,2,3]", '{"verdict": "maybe"}'])
    def test_unparseable_or_unknown_fails_open(self, raw):
        """判不出来就放行：坏掉的裁判不能把任务卡死。"""
        s = Supervisor(llm=FakeJudge(raw))
        v = s.review("写三章", "一章")
        assert v.verdict == "done", raw

    def test_continue_without_instruction_fails_open(self):
        """说 continue 却不给指令 → 主循环无法执行，按完成放行（避免空转）。"""
        s = Supervisor(llm=FakeJudge(_v("continue", "还差东西")))
        assert s.review("写三章", "一章").verdict == "done"

    def test_exception_fails_open_with_error(self):
        s = Supervisor(llm=FakeJudge(RuntimeError("监管端点 500")))
        v = s.review("写三章", "一章")
        assert v.verdict == "done" and "监管者不可用" in v.error

    def test_timeout_fails_open(self, monkeypatch):
        import agent.supervisor as sup
        s = Supervisor(llm=FakeJudge(_v("continue", "x", "y")),
                       config={"enabled": True, "timeout": 0.01, "model": ""})

        def hang(*a, **kw):
            import time
            time.sleep(1.0)
            return _v("continue", "x", "y")

        s.llm = SimpleNamespace(chat=hang)
        v = s.review("写三章", "一章")
        assert v.verdict == "done" and "超时" in v.error

    def test_disabled_supervisor_allows(self):
        s = Supervisor(llm=FakeJudge(_v("continue", "x", "y")),
                       config={"enabled": False})
        assert s.review("g", "f").verdict == "done"
        assert s.review("g", "f").used is False


class TestSupervisorLLMSelection:
    def test_defaults_to_cross_vendor(self, monkeypatch):
        """默认另一个厂商：同源模型自评容易自我确认。"""
        import config
        seen = {}

        class FakeLLM:
            def __init__(self, api_key=None, base_url=None, model=None):
                seen.update({"key": api_key, "base": base_url, "model": model})

        import models.llm as llm_mod
        monkeypatch.setattr(llm_mod, "LLM", FakeLLM)
        monkeypatch.setitem(config.SUPERVISOR_CONFIG, "model", "judge-model")
        monkeypatch.setitem(config.SUPERVISOR_CONFIG, "base_url", "https://judge.example/v1")
        monkeypatch.setitem(config.SUPERVISOR_CONFIG, "api_key", "k-judge")
        build_supervisor_llm(object())
        assert seen == {"key": "k-judge", "base": "https://judge.example/v1",
                        "model": "judge-model"}

    def test_falls_back_to_main_when_endpoint_missing(self, monkeypatch):
        import config
        monkeypatch.setitem(config.SUPERVISOR_CONFIG, "model", "judge-model")
        monkeypatch.setitem(config.SUPERVISOR_CONFIG, "base_url", "")
        monkeypatch.setitem(config.SUPERVISOR_CONFIG, "api_key", "")
        sentinel = object()
        assert build_supervisor_llm(sentinel) is sentinel


class FakeLLM:
    """执行模型的脚本（记录收到的对话，便于断言监管指令真的发给了它）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append(list(messages))
        if not self.script:
            return LLMToolResponse(content="（脚本耗尽）")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _make_executor(llm, supervisor=None):
    from agent.approval import ApprovalPolicy
    return Executor(
        tool_manager=ToolManager(), llm=llm,
        approval_policy=ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                                       interactive=False),
        guardian=None, rollout=None, instructions_text="", max_step_ops=10,
        llm_retry_delay=0, supervisor=supervisor,
    )


def _tool_call(i):
    return LLMToolResponse(content="", tool_calls=[
        ToolCall(str(i), "python", {"code": f"print({i})"})])


class TestExecutorIntegration:
    """核心场景：**只做了一部分就收尾 → 监管者把下一步指令推回来，任务继续做完**。"""

    def _args(self):
        # 目标要够长（监管者对 <12 字的闲聊问答不审）
        return dict(goal="写一部三章的小说，每章至少 300 字，全部写完再统一交付",
                    system_prompt="你是写作助手")

    def test_early_finish_is_pushed_back(self):
        judge = FakeJudge(_v("continue", "只写了第 1 章，目标要三章",
                             "继续写第 2、3 章，写完再一起交付"),
                          _v("continue", "复核确认：交付里只有第 1 章"),   # 独立复核轮
                          _v("done", "三章齐全"))
        sup = Supervisor(llm=judge)
        llm = FakeLLM([_tool_call(1), _tool_call(2),
                       LLMToolResponse(content="第 1 章写好了。"),
                       _tool_call(3), _tool_call(4),
                       LLMToolResponse(content="三章都写完了。")])
        ex = _make_executor(llm, supervisor=sup)
        events = []
        result = ex.execute_goal_loop(**self._args(),
                                      event_sink=lambda t, d: events.append((t, d)))

        assert result["success"] is True
        assert "三章都写完了" in result["output"], "应继续做完再交付"
        assert sup.continue_count == 1
        assert any(t == "supervisor" for t, _ in events), "监管裁决要可见"
        pushed = [d for t, d in events if t == "supervisor"]
        assert pushed[0]["verdict"] == "continue" and "第 1 章" in pushed[0]["reason"]
        # 监管者的**下一步指令**确实发给了执行模型（否则推回去也没用）
        tail = "\n".join(str(m.get("content")) for m in llm.calls[-1])
        assert "监管者复核" in tail and "继续写第 2、3 章" in tail, tail[-400:]

    def test_done_verdict_ends_normally(self):
        sup = Supervisor(llm=FakeJudge(_v("done", "三章齐全")))
        llm = FakeLLM([_tool_call(1), _tool_call(2),
                       LLMToolResponse(content="三章都写完了。")])
        ex = _make_executor(llm, supervisor=sup)
        result = ex.execute_goal_loop(**self._args())
        assert result["success"] is True and sup.continue_count == 0

    def test_supervisor_rounds_are_bounded(self):
        """监管者一直说 continue 也不能无限拉锯。"""
        sup = Supervisor(llm=FakeJudge(*[_v("continue", f"还差第 {i} 章", f"继续第 {i} 章")
                                         for i in range(10)]),
                         config={"enabled": True, "max_rounds": 2, "timeout": 30,
                                 "model": "", "min_turns": 2})
        llm = FakeLLM([_tool_call(1), _tool_call(2), LLMToolResponse(content="第 1 章。"),
                       _tool_call(3), _tool_call(4), LLMToolResponse(content="第 2 章。"),
                       _tool_call(5), _tool_call(6), LLMToolResponse(content="就到这里。")])
        ex = _make_executor(llm, supervisor=sup)
        result = ex.execute_goal_loop(**self._args())
        assert sup.continue_count == 2, "上限就是 max_rounds"
        assert result["output"], "到顶后仍要正常交付（不能卡死）"

    def test_simple_task_does_not_wake_the_supervisor(self):
        """还没干什么活就结束（一问一答）→ 不惊动监管者，省一次调用。"""
        judge = FakeJudge()
        sup = Supervisor(llm=judge)
        llm = FakeLLM([LLMToolResponse(content="3+4=7")])
        ex = _make_executor(llm, supervisor=sup)
        ex.execute_goal_loop(goal="3+4 等于几", system_prompt="s")
        assert judge.calls == [], "简单问答不该触发监管"

    def test_supervisor_failure_does_not_block_delivery(self):
        sup = Supervisor(llm=FakeJudge(RuntimeError("监管挂了")))
        llm = FakeLLM([_tool_call(1), _tool_call(2), LLMToolResponse(content="做完了。")])
        ex = _make_executor(llm, supervisor=sup)
        result = ex.execute_goal_loop(**self._args())
        assert result["success"] is True and "做完了" in result["output"]

    def test_no_supervisor_behaves_as_before(self):
        llm = FakeLLM([_tool_call(1), LLMToolResponse(content="做完了。")])
        ex = _make_executor(llm, supervisor=None)
        result = ex.execute_goal_loop(**self._args())
        assert result["success"] is True

    def test_evidence_is_facts_not_self_report(self):
        judge = FakeJudge(_v("done", "ok"))
        sup = Supervisor(llm=judge)
        llm = FakeLLM([_tool_call(1), _tool_call(2), LLMToolResponse(content="做完了。")])
        ex = _make_executor(llm, supervisor=sup)
        ex.execute_goal_loop(**self._args())
        prompt = judge.calls[0][-1]["content"]
        assert "工具调用" in prompt and "python×2" in prompt, "要给出事实供核对"


class TestAgentWiring:
    def test_agent_builds_supervisor(self):
        from agent import Agent, AgentConfig
        cfg = AgentConfig(verbose=False, guardian_enabled=False, approval_policy="never",
                          approval_interactive=False, session_name="", enable_vision=False)
        a = Agent(config=cfg)
        assert a.supervisor is not None, "默认应启用监管者"
        assert a.executor.supervisor is a.supervisor

    def test_cli_flag_can_disable_it(self):
        """--no-supervisor：模型说完成就结束，不再复核。"""
        from agent import Agent, AgentConfig
        cfg = AgentConfig(verbose=False, guardian_enabled=False, approval_policy="never",
                          approval_interactive=False, session_name="", enable_vision=False,
                          supervisor_enabled=False)
        a = Agent(config=cfg)
        assert a.supervisor is None and a.executor.supervisor is None


class TestVisibility:
    """用户要能看见它做了什么：统计行里显示推回次数。"""

    def test_metrics_line_shows_interventions(self):
        from agent.metrics import RunMetrics
        m = RunMetrics()
        m.turns, m.steps = 13, 20
        assert "监管者推回" not in m.render_line(), "没介入就不该显示"
        m.supervisor_rounds = 2
        assert "监管者推回 2 次" in m.render_line()

    def test_executor_records_interventions_into_metrics(self):
        from agent.metrics import RunMetrics

        sup = Supervisor(llm=FakeJudge(_v("continue", "只写了第 1 章", "继续写第 2 章"),
                                       _v("continue", "复核确认缺口成立"),
                                       _v("done", "ok")))
        llm = FakeLLM([_tool_call(1), _tool_call(2),
                       LLMToolResponse(content="第 1 章。"),
                       _tool_call(3), LLMToolResponse(content="第 2 章。")])
        ex = _make_executor(llm, supervisor=sup)
        ex.metrics = RunMetrics()
        ex.execute_goal_loop(goal="写一部两章的小说，每章 300 字以上，写完再交付",
                             system_prompt="s")
        assert ex.metrics.supervisor_rounds == 1

    def test_report_line_renders(self):
        from agent.metrics import RunMetrics
        m = RunMetrics()
        m.turns, m.steps, m.supervisor_rounds = 13, 20, 1
        line = m.render_line()
        assert "13 轮" in line and "监管者推回 1 次" in line


class TestFirstTurnReview:
    """min_turns 是 1 基语义：默认值 1 表示从第 1 轮起就复核。"""

    def test_supervisor_runs_on_first_turn(self):
        """首轮就收尾也要复核（旧实现 turn 从 0 起，被 off-by-one 整轮漏掉——那正是最该审的"纯文字交付长任务"）。"""
        judge = FakeJudge(_v("done", "ok"))
        sup = Supervisor(llm=judge)
        llm = FakeLLM([LLMToolResponse(content="三章都写完了。")])   # 第 0 轮就给最终答案
        ex = _make_executor(llm, supervisor=sup)
        ex.execute_goal_loop(goal="写一部三章的小说，每章至少 300 字，全部写完再统一交付",
                             system_prompt="你是写作助手")
        assert judge.calls, "首轮收尾同样要复核"

    def test_min_turns_one_based(self):
        """min_turns=2 表示前 1 轮不审（不是前 2 轮）。"""
        judge = FakeJudge(_v("done", "ok"))
        sup = Supervisor(llm=judge, config={"enabled": True, "max_rounds": 3, "timeout": 30,
                                            "model": "", "min_turns": 2})
        # 第 0 轮用一次工具调用（否则拿到最终答案就收尾了），第 1 轮才收尾
        llm = FakeLLM([_tool_call(1),
                       LLMToolResponse(content="第 2 稿。")])      # 第 1 轮（1 基第 2 轮）：应复核
        ex = _make_executor(llm, supervisor=sup)
        ex.execute_goal_loop(goal="写一部三章的小说，每章至少 300 字，全部写完再统一交付",
                             system_prompt="你是写作助手")
        assert judge.calls, "第 2 轮（1 基）起应复核"


class TestIndependentVerification:
    """判"未完成"前先独立复核一次：复核不成立就放行（不为不存在的问题白跑一轮）。"""

    def test_verifier_downgrades_false_continue(self):
        s = Supervisor(llm=FakeJudge(_v("continue", "说还缺第 3 章", "继续写第 3 章"),
                                     _v("done", "交付里其实三章齐全")))
        v = s.review("写三章", "三章都写完了")
        assert v.verdict == "done", "复核不成立应放行"
        assert s.continue_count == 0 and s.verify_count == 1

    def test_verifier_unparseable_keeps_continue(self):
        s = Supervisor(llm=FakeJudge(_v("continue", "缺第 3 章", "写第 3 章"),
                                     "复核模型胡说八道，没有 JSON"))
        assert s.review("写三章", "两章").verdict == "continue", "复核输出无法解析时维持原判"

    def test_verify_can_be_disabled(self):
        s = Supervisor(llm=FakeJudge(_v("continue", "缺第 3 章", "写第 3 章")),
                       config={"enabled": True, "verify_continue": False,
                               "max_rounds": 3, "timeout": 30, "model": "", "min_turns": 1})
        assert s.review("写三章", "两章").verdict == "continue"
        assert s.verify_count == 0, "关掉后不该多花一次复核调用"

    def test_verifier_error_keeps_continue(self):
        class BoomSecond:
            def __init__(self):
                self.n = 0

            def chat(self, messages, **kw):
                self.n += 1
                if self.n == 1:
                    return _v("continue", "缺第 3 章", "写第 3 章")
                raise RuntimeError("复核端点挂了")

        s = Supervisor(llm=BoomSecond())
        assert s.review("写三章", "两章").verdict == "continue", "复核故障不放大缺口损失"
