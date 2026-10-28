"""会话持久化模块（借鉴同类实现的 thread/session 概念）。"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from config import PROJECT_ROOT, SESSION_CONFIG
from agent.atomic_io import atomic_write_json
from agent.retention import list_by_mtime


def _resolve_session_dir(config: dict) -> str:
    """解析会话存储目录（优先绝对路径，杜绝 cwd 漂移导致保存失败）："""
    env_dir = os.getenv("SESSION_DIR")
    if env_dir:
        return os.path.expanduser(env_dir)
    cfg_dir = (config or {}).get("dir")
    if cfg_dir:
        expanded = os.path.expanduser(cfg_dir)
        if os.path.isabs(expanded):
            return expanded
        return os.path.abspath(os.path.join(PROJECT_ROOT, expanded))
    return os.path.join(PROJECT_ROOT, "memory", "sessions")


def generate_conversation_id() -> str:
    """生成对话 ID：conv-20260824-a1b2c3。"""
    return f"conv-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3)}"


def sanitize_name(name: str) -> str:
    """把会话名清理为安全文件名（连续分隔符折叠为单个）。"""
    raw = (name or "").strip()
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return "session"
    cleaned = cleaned[:60]
    # 清理**改变了**原名时补一个短哈希：否则 `调研: 2026` 与 `调研 2026`（以及 `a/b` 与 `a_b`）会折叠成同一个文件名。
    if cleaned != raw:
        cleaned = f"{cleaned}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]}"
    return cleaned



# : 每个会话文件一把进程内锁：dashboard 的侧任务与主任务在**同一进程的不同线程**里: 保存同一个会话（side-<main_id> 也常回写主会话）
_DUMP_LOCKS: Dict[str, threading.Lock] = {}
_DUMP_LOCKS_GUARD = threading.Lock()


def _dump_lock_for(path: str) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(path))
    with _DUMP_LOCKS_GUARD:
        lock = _DUMP_LOCKS.get(key)
        if lock is None:
            # RLock（可重入）：append_messages 要持锁调用 _atomic_dump，后者也会取同一把锁 —— 普通 Lock 会死锁。
            lock = _DUMP_LOCKS[key] = threading.RLock()
        return lock


def _atomic_dump(path: str, payload: dict) -> None:
    """原子写 JSON：紧凑序列化（长会话体积/耗时大幅下降）+ 临时文件替换。"""
    # 传入的是 RLock（可重入）：append_messages 会持锁调用本函数，两者取同一把锁不会死锁。
    atomic_write_json(path, payload, compact=True, lock=_dump_lock_for(path))


#: 追加在首条 user 消息后面的上下文段从这里开始（技能 / 经验 / 策略 / 系统提示 / 闲聊提示）。
#: 会话标题只该取"用户自己说的那句话"，别把注入内容当成名字。
_CTX_MARKERS = ("\n\n## ", "\n\n[系统提示]", "\n\n（")


def title_from_user(text: str) -> str:
    """从首条 user 消息里取用户真正说的第一行（去掉追加的上下文段），最多 60 字。"""
    body = str(text or "").strip()
    for marker in _CTX_MARKERS:
        cut = body.find(marker)
        if cut > 0:
            body = body[:cut]
            break
    body = body.strip()
    line = body.splitlines()[0].strip() if body else ""
    return line[:60]


def _title_looks_injected(title: str) -> bool:
    return any(marker.strip() in str(title or "") for marker in _CTX_MARKERS)


def _first_user(messages) -> str:
    return next((str(m.get("content", "")) for m in (messages or [])
                 if m.get("role") == "user"), "")


def display_title(payload: dict) -> str:
    """展示用标题：历史遗留的"注入内容标题"就地纠正，且永远压成一行（不改文件）。"""
    title = str((payload or {}).get("title", "") or "")
    if _title_looks_injected(title):
        title = title_from_user(_first_user((payload or {}).get("messages"))) or title
    line = title.strip().splitlines()[0].strip() if title.strip() else ""
    return line[:60] if line else title


class SessionStore:
    """会话文件存储。"""

    def __init__(self, config: dict = None, on_cleanup=None):
        self.config = config or SESSION_CONFIG
        # : cb([被删路径]) —— 让「超限清理」这件事能被前端/用户看见，而不是: 只 print 到后端控制台（桌面端用户根本看不到）
        self.on_cleanup = on_cleanup
        self.dir = _resolve_session_dir(self.config)
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, f"{sanitize_name(name)}.json")

    def _read_raw(self, conv_id: str) -> Optional[dict]:
        path = self._path(conv_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    # ================================================================
    # 对话（Conversation）管理 —— 全量记录
    # ================================================================

    def save_conversation(
        self,
        conv_id: str,
        messages: List[Dict],
        title: str = "",
        last_summary: str = "",
        model: str = "",
        base_url: str = "",
        title_fn=None,
    ) -> str:
        """保存对话（全量记录，不截断），并绑定当时使用的模型。"""
        now = datetime.now().isoformat()
        existing = self._read_raw(conv_id)
        created_at = existing.get("created_at") if existing else now

        if not title:
            title = (existing or {}).get("title", "")
        # title_auto：自动生成的标题（兜底/小模型）才允许被重新生成；用户改过的不动。
        title_auto = (existing or {}).get("title_auto")
        if title_auto is None:
            title_auto = (not title) or _title_looks_injected(title)
        # 小模型起标题：以前只在"完全没有标题"时才跑，而增量落盘一开始就写了兜底标题
        # → title_fn 永远轮不到（实测会话名全是首条消息截断）。
        if title_fn is not None and title_auto and messages:
            generated = ""
            try:
                generated = str(title_fn(messages) or "").strip()
            except Exception:               # noqa: BLE001
                generated = ""              # 小模型坏了不能挡住保存
            if generated:
                title, title_auto = generated, False
        if not title and messages:
            title = title_from_user(_first_user(messages)) or "新对话"
            title_auto = True

        payload = {
            "id": conv_id,
            "title": title,
            "title_auto": title_auto,
            "created_at": created_at,
            "updated_at": now,
            "messages": messages,        # 全量记录
            "last_summary": last_summary or "",
            "model": model or (existing or {}).get("model", ""),
            "base_url": base_url or (existing or {}).get("base_url", ""),
        }
        path = self._path(conv_id)
        _atomic_dump(path, payload)
        self._cleanup()
        return path

    def append_messages(self, conv_id: str, messages: List[Dict], **fields) -> str:
        """在**同一把锁内**读→追加→写，避免"读-改-写"被别人的全量重写切碎。"""
        path = self._path(conv_id)
        with _dump_lock_for(path):
            existing = self._read_raw(conv_id) or {}
            merged = list(existing.get("messages", [])) + list(messages)
            self.save_conversation(
                conv_id,
                messages=merged,
                title=fields.get("title") or existing.get("title", ""),
                last_summary=fields.get("last_summary") or existing.get("last_summary", ""),
            )
            return path

    def set_title(self, conv_id: str, title: str) -> bool:
        """只改标题（会话名），保留全部记录；对话不存在返回 False。

        会话 id/文件名不动：`/open`、`--session` 的引用继续有效，改的是给人看的名字。
        """
        path = self._path(conv_id)
        with _dump_lock_for(path):
            payload = self._read_raw(conv_id)
            if payload is None:
                return False
            payload["title"] = str(title or "").strip()[:120]
            payload["title_auto"] = False        # 用户改过：以后别再自动重生成
            payload["updated_at"] = datetime.now().isoformat()
            _atomic_dump(path, payload)
        return True

    def load_conversation(self, conv_id: str) -> Optional[dict]:
        """加载对话（含全部记录）；不存在返回 None。"""
        return self._read_raw(conv_id)

    @staticmethod
    def _last_preview(messages, limit: int = 160) -> str:
        """取最后一条非空消息的内容片段（会话卡片预览用）。"""
        for m in reversed(messages or []):
            c = (m.get("content") or "").strip() if isinstance(m, dict) else ""
            if c:
                return c.replace("\n", " ")[:limit]
        return ""

    def list_conversations(self) -> List[dict]:
        """列出全部对话：id / 标题 / 记录数 / 创建与更新时间 / 最后消息预览。"""
        result = []
        for f in os.listdir(self.dir):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, f), "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue
            if not payload.get("id"):
                continue
            msgs = payload.get("messages", [])
            result.append({
                "id": payload["id"],
                "title": display_title(payload),
                "count": len(msgs),
                "created_at": payload.get("created_at", ""),
                "updated_at": payload.get("updated_at", ""),
                "preview": self._last_preview(msgs),
            })
        result.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return result

    def delete_conversation(self, conv_id: str) -> bool:
        path = self._path(conv_id)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    # ================================================================
    # 旧接口（按名字存取，保留兼容）
    # ================================================================

    def save(self, name: str, data: Dict) -> str:
        """保存会话（旧接口）。"""
        payload = {
            "name": name,
            "saved_at": datetime.now().isoformat(),
            "data": data,
        }
        path = self._path(name)
        _atomic_dump(path, payload)
        self._cleanup()
        return path

    def load(self, name: str) -> Optional[Dict]:
        """加载会话，返回 data；不存在时返回 None。"""
        path = self._path(name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return payload.get("data")
        except Exception:
            return None

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def list_sessions(self) -> List[Dict]:
        """列出全部会话（名称 + 保存时间）。"""
        result = []
        for f in os.listdir(self.dir):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, f), "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                result.append({
                    "name": payload.get("name", f[:-5]),
                    "saved_at": payload.get("saved_at", ""),
                })
            except Exception:
                continue
        result.sort(key=lambda x: x.get("saved_at", ""), reverse=True)
        return result

    def _cleanup(self):
        """保留最近 max_sessions 个会话文件。"""
        try:
            entries = list_by_mtime(self.dir, suffix=".json")   # 最旧在前
            max_files = int(self.config.get("max_sessions", 50))
            doomed = [p for _, p in entries[:-max_files]] if max_files > 0 else []
            if doomed:
                print(f"[Session] 会话数超过上限（{max_files}），将清理最旧的 "
                      f"{len(doomed)} 个会话文件。它们是对话的唯一副本，"
                      f"如需保留请先把 {self.dir} 里的文件备份出去。")
            removed = []
            for f in doomed:
                try:
                    os.remove(f)
                    removed.append(f)
                    print(f"[Session] 已清理: {os.path.basename(f)}")
                except Exception:
                    pass
            if removed and self.on_cleanup is not None:
                try:
                    self.on_cleanup(removed)
                except Exception:
                    pass
            # 顺手清掉崩溃/被杀留下的临时文件（原子写的中间产物，永不参与读取）。只删超过 1 小时的：另一个写入者可能正开着它，删了会让它的 os.replace 失败。
            now = time.time()
            for name in os.listdir(self.dir):
                if not name.endswith(".tmp"):
                    continue
                fp = os.path.join(self.dir, name)
                try:
                    if now - os.path.getmtime(fp) > 3600:
                        os.remove(fp)
                except OSError:
                    pass
        except Exception:
            pass
