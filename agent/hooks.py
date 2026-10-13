"""Hooks 机制：Python 回调 + 事件化外部命令 hook（均为 fail-open）。
模块文件/hooks.json 缺失或加载失败时静默降级，绝不阻断主流程。"""

import hashlib
import importlib.util
import logging
import os
import sys
import threading
from types import ModuleType
from typing import Any, Dict, Optional

from agent.hook_events import (
    EVENT_POST_TOOL, EVENT_PRE_TOOL, EVENT_SESSION_START, EVENT_STOP,
    EVENT_USER_PROMPT, EventHookRunner, HookDecision, normalize_event,
)
from config import HOOKS_CONFIG

logger = logging.getLogger(__name__)


class HookManager:
    """按文件路径加载钩子模块，并在工具调用前后派发回调。"""

    def __init__(self, enabled: Optional[bool] = None,
                 hooks_file: Optional[str] = None,
                 events_file: Optional[str] = None,
                 event_timeout: Optional[float] = None,
                 project_dir: Optional[str] = None):
        self.enabled = HOOKS_CONFIG["enabled"] if enabled is None else bool(enabled)
        self.hooks_file = HOOKS_CONFIG["hooks_file"] if hooks_file is None else hooks_file
        self.events_file = HOOKS_CONFIG.get("events_file") if events_file is None else events_file
        timeout = HOOKS_CONFIG.get("event_timeout", 10.0) if event_timeout is None else event_timeout
        self.project_dir = project_dir or os.getcwd()
        self.pre_callback = None      # Optional[Callable[[str, dict], None]]
        self.post_callback = None     # Optional[Callable[[str, object], None]]
        self._module: Optional[ModuleType] = None
        self._load_error: Optional[str] = None
        # 事件化外部命令 hook（hooks.json）：同样受 enabled 开关约束
        self.events = EventHookRunner(events_file=self.events_file, timeout=timeout,
                                      enabled=self.enabled, cwd=self.project_dir)
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
    def on_pre_tool_use(self, tool_name: str, arguments: dict,
                        goal: str = "") -> HookDecision:
        """工具执行前：Python 回调 + PreToolUse 事件；返回决策供调用方判断是否放弃执行。"""
        decision = HookDecision()
        if not self.enabled:
            return decision
        if self.pre_callback is not None:
            decision = decision.merge(self._call_callback(
                self.pre_callback, tool_name, arguments, event="on_pre_tool_use"))
        decision = decision.merge(self.events.run(EVENT_PRE_TOOL, {
            "tool_name": tool_name, "tool_input": arguments, "goal": goal,
        }))
        return decision

    def on_post_tool_use(self, tool_name: str, result,
                         goal: str = "") -> HookDecision:
        """工具执行后：PostToolUse 事件 + Python 回调；messages 为提示型建议。"""
        decision = HookDecision()
        if not self.enabled:
            return decision
        decision = decision.merge(self.events.run(EVENT_POST_TOOL, {
            "tool_name": tool_name, "tool_response": result_payload(result), "goal": goal,
        }))
        if self.post_callback is not None:
            decision = decision.merge(self._call_callback(
                self.post_callback, tool_name, result, event="on_post_tool_use"))
        return decision

    def on_stop(self, state: Optional[Dict[str, Any]] = None) -> HookDecision:
        """模型准备收工时派发 Stop 事件；blocked 表示应拦下继续做。"""
        if not self.enabled:
            return HookDecision()
        return self.events.run(EVENT_STOP, dict(state or {}))

    def on_user_prompt(self, prompt: str) -> HookDecision:
        """用户提交输入时派发 UserPromptSubmit 事件。"""
        if not self.enabled:
            return HookDecision()
        return self.events.run(EVENT_USER_PROMPT, {"user_prompt": str(prompt or "")})

    def on_session_start(self, context: Optional[Dict[str, Any]] = None) -> HookDecision:
        """会话开始时派发 SessionStart 事件。"""
        if not self.enabled:
            return HookDecision()
        return self.events.run(EVENT_SESSION_START, dict(context or {}))

    def dispatch(self, event: str, payload: Optional[Dict[str, Any]] = None) -> HookDecision:
        """任意事件派发入口（自定义事件名按原样透传）。"""
        if not self.enabled:
            return HookDecision()
        return self.events.run(normalize_event(event), payload)

    @staticmethod
    def _call_callback(callback, *args, event: str = "") -> HookDecision:
        """执行 Python 回调；回调可返回 None、决策 dict 或 HookDecision。"""
        try:
            value = callback(*args)
        except Exception as e:
            logger.warning("%s 回调异常（fail-open 跳过）: %s", event or "hook", e)
            return HookDecision()
        if isinstance(value, HookDecision):
            return value
        if isinstance(value, dict):
            return EventHookRunner.decision_from_json(value)
        return HookDecision()

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<HookManager enabled={self.enabled} "
                f"file={self.hooks_file!r} events={self.events_file!r} "
                f"pre={'yes' if self.pre_callback else 'no'} "
                f"post={'yes' if self.post_callback else 'no'}>")


def result_payload(result, limit: int = 4000) -> Dict[str, Any]:
    """把工具结果摘要成可 JSON 序列化的 dict，供外部 hook 读取。"""
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    out = str(getattr(result, "output", "") or "")
    return {
        "success": bool(getattr(result, "success", False)),
        "output": out[:limit],
        "output_truncated": len(out) > limit,
        "error": str(getattr(result, "error", "") or "")[:2000],
        "truncated": bool(getattr(result, "truncated", False)),
    }


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
                       hooks_file: Optional[str] = None,
                       events_file: Optional[str] = None,
                       event_timeout: Optional[float] = None,
                       project_dir: Optional[str] = None) -> HookManager:
    """重建单例（测试/热重载用）；参数缺省时回到 HOOKS_CONFIG 配置。"""
    global _instance
    with _lock:
        _instance = HookManager(enabled=enabled, hooks_file=hooks_file,
                                events_file=events_file, event_timeout=event_timeout,
                                project_dir=project_dir)
    return _instance
