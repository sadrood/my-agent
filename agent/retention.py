"""目录保留策略的公共原语：按 mtime 取序（最旧在前）+ 按数量裁剪。

三个调用方（context_store / rollout / session）此前各写一遍"listdir + getmtime + sort"，
且容易在"升序还是降序、切哪一端"上漂移；这里只抽出取序这一层，删除策略仍由调用方各自决定。
"""
import os


def prune_oldest(dir_path: str, keep: int, *, suffix: str = None) -> int:
    """按 mtime 从最旧的开始删，直到只剩 keep 个；返回删除数量。"""
    keep = max(0, int(keep))
    entries = list_by_mtime(dir_path, suffix=suffix)
    excess = len(entries) - keep
    if excess <= 0:
        return 0
    removed = 0
    for _, path in entries[:excess]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            continue
    return removed


def list_by_mtime(dir_path: str, *, suffix: str = None, dirs_only: bool = False) -> list:
    """返回 [(mtime, path)]，按 mtime **升序**（最旧在前）。目录不存在/不可读时返回 []。
    取不到 mtime 的条目（竞态下已被删）跳过，不当成最新或最旧。"""
    try:
        names = os.listdir(dir_path)
    except OSError:
        return []
    entries = []
    for name in names:
        if suffix and not name.endswith(suffix):
            continue
        path = os.path.join(dir_path, name)
        if dirs_only and not os.path.isdir(path):
            continue
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            continue
    entries.sort(key=lambda kv: kv[0])
    return entries
