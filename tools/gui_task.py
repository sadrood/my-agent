"""任务级 GUI 工具：给一句话，让 agent 自己操作桌面到完成。

这是 computer（动作级积木）之上的任务级封装，接口仿主流桌面 agent：
    start     开任务（给自然语言目标）
    continue  跑下一轮 / 接着跑（可带 hint 纠偏）
    status    只查状态，不推进
    result    取格式化结果文本

会话隔离：状态存在模块级字典，按 session_key 分开。默认 key 固定；QQ bot 桥接可
通过 set_session_key() 按会话切换，避免 A 群的任务被 B 群 continue 掉。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from agent.gui_agent import GUISession
from tools.base import BaseTool, ToolResult

# 会话表：session_key -> GUISession。全局但按 key 隔离。
_SESSIONS: Dict[str, GUISession] = {}
_DEFAULT_KEY = "default"
_lock = threading.Lock()
# 当前会话 key 用 thread-local：QQ bot 的 handle_text 跑在 asyncio.to_thread 线程里，
# 每个请求线程各自设 key，避免并发请求之间串台（全局单变量会被后来的请求覆盖）。
_tls = threading.local()


def _default_key() -> str:
    return getattr(_tls, "key", _DEFAULT_KEY)


def set_session_key(key: str) -> None:
    """由宿主（如 QQ bot）在每次请求入口（当前线程）设置会话 key。空值回到默认。"""
    _tls.key = str(key or _DEFAULT_KEY)


def get_session_key() -> str:
    return _default_key()


def _key() -> str:
    return _default_key()


class GUITaskTool(BaseTool):
    risk_level: str = "high"              # 会操作真实桌面
    approval: str = "on-request"          # 高危，ask 模式弹审批
    min_sandbox_mode: str = "danger-full-access"

    @property
    def name(self) -> str:
        return "gui_task"

    @property
    def description(self) -> str:
        return (
            "任务级桌面操作：给一句自然语言目标，agent 自己反复看屏→决策→点击，"
            "直到完成。区别于 computer（那是动作级，要你手动报坐标）。\n"
            "流程：start(goal) 开任务 → continue 跑完（返回 continue=true 就再 continue，"
            "直到 done=true）→ result 取结果。需要人介入时 status=call_user。\n"
            "操作期间请别动鼠标键盘；你一动 agent 会自动让路并停下。"
            "复杂任务先用 computer 的 window 激活目标窗口，或用 start 的 window_title 参数。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "continue", "status", "result", "stop", "close"],
                    "description": "start=开任务; continue=推进; status=只查状态; "
                                   "result=格式化结果; stop=置停止信号; close=释放会话",
                },
                "goal": {"type": "string", "description": "start 时的自然语言目标（必填）"},
                "window_title": {"type": "string", "description": "start 时要先激活的窗口标题（可选）"},
                "hint": {"type": "string", "description": "continue 时的纠偏提示（可选）"},
                "max_steps": {"type": "integer", "description": "本次最多跑几步（默认取配置）"},
                "use_screenshot": {"type": "boolean", "description": "是否让 VLM 看截图（默认 true）"},
            },
            "required": ["action"],
        }

    def _get_session(self, create: bool = False) -> Optional[GUISession]:
        with _lock:
            s = _SESSIONS.get(_key())
            if s is None and create:
                s = self._build_session()
                _SESSIONS[_key()] = s
            return s

    @staticmethod
    def _build_session(use_screenshot: bool = True) -> GUISession:
        from tools.computer_use import DesktopTool
        from config import COMPUTER_USE_CONFIG

        max_steps = int(COMPUTER_USE_CONFIG.get("default_steps", 10)) * 3  # 任务级给更宽的上限
        return GUISession(
            computer_tool=DesktopTool(),
            max_steps=max_steps,
            use_screenshot=use_screenshot,
            stop_event=threading.Event(),
        )

    def execute(self, input_str: str) -> ToolResult:
        try:
            import json
            args = json.loads(input_str) if input_str else {}
        except Exception:
            args = {"action": "start", "goal": (input_str or "").strip()}
        if not isinstance(args, dict):
            args = {"action": "start", "goal": str(args)}
        return self.execute_json(args)

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action", "")).strip().lower()

        if action == "start":
            goal = str(arguments.get("goal", "") or "").strip()
            if not goal:
                return ToolResult(success=False, output="", error="start 需要 goal（自然语言目标）。")
            use_shot = bool(arguments.get("use_screenshot", True))
            with _lock:
                old = _SESSIONS.get(_key())
                if old is not None and old.state and not old.state.done:
                    # 有未完成任务：不静默覆盖，提示先关掉
                    return ToolResult(
                        success=False, output="",
                        error="当前会话有未完成的 GUI 任务；先 gui_task action=close 再开新任务。")
                s = self._build_session(use_screenshot=use_shot)
                _SESSIONS[_key()] = s
            window_title = str(arguments.get("window_title", "") or "").strip()
            state = s.start(goal, window_title=window_title)
            return ToolResult(success=True, output=(
                f"GUI 任务已开启。目标：{goal}\n"
                f"现在用 gui_task action=continue 开始执行。"
                + (f"\n已激活窗口：{window_title}" if window_title else "")
            ))

        s = self._get_session(create=False)
        if s is None:
            return ToolResult(success=False, output="", error="没有 GUI 任务；先用 action=start 开一个。")

        if action == "continue":
            hint = str(arguments.get("hint", "") or "").strip()
            max_steps = arguments.get("max_steps")
            try:
                max_steps = int(max_steps) if max_steps is not None else 0
            except (TypeError, ValueError):
                max_steps = 0
            if hint and s.state is not None:
                # 把纠偏提示记进任务目标，让下一步决策看得见
                s.state.task = f"{s.state.task}\n【用户纠偏】{hint}"
            try:
                state = s.run(max_steps=max_steps)
            except Exception as e:
                return ToolResult(success=False, output="", error=f"GUI 循环异常: {str(e)[:200]}")
            text = s.format_result(state)
            return ToolResult(success=True, output=text, metadata={
                "done": state.done, "status": state.status, "step_count": len(state.steps),
            })

        if action == "status":
            if s.state is None:
                return ToolResult(success=True, output="任务已创建但尚未开始（未调用 continue）。")
            st = s.state
            return ToolResult(success=True, output=(
                f"状态: {st.status or 'running'}\n完成: {st.done}\n"
                f"已执行步数: {len(st.steps)}\n摘要: {st.summary or '（无）'}"
            ), metadata={"done": st.done, "status": st.status})

        if action == "result":
            if s.state is None:
                return ToolResult(success=False, output="", error="尚无结果。")
            return ToolResult(success=True, output=s.format_result(s.state))

        if action == "stop":
            if s.stop_event is not None:
                s.stop_event.set()
            return ToolResult(success=True, output="已发出停止信号，将在下一个检查点停下。")

        if action == "close":
            with _lock:
                _SESSIONS.pop(_key(), None)
            return ToolResult(success=True, output="GUI 任务会话已释放。")

        return ToolResult(success=False, output="",
                          error=f"未知 action: {action}。可用: start/continue/status/result/stop/close")
