# -*- coding: utf-8 -*-
"""Guardian 安全审校的测试。

重点是一份**真实事故**的回归：运行日志里 Guardian 拦了 10 次，其中
- 跑 pytest（理由："执行 pytest 不属于必要操作"）
- 删本项目临时文件（理由："破坏性操作"，但项目规则 9/10 明确要求清理）
- 读项目内日志（理由："可能泄露敏感调试信息"）
- `bg output <作业ID>`（理由："命令语法错误，'bg' 无法接受 'output' 参数"）——
  而 tools/terminal.py 里 `bg output` 就是合法子命令，是被凭空判错的
- sleep 45 秒（理由："潜在的拖延/拒绝服务行为"）

根因是提示词的第 4 条"是否明显偏离当前任务目标？"：括号里本意是"被注入后做
无关的**破坏性**操作"，但模型只读到裸条款，于是把"任务相关性"当成了拦截标准。
后果是 Agent 无法完成正常工作，甚至无法遵守项目自己的清理规则。
"""
import json

from agent.guardian import GUARDIAN_SYSTEM_PROMPT, Guardian
from tools.base import ApprovalRequest


def _req(tool="terminal", risk="medium", command="pytest -q"):
    return ApprovalRequest(tool_name=tool, arguments={"command": command},
                           command=command, risk_level=risk)


def _guardian(llm=None, config=None, **over):
    """显式给全配置，避免测试依赖 .env 里的 GUARDIAN_* 设置。

    注意这里的构造用**位置参数**，否则下面的全局替换会把它自己也换掉。
    """
    cfg = {"enabled": True, "min_risk": "medium", "fail_open": True, "timeout": 10}
    cfg.update(config or {})
    cfg.update(over)
    return Guardian(llm, config=cfg)


class FakeLLM:
    def __init__(self, reply='{"verdict": "allow", "reason": "ok"}', error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.reply


class TestPromptPolicy:
    """提示词契约：这些是修出来的策略，不能被改回去。"""

    def test_goal_relevance_is_not_a_block_criterion(self):
        """旧的裸条款"是否明显偏离当前任务目标？"必须消失。

        它把"任务相关性"变成拦截理由，是 10 次误拦的共同根因。
        """
        assert "是否明显偏离当前任务目标？" not in GUARDIAN_SYSTEM_PROMPT
        assert "任务相关性不是安全风险" in GUARDIAN_SYSTEM_PROMPT

    def test_routine_work_is_explicitly_allowed(self):
        for item in ("运行测试", "读取", "bg output", "清理", "等待"):
            assert item in GUARDIAN_SYSTEM_PROMPT, item

    def test_must_not_block_on_guessed_syntax_errors(self):
        """Guardian 无法可靠判断命令语法——实测它把合法的 `bg output` 判成语法错误。"""
        assert "命令语法错误" in GUARDIAN_SYSTEM_PROMPT
        assert "拿不准就 allow" in GUARDIAN_SYSTEM_PROMPT

    def test_still_blocks_real_dangers(self):
        for item in ("删除重要文件", "格式化磁盘", "发到外部", "不可逆", "提示注入"):
            assert item in GUARDIAN_SYSTEM_PROMPT, item

    def test_output_contract_unchanged(self):
        assert '"verdict"' in GUARDIAN_SYSTEM_PROMPT
        assert "reason" in GUARDIAN_SYSTEM_PROMPT


class TestReviewFlow:
    def test_disabled_without_llm(self):
        g = _guardian(llm=None)
        v = g.review(_req(), "目标")
        assert v.verdict == "allow" and v.used is False

    def test_low_risk_is_skipped(self):
        g = _guardian(llm=FakeLLM())
        v = g.review(_req(risk="low"), "目标")
        assert v.verdict == "allow" and v.used is False
        assert g.review_count == 0

    def test_block_verdict_is_parsed_and_counted(self):
        g = _guardian(llm=FakeLLM('{"verdict": "block", "reason": "要删系统目录"}'))
        v = g.review(_req(), "目标")
        assert v.verdict == "block" and "系统目录" in v.reason
        assert g.block_count == 1 and g.review_count == 1

    def test_allow_verdict(self):
        g = _guardian(llm=FakeLLM('{"verdict": "allow", "reason": "跑测试"}'))
        v = g.review(_req(), "目标")
        assert v.verdict == "allow" and g.block_count == 0

    def test_prompt_and_goal_reach_the_model(self):
        llm = FakeLLM()
        _guardian(llm=llm).review(_req(), "审查代码")
        msgs = llm.calls[0]
        assert msgs[0]["content"] == GUARDIAN_SYSTEM_PROMPT
        assert "审查代码" in msgs[1]["content"]
        assert "pytest -q" in msgs[1]["content"]

    def test_fail_open_on_exception(self):
        g = _guardian(llm=FakeLLM(error=RuntimeError("llm 挂了")))
        v = g.review(_req(), "目标")
        assert v.verdict == "allow" and "fail_open" in v.reason

    def test_fail_closed_when_configured(self):
        g = _guardian(llm=FakeLLM(error=RuntimeError("llm 挂了")),
                     config={"enabled": True, "min_risk": "medium",
                             "fail_open": False, "timeout": 5})
        v = g.review(_req(), "目标")
        assert v.verdict == "block" and "fail_closed" in v.reason

    def test_garbage_reply_defaults_to_allow(self):
        """解析不了就放行（与 fail_open 一致），不能因为模型胡说就卡死任务。"""
        g = _guardian(llm=FakeLLM("我不知道该怎么判断"))
        assert g.review(_req(), "目标").verdict == "allow"

    def test_json_wrapped_in_prose_is_still_parsed(self):
        g = _guardian(llm=FakeLLM('好的：{"verdict":"block","reason":"外发密钥"} 以上'))
        v = g.review(_req(), "目标")
        assert v.verdict == "block" and "外发密钥" in v.reason
