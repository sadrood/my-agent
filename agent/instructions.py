"""
AGENTS.md 分层指令模块（借鉴同类实现的 AGENTS.md 约定）。

约定：
- 项目根目录的 AGENTS.md 是项目规则，自动注入系统提示
- 用户级 ~/.my_agent/AGENTS.md 是全局规则
- 目录分层：从当前目录向上查找，最近的优先（本项目实现为向上合并）

本项目实现（简化但同源）：
- 用户级指令文件：~/.my_agent/AGENTS.md（全局偏好）
- 项目级指令文件：工作目录及其父目录中的 AGENTS.md（向上查找至多 3 层）
- 内容截断到 INSTRUCTIONS_CONFIG["max_chars"]，避免撑爆上下文
- 按 mtime 缓存，文件变化后自动重新加载
"""
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
        """
        加载全部指令文本（用户级 + 项目级）。

        Args:
            project_dir: 项目目录（默认当前工作目录）

        Returns:
            合并后的指令文本；无任何 AGENTS.md 时返回空字符串。
        """
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
        """带 (mtime_ns, size) 缓存的文件读取。

        缓存失效判定同时看纳秒级 mtime 与文件大小：
        - 只用 float mtime 在 Windows 上不可靠（os.utime 可能 no-op、
          高负载下时间戳粒度变粗），同时间槽内改写内容不会触发失效；
        - size 变化能兜底绝大多数内容修改（测试/编辑几乎必然改变长度）。
        """
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
