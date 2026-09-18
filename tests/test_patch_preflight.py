# -*- coding: utf-8 -*-
"""preflight 失败摘要的测试（tools/patch.py::_summarize_test_failure）。

背景（agent 在运行日志里明确反馈的坑）：
    preflight 失败时原先只回喂 pytest 输出的**尾部 40 行**。全套测试失败时
    FAILURES 段很长，固定窗口经常只截到断言片段、丢掉"哪个用例失败"，实测导致
    agent 拿着 `assert 110 == 100` 全项目搜不到对应测试名，把（并发改动引起的）
    失败误判成自己改坏了代码。

夹具直接取自本机 pytest 9.1.1 的真实输出格式（含分隔符风格与
`FAILED ::test_x` 这种没有文件部分的 node id）。
"""
import os
import subprocess

import pytest

from tools.patch import EditTool

#: 真实 pytest 9.1.1 输出（已核对分隔符为下划线、summary 行形如 FAILED <node> - <msg>）
REAL_OUTPUT = """
============================= test session starts =============================
collected 3 items

test_sample.py .FF                                                       [100%]

================================== FAILURES ===================================
_______________________________ test_beta_fail ________________________________

    def test_beta_fail():
        got = 110
>       assert got == 100, "容量上限变了"
E       AssertionError: 容量上限变了
E       assert 110 == 100

D:\\proj\\tests\\test_sample.py:6: AssertionError
__________________________ TestGamma.test_gamma_fail __________________________

self = <test_sample.TestGamma object at 0x000001F664977110>

    def test_gamma_fail(self):
>       assert "a" == "b"
E       AssertionError: assert 'a' == 'b'

D:\\proj\\tests\\test_sample.py:10: AssertionError
=========================== short test summary info ===========================
FAILED ::test_beta_fail - AssertionError: 容量上限变了
FAILED ::TestGamma::test_gamma_fail - AssertionError: assert 'a' == 'b'
2 failed, 1 passed in 1.60s
"""


def _sum(combined, edited="", tail=80):
    return EditTool._summarize_test_failure(combined, edited, tail)


class TestFailureDigest:
    def test_lists_every_failed_node(self):
        out = _sum(REAL_OUTPUT)
        assert "失败用例（2 个）" in out
        assert "::test_beta_fail" in out
        assert "::TestGamma::test_gamma_fail" in out

    def test_prefers_the_assert_line_over_the_exception_line(self):
        """首个 E 行是 AssertionError: xxx，真正有用的是带 assert 的那行。"""
        out = _sum(REAL_OUTPUT)
        assert "assert 110 == 100" in out
        assert "assert 'a' == 'b'" in out

    def test_attributes_via_assertion_file_when_node_id_lacks_path(self):
        """`FAILED ::test_x` 没有文件部分时，用断言处的 `path:line:` 补出文件。

        否则"与本次改动无关"这个很有价值的提示会永远发不出来。
        """
        out = _sum(REAL_OUTPUT, edited="agent/memory.py")
        assert "无关" in out and "test_sample.py" in out

    def test_stays_silent_when_file_is_truly_unknown(self):
        """文件完全定位不到时宁可不说，也不能误导模型去回滚无关改动。"""
        text = "\n".join(ln for ln in REAL_OUTPUT.splitlines()
                         if ":6: AssertionError" not in ln and ":10: AssertionError" not in ln)
        out = _sum(text, edited="agent/memory.py")
        assert "无关" not in out, "没有文件证据时不应下归因结论"

    def test_warns_when_failures_are_in_other_files(self):
        out = _sum(REAL_OUTPUT, edited="agent/memory.py")
        # 断言处的文件是 tests/test_sample.py，与被改文件不同 → 应提示无关
        assert "无关" in out and "test_sample.py" in out

    def test_notes_related_failures(self):
        out = _sum(REAL_OUTPUT, edited="tests/test_sample.py")
        assert "相关" in out and "无关" not in out

    def test_reports_truncation_with_total_line_count(self):
        out = _sum(REAL_OUTPUT, tail=5)
        assert "共 %d 行" % len(REAL_OUTPUT.splitlines()) in out
        assert "末尾 5 行" in out

    def test_caps_list_and_says_how_many_are_hidden(self):
        blocks = []
        for i in range(12):
            blocks.append("________________________ test_case_%02d ________________________\n"
                          "    def test_case_%02d():\n"
                          ">       assert %d == 0\n"
                          "E       assert %d == 0\n"
                          "\nD:\\proj\\tests\\test_many.py:%d: AssertionError\n" % (i, i, i, i, i))
        summary = "\n".join("FAILED tests/test_many.py::test_case_%02d - AssertionError" % i
                            for i in range(12))
        out = _sum("\n".join(blocks) + "\n" + summary, edited="agent/memory.py")
        assert "失败用例（12 个）" in out
        assert "还有 4 个未列出" in out

    def test_falls_back_to_headers_when_no_short_summary(self):
        """收集阶段就崩 / 输出被裁剪时没有 FAILED 行，退化为用 FAILURES 段标题。"""
        text = ("================================== FAILURES ===================================\n"
                "_____________________________ test_only_header ______________________________\n"
                "    def test_only_header():\n"
                ">       assert False\n"
                "E       assert False\n"
                "\nD:\\proj\\tests\\test_x.py:3: AssertionError\n")
        out = _sum(text)
        assert "test_only_header" in out

    def test_handles_empty_output_without_crashing(self):
        out = _sum("")
        assert "未能从输出解析出失败用例" in out

    def test_accepts_dash_and_boxdrawing_separators(self):
        """不同 pytest 版本/终端下的分隔符风格都要认。"""
        for bar in ("-", "\u2500", "\u2501"):
            text = ("================================== FAILURES ===================================\n"
                    + bar * 30 + " test_x " + bar * 30 + "\n"
                    + "    def test_x():\n>       assert 1 == 2\n"
                    + "E       assert 1 == 2\n\nD:\\proj\\tests\\t.py:1: AssertionError\n")
            assert "test_x" in _sum(text)


class TestPreflightWiring:
    """确认摘要真的接到了 ToolResult.error 上（不只是函数单测过）。"""

    def test_run_preflight_puts_digest_into_error(self, tmp_path, monkeypatch):
        from config import TEST_CONFIG, TOOL_CONFIG

        target = tmp_path / "m.py"
        target.write_text("x = 1\n", encoding="utf-8")
        backup = tmp_path / "m.py.bak"
        backup.write_text("x = 0\n", encoding="utf-8")

        monkeypatch.setitem(TOOL_CONFIG, "edit_preflight", True)
        monkeypatch.setitem(TEST_CONFIG, "command", "pytest -q")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        tool = EditTool()
        monkeypatch.setattr(tool, "_related_test_command", lambda f, c: c)
        monkeypatch.setattr(tool, "_find_repo_root", lambda f: str(tmp_path))

        class _Proc:
            returncode = 1
            stdout = REAL_OUTPUT
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())

        res = tool._run_preflight(str(target), str(backup))
        assert res is not None and res.success is False
        assert "preflight 测试未通过" in res.error
        assert "::test_beta_fail" in res.error, "摘要应进到 error 里"
        assert "assert 110 == 100" in res.error
        assert res.metadata.get("test_summary")
        assert target.read_text(encoding="utf-8") == "x = 0\n", "失败后应已回滚"
