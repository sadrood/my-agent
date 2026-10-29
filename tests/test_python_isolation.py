"""python 工具的执行隔离：子进程 + 审计钩子。

进程内受限命名空间不是安全边界（同类方案被 CVE-2025-5120 证伪）：
拿到真实 builtins 就能 exec 任意代码。这里锁住两件事——
① 危险动作（起进程 / 连网络 / 加载原生库）即使绕过导入黑名单也会被审计钩子拦下；
② 超时能真的杀掉执行进程，不再留一个杀不掉的线程在宿主里空转。
"""
import time

import pytest

from tools.python import PythonTool


@pytest.fixture
def tool():
    return PythonTool()


def run(tool, code):
    return tool.execute(code)


def test_builtins_import_blocked(tool):
    r = run(tool, "import builtins\nprint(builtins.exec)")
    assert not r.success
    assert "安全限制" in (r.output or "")


@pytest.mark.parametrize("name,code", [
    # 经内省拿到真实 builtins（导入黑名单挡不住这条），动作仍必须被拦
    ("起子进程",
     "b = print.__self__\n"
     "m = b.__import__('subprocess')\n"
     "print(m.run(['cmd', '/c', 'echo PWNED'], capture_output=True).stdout)"),
    ("os.system",
     "b = print.__self__\nb.exec(\"import os; os.system('echo PWNED')\")"),
    ("连网络",
     "b = print.__self__\n"
     "s = b.__import__('socket')\n"
     "s.create_connection(('127.0.0.1', 9), timeout=2)"),
    ("加载原生库",
     "b = print.__self__\nc = b.__import__('ctypes')\nc.CDLL('kernel32')"),
])
def test_escape_actions_blocked(tool, name, code):
    r = run(tool, code)
    assert "PWNED" not in (r.output or ""), f"{name}真的执行了"
    assert r.success is False, f"{name}不该成功"


def test_normal_work_unaffected(tool, tmp_path):
    p = str(tmp_path / "f.txt").replace("\\", "/")
    assert run(tool, f"open({p!r}, 'w', encoding='utf-8').write('hi')").success
    r = run(tool, f"print(open({p!r}, encoding='utf-8').read())")
    assert r.success and "hi" in r.output
    assert run(tool, "import math\nprint(math.sqrt(16))").success


def test_timeout_really_returns(tool, monkeypatch):
    from config import TOOL_CONFIG
    monkeypatch.setitem(TOOL_CONFIG, "python_timeout", 0.6)
    t0 = time.time()
    r = run(tool, "import time\ntime.sleep(30)")
    elapsed = time.time() - t0
    assert not r.success and "超时" in (r.error or "")
    assert elapsed < 5, f"应立刻返回，实测 {elapsed:.1f}s"


def test_timeout_keeps_partial_output(tool, monkeypatch):
    from config import TOOL_CONFIG
    monkeypatch.setitem(TOOL_CONFIG, "python_timeout", 0.6)
    r = run(tool, "print('进度: 第一步完成')\nimport time\ntime.sleep(30)")
    assert "进度" in (r.output or "")


def test_repeated_calls_are_independent(tool):
    """执行进程之间不共享状态（进程内 exec 时同理，这里锁住不被改成常驻）。"""
    assert run(tool, "x = 1\nprint('set')").success
    r = run(tool, "print('x' in vars())")
    assert r.success and r.output.strip().endswith("False")


def test_lone_surrogate_in_exception_does_not_crash(tool):
    """孤立代理字符编不进 UTF-8：状态行写不出去，连错误报告都拿不到。"""
    r = run(tool, "raise ValueError('x' + chr(0xdc8d))")
    assert r.success is False
    assert "ValueError" in (r.error or "")
