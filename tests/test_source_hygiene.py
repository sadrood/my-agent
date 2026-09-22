# -*- coding: utf-8 -*-
"""全仓库语法自检：任何 .py 文件编译不过就直接失败。

为什么单独一条测试：语法错误本来会在 import 时暴露，但只有**被测试导入到**的模块
才会暴露。实测踩过两次——`tools/memory_tool.py` 与 `agent/small_model.py` 里
中文提示词里混了半角双引号，写文件时看不出来，直到运行时（甚至上线后）才炸。

这条测试把仓库里所有 .py 都编译一遍（毫秒级），把"写错了但没人 import"这一类
问题挡在提交前。也顺手挡掉不可见字符/编码损坏。
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "output", "memory",
             "rollouts", "generated_images", "generated_videos", "screenshots"}


def _py_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(base, name)


def test_all_python_files_compile():
    files = list(_py_files())
    assert len(files) > 30, f"只找到 {len(files)} 个 .py，扫描逻辑可能有问题"
    broken = []
    for path in files:
        try:
            with open(path, "rb") as f:
                source = f.read()
            # 用 compile() 而不是 py_compile：后者要写 .pyc（Windows 上 os.devnull
            # 是 "nul" 设备，py_compile 会拒绝）；这里只要语法检查，不落任何文件。
            compile(source, path, "exec")
        except SyntaxError as e:
            broken.append(f"{os.path.relpath(path, ROOT)}:{e.lineno}: {e.msg}")
        except ValueError as e:                 # 含空字节 / 编码声明损坏
            broken.append(f"{os.path.relpath(path, ROOT)}: {str(e)[:120]}")
    assert not broken, "以下文件编译失败：\n" + "\n".join(broken)


def test_no_invisible_control_chars_in_source():
    """源码里混进不可见控制字符（复制粘贴事故）会让逐字比较的工具链失灵。"""
    bad = []
    for path in _py_files():
        if "/tools/local/" in path.replace("\\", "/"):
            continue                            # 本地工具不参与仓库质量门
        with open(path, "rb") as f:
            raw = f.read()
        for ch in (b"\x00", b"\x1b", b"\x0c"):
            if ch in raw:
                bad.append(f"{os.path.relpath(path, ROOT)}: 含 {ch!r}")
    assert not bad, "以下文件含不可见控制字符：\n" + "\n".join(bad)
