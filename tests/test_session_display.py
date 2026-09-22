# -*- coding: utf-8 -*-
"""会话历史显示测试。

用户反馈原话："现在重新打开之前的对话 cli 看不到历史记录，没有之前的对话显示"。
实测：数据没问题（27 个会话文件都在、能加载），**是显示问题**——
`--session` / `-r` 恢复时只打印一行"已恢复（N 条记录）"，屏幕上没有任何历史内容；
`/open` 只给最近 4 条 × 80 字。恢复上下文却不显示上下文，等于让人猜上次聊到哪。
"""
import pytest

from main import _print_transcript


class FakeConsole:
    """记录渲染出的行（rich 标记原样保留，便于断言）。"""

    def __init__(self):
        self.lines = []

    def print(self, text, **kw):
        self.lines.append(str(text))

    def text(self) -> str:
        return "\n".join(self.lines)


def _msgs(n=6):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"问题{i}"})
        out.append({"role": "assistant", "content": f"回答{i}"})
    return out


class TestPrintTranscript:
    def test_shows_roles_and_content(self):
        c = FakeConsole()
        _print_transcript(c, _msgs(2), limit=10, header="—— 标题 ——")
        t = c.text()
        assert "—— 标题 ——" in t
        assert "你: 问题1" in t and "小悟: 回答1" in t

    def test_limits_to_recent_and_says_so(self):
        """截断必须显式：告诉用户共多少条、显示了多少、去哪看更多。"""
        c = FakeConsole()
        _print_transcript(c, _msgs(10), limit=4)     # 20 条消息
        t = c.text()
        assert t.count("你: ") + t.count("小悟: ") == 4
        assert "共 20 条" in t and "最近 4 条" in t and "/history" in t

    def test_no_truncation_notice_when_all_shown(self):
        c = FakeConsole()
        _print_transcript(c, _msgs(2), limit=10)
        t = c.text()
        assert "共 4 条" in t and "看更多" not in t

    def test_long_message_is_clipped_with_ellipsis(self):
        c = FakeConsole()
        _print_transcript(c, [{"role": "user", "content": "长" * 500}], limit=5, width=50)
        line = [l for l in c.lines if "你: " in l][0]
        body = line.split("你: ", 1)[1].rstrip("[/white]")
        assert len(body) <= 51 and body.endswith("…"), body[-10:]

    def test_newlines_collapsed_to_one_line(self):
        c = FakeConsole()
        _print_transcript(c, [{"role": "assistant", "content": "第一行\n第二行"}])
        assert "第一行 第二行" in c.text()

    def test_empty_history_prints_nothing(self):
        c = FakeConsole()
        _print_transcript(c, [])
        _print_transcript(c, [{"role": "user", "content": "   "}])
        assert c.lines == [], "没有内容时不该打印空标题"

    def test_accepts_session_file_format(self):
        """会话文件里的 messages 就是这个结构（role/content），要能直接喂进来。"""
        c = FakeConsole()
        _print_transcript(c, [{"role": "user", "content": "在 output/x.md 里写三段短文"}],
                          limit=5, header="—— 上次对话 ——")
        assert "三段短文" in c.text()
