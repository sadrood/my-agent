"""AGENTS.md 分层指令模块（借鉴同类实现的 AGENTS.md 约定）。"""
import os
from typing import List, Optional

from config import INSTRUCTIONS_CONFIG


class InstructionsLoader:
    """分层指令加载器。"""

    def __init__(self, config: dict = None):
        self.config = config or INSTRUCTIONS_CONFIG
        self._cache: dict = {}   # {path: (mtime, content)}

    # ================================================================
    # 加载
    # ================================================================

    def load(self, project_dir: Optional[str] = None) -> str:
        """加载全部指令文本（用户级 + 项目级）。"""
        if not self.config.get("enabled", True):
            return ""

        sections: List[str] = []

        user_text = self._load_user()
        if user_text:
            sections.append(f"## 用户全局指令（~/.my_agent/AGENTS.md）\n{user_text}")

        project_text = self._load_project(project_dir)
        if project_text:
            sections.append(f"## 项目指令（AGENTS.md）\n{project_text}")

        if not sections:
            return ""

        merged = "\n\n".join(sections)
        max_chars = int(self.config.get("max_chars", 20000))
        if len(merged) > max_chars:
            merged = merged[:max_chars] + "\n\n...[指令过长，已截断]..."
        return merged

    def _load_user(self) -> str:
        path = os.path.expanduser(self.config.get("user_file", "~/.my_agent/AGENTS.md"))
        return self._read_cached(path)

    def _load_project(self, project_dir: Optional[str] = None) -> str:
        """从 project_dir 向上查找 AGENTS.md（至多向上 3 层，最近的优先）。"""
        base = os.path.abspath(project_dir or os.getcwd())
        file_name = self.config.get("project_file", "AGENTS.md")

        texts: List[str] = []
        current = base
        for _ in range(4):  # 当前目录 + 向上 3 层
            candidate = os.path.join(current, file_name)
            content = self._read_cached(candidate)
            if content:
                texts.append(f"[{os.path.basename(current) or current}] {content.strip()}")
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

        return "\n\n".join(texts)

    def _read_cached(self, path: str) -> str:
        """带 (mtime_ns, size) 缓存的文件读取。"""
        try:
            st = os.stat(path)
            mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
            key = (mtime_ns, st.st_size)
            cached = self._cache.get(path)
            if cached and cached[0] == key:
                return cached[1]
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            self._cache[path] = (key, content)
            return content
        except FileNotFoundError:
            self._cache.pop(path, None)
            return ""
        except Exception:
            return ""

    def clear_cache(self):
        self._cache.clear()


# 模块级单例（供 Agent / Executor 共用）
_default_loader: Optional[InstructionsLoader] = None


def get_instructions_loader() -> InstructionsLoader:
    """获取全局指令加载器单例。"""
    global _default_loader
    if _default_loader is None:
        _default_loader = InstructionsLoader()
    return _default_loader
