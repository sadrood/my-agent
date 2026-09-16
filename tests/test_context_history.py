"""
回归测试：会话历史不能丢最后一条（否则模型会回去答上一题）。

真实故障（2026-09-16，用户报"我每次粘贴大量文字到 cli，它都看不见，然后回到上一个
问题回答"）：

- rollout run-20260916-202234 的 goal 是用户粘贴的 3028 字新剧本，`run_start`
  里 goal 完整无误，**粘贴本身没问题**；
- 但 agent 后续 4 轮全在改"字幕太大"——也就是上一个问题；
- 把当时真正发给模型的 messages 原样回放，模型依然答"字幕"，100% 复现；
- 原因：`agent.py` 上下文构建里 `for m in recent[:-1]` 假设"memory 最后一条就是
  本轮 goal"。该假设只在**计划模式**成立（run() 开头就 add_message(goal)）；
  而默认的**单循环模式**在第 482 行就直接 return _run_loop()，goal 要到本次 run
  结束才写进 memory（同文件末尾 add_message），于是 recent[:-1] 白白删掉了上一条
  真实历史。
- 这次被删掉的恰好是助手那句"新字幕版已经烧好了"，历史便以一句**没人回应的旧
  抱怨**（"字幕太大了"）结尾，模型于是回去答那个旧问题，完全无视新目标。
- 对照实验：同一个 goal，仅把被删的最后一条历史补回去，模型立刻正确回应新剧本。
"""
import pytest

from agent.agent import Agent, AgentConfig
from agent.memory import Memory
from models.llm import LLMToolResponse
from tools.tool_manager import ToolManager


class CapturingLLM:
    """记录每次收到的 messages，并立刻返回纯文本结束循环。"""

    def __init__(self):
        self.tools_calls = []
        self.chat_calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.tools_calls.append(messages)
        return LLMToolResponse(content="好的。")

    def chat(self, messages, **kwargs):
        self.chat_calls.append(messages)
        return "好的。"


def make_agent(tmp_path):
    config = AgentConfig(
        exec_mode="loop",
        verbose=False,
        rollout_enabled=False,
        guardian_enabled=False,
        approval_policy="never",
        sandbox_mode="workspace-write",
        approval_interactive=False,
        instructions_enabled=False,
        enable_vision=False,
        enable_frame_compare=False,
        enable_anomaly_detect=False,
        snapshot_enabled=False,
        checkpoint_per_tool=False,
        repomap_enabled=False,
    )
    return Agent(
        llm=CapturingLLM(),
        tool_manager=ToolManager(),
        memory=Memory(db_path=str(tmp_path)),
        config=config,
    )


def user_message(llm) -> str:
    msgs = llm.tools_calls[0]
    return next(m["content"] for m in msgs if m["role"] == "user")


# ----------------------------------------------------------------------
# 核心回归
# ----------------------------------------------------------------------

def test_last_history_message_is_kept(tmp_path):
    """单循环模式下 goal 还没进 memory，最后一条历史必须留下。

    丢掉它会制造"上一轮没人回应的请求"结尾，模型据此回去答旧题。
    """
    agent = make_agent(tmp_path)
    agent.memory.add_message("user", "字幕太大了，都占满半边屏幕了")
    agent.memory.add_message("assistant", "搞定了！新字幕版已经烧好了。")

    agent.run("我又重新设计了一版剧本：# 镜头1 雨夜街角……", keep_session=True)

    text = user_message(agent.llm)
    assert "新字幕版已经烧好了" in text, (
        "最后一条历史（助手已完成的那句）被丢掉了——正是它让模型误以为"
        "上一轮抱怨还没被处理，于是回去答旧题"
    )
    assert "之前的对话记录" in text


def test_full_recent_history_included(tmp_path):
    """历史应完整进入上下文（不止最后一条）。"""
    agent = make_agent(tmp_path)
    for i in range(3):
        agent.memory.add_message("user", f"问题{i}")
        agent.memory.add_message("assistant", f"回答{i}")

    agent.run("新目标", keep_session=True)
    text = user_message(agent.llm)
    for i in range(3):
        assert f"回答{i}" in text


def test_current_goal_not_duplicated_in_history(tmp_path):
    """若 goal 已在 memory 里（计划模式的行为），历史中不应重复出现它。"""
    agent = make_agent(tmp_path)
    goal = "把字幕改小"
    agent.memory.add_message("user", "上一句闲聊")
    agent.memory.add_message("assistant", "上一句回答")
    agent.memory.add_message("user", goal)     # 模拟计划模式：goal 已入 memory

    agent.run(goal, keep_session=True)
    text = user_message(agent.llm)
    assert text.count(goal) == 1, "本轮 goal 不应既当目标、又在历史里重复一遍"
    assert "上一句回答" in text, "排除 goal 时不该连真实历史一起删"


def test_new_goal_still_present_and_first(tmp_path):
    """新目标必须完整出现在用户消息最前面（粘贴的长文不能被挤掉）。"""
    agent = make_agent(tmp_path)
    agent.memory.add_message("user", "字幕太大了")
    agent.memory.add_message("assistant", "已经烧好小字幕版了")
    long_goal = "新剧本：" + "镜头内容" * 500

    agent.run(long_goal, keep_session=True)
    text = user_message(agent.llm)
    assert text.startswith(long_goal), "新目标必须原样、完整地放在最前面"


# ----------------------------------------------------------------------
# 助手函数本身
# ----------------------------------------------------------------------

class TestHistoryHelper:
    def test_drops_only_when_last_is_current_goal(self):
        hist = [{"role": "user", "content": "旧的"},
                {"role": "assistant", "content": "回过的"},
                {"role": "user", "content": "本轮目标"}]
        kept = Agent._history_without_current_goal(hist, "本轮目标")
        assert kept == hist[:-1]

    def test_keeps_last_when_it_is_real_history(self):
        hist = [{"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"}]
        assert Agent._history_without_current_goal(hist, "全新的目标") == hist

    def test_handles_empty_and_none(self):
        assert Agent._history_without_current_goal([], "x") == []
        assert Agent._history_without_current_goal(None, "x") is None

    def test_ignores_whitespace_difference(self):
        hist = [{"role": "user", "content": "  目标  "}]
        assert Agent._history_without_current_goal(hist, "目标") == []

    def test_assistant_last_message_is_never_dropped(self):
        hist = [{"role": "assistant", "content": "目标"}]
        assert Agent._history_without_current_goal(hist, "目标") == hist
