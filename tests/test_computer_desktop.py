"""
桌面操控工具（DesktopTool）测试。
不触发真实鼠标键盘：输入原语全部 monkeypatch，只验证 schema/分发/解析逻辑。
"""
import pytest

from tools.computer_use import DesktopTool, _VK_MAP, _os_key_combo


class _FakeVision:
    def analyze(self, b64, question, max_tokens=1500):
        return "【测试分析】屏幕上有一个按钮。"


def _must_not_touch_real_input(name: str):
    """把会向系统发合成输入的原语换成"一调用就炸"的桩。

    用 `pytest.fail` 而不是 `assert`：它抛的 `Failed` 是 `BaseException` 子类，
    不会被 `execute_json` 里的 `except Exception` 吞成失败结果 —— 漏 mock 时测试
    直接红，而不是悄悄过去。
    """
    def _stub(*_a, **_kw):
        pytest.fail(f"测试不得触碰真实键鼠：{name}() 被真的调用了。"
                    f"这条测试确实要用它，请自己 monkeypatch 成记录器。",
                    pytrace=False)
    return _stub


@pytest.fixture
def tool(monkeypatch):
    """默认不碰真实键鼠：让路闸门中立化 + 输入原语换成"一调用就炸"的桩。

    闸门看的是"最近 700ms 有没有真人输入"（机器状态而非代码状态），真人一动鼠标
    断言就随机变红；原语则容易"忘了 mock"（本文件漏过一次），故改成默认禁止。
    """
    monkeypatch.setattr("tools.computer_use._user_is_active", lambda *a, **kw: False)
    # 前后各调一次的真实"抬起"事件（异常/中断时不留卡键）：测试里换成空操作
    monkeypatch.setattr("tools.computer_use._release_all_inputs", lambda: None)
    for _name in ("_os_click", "_os_type_text", "_os_key_combo", "_os_scroll"):
        monkeypatch.setattr(f"tools.computer_use.{_name}",
                            _must_not_touch_real_input(_name))
    return DesktopTool(vision_model=_FakeVision())


def test_registered_in_manager():
    """computer 工具应被 ToolManager 注册，且审批元数据为高危。"""
    from tools.tool_manager import ToolManager
    tm = ToolManager()
    t = tm.get_tool("computer")
    assert t is not None
    assert t.risk_level == "high"
    assert t.approval == "on-request"
    assert t.min_sandbox_mode == "danger-full-access"


def test_on_request_survives_into_approval_request():
    """`approval="on-request"` 必须真的传进 ApprovalRequest —— 断言类属性是不够的。

    实测故障（2026-09-22 审计）：工具层 8 份 `build_approval_request` 覆写
    （terminal / file / patch / browser / python / installer / 短剧工厂服务 / zhihu）
    谁都没传 `approval`，字段恒为 dataclass 默认的 "auto"，于是
    `ApprovalPolicy.decide` 第 3 步（`request.approval == "on-request"`）在真实
    链路上永不触发。而 computer 的 `min_sandbox_mode` 是 danger-full-access，
    恰好让 decide() 的高危分支（`risk=="high" and sandbox != danger-full-access`）
    也不生效 ⇒ 点击/输入变成零确认执行。
    上面 `test_registered_in_manager` 只断言类属性、`test_approval.py` 手工塞字段，
    两端都盖不到这条链路。
    """
    from tools.tool_manager import ToolManager
    from agent.approval import ApprovalPolicy

    tm = ToolManager()
    req = tm.build_approval_request("computer", {"action": "type", "text": "hi"})
    assert req is not None
    assert req.approval == "on-request", "on-request 元数据在链路中丢了"

    # on-request 必须走到询问：approver 说不行就不行
    p = ApprovalPolicy(mode="on-failure", sandbox_mode="danger-full-access",
                       interactive=False, approver=lambda r: False)
    assert p.decide(req).allowed is False

    # never（无人值守）：工具主动要求批准 → 直接拒
    p2 = ApprovalPolicy(mode="never", sandbox_mode="danger-full-access",
                        interactive=False)
    assert p2.decide(req).allowed is False


def test_default_approval_metadata_still_auto():
    """没声明 on-request 的工具不受影响，走的还是原来的 auto 路径。"""
    from tools.tool_manager import ToolManager
    req = ToolManager().build_approval_request("terminal", {"command": "pip list"})
    assert req.approval == "auto"


def test_schema_actions():
    tool = DesktopTool()
    s = tool.schema
    assert s["type"] == "object"
    assert set(s["properties"]["action"]["enum"]) == {
        "screenshot", "a11y", "click", "type", "key", "scroll", "window"}


def test_unknown_action(tool):
    r = tool.execute_json({"action": "fly"})
    assert not r.success
    assert "未知 action" in r.error


def test_click_requires_coords(tool, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tools.computer_use._os_click",
        lambda x, y, button="left", double=False: calls.append((x, y, button, double)) or True)
    # 密封性：computer 工具有"用户优先"保护——检测到真人正在动鼠标/键盘就让出本次
    # 动作。测试若依赖真实输入状态，跑测试时用户恰好碰一下鼠标就会失败（实测过）。
    monkeypatch.setattr("tools.computer_use._user_is_active", lambda *a, **kw: False)

    r = tool.execute_json({"action": "click"})
    assert not r.success and "坐标" in r.error
    assert calls == []

    r2 = tool.execute_json({"action": "click", "x": 100, "y": 200, "double": True})
    assert r2.success
    assert calls == [(100, 200, "left", True)]


def test_yield_to_active_user(tool, monkeypatch):
    """反向保证：真人正在操作时确实让出（这条保护本身不能被测试改坏）。"""
    monkeypatch.setattr("tools.computer_use._user_is_active", lambda *a, **kw: True)
    monkeypatch.setattr("tools.computer_use._yield_enabled", lambda: True)
    monkeypatch.setenv("COMPUTER_YIELD_WAIT_MS", "0")
    r = tool.execute_json({"action": "click", "x": 1, "y": 1})
    assert not r.success and "让出" in r.error


def test_unmocked_input_primitive_fails_loudly(tool):
    """安全网自检：没换掉原语的测试必须**直接红**，而不是真往系统发按键。

    没有这条，那组桩一旦失效（原语改名、换实现）就会静默降级回"真的点下去"。
    """
    with pytest.raises(pytest.fail.Exception, match="不得触碰真实键鼠"):
        tool.execute_json({"action": "click", "x": 1, "y": 2})


def test_type_dispatch(tool, monkeypatch):
    typed = []
    monkeypatch.setattr("tools.computer_use._os_type_text", lambda t: typed.append(t) or True)

    r = tool.execute_json({"action": "type", "text": "你好世界"})
    assert r.success and typed == ["你好世界"]

    r2 = tool.execute_json({"action": "type", "text": ""})
    assert not r2.success


def test_key_dispatch(tool, monkeypatch):
    pressed = []
    monkeypatch.setattr("tools.computer_use._os_key_combo",
                        lambda c: pressed.append(c) or [0x11])
    r = tool.execute_json({"action": "key", "combo": "ctrl+s"})
    assert r.success and pressed == ["ctrl+s"]

    # 真实解析：非法键名必须在触碰硬件前就拒绝（先校验后按键）
    assert _os_key_combo("ctrl+不存在的键") is None
    assert _os_key_combo("") is None


def test_vk_map_core_keys():
    for k in ("ctrl", "shift", "alt", "enter", "esc", "delete", "f4", "a", "5", "win", "space"):
        assert k in _VK_MAP


def test_window_list_and_activate(tool, monkeypatch):
    monkeypatch.setattr("tools.computer_use._os_windows",
                        lambda: [(1, "记事本"), (2, "Edge")])
    monkeypatch.setattr("tools.computer_use._os_activate",
                        lambda t: "记事本 - 无标题" if "记事本" in t else None)

    r = tool.execute_json({"action": "window"})
    assert r.success and "记事本" in r.output

    r2 = tool.execute_json({"action": "window", "title": "记事本"})
    assert r2.success and "已激活" in r2.output

    r3 = tool.execute_json({"action": "window", "title": "不存在的窗口xyz"})
    assert not r3.success


def test_screenshot_with_vision(tool, monkeypatch, tmp_path):
    png = tmp_path / "s.png"
    png.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr("tools.computer_use._take_screenshot", lambda: str(png))
    monkeypatch.setattr("tools.computer_use._active_window_title", lambda: "测试窗口")

    r = tool.execute_json({"action": "screenshot"})
    assert r.success
    assert "【屏幕分析】" in r.output
    assert "测试窗口" in r.output
    assert str(png) in r.output
