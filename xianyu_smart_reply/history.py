"""
闲鱼智能回复系统 - 对话历史管理
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ConversationHistory:
    """对话历史记录"""

    def __init__(self, history_file: str = "reply_history.json"):
        self.history_file = Path(history_file)
        self._history: dict = {}  # conversation_id -> [{role, content, timestamp}]
        self._load()

    def _load(self):
        """加载历史记录"""
        if str(self.history_file) == ":memory:":
            return
        if self.history_file.exists():
            try:
                data = json.loads(self.history_file.read_text(encoding="utf-8"))
                self._history = data
                logger.info(f"已加载 {len(self._history)} 段对话历史")
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"加载历史记录失败: {e}")
                self._history = {}

    def _save(self):
        """保存历史记录（:memory: 模式跳过）"""
        if str(self.history_file) == ":memory:":
            return
        try:
            self.history_file.write_text(
                json.dumps(self._history, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"保存历史记录失败: {e}")

    def add_message(self, conversation_id: str, role: str, content: str):
        """添加一条消息到对话历史"""
        if conversation_id not in self._history:
            self._history[conversation_id] = []

        self._history[conversation_id].append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        })

        # 限制每个对话的历史长度（保留最近20条）
        if len(self._history[conversation_id]) > 20:
            self._history[conversation_id] = self._history[conversation_id][-20:]

        self._save()

    def get_history(self, conversation_id: str,
                    max_turns: int = 10) -> list:
        """
        获取对话历史

        Returns:
            [[role, content], ...]
        """
        messages = self._history.get(conversation_id, [])
        # 只返回最近 max_turns 条
        recent = messages[-max_turns:]
        return [[m["role"], m["content"]] for m in recent]

    def get_conversation_count(self) -> int:
        """获取对话总数"""
        return len(self._history)

    def get_total_messages(self) -> int:
        """获取消息总数"""
        return sum(len(v) for v in self._history.values())

    def export_stats(self) -> dict:
        """导出统计信息"""
        return {
            "conversations": self.get_conversation_count(),
            "total_messages": self.get_total_messages(),
            "recent_conversations": list(self._history.keys())[:10],
        }