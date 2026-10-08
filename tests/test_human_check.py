"""人机验证识别：命中就交给人，别让 agent 硬试验证码空转。"""
import pytest

from tools import human_check
from tools.base import ToolResult
from tools.browser import BrowserTool


class TestDetection:
    def test_url_signals(self):
        assert human_check.hint_for_url("https://site.com/verify?next=/home")
        assert human_check.hint_for_url("https://x.com/captcha")
        assert human_check.hint_for_url("https://site.com/cdn-cgi/challenge-platform/x")
        assert human_check.hint_for_url("https://site.com/login") == ""

    def test_dom_signals(self):
        info = human_check.verdict(dom_hits=["iframe[src*='recaptcha']"])
        assert info["needs_human"] and "recaptcha" in info["reasons"][0]

    def test_text_signals(self):
        assert human_check.verdict(text="请完成安全验证后继续")["needs_human"]
        assert human_check.verdict(text="Verify you are human")["needs_human"]

    def test_clean_page(self):
        info = human_check.verdict(url="https://site.com/list", text="商品列表")
        assert info["needs_human"] is False and info["hint"] == ""

    def test_hint_says_who_has_to_do_it(self):
        info = human_check.verdict(url="https://x.com/captcha")
        assert "人工" in info["hint"] and "agent 无法" in info["hint"]

    def test_url_from_status_text(self):
        assert human_check.url_from_status("页面: 登录\nURL: https://a.com/captcha\n") == \
            "https://a.com/captcha"
        assert human_check.url_from_status("没有 URL 字段") == ""


class _Page:
    def __init__(self, url, dom=None, body=""):
        self.url = url
        self._dom = dom or []
        self._body = body

    def query_selector(self, selector):
        return object() if selector in self._dom else None

    def inner_text(self, selector):
        return self._body


def _tool(page):
    tool = BrowserTool.__new__(BrowserTool)
    tool._pages = [page]
    tool._current_page_idx = 0
    return tool


class TestBrowserCommand:
    def test_reports_human_verification(self):
        tool = _tool(_Page("https://x.com/captcha"))
        result = tool._human_check()
        assert result.success and result.metadata["human_check"] is True
        assert "人工" in result.output

    def test_reports_clean_page(self):
        tool = _tool(_Page("https://x.com/list", body="商品列表"))
        result = tool._human_check()
        assert result.success and result.metadata["human_check"] is False

    def test_screenshot_appends_hint_for_challenge_url(self, tmp_path, monkeypatch):
        tool = _tool(_Page("https://x.com/verify"))

        class _Shot:
            url = "https://x.com/verify"

            def wait_for_load_state(self, *a, **kw):
                return None

            def screenshot(self, **kw):
                return b"\x89PNG\r\n\x1a\n"

            def title(self):
                return "验证"

        tool._pages = [_Shot()]
        tool._screenshot_dir = str(tmp_path)
        monkeypatch.setattr(BrowserTool, "_ensure_page",
                            lambda self: type("R", (), {"success": True})())
        result = tool._screenshot(full_base64=False)
        assert result.metadata.get("human_check") is True
        assert "人工" in result.output
