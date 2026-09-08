"""
本地全文搜索（借鉴同类实现：检索会话历史 + 工作区文件）。

- 会话：遍历 SessionStore 的历史消息，子串匹配（对中文友好，无需分词器）。
- 文件：遍历工作目录，按文件名 + 内容（读前 N KB）匹配。
- 用法：search_sessions(q) / search_files(root, q)，返回带摘要的命中列表。
"""
import os
from typing import List

_SEARCH_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "release",
    ".pytest_cache", ".idea", ".mypy_cache", "skills", "desktop",
}
_FILE_READ_LIMIT = 40000   # 内容搜索读取前 40KB


def _snippet(text: str, q: str, radius: int = 60) -> str:
    """取命中关键词附近的上下文摘要。"""
    idx = text.lower().find(q.lower())
    if idx < 0:
        return text[:radius * 2]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(q) + radius)
    seg = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + seg + ("…" if end < len(text) else "")


def search_sessions(q: str, limit: int = 30) -> List[dict]:
    """在全部会话历史里子串匹配，返回 [{session_id, title, role, snippet}]。"""
    q = (q or "").strip().lower()
    if not q:
        return []
    from agent.session import SessionStore
    store = SessionStore()
    results = []
    try:
        for conv in store.list_conversations():
            data = store.load_conversation(conv["id"])
            if not data:
                continue
            title = data.get("title", "") or ""
            for m in data.get("messages", []):
                content = str(m.get("content", "") or "")
                if q in content.lower():
                    results.append({
                        "session_id": conv["id"],
                        "title": title[:60],
                        "role": str(m.get("role", "user")),
                        "snippet": _snippet(content, q),
                    })
                    if len(results) >= limit:
                        return results
    except Exception:
        pass
    return results


def search_files(root: str, q: str, limit: int = 30) -> List[dict]:
    """在工作区文件里按文件名/内容匹配，返回 [{path, match: filename|content, snippet?}]。"""
    q = (q or "").strip().lower()
    if not q or not root or not os.path.isdir(root):
        return []
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if q in fn.lower():
                results.append({"path": rel, "match": "filename"})
            else:
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        head = f.read(_FILE_READ_LIMIT)
                    if q in head.lower():
                        results.append({
                            "path": rel, "match": "content",
                            "snippet": _snippet(head, q),
                        })
                except Exception:
                    pass
            if len(results) >= limit:
                return results
    return results
