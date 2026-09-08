"""
持久目标（参照上游同类 `goal` 机制）：按会话持久化的完成目标。

每个会话（conversation id）一个目标，存 memory/goals/<session>.json。
任务失败 / 重启后目标仍在：可 /goal 查看，重发即可续跑（上下文已保留）。
"""
import json
import os
import re

_GOAL_DIR = os.path.join("memory", "goals")


def _safe_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", key or "")[:80] or "default"


class GoalStore:
    """按会话持久化目标。"""

    def _path(self, key: str) -> str:
        return os.path.join(_GOAL_DIR, f"{_safe_name(key)}.json")

    def set(self, key: str, goal: str) -> None:
        if not key or not goal:
            return
        os.makedirs(_GOAL_DIR, exist_ok=True)
        with open(self._path(key), "w", encoding="utf-8") as f:
            json.dump({"goal": goal[:2000]}, f, ensure_ascii=False, indent=2)

    def get(self, key: str) -> str:
        if not key:
            return ""
        try:
            with open(self._path(key), "r", encoding="utf-8") as f:
                return json.load(f).get("goal", "")
        except Exception:
            return ""
