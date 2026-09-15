"""
Python 工具测试：受限命名空间必须"够用"（常规语法都能跑）且"够严"（黑名单模块与
eval/exec/globals 不可用）。

背景：早期版本手写了一份几十个内置函数的迷你表，缺 __build_class__ / all / any 等，
导致 Agent 写的普通代码（定义个类、用 all()）就 NameError——桌面端表现为
"python 工具一直失败"。
"""
import os

from tools.python import PythonTool


def make_tool():
    return PythonTool()


# ----------------------------------------------------------------------
# 常规语法必须可用
# ----------------------------------------------------------------------

def test_class_definition_works():
    """定义类（依赖 __build_class__ + __name__）不再 NameError。"""
    r = make_tool().execute("class A:\n    def f(self):\n        return 42\nprint(A().f())")
    assert r.success, r.output
    assert "42" in r.output


def test_dataclass_and_decorator_works():
    t = make_tool()
    r = t.execute("from dataclasses import dataclass\n@dataclass\nclass P:\n    x: int\nprint(P(1))")
    assert r.success, r.output
    assert "P(x=1)" in r.output


def test_common_builtins_available():
    r = make_tool().execute(
        "print(all([True, 1 > 0]), any([False, True]), sorted([3, 1, 2]), "
        "list(map(abs, [-1, -2])), sum([1, 2]), max([1, 5]), min([1, 5]), "
        "len('abc'), repr('x'), format(3.14159, '.2f'), str(round(2.567, 1)))"
    )
    assert r.success, r.output
    assert r.output.strip() == "True True [1, 2, 3] [1, 2] 3 5 1 3 'x' 3.14 2.6"


def test_exceptions_and_try_except():
    r = make_tool().execute(
        "try:\n    {}['missing']\nexcept KeyError as e:\n    print('caught', type(e).__name__)"
    )
    assert r.success, r.output
    assert "caught KeyError" in r.output


def test_super_and_inheritance():
    r = make_tool().execute(
        "class B:\n    def hi(self):\n        return 'B'\n"
        "class C(B):\n    def hi(self):\n        return super().hi() + 'C'\n"
        "print(C().hi())"
    )
    assert r.success, r.output
    assert "BC" in r.output


def test_file_write_and_read(tmp_path):
    """文件读写（open / pathlib）仍可用。"""
    t = make_tool()
    p = str(tmp_path / "out.txt").replace("\\", "/")
    w = t.execute(f"open({p!r}, 'w', encoding='utf-8').write('你好 world')")
    assert w.success, w.output
    r = t.execute(f"print(open({p!r}, encoding='utf-8').read())")
    assert r.success
    assert "你好 world" in r.output
    assert os.path.isfile(p)


def test_preloaded_modules_available():
    r = make_tool().execute("import json, math, re, datetime, os, pathlib, collections, itertools\nprint(math.sqrt(16))")
    assert r.success, r.output
    assert "4.0" in r.output


# ----------------------------------------------------------------------
# 安全边界必须守住
# ----------------------------------------------------------------------

def test_blocked_imports_still_rejected():
    for mod in ("subprocess", "socket", "sys", "ctypes", "shutil", "multiprocessing"):
        r = make_tool().execute(f"import {mod}")
        assert not r.success, f"{mod} 不应可导入"
        assert "安全限制" in r.output


def test_eval_exec_globals_unavailable():
    """eval/exec/compile/globals/locals 不在内置项里（防止绕过沙箱）。"""
    for code in ("eval('1+1')", "exec('x=1')", "compile('1', '<s>', 'eval')",
                 "globals()", "locals()", "input()"):
        r = make_tool().execute(code)
        assert not r.success, f"{code} 不应可用"
        assert "not defined" in r.output or "NameError" in r.output


def test_stdout_captured_and_restored():
    """print 输出被捕获，且执行结束后 sys.stdout 被还原。"""
    import sys
    before = sys.stdout
    t = make_tool()
    r = t.execute("print('captured')")
    assert r.success and "captured" in r.output
    assert sys.stdout is before, "sys.stdout 未被还原（会影响后端主进程输出）"


def test_empty_code_rejected():
    r = make_tool().execute("   ")
    assert not r.success


class TestExecutionTimeout:
    """回归：该工具体此前**没有任何超时**（description 却声称 30 秒），
    模型写下 sleep 轮询/长循环时会一直挂到工具层 300s 硬超时才返回。"""

    def test_sleep_times_out_quickly(self, monkeypatch):
        import time
        from config import TOOL_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "python_timeout", 0.5)
        t = make_tool()
        t0 = time.time()
        r = t.execute("import time; time.sleep(10)")
        elapsed = time.time() - t0
        assert elapsed < 5, "应在自身超时内返回，而不是跑满 sleep"
        assert not r.success
        assert "超时" in r.error

    def test_timeout_hint_mentions_background(self, monkeypatch):
        """超时错误要给出正确出路（用 terminal 后台任务，别轮询）。"""
        from config import TOOL_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "python_timeout", 0.5)
        r = make_tool().execute("import time; time.sleep(10)")
        assert "background" in r.error or "后台" in r.error

    def test_partial_output_returned_on_timeout(self, monkeypatch):
        """超时前已产生的输出要回传，便于判断卡在哪一步。"""
        from config import TOOL_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "python_timeout", 0.5)
        r = make_tool().execute("print('进度: 第一步完成'); import time; time.sleep(10)")
        assert "进度" in r.output

    def test_normal_code_unaffected(self):
        r = make_tool().execute("print(6 * 7)")
        assert r.success and "42" in r.output

    def test_description_states_real_timeout(self):
        """description 承诺的超时秒数必须与实际配置一致（避免误导模型）。"""
        from config import TOOL_CONFIG
        from tools.python import _timeout_seconds
        desc = make_tool().description
        assert str(int(_timeout_seconds())) in desc
        assert "轮询" in desc   # 明确劝阻 sleep 轮询
