"""
Dashboard Web 服务器。
提供 FastAPI + WebSocket 实时监控 Agent 执行过程。

功能：
- 实时步骤流（WebSocket 推送）
- 截图回放
- 对话历史查看
- 团队协作状态面板
- 工具调用日志
"""
import json
import threading
import time
import asyncio
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field


@dataclass
class DashboardEvent:
    """Dashboard 事件。"""
    type: str                      # step_start / step_end / tool_call / tool_result / plan / error / team_update
    data: dict
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "data": self.data,
            "timestamp": self.timestamp,
        }


class DashboardHub:
    """
    Dashboard 事件中心。
    Agent 通过此 Hub 发布事件，Dashboard 前端通过 WebSocket 订阅。

    用法（在 Agent 中）:
        hub = DashboardHub()
        hub.emit("step_start", {"step": 1, "description": "..."})
        hub.emit("tool_call", {"tool": "browser", "input": "goto ..."})
        hub.emit("tool_result", {"success": True, "output": "..."})
    """

    def __init__(self, max_history: int = 500):
        self._subscribers: Dict[str, asyncio.Queue] = {}
        self._history: List[DashboardEvent] = []
        self._max_history = max_history
        self._lock = threading.Lock()
        self._current_run: Dict[str, Any] = {
            "goal": "",
            "steps": [],
            "status": "idle",
            "start_time": None,
        }
        self._screenshots: List[Dict] = []
        self._tool_calls: List[Dict] = []

    def emit(self, event_type: str, data: dict):
        """发送事件给所有订阅者。"""
        event = DashboardEvent(type=event_type, data=data)

        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            # 更新运行状态
            if event_type == "run_start":
                self._current_run = {
                    "goal": data.get("goal", ""),
                    "steps": [],
                    "status": "running",
                    "start_time": event.timestamp,
                }
                self._screenshots = []
                self._tool_calls = []
            elif event_type == "run_end":
                self._current_run["status"] = data.get("status", "completed")
            elif event_type == "step_start":
                self._current_run["steps"].append(data)
            elif event_type == "screenshot":
                self._screenshots.append(data)
            elif event_type == "tool_call":
                self._tool_calls.append(data)
            elif event_type == "tool_result":
                for tc in reversed(self._tool_calls):
                    if tc.get("tool") == data.get("tool"):
                        tc["result"] = data
                        break

        # 推送给订阅者
        event_dict = event.to_dict()
        for queue in list(self._subscribers.values()):
            try:
                queue.put_nowait(event_dict)
            except asyncio.QueueFull:
                pass

    def subscribe(self, client_id: str) -> asyncio.Queue:
        """订阅事件流。"""
        queue = asyncio.Queue(maxsize=200)
        self._subscribers[client_id] = queue

        # 发送历史事件
        for event in self._history[-50:]:
            try:
                queue.put_nowait(event.to_dict())
            except asyncio.QueueFull:
                break

        return queue

    def unsubscribe(self, client_id: str):
        """取消订阅。"""
        self._subscribers.pop(client_id, None)

    def get_current_state(self) -> dict:
        """获取当前运行状态。"""
        with self._lock:
            return {
                "run": self._current_run,
                "screenshots": self._screenshots[-20:],
                "tool_calls": self._tool_calls[-50:],
                "subscriber_count": len(self._subscribers),
                "total_events": len(self._history),
            }


# 全局单例
_dashboard_hub: Optional[DashboardHub] = None


def get_dashboard_hub() -> DashboardHub:
    """获取全局 DashboardHub 实例。"""
    global _dashboard_hub
    if _dashboard_hub is None:
        _dashboard_hub = DashboardHub()
    return _dashboard_hub
