"""依赖清单卫生：Windows 专用包必须带平台标记，否则 Linux 上 pip 直接失败。"""
import io
import os

from packaging.requirements import Requirement

REQUIREMENTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "requirements.txt")

#: 已知只在 Windows 上有意义的包（Linux 上装不上或装了也用不了）
WINDOWS_ONLY = {"uiautomation", "pywin32", "win32api", "pywinpty"}


def _entries():
    text = io.open(REQUIREMENTS, encoding="utf-8").read()
    return [Requirement(line.strip()) for line in text.split("\n")
            if line.strip() and not line.strip().startswith("#")]


def test_every_requirement_parses():
    entries = _entries()
    assert entries, "requirements.txt 不该是空的"


def test_windows_only_packages_are_marked():
    """没有平台标记，`install_deps.sh` 会在第 3 步（pip install -r）整个中断。"""
    unmarked = [r.name for r in _entries()
                if r.name.lower() in WINDOWS_ONLY
                and (r.marker is None or "win32" not in str(r.marker))]
    assert not unmarked, "这些包必须带 sys_platform == \"win32\" 标记：%s" % unmarked


def test_linux_install_skips_windows_only():
    marked = {r.name.lower(): r for r in _entries()}
    for name in WINDOWS_ONLY & set(marked):
        assert not marked[name].marker.evaluate({"sys_platform": "linux"}), \
            "%s 在 Linux 上不该被安装" % name
