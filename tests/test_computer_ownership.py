"""桌面键鼠：输入账本只释放我们自己按下的键；用户接管后硬停且不自动抢回。"""
import pytest

import tools.computer_use as cu
from tools.computer_use import DesktopTool


class _FakeUser32:
    def __init__(self):
        self.events = []

    def keybd_event(self, vk, scan, flags, extra):
        self.events.append(("key", vk, flags))

    def mouse_event(self, flag, dx, dy, data, extra):
        self.events.append(("mouse", flag))

    def SetCursorPos(self, x, y):
        return True

    def GetLastInputInfo(self, ptr):
        return 0


@pytest.fixture(autouse=True)
def _clean_state():
    cu.set_owner("agent")
    cu._HELD_KEYS.clear()
    cu._HELD_BUTTONS.clear()
    yield
    cu.set_owner("agent")
    cu._HELD_KEYS.clear()
    cu._HELD_BUTTONS.clear()


@pytest.fixture
def fake_user32(monkeypatch):
    fake = _FakeUser32()
    monkeypatch.setattr(cu, "_user32", lambda: fake)
    monkeypatch.setattr(cu, "_mark_synthetic", lambda: None)
    return fake


class TestInputLedger:
    def test_release_only_touches_keys_we_hold(self, fake_user32):
        """用户正按着 Ctrl 时，兜底释放不能去抬它。"""
        cu._HELD_KEYS.add(0x11)                 # 我们自己按下的 Ctrl
        cu._release_all_inputs()
        released = [vk for kind, vk, _ in fake_user32.events if kind == "key"]
        assert released == [0x11]
        assert cu._HELD_KEYS == set()

    def test_nothing_held_means_no_input_events(self, fake_user32):
        cu._release_all_inputs()
        assert fake_user32.events == []

    def test_key_combo_pairs_down_up_and_empties_ledger(self, fake_user32):
        vks = cu._os_key_combo("ctrl+s")
        assert vks and cu._HELD_KEYS == set(), "按下后必须抬起，账本清空"
        downs = [vk for kind, vk, flags in fake_user32.events if kind == "key" and flags == 0]
        ups = [vk for kind, vk, flags in fake_user32.events
               if kind == "key" and flags == cu._KEYEVENTF_KEYUP]
        assert sorted(downs) == sorted(ups) == sorted(vks)

    def test_click_records_and_clears_button(self, fake_user32):
        assert cu._os_click(10, 20) is True
        assert cu._HELD_BUTTONS == set()


class TestOwnership:
    def _tool(self, monkeypatch, user_active=False):
        tool = DesktopTool(vision_model=None)
        monkeypatch.setattr(cu, "_user_is_active", lambda *a, **kw: user_active)
        monkeypatch.setattr(cu, "_yield_enabled", lambda: True)
        monkeypatch.setattr(cu, "_release_all_inputs", lambda: None)
        monkeypatch.setattr(cu, "get_overlay", lambda: None)
        monkeypatch.setattr(cu, "_os_click", lambda *a, **kw: True)
        monkeypatch.setattr(cu, "_os_type_text", lambda t: True)
        monkeypatch.setenv("COMPUTER_YIELD_WAIT_MS", "0")
        return tool

    def test_active_user_takes_over_control(self, monkeypatch):
        tool = self._tool(monkeypatch, user_active=True)
        result = tool.execute_json({"action": "click", "x": 1, "y": 1})
        assert result.success is False
        assert "交给你" in result.error and "resume" in result.error
        assert cu.ownership() == "user"

    def test_blocked_while_user_owns_control(self, monkeypatch):
        tool = self._tool(monkeypatch, user_active=False)
        cu.set_owner("user")
        result = tool.execute_json({"action": "type", "text": "hi"})
        assert result.success is False and "控制权在用户手里" in result.error

    def test_no_auto_reclaim(self, monkeypatch):
        """连续调用也不能自行抢回——每次都还得由用户 resume。"""
        tool = self._tool(monkeypatch, user_active=False)
        cu.set_owner("user")
        for _ in range(3):
            assert tool.execute_json({"action": "click", "x": 1, "y": 1}).success is False
        assert cu.ownership() == "user"

    def test_resume_hands_back(self, monkeypatch):
        tool = self._tool(monkeypatch, user_active=False)
        cu.set_owner("user")
        result = tool.execute_json({"action": "resume"})
        assert result.success is True and "交回" in result.output
        assert cu.ownership() == "agent"
        assert tool.execute_json({"action": "click", "x": 1, "y": 1}).success is True

    def test_agent_actions_unaffected_when_owner_is_agent(self, monkeypatch):
        tool = self._tool(monkeypatch, user_active=False)
        assert tool.execute_json({"action": "click", "x": 1, "y": 1}).success is True
