"""
闲鱼智能回复系统 - 智能回复引擎（核心）
负责：意图识别 → 场景匹配 → 提示词组装 → LLM 调用 → 回复生成
"""
import logging
import re
from datetime import datetime
from typing import Optional

from .config import SystemConfig
from .llm_client import LLMClient
from .prompts import (
    SYSTEM_PROMPT, PRICE_PROMPT, AVAILABILITY_PROMPT,
    SHIPPING_PROMPT, NEGOTIATION_PROMPT, INQUIRY_PROMPT, DEFAULT_PROMPT,
)

logger = logging.getLogger(__name__)


class ReplyEngine:
    """智能回复引擎"""

    def __init__(self, config: SystemConfig):
        self.config = config
        self.llm = LLMClient(
            api_base=config.llm.api_base,
            api_key=config.llm.api_key,
            model=config.llm.model,
            max_tokens=config.llm.max_tokens,
            temperature=config.llm.temperature,
            timeout=config.llm.timeout,
            provider=config.llm.provider,
        )

    def generate_reply(self, user_message: str,
                       item_name: str = "",
                       item_price: float = 0,
                       item_description: str = "",
                       stock_status: str = "有货",
                       shipping_from: str = "",
                       conversation_history: Optional[list] = None,
                       buyer_offer: Optional[float] = None) -> str:
        """
        生成智能回复

        Args:
            user_message: 买家消息
            item_name: 商品名称
            item_price: 商品定价
            item_description: 商品描述
            stock_status: 库存状态
            shipping_from: 发货地
            conversation_history: 对话历史
            buyer_offer: 买家出价（砍价场景）

        Returns:
            生成的回复文本
        """
        # 1. 意图识别
        intent = self._detect_intent(user_message)
        logger.info(f"意图识别: {intent}")

        # 2. 选择场景提示词
        scene_prompt = self._select_scene_prompt(
            intent, user_message, item_name, item_price,
            item_description, stock_status, shipping_from, buyer_offer
        )

        # 3. 组装系统提示词
        system_prompt = self._build_system_prompt()

        # 4. 调用 LLM
        reply = self.llm.chat(
            system_prompt=system_prompt,
            user_message=scene_prompt,
            conversation_history=conversation_history,
        )

        # 5. 后处理
        reply = self._post_process(reply)
        logger.info(f"最终回复: {reply}")
        return reply

    def _detect_intent(self, message: str) -> str:
        """
        意图识别：判断买家消息属于哪个场景
        返回: price / availability / shipping / negotiation / inquiry / default
        """
        msg_lower = message.lower()
        seller = self.config.seller

        # 砍价意图（优先级最高，因为砍价也包含价格关键词）
        negotiation_patterns = [
            r"便宜\s*[点多少]", r"少\s*[点多少]", r"打折", r"优惠",
            r"最低\s*[多少]", r"底价", r"让\s*[点多少]",
            r"\d+[^0-9]*行", r"这个价", r"给个价",
            r"\d+[^0-9]*卖不卖", r"\d+[^0-9]*卖吗", r"\d+[^0-9]*能卖",
            r"\d+[^0-9]*收", r"\d+[^0-9]*拿走",
        ]
        for pattern in negotiation_patterns:
            if re.search(pattern, msg_lower):
                return "negotiation"

        # 检查关键词匹配
        for intent, keywords in seller.keywords.items():
            for kw in keywords:
                if kw in message:
                    # 价格关键词中排除砍价类
                    if intent == "price":
                        if not any(p in message for p in ["便宜点", "少点", "打折", "优惠", "让让"]):
                            return "price"
                    return intent

        # 尝试提取数字判断是否在问价
        numbers = re.findall(r"\d+\.?\d*", message)
        if numbers and any(kw in message for kw in ["吗", "？", "?", "么", "不"]):
            return "price"

        return "default"

    def _select_scene_prompt(self, intent: str, user_message: str,
                             item_name: str, item_price: float,
                             item_description: str, stock_status: str,
                             shipping_from: str, buyer_offer: Optional[float]) -> str:
        """根据意图选择并填充场景提示词"""
        base_vars = {
            "user_message": user_message,
            "item_name": item_name,
            "item_price": f"{item_price}元" if item_price else "未定价",
            "item_description": item_description,
            "stock_status": stock_status,
            "shipping_from": shipping_from,
        }

        if intent == "negotiation":
            base_vars["buyer_offer"] = f"{buyer_offer}元" if buyer_offer else "未提及"
            return NEGOTIATION_PROMPT.format(**base_vars)
        elif intent == "price":
            return PRICE_PROMPT.format(**base_vars)
        elif intent == "availability":
            return AVAILABILITY_PROMPT.format(**base_vars)
        elif intent == "shipping":
            return SHIPPING_PROMPT.format(**base_vars)
        elif intent == "inquiry":
            return INQUIRY_PROMPT.format(**base_vars)
        else:
            return DEFAULT_PROMPT.format(**base_vars)

    def _build_system_prompt(self) -> str:
        """组装系统级提示词"""
        seller = self.config.seller
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return SYSTEM_PROMPT.format(
            seller_name=seller.name,
            tone=self._tone_description(seller.tone),
            signature=seller.signature,
            current_time=now,
            business_hours=seller.business_hours,
        )

    def _tone_description(self, tone: str) -> str:
        """将 tone 枚举转为描述"""
        tone_map = {
            "friendly": "亲切友好，像朋友聊天一样",
            "professional": "专业可靠，简洁高效",
            "casual": "随性自然，轻松随意",
        }
        return tone_map.get(tone, "亲切友好")

    def _post_process(self, reply: str) -> str:
        """回复后处理"""
        # 去除多余空白
        reply = re.sub(r"\n{3,}", "\n\n", reply)
        reply = reply.strip()

        # 长度限制（闲鱼消息不宜过长）
        if len(reply) > 300:
            reply = reply[:297] + "..."

        return reply