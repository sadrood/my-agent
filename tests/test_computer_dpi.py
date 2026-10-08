"""桌面坐标口径：屏幕绝对坐标（逻辑像素）+ physical=true 时的 DPI 换算。"""
import pytest

import tools.computer_use as cu
from tools.computer_use import DesktopTool


class TestDpiHelpers:
    def test_scale_is_sane(self):
        scale = cu._dpi_scale()
        assert isinstance(scale, float) and 1.0 <= scale <= 8.0

    def test_conversion_round_trip(self):
        assert cu._convert_xy(1000, 500, scale=2.0, to_logical=True) == (500, 250)
        assert cu._convert_xy(500, 250, scale=2.0, to_logical=False) == (1000, 500)

    def test_scale_one_is_identity(self):
        assert cu._convert_xy(640, 480, scale=1.0, to_logical=True) == (640, 480)

    def test_bad_scale_falls_back_to_one(self):
        assert cu._convert_xy(640, 480, scale=0, to_logical=True) == (640, 480)

    def test_scale_note_only_when_scaled(self, monkeypatch):
        monkeypatch.setattr(cu, "_dpi_scale", lambda: 1.0)
        assert cu._scale_note() == ""
        monkeypatch.setattr(cu, "_dpi_scale", lambda: 1.5)
        assert "1.5x" in cu._scale_note()
        assert "physical=true" in cu._coord_note()


class TestClickConversion:
    def _tool(self, monkeypatch, scale):
        monkeypatch.setattr(cu, "_dpi_scale", lambda: scale)
        monkeypatch.setattr(cu, "_user_is_active", lambda *a, **kw: False)
        monkeypatch.setattr(cu, "_release_all_inputs", lambda: None)
        monkeypatch.setattr(cu, "get_overlay", lambda: None)
        seen = {}
        monkeypatch.setattr(cu, "_os_click",
                            lambda x, y, button="left", double=False: seen.update(
                                x=x, y=y, button=button) or True)
        return DesktopTool(vision_model=None), seen

    def test_default_is_logical(self, monkeypatch):
        tool, seen = self._tool(monkeypatch, 2.0)
        result = tool.execute_json({"action": "click", "x": 1000, "y": 500})
        assert result.success and (seen["x"], seen["y"]) == (1000, 500)

    def test_physical_flag_converts(self, monkeypatch):
        tool, seen = self._tool(monkeypatch, 2.0)
        result = tool.execute_json({"action": "click", "x": 1000, "y": 500, "physical": True})
        assert result.success and (seen["x"], seen["y"]) == (500, 250)

    def test_scroll_converts_too(self, monkeypatch):
        tool, _ = self._tool(monkeypatch, 2.0)
        seen = {}
        monkeypatch.setattr(cu, "_os_scroll", lambda x, y, n: seen.update(x=x, y=y, n=n))
        tool.execute_json({"action": "scroll", "x": 800, "y": 400, "physical": True})
        assert (seen["x"], seen["y"]) == (400, 200)

    def test_a11y_output_states_coordinate_convention(self, monkeypatch):
        tool, _ = self._tool(monkeypatch, 1.25)
        monkeypatch.setattr(cu, "_a11y_tree", lambda max_elements=0, max_depth=0: "树")
        result = tool.execute_json({"action": "a11y"})
        assert "坐标口径" in result.output and "physical=true" in result.output
