"""
python 工具"可行动报错"测试（v3，2026-09-15）。

背景：漫剧任务里模型写 `import shutil, pathlib` 复制产物图片，被黑名单正确拦下，
但工具返回的 error 字段恒为「代码执行出错。」，真正有用的信息埋在 12 行 traceback
中间（还混着 tools/python.py 的内部帧），模型据此认定"报错没有详情"，
连续 3 轮瞎猜（换绝对路径、换相对路径、查 cwd）才绕过去。

结论：黑名单不能放松（AGENTS.md 安全底线：只能增不能减），要修的是报错质量。
"""
from tools.python import BlockedImportError, PythonTool


def make_tool():
    return PythonTool()


# ----------------------------------------------------------------------
# 黑名单报错必须"说清楚 + 给出路"
# ----------------------------------------------------------------------

def test_blocked_import_error_field_is_meaningful():
    """error 字段必须点名模块，而不是泛泛的"代码执行出错"。"""
    r = make_tool().execute("import shutil")
    assert not r.success
    assert "shutil" in r.error, "error 字段应点名被禁模块"
    assert "代码执行出错" not in r.error


def test_blocked_import_message_survives_in_output_too():
    """output 与 error 都要有信息（模型读哪个字段都能换路）。"""
    r = make_tool().execute("import shutil")
    assert "安全限制" in r.output
    assert "shutil" in r.output


def test_shutil_hint_points_to_file_tool():
    """copy 场景的出路必须被明确指出来（这是当初卡住的那一步）。"""
    r = make_tool().execute("import shutil")
    assert "file" in r.error.lower()
    assert "copy" in r.error.lower()


def test_subprocess_hint_points_to_terminal():
    r = make_tool().execute("import subprocess")
    assert "terminal" in r.error.lower()


def test_every_blocked_module_has_no_traceback_noise():
    """黑名单命中不该回传内部实现帧（raise box["err"] / exec(...)）。"""
    for mod in ("subprocess", "socket", "sys", "ctypes", "shutil", "multiprocessing",
                "winreg", "pickle", "importlib", "inspect", "gc", "traceback"):
        r = make_tool().execute(f"import {mod}")
        assert not r.success, mod
        assert "tools\\python.py" not in r.output and "tools/python.py" not in r.output, mod
        assert "Traceback" not in r.output, mod


def test_blocked_class_is_specific_type():
    """用独立异常类型区分黑名单与"模块名写错"，否则提示会串台。"""
    from tools.python import _safe_import
    try:
        _safe_import("shutil")
    except BlockedImportError:
        pass
    else:
        raise AssertionError("_safe_import 应抛 BlockedImportError")


def test_unknown_module_is_not_a_blocked_import():
    """写错模块名应走普通报错（ModuleNotFoundError），不该谎称安全限制。"""
    r = make_tool().execute("import totally_not_a_real_module_xyz")
    assert not r.success
    assert "安全限制" not in r.output
    assert "ModuleNotFoundError" in r.output


# ----------------------------------------------------------------------
# 普通报错的 traceback 必须只留用户代码帧
# ----------------------------------------------------------------------

def test_user_traceback_has_no_internal_frames():
    """回归：此前 traceback 里全是 tools/python.py 的帧，看不到用户代码行号。"""
    r = make_tool().execute("x = 1\nraise ValueError('boom')")
    assert not r.success
    assert "ValueError: boom" in r.output
    assert "tools\\python.py" not in r.output and "tools/python.py" not in r.output


def test_user_traceback_keeps_helpful_error_type():
    r = make_tool().execute("{}['missing']")
    assert not r.success
    assert "KeyError" in r.output


def test_generic_error_field_names_exception():
    """error 字段不再是恒定的"代码执行出错。"。"""
    r = make_tool().execute("1 / 0")
    assert not r.success
    assert "ZeroDivisionError" in r.error


def test_stdout_before_error_is_preserved():
    r = make_tool().execute("print('进度 ok')\nraise RuntimeError('x')")
    assert not r.success
    assert "进度 ok" in r.output


# ----------------------------------------------------------------------
# 安全底线未被削弱
# ----------------------------------------------------------------------

def test_blacklist_still_enforced():
    for mod in ("subprocess", "socket", "shutil", "sys", "ctypes"):
        r = make_tool().execute(f"import {mod}")
        assert not r.success, f"{mod} 仍必须被禁"


def test_normal_code_unaffected():
    r = make_tool().execute("import pathlib, json\nprint(json.dumps({'a': 1}))")
    assert r.success, r.output
    assert '{"a": 1}' in r.output


def test_description_warns_about_shutil_and_points_to_file_tool():
    """提示词前置告知，避免模型再浪费轮次去试。"""
    desc = make_tool().description
    assert "shutil" in desc
    assert "file" in desc and "copy" in desc
