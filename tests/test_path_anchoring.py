# -*- coding: utf-8 -*-
"""存储路径锚定项目根的回归测试。"""
import os
import subprocess
import sys

import pytest

import config
from config import PROJECT_ROOT, resolve_under_root

REPO = PROJECT_ROOT

#: 需要在"cwd 漂移"下仍然锚定项目根的配置项
ANCHORED_PATHS = [
    ("BROWSER_CONFIG", "profile_dir"),
    ("EXPERIENCE_CONFIG", "cache_dir"),
    ("ROLLOUT_CONFIG", "dir"),
    ("SESSION_CONFIG", "dir"),
    ("IMAGE_GEN_CONFIG", "save_dir"),
    ("VIDEO_GEN_CONFIG", "save_dir"),
    ("TTS_CONFIG", "save_dir"),
    ("VIDEO_EDIT_CONFIG", "save_dir"),
    ("ZHIHU_CONFIG", "save_dir"),
]


class TestResolveUnderRoot:
    def test_relative_is_anchored_to_project_root(self):
        assert resolve_under_root("./memory/x") == os.path.join(REPO, "memory", "x")
        assert resolve_under_root("output/zhihu") == os.path.join(REPO, "output", "zhihu")

    def test_absolute_is_left_alone(self, tmp_path):
        """显式配置的绝对路径不该被改写（容器 / 自定义部署依赖这点）。"""
        target = str(tmp_path / "custom")
        assert resolve_under_root(target) == target

    def test_drive_relative_path_gets_current_drive(self):
        """Windows 的 "驱动器相对" 路径（\foo，无盘符）按 abspath 语义补当前盘符。"""
        out = resolve_under_root(os.path.join(os.sep, "custom", "place"))
        assert os.path.isabs(out), "结果必须是可用的绝对路径"
        assert out.endswith(os.path.join("custom", "place"))

    def test_empty_means_project_root(self):
        assert resolve_under_root("") == REPO
        assert resolve_under_root(None) == REPO
        assert resolve_under_root("   ") == REPO

    def test_expanduser_and_strip(self):
        assert resolve_under_root("  ~/x  ").startswith(os.path.expanduser("~"))


class TestResolveIsCwdIndependent:
    def test_result_does_not_follow_cwd(self, tmp_path, monkeypatch):
        """锚定后结果必须与 cwd 无关——这就是这个函数的全部意义。"""
        monkeypatch.chdir(tmp_path)
        assert resolve_under_root("./memory/a") == os.path.join(REPO, "memory", "a")
        assert str(tmp_path) not in resolve_under_root("./memory/a")


class TestConfigPathsAreAnchored:
    @pytest.mark.real_paths          # 断言看的是真实根目录，跳过 conftest 的存储隔离
    @pytest.mark.parametrize("cfg_name,key", ANCHORED_PATHS)
    def test_path_is_absolute_under_project_root(self, cfg_name, key):
        cfg = getattr(config, cfg_name)
        value = str(cfg.get(key) or "")
        assert os.path.isabs(value), "%s[%s] 仍是相对路径: %r" % (cfg_name, key, value)
        assert value.startswith(REPO), "%s[%s] 没锚定项目根: %r" % (cfg_name, key, value)

    def test_computer_screenshot_dir_keeps_empty_semantics(self):
        """空值有特殊含义（回退到默认子目录），必须保持空——不能被锚成项目根。"""
        assert config.TOOL_CONFIG.get("computer_screenshot_dir") == ""

    def test_browser_local_helper_delegates_to_config(self):
        """browser.py 曾有一份自己的实现，现在必须是同一个函数（一处真相）。"""
        from tools import browser as browser_mod
        assert browser_mod._resolve_under_root("./x") == resolve_under_root("./x")
        assert browser_mod.PROJECT_ROOT == REPO


class TestDriftedLaunchInSubprocess:
    """真正模拟"从别的目录启动"：新进程 + 不同 cwd 下读配置。
    只 monkeypatch.chdir 是测不到的——配置在 import 时就解析完了，必须换进程。"""

    CODE = (
        "import os, sys\n"
        "sys.path.insert(0, r'%s')\n" % REPO +
        "import config\n"
        "print(config.PROJECT_ROOT)\n"
        "for name, key in %r:\n" % (ANCHORED_PATHS,) +
        "    print(str(getattr(config, name).get(key)))\n"
    )

    def test_launched_from_elsewhere_still_anchors(self, tmp_path):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTEST_CURRENT_TEST", None)
        proc = subprocess.run([sys.executable, "-c", self.CODE], cwd=str(tmp_path),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=90)
        assert proc.returncode == 0, proc.stderr[-500:]
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        assert lines[0] == REPO, "PROJECT_ROOT 应始终是仓库根"
        values = lines[1:]
        assert len(values) == len(ANCHORED_PATHS)
        for (cfg_name, key), value in zip(ANCHORED_PATHS, values):
            assert value.startswith(REPO), \
                "cwd 漂移后 %s[%s] 跑到了 %r" % (cfg_name, key, value)
            assert str(tmp_path) not in value, \
                "cwd 漂移后 %s[%s] 跟着走了: %r" % (cfg_name, key, value)


class TestSchedulerAndGoalAnchoring:
    """定时任务与持久目标的存储路径必须锚定项目根。"""

    def test_scheduler_file_is_anchored(self):
        import os

        import agent.scheduler as s
        from config import PROJECT_ROOT
        assert os.path.isabs(s._SCHED_FILE), "仍是相对路径，会随 cwd 漂移"
        assert s._SCHED_FILE.startswith(PROJECT_ROOT)

    def test_goal_dir_is_anchored(self):
        import os

        import agent.goal as g
        from config import PROJECT_ROOT
        assert os.path.isabs(g._GOAL_DIR), "仍是相对路径，会随 cwd 漂移"
        assert g._GOAL_DIR.startswith(PROJECT_ROOT)

    def test_paths_stable_across_cwd(self):
        """换个 cwd 重新导入，解析结果必须一致。"""
        import os
        import subprocess
        import sys

        from config import PROJECT_ROOT
        code = ("import agent.scheduler as s, agent.goal as g;"
                "print(s._SCHED_FILE); print(g._GOAL_DIR)")
        outs = set()
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
        for cwd in (str(PROJECT_ROOT), os.path.dirname(str(PROJECT_ROOT))):
            p = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180)
            assert p.returncode == 0, p.stderr[-400:]
            outs.add(p.stdout.strip())
        assert len(outs) == 1, f"cwd 一变路径就变了：{outs}"
