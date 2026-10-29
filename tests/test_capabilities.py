"""本机能力清单：模型必须知道这台机器缺什么（否则会去试 Windows 专属能力）。"""
import platform

from agent.capabilities import describe


def test_reports_os_line():
    text = describe()
    assert platform.system() in text
    assert "系统:" in text


def test_reports_desktop_control_availability():
    text = describe()
    assert "桌面操控" in text
    # 只在 Windows 上可用：Linux/macOS 必须明说不可用，而不是不出现
    if platform.system() != "Windows":
        assert "不可用" in text


def test_reports_browser_backend_when_tool_manager_given():
    class _TM:
        def get_tool(self, name):
            return object()          # 未知实现也要给出名字，不能崩

    text = describe(_TM())
    assert "浏览器后端" in text


def test_works_without_tool_manager():
    text = describe(None)
    assert "浏览器后端" not in text   # 没给工具管理器就不编一个
