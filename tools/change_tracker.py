"""
文件变更追踪器（diff 视图的数据底座）。

动机：桌面端/仪表盘的"透明可控"体验需要回答"Agent 到底改了什么"，
而不只是"哪个文件被改了"。写文件工具（file write / edit）在成功落盘前
把旧内容快照到这里，dashboard 的 /api/diff 据此生成修改前后对比。

设计要点：
- 同一路径多次修改只保留**最旧的快照**——diff 展示的是
  "本次会话累计改了什么"，而不是最后一次增量（更符合审计直觉）。
- 内存上限保护：路径数、单文件快照大小都有硬上限，超限静默降级
  （该文件不再追踪，不影响写操作本身）。
- 线程安全：工具在 worker 线程执行，API 在事件循环线程读取。
- 快照是内存态，不写磁盘——重启即清空，不引入新的持久化安全面。
"""
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

    def record(self, path: str, tool: str) -> None:
        """在**写操作成功后**调用。内部自行快照旧内容语义如下：
        首次记录时快照应为"修改前内容"——因此调用方要在写入前先调
        snapshot_before()，写入后再调 commit()；本方法是两步的便捷封装
        （适用于调用方能拿到旧内容的场景）。"""
        abs_path = self._norm(path)
        with self._lock:
            rec = self._records.get(abs_path)
            if rec is not None:
                rec.writes += 1
                return
            if len(self._records) >= self._max_paths:
                return  # 超限静默降级
        # 注意：这里假定调用时尚未写入（供 write 工具在打开文件前调用）
        old = self._snapshot(abs_path)
        is_new = not os.path.exists(abs_path)
        with self._lock:
            # 双重检查：等待快照期间可能已有其他线程记录
            if abs_path in self._records:
                self._records[abs_path].writes += 1
                return
            self._records[abs_path] = ChangeRecord(abs_path, old, is_new, tool)

    def record_with_old(self, path: str, tool: str, old_content: Optional[str]) -> None:
        """调用方已持有修改前内容时直接登记（edit 工具场景）。"""
        abs_path = self._norm(path)
        with self._lock:
            rec = self._records.get(abs_path)
            if rec is not None:
                rec.writes += 1
                return
            if len(self._records) >= self._max_paths:
                return
            self._records[abs_path] = ChangeRecord(
                abs_path, old_content, old_content is None, tool)

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