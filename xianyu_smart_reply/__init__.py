"""
闲鱼智能回复系统 - 大模型加持的自动回复工具
============================================

核心模块:
    - config: 系统配置
    - prompts: 提示词模板
    - llm_client: 大模型客户端
    - reply_engine: 智能回复引擎（核心）
    - browser_monitor: 浏览器消息监听器
    - history: 对话历史管理
"""

__version__ = "1.0.0"

from .config import SystemConfig, load_config_from_env
from .reply_engine import ReplyEngine
from .history import ConversationHistory
from .browser_monitor import BrowserMonitor, Message

__all__ = [
    "SystemConfig",
    "load_config_from_env",
    "ReplyEngine",
    "ConversationHistory",
    "BrowserMonitor",
    "Message",
]