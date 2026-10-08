"""browser 的 download 命令：点击触发下载、落盘、同名不覆盖、失败可读。"""
import os

import pytest

from config import BROWSER_CONFIG
from tools.base import ToolResult
from tools.browser import BrowserTool


class _Download:
    def __init__(self, name="报表.csv"):
        self.suggested_filename = name
        self.saved_to = None

    def save_as(self, path):
        with open(path, "wb") as fh:
            fh.write(b"a,b\n1,2\n")
        self.saved_to = path


class _Waiter:
    def __init__(self, download):
        self.value = download

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Page:
    def __init__(self, download=None, boom=None):
        self._download = download
        self._boom = boom
        self.timeout_ms = None

    def expect_download(self, timeout=None):
        self.timeout_ms = timeout
        if self._boom:
            raise self._boom
        return _Waiter(self._download)


def _tool(tmp_path, page, monkeypatch, click_ok=True):
    tool = BrowserTool.__new__(BrowserTool)
    tool._pages = [page]            # _page 是只读属性，由 _pages/_current_page_idx 派生
    tool._current_page_idx = 0
    tool._download_dir = str(tmp_path)
    monkeypatch.setattr(
        BrowserTool, "_click",
        lambda self, selector: (ToolResult(success=True, output=f"已点击 {selector}")
                                if click_ok else
                                ToolResult(success=False, output="", error="找不到元素")))
    return tool


class TestDownload:
    def test_saves_file_and_reports_path(self, tmp_path, monkeypatch):
        download = _Download()
        page = _Page(download)
        tool = _tool(tmp_path, page, monkeypatch)
        result = tool._download("#export")
        assert result.success, result.error
        assert "保存路径" in result.output and "报表.csv" in result.output
        assert result.metadata["path"] == str(tmp_path / "报表.csv")
        assert os.path.exists(result.metadata["path"])
        assert page.timeout_ms == int(float(BROWSER_CONFIG.get("download_timeout", 30)) * 1000)

    def test_same_name_does_not_overwrite(self, tmp_path, monkeypatch):
        (tmp_path / "报表.csv").write_bytes(b"old")
        tool = _tool(tmp_path, _Page(_Download()), monkeypatch)
        result = tool._download("#export")
        assert result.success
        assert result.metadata["path"].endswith("报表-1.csv")
        assert (tmp_path / "报表.csv").read_bytes() == b"old"

    def test_missing_selector_is_rejected(self, tmp_path, monkeypatch):
        tool = _tool(tmp_path, _Page(_Download()), monkeypatch)
        result = tool._download("   ")
        assert not result.success and "用法" in result.error

    def test_click_failure_is_propagated(self, tmp_path, monkeypatch):
        tool = _tool(tmp_path, _Page(_Download()), monkeypatch, click_ok=False)
        result = tool._download("#nope")
        assert not result.success and "找不到元素" in result.error

    def test_no_download_becomes_readable_error(self, tmp_path, monkeypatch):
        tool = _tool(tmp_path, _Page(boom=RuntimeError("Timeout 30000ms exceeded")),
                     monkeypatch)
        result = tool._download("#export")
        assert not result.success
        assert "下载失败" in result.error and "不触发下载" in result.error
