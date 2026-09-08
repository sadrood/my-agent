"""desktop_gui.apply_event 渲染逻辑单测（不依赖 Tk 显示环境）。

覆盖评审要求的"工具调用卡片"：名称 + 参数 + 状态（成功/失败）+ 耗时，
以及结果行回写、answer 渲染、同工具多次调用的卡片配对。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.desktop_gui import apply_event


class StubText:
    """模拟 Tkinter Text 的最小容器：按行存放文本。"""

    def __init__(self):
        self.buf = []                # list[str]，按行
        self.calls = []              # 记录调用（供断言 tag 行为）

    def config(self, **kw):
        self.calls.append(("config", kw))

    def insert(self, index, text, tag=None):
        self.calls.append(("insert", index, text, tag))
        if index == "end":
            self.buf.append(str(text).rstrip("\n"))
        else:
            line = int(index.split(".")[0])
            while len(self.buf) < line:
                self.buf.append("")
            self.buf[line - 1] = str(text)

    def see(self, index):
        self.calls.append(("see", index))

    def index(self, index):
        return f"{len(self.buf)}.0"

    def tag_add(self, tag, *args):
        self.calls.append(("tag_add", tag, args))

    def delete(self, index1, index2):
        self.calls.append(("delete", index1, index2))
        line = int(index1.split(".")[0])
        if 1 <= line <= len(self.buf):
            self.buf[line - 1] = ""

    def all_lines(self):
        return list(self.buf)


def _setup():
    out = StubText()
    pending = []
    return out, pending


def test_tool_call_renders_card_with_pending():
    out, pending = _setup()
    apply_event(out, "tool_call", {"tool": "bash", "arguments": {"cmd": "ls -la"}}, pending)
    lines = out.all_lines()
    assert any("⏺ bash" in ln and "运行中" in ln for ln in lines), lines
    assert any("ls -la" in ln for ln in lines), lines
    assert len(pending) == 1
    assert pending[0]["tool"] == "bash"
    # 头部与参数行都应挂卡片底色 tag
    tag_names = [t for c in out.calls if c[0] == "tag_add"
                 for t in ([c[1]] if isinstance(c[1], str) else c[1]) if isinstance(c[1], str)]
    assert "card" in tag_names, tag_names


def test_tool_result_updates_card_status_and_appends_result():
    out, pending = _setup()
    apply_event(out, "tool_call", {"tool": "bash", "arguments": {"cmd": "pwd"}}, pending)
    apply_event(out, "tool_result", {
        "tool": "bash", "success": True, "output": "/data/work", "arguments": {"cmd": "pwd"},
    }, pending)
    lines = out.all_lines()
    head = next(ln for ln in lines if "⏺ bash" in ln)
    assert "✅ 成功" in head and "s）" in head, head          # 状态 + 耗时
    assert any("⎿ " in ln and "/data/work" in ln for ln in lines), lines
    assert pending == [], "卡片结算后应从待处理列表移除"


def test_tool_failure_marks_error():
    out, pending = _setup()
    apply_event(out, "tool_call", {"tool": "write_file", "arguments": {"path": "a.txt"}}, pending)
    apply_event(out, "tool_result", {
        "tool": "write_file", "success": False, "output": "", "error": "permission denied",
    }, pending)
    head = next(ln for ln in out.all_lines() if "⏺ write_file" in ln)
    assert "❌ 失败" in head, head
    assert any("permission denied" in ln for ln in out.all_lines())


def test_same_tool_multiple_calls_pairs_in_order():
    out, pending = _setup()
    for i in range(2):
        apply_event(out, "tool_call", {"tool": "read_file", "arguments": {"path": f"f{i}.txt"}}, pending)
    apply_event(out, "tool_result", {"tool": "read_file", "success": True, "output": "AAA"}, pending)
    assert len(pending) == 1, "第一张卡片应已结算，还剩一张"
    apply_event(out, "tool_result", {"tool": "read_file", "success": True, "output": "BBB"}, pending)
    assert pending == []
    # 两张头部都应带上最终状态（第一张被重写，第二张也重写）
    heads = [ln for ln in out.all_lines() if "⏺ read_file" in ln]
    assert len(heads) == 2 and all("✅ 成功" in h for h in heads), heads


def test_answer_renders_separator_and_output():
    out, pending = _setup()
    apply_event(out, "answer", {"output": "任务完成！"}, pending)
    lines = out.all_lines()
    assert any(ln.strip() == "─" * 46 for ln in lines)
    assert "任务完成！" in lines
    assert pending == []


def test_tool_call_without_arguments_is_compact():
    out, pending = _setup()
    apply_event(out, "tool_call", {"tool": "think", "arguments": {}}, pending)
    head = next(ln for ln in out.all_lines() if "⏺ think" in ln)
    assert "运行中" in head