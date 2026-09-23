"""浏览器工具的探活与点击兜底回归（用假对象，不启动真浏览器）。

实测故障（2026-09-22 审计）：
1. `Browser.is_connected` 在 playwright 里是**方法**不是 property，而代码写的是
   `bool(self._browser.is_connected)` —— 等于 `bool(<bound method>)`，**恒为 True**。
   于是"浏览器已死就重建"的分支永不触发，坏状态再不自愈。
2. `context.pages` 是 property，context 关闭后返回 `[]` 而不抛异常，`_is_context_alive`
   同样恒为 True。（以上两条都已在真实 playwright 上复现。）
3. `_click` 里 `element = get_by_text(...).first; if element:` —— Locator 没有
   `__bool__`/`__len__`，`bool(locator)` 恒为 True，于是未匹配时白等 10 秒、
   直接抛到外层 except，**role 兜底永远执行不到**（图标按钮就该走那条）。
"""
import pytest

from tools.browser import BrowserTool


# ----------------------------------------------------------------------
# 假对象：只实现探活用到的那几个接口
# ----------------------------------------------------------------------
class _FakeBrowser:
    """is_connected 做成**方法**，与真实 playwright 一致。"""

    def __init__(self, connected: bool):
        self._connected = connected
        self.contexts = []

    def is_connected(self):
        return self._connected


class _FakePage:
    def __init__(self, closed: bool = False):
        self._closed = closed

    def is_closed(self):
        return self._closed


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages


# ----------------------------------------------------------------------
# 1/2. 探活
# ----------------------------------------------------------------------
class TestLivenessProbes:
    def test_dead_browser_is_detected(self):
        """is_connected() 返回 False 时必须判死（旧实现恒判活）。"""
        tool = BrowserTool()
        tool._browser = _FakeBrowser(connected=False)
        assert tool._is_browser_alive() is False

    def test_live_browser_is_detected(self):
        tool = BrowserTool()
        tool._browser = _FakeBrowser(connected=True)
        assert tool._is_browser_alive() is True

    def test_closed_context_with_no_pages_is_dead(self):
        """context 关闭后 pages 返回 []，不能据此判"还活着"。"""
        tool = BrowserTool()
        tool._context = _FakeContext(pages=[])
        tool._pages = [_FakePage(closed=True)]
        tool._current_page_idx = 0
        assert tool._is_context_alive() is False

    def test_context_with_open_page_is_alive(self):
        tool = BrowserTool()
        tool._context = _FakeContext(pages=[object()])
        assert tool._is_context_alive() is True

    def test_context_with_no_page_but_open_tracked_page_is_alive(self):
        """持久模式下刚建 context、还没开新页：跟踪的 page 还开着就算活着。"""
        tool = BrowserTool()
        tool._context = _FakeContext(pages=[])
        tool._pages = [_FakePage(closed=False)]
        tool._current_page_idx = 0
        assert tool._is_context_alive() is True

    def test_probe_exception_means_dead(self):
        class _Boom:
            @property
            def pages(self):
                raise RuntimeError("Target closed")

        tool = BrowserTool()
        tool._context = _Boom()
        assert tool._is_context_alive() is False


# ----------------------------------------------------------------------
# 3. _click 的 role 兜底
# ----------------------------------------------------------------------
class _Locator:
    """get_by_text / get_by_role 的返回值：有 .first，click 命中与否可控。"""

    def __init__(self, page, kind, role=None):
        self._page, self._kind, self._role = page, kind, role

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self._page.attempts.append((self._kind, self._role, timeout))
        if self._page.wins == self._kind and (self._kind != "role"
                                              or self._role == self._page.win_role):
            return None
        raise RuntimeError(f"{self._kind} 没匹配到")


class _ClickPage:
    """只够 _click 用：CSS 一定失败，文本/role 按 `wins` 决定成败。"""

    def __init__(self, wins: str, win_role: str = "button"):
        self.wins, self.win_role = wins, win_role
        self.attempts = []

    def click(self, selector, timeout=None):
        self.attempts.append(("css", None, timeout))
        raise RuntimeError("css 没匹配到")

    def get_by_text(self, selector, exact=False):
        return _Locator(self, "text")

    def get_by_role(self, role, name=None):
        return _Locator(self, "role", role=role)


def _tool_with_page(page) -> BrowserTool:
    tool = BrowserTool()
    # _page 是只读 property（由 _pages + _current_page_idx 支撑），走真实结构塞进去
    tool._pages = [page]
    tool._current_page_idx = 0
    # 跳过 _ensure_page 的真实检查
    tool._ensure_page = lambda: type("R", (), {"success": True, "error": ""})()
    return tool


class TestClickFallbacks:
    def test_icon_button_is_found_via_role(self):
        """无文字的图标按钮：css/文本都失败，必须还能走 role 兜底。"""
        page = _ClickPage(wins="role", win_role="button")
        r = _tool_with_page(page)._click("Close")
        assert r.success is True, f"role 兜底没生效：{r.error}"
        assert any(k == "role" for k, _, _ in page.attempts), "role 分支没被执行"

    def test_text_match_shorter_timeout_before_role(self):
        """文本匹配的等待时间要收敛，别把 10 秒全耗在不可能命中的 locator 上。"""
        page = _ClickPage(wins="role", win_role="button")
        _tool_with_page(page)._click("Close")
        text_attempts = [t for k, _, t in page.attempts if k == "text"]
        assert text_attempts and text_attempts[0] <= 3000, \
            f"文本匹配还在等 {text_attempts} ms，role 兜底会被拖到超时"

    def test_text_match_still_wins_when_present(self):
        page = _ClickPage(wins="text")
        r = _tool_with_page(page)._click("提交")
        assert r.success is True and "文本" in r.output

    def test_all_failures_report_actionable_error(self):
        page = _ClickPage(wins="none")
        r = _tool_with_page(page)._click("不存在的东西")
        assert r.success is False
        assert "visionclick" in r.error or "screenshot" in r.error


class _FakePlaywright:
    """只记 stop() 有没有被调到（真实 Playwright 没有 __del__，stop 是唯一杀驱动的入口）。"""

    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class TestPlaywrightDriverNotLeaked:
    """作废引用时必须真的 stop()，否则每次自愈漂一个 node.exe。

    实测故障（2026-09-22 审计）：只有 `_close_impl` 调 `stop()`，而 `_invalidate()` /
    `reset()` / `_dispatch` 的守卫都只把 `self._playwright` 置 None。playwright sync 的
    Playwright/Connection 没有 `__del__` —— 引用一丢，驱动进程就再也没人收，
    而 `_force_cleanup_residual` 只按 `--user-data-dir=` 匹配 chrome.exe。
    """

    def _tool(self):
        tool = BrowserTool()
        pw = _FakePlaywright()
        tool._playwright = pw
        # reset() 还会去 taskkill 真进程，测试里屏蔽掉
        tool._force_cleanup_residual = lambda: 0
        return tool, pw

    def test_invalidate_stops_driver(self):
        tool, pw = self._tool()
        tool._invalidate()
        assert pw.stopped == 1, "_invalidate 没停驱动进程"
        assert tool._playwright is None

    def test_reset_stops_driver(self):
        tool, pw = self._tool()
        tool.reset()
        assert pw.stopped == 1, "reset 没停驱动进程"

    def test_stop_is_idempotent(self):
        """已经置空之后再调不能炸。"""
        tool, pw = self._tool()
        tool._stop_playwright()
        tool._stop_playwright()
        assert pw.stopped == 1

    def test_stop_failure_is_swallowed(self):
        """跨线程 stop 可能抛 —— 只能吞掉，不能把作废流程带崩。"""
        class _Boom:
            def stop(self):
                raise RuntimeError("绑定在别的线程上")

        tool = BrowserTool()
        tool._playwright = _Boom()
        tool._stop_playwright()          # 不该抛
        assert tool._playwright is None
