"""
视觉分析模块。
调用多模态 LLM（GPT-4V / Qwen-VL 等）分析截图：
- 页面内容描述
- 元素定位（返回坐标）
- OCR 文字提取
- 页面结构分析
"""
import base64
import json
import re
from typing import Optional

from openai import OpenAI
from config import LLM_CONFIG, VISION_CONFIG


class VisionModel:
    """
    视觉分析模型。
    接收截图 + 问题，调用多模态 LLM 返回分析结果。

    支持的模型：GPT-4o、GPT-4V、Qwen-VL-Max 等多模态视觉模型。
    普通的 text-only 模型（如 gpt-4o-mini 无视觉能力）不可用于此模块。
    """

    # 可选择的视觉模型优先级列表
    RECOMMENDED_MODELS = [
        "gpt-4o",
        "gpt-4o-mini",
        "ox-alpha",          # OpenRouter stealth 推理模型（图片支持视上游而定）
        "gpt-4-vision-preview",
        "gpt-4-turbo",
        "qwen-vl-max",
        "qwen-vl-plus",
        "gemini-2.0-flash",
    ]

    def __init__(
        self,
        vision_model: str = None,
        base_url: str = None,
        api_key: str = None,
    ):
        """
        Args:
            vision_model: 视觉模型名称，默认自动检测。
            base_url / api_key: 覆盖端点与密钥（桌面端设置下发）；
                留空回退 VISION_* 环境变量，再回退主 LLM 配置。
        """
        # 视觉模型可走独立端点（VISION_API_KEY / VISION_BASE_URL），
        # 留空时回退到主 LLM 端点（如主模型用 OpenRouter、视觉用商汤）
        self.client = OpenAI(
            api_key=api_key or VISION_CONFIG.get("api_key") or LLM_CONFIG["api_key"],
            base_url=base_url or VISION_CONFIG.get("base_url") or LLM_CONFIG["base_url"],
        )
        self.vision_model = vision_model or VISION_CONFIG.get("vision_model") or self._auto_detect_model()
        self.screenshot_dir = VISION_CONFIG.get("screenshot_path", "./screenshots")

    def _auto_detect_model(self) -> str:
        """自动选择可用的视觉模型。"""
        current = LLM_CONFIG.get("default_model", "gpt-4o-mini")
        # 如果当前默认模型本身支持视觉，直接使用
        for rec in self.RECOMMENDED_MODELS:
            if rec in current.lower():
                return current
        # 否则使用第一个推荐的（通常是 gpt-4o）
        return self.RECOMMENDED_MODELS[0]

    def analyze(
        self,
        image_data: str,
        question: str,
        image_type: str = "image/png",
        detail: str = "auto",
        max_tokens: int = 2000,
        reasoning_effort: str = "none",
        system_prompt: str = "你是一个说话客观公正的小助手。",
    ) -> str:
        """
        分析截图，回答关于页面内容的问题。

        Args:
            image_data: 图片的 base64 编码（不含 data URI 前缀）。
            question: 要问的问题（如"页面上有哪些可点击的按钮？"）。
            image_type: 图片 MIME 类型。
            detail: 图片分析精度（'auto' / 'low' / 'high'）。
            max_tokens: 最大输出 token。
            reasoning_effort: 推理强度（官方推荐 "none"：视觉+推理组合在
                部分网关不稳定/易 500/拖慢；支持 "none"/"low"/"medium"/"high"）。
            system_prompt: 可选的 system 消息（对齐官方示例）。

        Returns:
            模型的分析结果文本。
        """
        data_uri = f"data:{image_type};base64,{image_data}"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": question,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_uri,
                            "detail": detail,
                        },
                    },
                ],
            }
        )

        try:
            kwargs = dict(model=self.vision_model, messages=messages, max_tokens=max_tokens)
            if reasoning_effort:
                # 网关把 reasoning_effort 作为顶层参数（OpenAI SDK 需 extra_body 透传）
                kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}
            response = self.client.chat.completions.create(**kwargs)
        except Exception as e:
            raise RuntimeError(f"视觉模型调用失败: {str(e)}")

        # 响应加固：纯文本模型收到图片、或被安全拦截时，常见返回空 choices/空 content，
        # 直接下标访问会抛晦涩的 NoneType 错误——这里转成可读的明确提示
        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError(
                "视觉模型返回空 choices：该模型可能不支持图片输入，或请求被服务端拒绝。"
                "请换一个多模态（视觉）模型。")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not content:
            reason = getattr(choices[0], "finish_reason", "") or ""
            raise RuntimeError(
                f"视觉模型返回空内容（finish_reason={reason or '未知'}）："
                "该模型可能不支持图片输入，或内容被安全策略拦截。请换多模态模型。")
        return content

    # ================================================================
    # 高级封装：常用分析场景
    # ================================================================

    def describe_page(self, screenshot_base64: str) -> str:
        """
        描述页面的整体内容和布局。

        Returns:
            页面内容描述。
        """
        question = (
            "请详细描述这个网页的内容和布局。包括：\n"
            "1. 页面的主要标题和主题\n"
            "2. 可见的主要内容区域（文章、列表、表单等）\n"
            "3. 所有可见的按钮、链接和交互元素\n"
            "4. 所有可见的输入框和表单元素\n"
            "5. 页面顶部导航栏的内容\n"
            "用中文回答，尽量详细但不啰嗦。"
        )
        return self.analyze(screenshot_base64, question, max_tokens=1500)

    def locate_element(self, screenshot_base64: str, description: str) -> dict:
        """
        在截图中定位指定元素，返回其大致坐标。

        Args:
            screenshot_base64: 截图 base64 数据。
            description: 元素描述（如"登录按钮"、"搜索输入框"等）。

        Returns:
            包含 x, y, width, height, found 的字典。
        """
        question = (
            f"请在截图中找到以下元素：{description}\n\n"
            "你需要返回一个 JSON 对象，格式如下：\n"
            '{{"found": true/false, "x": 中心x坐标, "y": 中心y坐标, '
            '"width": 元素大致宽度, "height": 元素大致高度, '
            '"selector_hint": "可能的CSS选择器或元素描述"}}\n\n'
            "坐标是相对于截图左上角的像素位置。\n"
            "如果找不到该元素，found 设为 false。\n"
            "只输出 JSON，不要其他文字。"
        )
        result = self.analyze(screenshot_base64, question, max_tokens=500)

        # 解析 JSON
        json_match = re.search(r'\{[\s\S]*\}', result)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return {"found": False, "error": f"无法解析模型输出: {result[:200]}"}

    def extract_text(self, screenshot_base64: str) -> str:
        """
        从截图中提取所有可见文字（视觉 OCR）。

        Returns:
            提取的文字内容。
        """
        question = (
            "请提取截图中所有可见的文字内容。\n"
            "保持文字的顺序和层级关系。\n"
            "用 [标题]、[按钮]、[链接]、[正文] 等标签标注文字类型。\n"
            "不要添加额外的解释。"
        )
        return self.analyze(screenshot_base64, question, max_tokens=3000)

    def find_interactive_elements(self, screenshot_base64: str) -> str:
        """
        找出页面上所有可交互的元素。

        Returns:
            交互元素列表。
        """
        question = (
            "请列出截图中所有可点击、可交互的元素及其大致位置。\n"
            "以列表形式输出，每行格式：\n"
            "- 元素类型（按钮/链接/输入框/下拉菜单/复选框等）: 文字内容或描述 (大致位置：左上/中间/右上/左侧/右侧/底部)\n"
            "用中文回答。"
        )
        return self.analyze(screenshot_base64, question, max_tokens=1500)
