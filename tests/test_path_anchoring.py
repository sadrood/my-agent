# -*- coding: utf-8 -*-
"""存储路径锚定项目根的回归测试。

同一个坑在这个仓库里被踩了三次（browser profile、memory/session 存储、经验库缓存
与各 save_dir），所以把所有相对路径收敛到 config.resolve_under_root 一处，
并在这里钉住行为。

实测过的后果：
- 从 C:\\Users\\Administrator 启动时 `./memory/browser_profile` → 该目录下的空 profile，
  表现为"自动化浏览器每次打开都没有记录"；
- `./memory/experience_lib` → 另一个不存在的目录，**云经验库看起来凭空变空**；
- `./rollouts` → 运行日志找不到；`./generated_*` → 产物落到启动目录。
"""
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
        r"""Windows 的 "驱动器相对" 路径（\foo，无盘符）按 abspath 语义补当前盘符。

        钉住这条语义，免得以后有人以为 isabs(r"\foo") 是 True——
        它在 Windows 上是 False，会被当成相对路径处理。
        """
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

    只 monkeypatch.chdir 是测不到的——配置在 import 时就解析完了，必须换进程。
    """

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
