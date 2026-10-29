"""磁盘保留策略：截图只留最近 N 张，输出目录体积进 --doctor。

背景：截图是给模型看的中间产物，此前每张都留在 screenshots/ 里（实测 139 张 55MB），
而 output/ 这类产物目录不在 git 里，涨到几 GB 也没人提醒。
"""
import os

import pytest

from agent.retention import list_by_mtime, prune_oldest
from tools.browser import BrowserTool


def _make(path, mtime):
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


class TestPruneOldest:
    def test_keeps_newest_and_deletes_rest(self, tmp_path):
        for i in range(10):
            _make(tmp_path / f"s{i:02d}.png", 1_000_000 + i)
        removed = prune_oldest(str(tmp_path), 4, suffix=".png")
        assert removed == 6
        left = sorted(p.name for p in tmp_path.glob("*.png"))
        assert left == ["s06.png", "s07.png", "s08.png", "s09.png"], "应保留最新的 4 张"

    def test_under_limit_is_noop(self, tmp_path):
        for i in range(3):
            _make(tmp_path / f"s{i}.png", 1_000_000 + i)
        assert prune_oldest(str(tmp_path), 10, suffix=".png") == 0
        assert len(list(tmp_path.glob("*.png"))) == 3

    def test_only_matching_suffix_removed(self, tmp_path):
        _make(tmp_path / "a.png", 1)
        _make(tmp_path / "b.txt", 2)
        prune_oldest(str(tmp_path), 0, suffix=".png")
        assert list(tmp_path.glob("*.png")) == []
        assert (tmp_path / "b.txt").exists(), "不该动别的文件"

    def test_missing_dir_is_safe(self, tmp_path):
        assert prune_oldest(str(tmp_path / "nope"), 5) == 0
        assert list_by_mtime(str(tmp_path / "nope")) == []


class TestScreenshotRetention:
    def _tool(self, monkeypatch, tmp_path, keep):
        from config import BROWSER_CONFIG
        monkeypatch.setitem(BROWSER_CONFIG, "screenshot_keep", keep)
        tool = BrowserTool.__new__(BrowserTool)
        tool._screenshot_dir = str(tmp_path)
        return tool

    def test_screenshots_pruned_to_keep(self, monkeypatch, tmp_path):
        for i in range(12):
            _make(tmp_path / f"shot{i:02d}.png", 1_000_000 + i)
        self._tool(monkeypatch, tmp_path, 5)._prune_screenshots()
        left = sorted(p.name for p in tmp_path.glob("*.png"))
        assert len(left) == 5 and left[0] == "shot07.png"

    def test_zero_keep_disables_pruning(self, monkeypatch, tmp_path):
        for i in range(12):
            _make(tmp_path / f"shot{i:02d}.png", 1_000_000 + i)
        self._tool(monkeypatch, tmp_path, 0)._prune_screenshots()
        assert len(list(tmp_path.glob("*.png"))) == 12


class TestDoctorDiskCheck:
    def test_reports_sizes_and_warns_when_large(self, monkeypatch, tmp_path):
        import agent.doctor as doc
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "a.bin").write_bytes(b"x" * (3 * 1024 * 1024))
        (tmp_path / "screenshots").mkdir()
        (tmp_path / "screenshots" / "s.png").write_bytes(b"x" * 1024)
        monkeypatch.setattr("config.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("DOCTOR_DISK_WARN_MB", "1")

        r = doc._check_disk()
        assert "output 3MB" in r["message"] and "screenshots" in r["message"]
        assert r["ok"] is False and "清理" in r["hint"]

    def test_small_dirs_are_ok(self, monkeypatch, tmp_path):
        import agent.doctor as doc
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "a.txt").write_text("hi", encoding="utf-8")
        monkeypatch.setattr("config.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("DOCTOR_DISK_WARN_MB", "100")
        r = doc._check_disk()
        assert r["ok"] is True and not r["hint"]
