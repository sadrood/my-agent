"""
会话持久化模块（借鉴同类实现的 thread/session 概念）。

v2：对话（Conversation）体系
- 每个对话有唯一 ID（conv-YYYYMMDD-随机码）
- 每轮结束自动**全量**保存对话记录（不截断）
- 用 ID 可随时打开并恢复该对话的全部记录继续聊

旧接口（save/load/list_sessions，按名字存取）保留兼容。
"""
import json
import os
import re
import secrets
from datetime import datetime
from typing import Dict, List, Optional

from config import SESSION_CONFIG


def generate_conversation_id() -> str:
    """生成对话 ID：conv-20260824-a1b2c3。"""
    return f"conv-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3)}"


def sanitize_name(name: str) -> str:
    """把会话名清理为安全文件名（连续分隔符折叠为单个）。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]", "_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:60] or "session"



def _atomic_dump(path: str, payload: dict) -> None:
    """原子写 JSON：紧凑序列化（长会话体积/耗时大幅下降）+ 临时文件替换，
    避免写入中断产生半损坏文件。"""
    import tempfile as _temp
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, path)


class SessionStore:
    """会话文件存储。"""

    def __init__(self, config: dict = None):
        self.config = config or SESSION_CONFIG
        self.dir = os.path.expanduser(self.config.get("dir", "./memory/sessions"))
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
    # v2：对话（Conversation）管理 —— 全量记录
    # ================================================================

    def save_conversation(
        self,
        conv_id: str,
        messages: List[Dict],
        title: str = "",
        last_summary: str = "",
        model: str = "",
        base_url: str = "",
    ) -> str:
        """
        保存对话（全量记录，不截断），并绑定当时使用的模型。

        Args:
            conv_id: 对话 ID
            messages: 全部消息 [{"role": "user"|"assistant", "content": str}]
            title: 标题；为空时沿用已有标题，否则取第一条用户消息作为标题
            last_summary: 最后一轮执行摘要
            model / base_url: 该对话绑定的模型（恢复对话时自动切回）

        Returns:
            保存的文件路径
        """
        now = datetime.now().isoformat()
        existing = self._read_raw(conv_id)
        created_at = existing.get("created_at") if existing else now

        if not title:
            title = (existing or {}).get("title", "")
        if not title and messages:
            first_user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
            title = (first_user or "").strip()[:60] or "新对话"

        payload = {
            "id": conv_id,
            "title": title,
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
                "title": payload.get("title", ""),
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
        """
        保存会话（旧接口）。

        Args:
            name: 会话名
            data: 任意可 JSON 序列化的数据（通常含 goal / messages / summary）

        Returns:
            保存的文件路径
        """
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
            files = sorted(
                (os.path.join(self.dir, f) for f in os.listdir(self.dir) if f.endswith(".json")),
                key=os.path.getmtime,
            )
            max_files = int(self.config.get("max_sessions", 50))
            for f in files[:-max_files]:
                try:
                    os.remove(f)
                except Exception:
                    pass
        except Exception:
            pass
