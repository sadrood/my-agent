"""
视觉分析工具（SeeTool）。
把原来 Executor 里的 "see" 动作升级为标准工具：模型可以主动请求
"截取当前页面 → 视觉模型分析 → 返回分析文本"，用于 Computer Use 场景。

依赖：tools.browser.BrowserTool（截图）+ models.vision.VisionModel（分析）。
视觉模型不可用时返回错误，模型会自行改用 text/html 等方式。
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

DEFAULT_QUESTION = """请分析这个网页截图的内容，并提供以下信息：
1. 页面主要内容（标题、核心信息）
2. 当前页面状态（是否加载完成、是否有错误、是否有弹窗）
3. 可见的交互元素列表（按钮、输入框、链接、下拉菜单等）
4. 当前任务可能需要操作的元素的描述和位置
用中文回答，简洁明了。"""


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
            "需要先确保浏览器已启动（browser launch）。"
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

        # 2. 视觉分析
        vision = self._get_vision_model()
        if vision is None:
            return ToolResult(
                success=False, output="",
                error="视觉模型不可用。请确保配置了支持多模态的 LLM（如 GPT-4o），或改用 browser text/html 命令。",
            )
        try:
            analysis = vision.analyze(base64_data, question, max_tokens=1500)
        except Exception as e:
            return ToolResult(success=False, output="", error=f"视觉分析失败: {e}")

        return ToolResult(
            success=True,
            output=f"【视觉分析结果】\n{analysis}",
            metadata={"screenshot_base64": base64_data},
        )
