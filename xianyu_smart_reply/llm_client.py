"""
闲鱼智能回复系统 - 大模型客户端
支持 OpenAI / DeepSeek / 自定义兼容 API
"""
import json
import logging
from typing import Optional
from datetime import datetime

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger(__name__)


# 各模型提供商的默认 API 地址
PROVIDER_API_BASES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",  # 通义千问
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",      # 通义千问别名
}


class LLMClient:
    """大模型调用客户端"""

    def __init__(self, api_base: str = "", api_key: str = "",
                 model: str = "gpt-4o-mini", max_tokens: int = 500,
                 temperature: float = 0.7, timeout: int = 30,
                 provider: str = "openai"):
        self.provider = provider.lower()
        self.api_base = api_base or PROVIDER_API_BASES.get(
            self.provider, "https://api.openai.com/v1"
        )
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

    def _get_client(self):
        if httpx is None:
            raise ImportError("需要安装 httpx: pip install httpx")
        return httpx.Client(
            base_url=self.api_base,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )

    def chat(self, system_prompt: str, user_message: str,
             conversation_history: Optional[list] = None) -> str:
        """
        调用大模型生成回复

        Args:
            system_prompt: 系统提示词
            user_message: 用户消息
            conversation_history: 对话历史 [[role, content], ...]

        Returns:
            生成的回复文本
        """
        messages = [{"role": "system", "content": system_prompt}]

        if conversation_history:
            for role, content in conversation_history:
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        logger.info(f"调用 LLM: model={self.model}, messages={len(messages)}")

        try:
            with self._get_client() as client:
                resp = client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()

            content = data["choices"][0]["message"]["content"].strip()
            logger.info(f"LLM 回复: {content[:80]}...")
            return content

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise

    def chat_stream(self, system_prompt: str, user_message: str,
                    on_chunk: callable) -> str:
        """
        流式调用大模型（可选）

        Args:
            on_chunk: 每收到一块内容时调用的回调函数
        Returns:
            完整回复文本
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }

        full_text = ""
        try:
            with self._get_client() as client:
                resp = client.post("/chat/completions", json=payload)
                resp.raise_for_status()

                for line in resp.iter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        chunk = delta.get("content", "")
                        if chunk:
                            full_text += chunk
                            on_chunk(chunk)
                    except json.JSONDecodeError:
                        continue

            return full_text

        except Exception as e:
            logger.error(f"LLM 流式调用失败: {e}")
            raise