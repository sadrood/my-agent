"""
视觉分析工具（SeeTool）。
把原来 Executor 里的 "see" 动作升级为标准工具：模型可以主动请求
"截取当前页面 → 视觉模型分析 → 返回分析文本"，用于 Computer Use 场景。

依赖：tools.browser.BrowserTool（截图）+ models.vision.VisionModel（分析）。
视觉模型不可用时返回错误，模型会自行改用 text/html 等方式。
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

# 提示词统一放 models/prompts.py（AGENTS.md 规则 5）。这里此前抄了一份
# 逐字相同的副本，两边已经开始漂移（多一行空行）——改为单一来源。
from models.prompts import VISION_PAGE_ANALYSIS_QUESTION as DEFAULT_QUESTION


class SeeTool(BaseTool):
    """视觉分析工具：截图 + 视觉模型分析当前页面。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"

    def __init__(self, browser_tool=None, vision_model=None):
        """
        Args:
            browser_tool: BrowserTool 实例（延迟获取：从 ToolManager 查找）
            vision_model: VisionModel 实例（None 时延迟创建）
        """
        self._browser_tool = browser_tool
        self._vision_model = vision_model

    @property
    def name(self) -> str:
        return "see"

    @property
    def description(self) -> str:
        return (
            "视觉分析工具。截取当前浏览器页面并交给视觉模型分析，"
            "返回页面内容、状态、可交互元素及位置等结构化描述。"
            "当你需要理解页面内容、定位按钮/输入框坐标、判断操作是否成功时使用。"
            "需要先确保浏览器已启动（browser launch）。\n"
            "只要**文字内容**（报错信息、表格、按钮文案）时优先用 ocr 工具：本地引擎、"
            "离线、不耗视觉配额；本工具在视觉模型不可用或超时时也会自动降级到 OCR。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "希望视觉模型回答的具体问题（可选，默认分析页面整体）",
                }
            },
        }

    def _get_vision_model(self):
        if self._vision_model is not None:
            return self._vision_model
        try:
            from models.vision import VisionModel
            self._vision_model = VisionModel()
        except Exception:
            self._vision_model = None
        return self._vision_model

    def _get_browser_tool(self):
        return self._browser_tool

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        question = str(arguments.get("question", "") or "").strip() or DEFAULT_QUESTION
        return self._run(question)

    def execute(self, input_str: str) -> ToolResult:
        question = input_str.strip() or DEFAULT_QUESTION
        return self._run(question)

    def _run(self, question: str) -> ToolResult:
        import re

        browser = self._get_browser_tool()
        if browser is None:
            return ToolResult(success=False, output="", error="浏览器工具不可用。")

        # 1. 截图（base64）
        try:
            result = browser.execute("screenshot_base64")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"截图失败: {e}")

        if not result.success:
            return ToolResult(success=False, output="", error=f"截图失败: {result.error}")

        match = re.search(r"\[FULL_BASE64\](.*?)\[/FULL_BASE64\]", result.output, re.DOTALL)
        if not match:
            return ToolResult(success=False, output="", error="截图返回格式异常（缺少 base64 数据）。")

        base64_data = match.group(1)

        # 2. 文字类请求优先本地 OCR：识字是本地引擎的强项（离线、免费、不超时），
        #    没必要为了"读出图里有什么字"去赌一次多模态调用（用户痛点：视觉模型
        #    无响应就整个废掉）。
        if self._wants_text(question):
            ocr_text = self._ocr_fallback(base64_data)
            if ocr_text:
                return ToolResult(
                    success=True,
                    output=f"【本地 OCR 识别结果】（未调用视觉模型）\n{ocr_text}",
                    metadata={"screenshot_base64": base64_data, "via": "ocr"},
                )

        # 3. 视觉分析
        vision = self._get_vision_model()
        if vision is None:
            return self._ocr_or_error(base64_data, "视觉模型不可用")
        try:
            analysis = vision.analyze(base64_data, question, max_tokens=1500)
        except Exception as e:
            # 视觉模型挂掉时不再"就废了"：降级到本地 OCR，至少把字读出来
            return self._ocr_or_error(base64_data, f"视觉分析失败: {str(e)[:160]}")

        return ToolResult(
            success=True,
            output=f"【视觉分析结果】{getattr(vision, 'fallback_note', lambda: '')()}\n{analysis}",
            metadata={"screenshot_base64": base64_data,
                      "vision_model": getattr(vision, "last_model", "")},
        )

    # ------------------------------------------------------------

    @staticmethod
    def _wants_text(question: str) -> bool:
        """这个问题是不是"只要文字"（而非理解版面/找元素）。"""
        try:
            from models.ocr import auto_fallback_enabled
            from config import OCR_CONFIG
            if not auto_fallback_enabled() or not OCR_CONFIG.get("prefer_for_text", True):
                return False
        except Exception:                       # noqa: BLE001
            return False
        q = (question or "").lower()
        keys = ("提取文字", "识别文字", "读取文字", "所有文字", "有哪些文字", "文字内容",
                "ocr", "识字", "念出来", "识别一下文字", "读出")
        return any(k in q for k in keys)

    @staticmethod
    def _ocr_fallback(base64_data: str) -> str:
        """本地 OCR（失败返回空串，不抛）。"""
        try:
            from models.ocr import auto_fallback_enabled, recognize_image
            if not auto_fallback_enabled():
                return ""
            result = recognize_image(image_base64=base64_data)
            if not result.text.strip():
                return ""
            return f"{result.summary()}\n{result.text}"
        except Exception:                       # noqa: BLE001
            return ""

    def _ocr_or_error(self, base64_data: str, why: str) -> ToolResult:
        """视觉不可用 → 尽量用 OCR 兜住；连 OCR 都没有才报错。"""
        ocr_text = self._ocr_fallback(base64_data)
        if ocr_text:
            return ToolResult(
                success=True,
                output=(f"【本地 OCR 识别结果】（{why}，已自动降级到本地 OCR——"
                        f"只有文字，没有版面/元素坐标判断）\n{ocr_text}"),
                metadata={"screenshot_base64": base64_data, "via": "ocr_fallback"},
            )
        return ToolResult(
            success=False, output="",
            error=(f"{why}；本地 OCR 也不可用或没识别到文字。"
                   f"可改用 browser text/html（读 DOM 文本），或用 ocr 工具单独识别图片。"),
        )
