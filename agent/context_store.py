"""零驻留上下文：大负载不留驻消息里，只留**确定性指针**，需要时可按句柄逐字取回。

与 LLM 摘要的区别：指针不丢信息、不花钱、不依赖上游（摘要是有损的，还得再调一次模型）。
落盘在 `memory/ctx/<命名空间>/<序号>.txt`，句柄形如 `run-1759-0007`。
"""
import os
import re
import time
from typing import Optional

POINTER_RE = re.compile(r"\[已折叠\s+#([A-Za-z0-9_\-]+)\s+([\d.]+)KB\]")


def store_root() -> str:
    from config import CONTEXT_STORE_CONFIG, PROJECT_ROOT
    raw = str(CONTEXT_STORE_CONFIG.get("dir") or "memory/ctx")
    return raw if os.path.isabs(raw) else os.path.join(PROJECT_ROOT, raw)


def enabled() -> bool:
    from config import CONTEXT_STORE_CONFIG
    return bool(CONTEXT_STORE_CONFIG.get("enabled", True))


def threshold_chars() -> int:
    from config import CONTEXT_STORE_CONFIG
    try:
        return max(200, int(CONTEXT_STORE_CONFIG.get("threshold_chars") or 3000))
    except (TypeError, ValueError):
        return 3000


def new_namespace(prefix: str = "run") -> str:
    """本次运行的命名空间（同一运行内指针都能被 recall 找到）。"""
    prune()
    return f"{prefix}-{int(time.time()) % 100000}-{os.getpid() % 1000:03d}"


def prune(max_namespaces: int = 20, max_age_days: float = 7.0) -> int:
    """清理旧的折叠目录：只留最近若干个命名空间与未过期的条目。返回删除的目录数。"""
    root = store_root()
    if not os.path.isdir(root):
        return 0
    import shutil

    folders = []
    for name in os.listdir(root):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        try:
            folders.append((os.path.getmtime(folder), folder))
        except OSError:
            continue
    folders.sort(reverse=True)
    removed = 0
    deadline = time.time() - max_age_days * 86400
    for index, (mtime, folder) in enumerate(folders):
        if index < max_namespaces and mtime >= deadline:
            continue
        try:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "_", str(part or ""))[:64]


def handle_for(namespace: str, seq: int) -> str:
    return f"{_safe(namespace)}-{int(seq):04d}"


def path_for(handle: str) -> str:
    """句柄 → 落盘路径（句柄 = <命名空间>-<序号>，命名空间内可能含 -）。"""
    text = str(handle or "").strip()
    if not text or "/" in text or "\\" in text:
        return ""
    namespace, _, seq = text.rpartition("-")
    if not namespace or not seq.isdigit():
        return ""
    return os.path.join(store_root(), _safe(namespace), f"{seq}.txt")


def save(handle: str, text: str) -> str:
    """把全文写盘；返回路径（失败返回空串，调用方仍可用指针，只是取不回）。"""
    path = path_for(handle)
    if not path:
        return ""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path
    except OSError:
        return ""


def load(handle: str) -> Optional[str]:
    """按句柄逐字取回（不存在返回 None）。"""
    path = path_for(handle)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def first_line(text: str, limit: int = 120) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    line = re.sub(r"\s+", " ", line).strip()
    return line[:limit]


def pointer(handle: str, text: str) -> str:
    """确定性指针：体积 + 首行 + 取回方式。同一输入永远得到同一指针。"""
    size_kb = round(len(str(text or "").encode("utf-8")) / 1024, 1)
    head = first_line(text)
    return (f"[已折叠 #{handle} {size_kb}KB] {head}\n"
            f"（全文未进上下文，需要原文时用 context recall {handle} 逐字取回）")


def fold(handle: str, text: str) -> tuple:
    """超过阈值就落盘并返回指针；否则原样返回。返回 (文本, 是否折叠)。"""
    body = str(text or "")
    if not enabled() or len(body) < threshold_chars():
        return body, False
    if save(handle, body):
        return pointer(handle, body), True
    return body, False


def entries(namespace: str = "") -> list:
    """已落盘的条目 [(句柄, 字节数, 首行)]，按句柄排序。"""
    root = store_root()
    out = []
    if not os.path.isdir(root):
        return out
    names = [namespace] if namespace else sorted(os.listdir(root))
    for name in names:
        folder = os.path.join(root, _safe(name))
        if not os.path.isdir(folder):
            continue
        for fname in sorted(os.listdir(folder)):
            if not fname.endswith(".txt"):
                continue
            path = os.path.join(folder, fname)
            try:
                size = os.path.getsize(path)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    head = first_line(fh.read(200))
            except OSError:
                continue
            out.append((f"{_safe(name)}-{fname[:-4]}", size, head))
    return out


def stats(namespace: str = "") -> dict:
    """零驻留账本：条目数、总字节、估算省下的 token。"""
    items = entries(namespace)
    total = sum(size for _, size, _ in items)
    return {
        "entries": len(items),
        "bytes": total,
        "saved_tokens_estimate": int(total / 3.2),
    }


def is_pointer(text: str) -> bool:
    return bool(POINTER_RE.search(str(text or "")))
