"""delegate 工具测试：外部引擎委托（custom 真跑/不可用报错/嵌套上限）。"""
import pytest

from tools.delegate import DelegateTool


def _out(res) -> str:
    return (res.output or "") + (res.error or "")


def test_custom_runtime_runs():
    """custom 引擎：命令模板经 shell 执行，输出被收集返回。"""
    tool = DelegateTool()
    res = tool.execute_json({
        "runtime": "custom",
        "goal": "hello-from-delegate",
        "command_template": "echo DELEGATE-OK-{goal}",
    })
    assert res.success is True, _out(res)
    assert "DELEGATE-OK-hello-from-delegate" in res.output
    assert res.metadata.get("runtime") == "custom"
    assert res.metadata.get("nested_depth") == 1


def test_custom_without_goal_placeholder_rejected():
    tool = DelegateTool()
    res = tool.execute_json({
        "runtime": "custom",
        "goal": "x",
        "command_template": "python run.py",  # 缺 {goal}
    })
    assert res.success is False
    assert "{goal}" in _out(res)


def test_unavailable_engine_rejected(monkeypatch):
    """探测不到可执行文件时给出明确错误（不启动子进程）。"""
    from agent import runtime as ar
    monkeypatch.setattr(ar.shutil, "which", lambda name: None)
    tool = DelegateTool()
    res = tool.execute_json({"runtime": "codex", "goal": "x"})
    assert res.success is False
    assert "未安装" in _out(res)


def test_depth_guard_blocks_nested_delegate(monkeypatch):
    """嵌套委托上限：子进程环境 depth 已满时拒绝，防引擎→my-agent→引擎失控链。"""
    monkeypatch.setenv("MY_AGENT_DELEGATE_DEPTH", "2")   # 已到上限
    tool = DelegateTool()
    res = tool.execute_json({
        "runtime": "custom",
        "goal": "x",
        "command_template": "echo should-not-run-{goal}",
    })
    assert res.success is False
    assert "嵌套已达上限" in _out(res)


def test_string_interface():
    """旧字符串接口：JSON 参数可用；custom 两段式缺模板时给明确指引。"""
    tool = DelegateTool()
    res = tool.execute('{"runtime":"custom","goal":"a","command_template":"echo J-{goal}"}')
    assert res.success is True and "J-a" in res.output
    res2 = tool.execute("custom 随便一句话")
    assert res2.success is False
    assert "command_template" in _out(res2)


def test_unknown_runtime_rejected():
    tool = DelegateTool()
    res = tool.execute_json({"runtime": "myagent", "goal": "x"})
    assert res.success is False
    assert "delegate 目标必须是" in _out(res)
