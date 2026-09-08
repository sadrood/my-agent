# -*- mode: python ; coding: utf-8 -*-
"""myagent-backend 打包 spec：桌面端内置后端（单文件、无控制台）。"""
import os
from PyInstaller.utils.hooks import collect_submodules

PKG_DIR = os.path.dirname(os.path.abspath(SPEC)) if "SPEC" in globals() and os.path.basename(os.getcwd()) == "packaging" else os.getcwd()
if not os.path.exists(os.path.join(PKG_DIR, "backend_entry.py")):
    PKG_DIR = os.path.join(os.getcwd(), "packaging")
ROOT = os.path.dirname(PKG_DIR)

hidden = []
for pkg in ("dashboard", "agent", "tools", "models"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

a = Analysis(
    [os.path.join(PKG_DIR, "backend_entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "playwright", "pytest", "IPython", "notebook",
        "PyQt5", "PySide6", "matplotlib", "pandas", "numpy",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="myagent-backend",
    console=False,
    disable_windowed_traceback=False,
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="myagent-backend")
