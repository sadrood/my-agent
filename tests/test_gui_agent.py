"""任务级 GUI Agent 单测：全程 mock，不碰真实桌面、不消耗模型额度。"""
import threading


from agent.gui_agent import (
    GUISession,
    _extract_json,
    parse_decision,
)


class FakeResult:
    def __init__(self, success=True, output="", error=""):
        self.success = success
        self.output = output
        self.error = error


class FakeComputer:
    """记录动作，返回可预期的观察结果。"""

    def __init__(self, a11y="【a11y】Button '保存' (10,20)-(60,40)", shot="【截图】保存按钮可见 [截图: x.png]"):
        self.a11y = a11y
        self.shot = shot
        self.calls = []

    def execute_json(self, args):
        self.calls.append(args)
        a = args.get("action")
        if a == "a11y":
            return FakeResult(True, self.a11y)
        if a == "screenshot":
            return FakeResult(True, self.shot)
        return FakeResult(True, f"已执行 {a}")


class FakeVision:
    """按脚本依次返回决策文本。analyze 签名与 VisionModel 对齐。"""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def analyze(self, image_data, question, max_tokens=2000, system_prompt=None, **kw):
        out = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return out


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert _extract_json('```json\n{"a": 2}\n```') == {"a": 2}


def test_extract_json_with_prose():
    text = '好的，我的决定是：\n{"action": {"action": "click", "x": 5}, "status": "continue"}\n就是这样。'
    obj = _extract_json(text)
    assert obj["action"]["action"] == "click"


def test_extract_json_none():
    assert _extract_json("完全没有 json") is None


def test_parse_decision_click():
    d = parse_decision('{"reasoning":"点保存","action":{"action":"click","x":"35.0","y":30,"button":"LEFT"},"status":"continue"}')
    assert d["status"] == "continue"
    assert d["action"] == {"action": "click", "x": 35, "y": 30, "button": "left"}


def test_parse_decision_done():
    d = parse_decision('{"reasoning":"已完成","action":{},"status":"done"}')
    assert d["status"] == "done"


def test_parse_decision_invalid_missing_coords():
    d = parse_decision('{"reasoning":"x","action":{"action":"click"},"status":"continue"}')
    assert d["status"] == "invalid"
    assert d["action"] == {}


def test_parse_decision_bad_action():
    d = parse_decision('{"reasoning":"x","action":{"action":"teleport","x":1,"y":2},"status":"continue"}')
    assert d["status"] == "invalid"


def test_parse_decision_garbage():
    d = parse_decision("模型胡言乱语")
    assert d["status"] == "invalid"


def test_loop_click_then_done(monkeypatch):
    """第一步 click，第二步 done；验证动作执行与状态收敛。"""
    vision = FakeVision([
        '{"reasoning":"点保存按钮","action":{"action":"click","x":35,"y":30},"status":"continue"}',
        '{"reasoning":"保存完成","action":{},"status":"done"}',
    ])
    comp = FakeComputer()
    s = GUISession(comp, vision_model=vision, max_steps=5, use_screenshot=True)
    state = s.run(task="点击保存")
    assert state.done and state.status == "done"
    assert state.summary == "保存完成"
    # 两次循环各一次 a11y + screenshot；外加一次 click 动作
    actions = [c.get("action") for c in comp.calls]
    assert actions.count("a11y") == 2
    assert actions.count("click") == 1


def test_loop_call_user(monkeypatch):
    vision = FakeVision([
        '{"reasoning":"需要验证码","action":{},"status":"call_user"}',
    ])
    s = GUISession(FakeComputer(), vision_model=vision, max_steps=5, use_screenshot=False)
    state = s.run(task="登录")
    assert state.status == "call_user"
    assert state.summary == "需要验证码"
    # 不该执行任何危险动作
    assert not any(c.get("action") in ("click", "type", "key", "scroll") for c in s.computer.calls)


def test_loop_max_steps():
    vision = FakeVision([
        '{"reasoning":"继续","action":{"action":"click","x":1,"y":2},"status":"continue"}',
    ])
    s = GUISession(FakeComputer(), vision_model=vision, max_steps=2, use_screenshot=False)
    state = s.run(task="无限点")
    assert state.status == "max_steps"
    assert len(state.steps) == 2


def test_loop_invalid_streak_fails():
    vision = FakeVision(["胡言乱语"])
    s = GUISession(FakeComputer(), vision_model=vision, max_steps=10, use_screenshot=False)
    state = s.run(task="目标")
    assert state.status == "fail"
    assert "连续 3 次" in state.summary
    assert all(step.action == {} for step in state.steps)


def test_stop_event():
    ev = threading.Event()
    ev.set()
    s = GUISession(FakeComputer(), vision_model=FakeVision([]), max_steps=5,
                   use_screenshot=False, stop_event=ev)
    state = s.run(task="目标")
    assert state.status == "stopped"
    assert len(state.steps) == 0


def test_user_ownership_pauses(monkeypatch):
    """控制权在用户手里时，任务暂停，不执行动作。"""
    import tools.computer_use as cu

    monkeypatch.setattr(cu, "ownership", lambda: "user")
    vision = FakeVision([
        '{"reasoning":"点一下","action":{"action":"click","x":1,"y":2},"status":"continue"}',
    ])
    s = GUISession(FakeComputer(), vision_model=vision, max_steps=5, use_screenshot=False)
    state = s.run(task="目标")
    assert state.status == "call_user"
    assert "控制权" in state.summary
    assert not any(c.get("action") == "click" for c in s.computer.calls)


def test_format_result():
    from agent.gui_agent import GUIStep, GUITaskState

    st = GUITaskState(task="点保存", status="done", summary="完成")
    st.steps.append(GUIStep(index=1, reasoning="点按钮", action={"action": "click", "x": 1, "y": 2},
                            result="已点击"))
    text = GUISession(FakeComputer(), vision_model=FakeVision([])).format_result(st)
    assert "完成" in text and "点保存" in text and "click" in text
