import os
from types import SimpleNamespace

import pytest

from tools.base import ToolResult
from tools.browser import BrowserTool
from tools.research import DeepResearcher


def test_qq_agent_injects_restricted_browser_for_every_backend(tmp_path, monkeypatch):
    import agent.agent as agent_module
    import tools.tool_manager as manager_module
    from agent.qqbot import UserSession

    captured = {}

    class Manager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    def agent_factory(config, tool_manager):
        captured["manager"] = tool_manager
        return SimpleNamespace(approval=SimpleNamespace(apply_preset=lambda preset: None))

    monkeypatch.setattr(manager_module, "ToolManager", Manager)
    monkeypatch.setattr(agent_module, "Agent", agent_factory)
    monkeypatch.setattr("tools.browser._resolve_under_root", lambda p: str(tmp_path / os.path.basename(p)))
    session = UserSession("c:user", browser_allowed_origins=("http://localhost:8000",))
    session.ensure_agent()
    browser = captured["browser_tool"]
    assert type(browser) is BrowserTool
    assert browser._network_restricted
    assert browser._network_origins == ("http://localhost:8000",)
    assert browser._profile_dir.endswith(session.session_name())


def test_proxy_options_do_not_allow_implicit_loopback_bypass(tmp_path):
    tool = BrowserTool(screenshot_dir=str(tmp_path), download_dir=str(tmp_path),
                       persistent=False, network_restricted=True)
    try:
        options = tool._network_launch_options()
        assert options["proxy"]["bypass"] == "<-loopback>"
        assert "--disable-quic" in options["args"]
        assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in options["args"]
    finally:
        tool._network_proxy.close()
    assert BrowserTool.__new__(BrowserTool)._network_launch_options() == {}


@pytest.mark.parametrize("target", ["file:///etc/passwd", "data:text/html,secret",
                                     "http://user:secret@example.com"])
def test_qq_navigation_rejects_non_web_and_embedded_credentials(target):
    tool = BrowserTool.__new__(BrowserTool)
    tool._network_restricted = True
    with pytest.raises(ValueError):
        tool._check_navigation(target)
    tool._check_navigation("about:blank")
    tool._check_navigation("https://example.com")
    tool._network_restricted = False
    tool._check_navigation(target)


def test_research_does_not_reuse_old_page_after_rejected_navigation():
    calls = []

    class Manager:
        def execute(self, name, command):
            calls.append(command)
            if command.startswith("goto"):
                return ToolResult(success=False, output="", error="network denied")
            return ToolResult(success=True, output="old page must not become a new source")

    assert DeepResearcher(Manager())._extract_page_content("http://127.0.0.1") == ""
    assert len(calls) == 1
