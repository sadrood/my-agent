"""
Hooks 机制：工具调用前后回调（fail-open，最小可用实现）。

用法：
    from agent.hooks import get_hook_manager, reset_hook_manager

    mgr = get_hook_manager()                 # 单例，按 HOOKS_CONFIG 配置
    mgr.on_pre_tool_use("edit", {...})       # 工具执行前
    mgr.on_post_tool_use("edit", result)     # 工具执行后

hooks_file 指定的 Python 模块可定义（均为可选）：
    def on_pre_tool_use(tool_name: str, arguments: dict) -> None
    def on_post_tool_use(tool_name: str, result) -> None

任何回调异常只记录 warning，绝不阻断主流程（fail-open）；
文件不存在/加载失败同样静默降级。
"""

import hashlib
import importlib.util
import logging
import os
import sys
import threading
from types import ModuleType
from typing import Optional

from config import HOOKS_CONFIG

logger = logging.getLogger(__name__)


class HookManager:
    """按文件路径加载钩子模块，并在工具调用前后派发回调。"""

    def __init__(self, enabled: Optional[bool] = None,
                 hooks_file: Optional[str] = None):
        self.enabled = HOOKS_CONFIG["enabled"] if enabled is None else bool(enabled)
        self.hooks_file = HOOKS_CONFIG["hooks_file"] if hooks_file is None else hooks_file
        self.pre_callback = None      # Optional[Callable[[str, dict], None]]
        self.post_callback = None     # Optional[Callable[[str, object], None]]
        self._module: Optional[ModuleType] = None
        self._load_error: Optional[str] = None
        if self.enabled:
            self._load()

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def _load(self) -> None:
        """按文件路径加载钩子模块；失败只记录警告，不抛出。"""
        path = self.hooks_file
        if not path or not os.path.exists(path):
            # 文件不存在：静默跳过（不加载、不报错）
            return
        try:
            module_name = "user_hooks_" + hashlib.md5(
                os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法解析钩子模块: {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            self._module = module
            self.pre_callback = getattr(module, "on_pre_tool_use", None)
            self.post_callback = getattr(module, "on_post_tool_use", None)
            if not callable(self.pre_callback):
                self.pre_callback = None
            if not callable(self.post_callback):
                self.post_callback = None
        except Exception as e:  # fail-open：加载失败只警告
            self._load_error = str(e)
            logger.warning("加载 hooks 模块失败（fail-open 跳过）: %s", e)

    # ------------------------------------------------------------------
    # 派发
    # ------------------------------------------------------------------
    def on_pre_tool_use(self, tool_name: str, arguments: dict) -> None:
        """工具执行前回调；任何异常只警告，绝不阻断主流程。"""
        if not self.enabled or self.pre_callback is None:
            return
        try:
            self.pre_callback(tool_name, arguments)
        except Exception as e:
            logger.warning("on_pre_tool_use 回调异常（fail-open 跳过）: %s", e)

    def on_post_tool_use(self, tool_name: str, result) -> None:
        """工具执行后回调；任何异常只警告，绝不阻断主流程。"""
        if not self.enabled or self.post_callback is None:
            return
        try:
            self.post_callback(tool_name, result)
        except Exception as e:
            logger.warning("on_post_tool_use 回调异常（fail-open 跳过）: %s", e)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<HookManager enabled={self.enabled} "
                f"file={self.hooks_file!r} pre={'yes' if self.pre_callback else 'no'} "
                f"post={'yes' if self.post_callback else 'no'}>")


# ----------------------------------------------------------------------
# 模块级单例
# ----------------------------------------------------------------------
_instance: Optional[HookManager] = None
_lock = threading.Lock()


def get_hook_manager() -> HookManager:
    """返回模块级单例 HookManager（按 HOOKS_CONFIG 配置创建）。"""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = HookManager()
    return _instance


def reset_hook_manager(enabled: Optional[bool] = None,
                       hooks_file: Optional[str] = None) -> HookManager:
    """重建单例（测试/热重载用）；参数缺省时回到 HOOKS_CONFIG 配置。"""
    global _instance
    with _lock:
        _instance = HookManager(enabled=enabled, hooks_file=hooks_file)
    return _instance