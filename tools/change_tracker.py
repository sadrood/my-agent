"""文件变更追踪器（diff 视图的数据底座）。"""
import os
import threading
import time
from typing import Dict, List, Optional

# 单文件快照上限（与 dashboard 预览上限对齐）：超大文件不追踪
_SNAPSHOT_MAX_BYTES = 200 * 1024
# 最多追踪的路径数（防长跑任务内存膨胀）
_MAX_TRACKED_PATHS = 200


class ChangeRecord:
    """一条文件变更记录。"""

    __slots__ = ("path", "old_content", "is_new", "tool", "timestamp", "writes")

    def __init__(self, path: str, old_content: Optional[str], is_new: bool, tool: str):
        self.path = path                    # 绝对路径（realpath 归一化）
        self.old_content = old_content      # 首次修改前的内容；过大/二进制为 None
        self.is_new = is_new                # 本次会话新建的文件
        self.tool = tool                    # 首次修改它的工具名
        self.timestamp = time.time()
        self.writes = 1                     # 累计写入次数

    def to_dict(self, workspace_root: str = "") -> dict:
        rel = self.path
        if workspace_root:
            try:
                rel = os.path.relpath(self.path, workspace_root).replace("\\", "/")
            except ValueError:
                pass
        return {
            "path": rel,
            "tool": self.tool,
            "is_new": self.is_new,
            "writes": self.writes,
            "timestamp": self.timestamp,
            "has_snapshot": self.old_content is not None,
        }


class ChangeTracker:
    """会话级文件变更追踪（单例使用）。"""

    def __init__(self, max_paths: int = _MAX_TRACKED_PATHS,
                 snapshot_max_bytes: int = _SNAPSHOT_MAX_BYTES):
        self._records: Dict[str, ChangeRecord] = {}
        self._lock = threading.Lock()
        self._max_paths = max_paths
        self._snapshot_max = snapshot_max_bytes

    @staticmethod
    def _norm(path: str) -> str:
        return os.path.realpath(os.path.abspath(path))

    def snapshot(self, path: str) -> Optional[str]:
        """公开版：读取任意路径当前内容（写入前调用，配合 record_with_old）。"""
        return self._snapshot(self._norm(path))

    def _snapshot(self, abs_path: str) -> Optional[str]:
        """读取修改前内容；不存在→None（配合 is_new），过大/二进制→None。"""
        if not os.path.isfile(abs_path):
            return None
        try:
            if os.path.getsize(abs_path) > self._snapshot_max:
                return None
            with open(abs_path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None

    def record_with_old(self, path: str, tool: str, old_content: Optional[str],
                        existed: Optional[bool] = None) -> None:
        """调用方已持有修改前内容时直接登记（edit / file 工具场景）。"""
        abs_path = self._norm(path)
        with self._lock:
            rec = self._records.get(abs_path)
            if rec is not None:
                rec.writes += 1
                return
            if len(self._records) >= self._max_paths:
                return
            self._records[abs_path] = ChangeRecord(
                abs_path, old_content,
                (old_content is None) if existed is None else (not existed), tool)

    def get(self, path: str) -> Optional[ChangeRecord]:
        with self._lock:
            return self._records.get(self._norm(path))

    def list_changes(self, workspace_root: str = "") -> List[dict]:
        with self._lock:
            recs = sorted(self._records.values(), key=lambda r: r.timestamp)
        return [r.to_dict(workspace_root) for r in recs]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


_tracker = ChangeTracker()


def get_change_tracker() -> ChangeTracker:
    """全局单例（worker 线程与 API 线程共享）。"""
    return _tracker