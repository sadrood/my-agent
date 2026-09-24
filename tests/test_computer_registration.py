"""`computer` 工具只在有实现的平台上注册。

原先无条件注册：非 Windows 上模型会反复调用一个恒返回「仅支持 Windows」的工具，
agent 还会因它存在而追加整套桌面操控提示。
"""
import platform

import tools.computer_use as computer_use
from tools.tool_manager import ToolManager


class TestDesktopToolRegistration:
    def test_not_registered_when_unsupported(self, monkeypatch):
        monkeypatch.setattr(computer_use, "COMPUTER_SUPPORTED", False)
        assert ToolManager().get_tool("computer") is None

    def test_registered_when_supported(self, monkeypatch):
        monkeypatch.setattr(computer_use, "COMPUTER_SUPPORTED", True)
        assert ToolManager().get_tool("computer") is not None

    def test_registration_matches_this_host(self):
        """跟着真实平台走：Windows 上必须有、别的平台上必须没有。

        这条同时在两个平台上有效 —— 在 Linux 上跑测试时会直接暴露漏改。
        """
        expected = platform.system() == "Windows"
        assert (ToolManager().get_tool("computer") is not None) == expected

    def test_flag_is_public_and_platform_derived(self):
        """标志必须是公开名（注册方要用），且不能写死。"""
        assert hasattr(computer_use, "COMPUTER_SUPPORTED")
        assert computer_use.COMPUTER_SUPPORTED == (platform.system() == "Windows")
