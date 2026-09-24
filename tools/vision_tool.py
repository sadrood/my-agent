"""
视觉分析工具（SeeTool）。
把原来 Executor 里的 "see" 动作升级为标准工具：模型可以主动请求
"截取当前页面 → 视觉模型分析 → 返回分析文本"，用于 Computer Use 场景。

给 `path` 时改为分析本地图片/视频；视频走"等间隔抽帧 → 多图一次请求"
（上游没有模型支持 video 输入）。

依赖：tools.browser.BrowserTool（截图）+ models.vision.VisionModel（分析）。
视觉模型不可用时返回错误，模型会自行改用 text/html 等方式。
"""
import base64
import os
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

# 提示词统一放 models/prompts.py（AGENTS.md 规则 5）
from models.prompts import VISION_IMAGE_FILE_QUESTION, VISION_PAGE_ANALYSIS_QUESTION as DEFAULT_QUESTION

#: 视频扩展名：走抽帧理解
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".wmv",
              ".mpg", ".mpeg", ".ts", ".3gp")
#: 图片扩展名：直接发原图
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
         ".tif": "image/tiff", ".tiff": "image/tiff"}


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
            "视觉分析工具。不给 path 时截取当前浏览器页面并交给视觉模型分析，"
            "返回页面内容、状态、可交互元素及位置等结构化描述。"
            "当你需要理解页面内容、定位按钮/输入框坐标、判断操作是否成功时使用。"
            "需要先确保浏览器已启动（browser launch）。\n"
            "给了 path 时分析**本地文件**：图片直接看图，视频会等间隔抽帧后按时间"
            "顺序交给视觉模型（上游没有支持视频输入的模型，所以是抽帧理解，"
            "快速切换的画面可能被漏掉）。\n"
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
                },
                "path": {
                    "type": "string",
                    "description": ("本地图片/视频文件路径（省略=截当前浏览器页面）。"
                                    "支持 .mp4/.mov/.mkv/.avi/.webm 等视频与 "
                                    ".png/.jpg/.webp 等图片；相对路径按项目根解析。"),
                },
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
        question = str(arguments.get("question", "") or "").strip()
        path = str(arguments.get("path", "") or "").strip()
        if path:
            return self._run(question, path=path)       # 问法留空 = 走文件默认问法
        return self._run(question or DEFAULT_QUESTION)

    def execute(self, input_str: str) -> ToolResult:
        text = (input_str or "").strip()
        # 字符串接口传进来一个存在的媒体文件路径时，按"看这个文件"处理
        if text and self._is_media_file(text):
            return self._run("", path=text)
        return self._run(text or DEFAULT_QUESTION)

    def _run(self, question: str, path: str = "") -> ToolResult:
        if path:
            return self._run_file(question, path)

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
    # 本地文件（图片 / 视频）
    # ------------------------------------------------------------

    @staticmethod
    def _resolve(path: str) -> str:
        """把传入路径锚定到项目根（与仓库其它工具一致）。"""
        from config import resolve_under_root
        return resolve_under_root(str(path).strip().strip('"').strip("'"))

    def _is_media_file(self, text: str) -> bool:
        """这个字符串是不是"存在的图片/视频文件路径"。"""
        try:
            target = self._resolve(text)
        except Exception:                           # noqa: BLE001
            return False
        if not os.path.isfile(target):
            return False
        ext = os.path.splitext(target)[1].lower()
        return ext in VIDEO_EXTS or ext in IMAGE_EXTS

    def _run_file(self, question: str, path: str) -> ToolResult:
        """分析本地图片/视频文件。"""
        target = self._resolve(path)
        if not os.path.exists(target):
            return ToolResult(success=False, output="", error=f"文件不存在: {target}")
        if not os.path.isfile(target):
            return ToolResult(success=False, output="", error=f"不是文件: {target}")

        ext = os.path.splitext(target)[1].lower()
        name = os.path.basename(target)
        vision = self._get_vision_model()

        if ext in VIDEO_EXTS:
            return self._analyze_video_file(question, target, name, vision)

        if ext not in IMAGE_EXTS:
            return ToolResult(
                success=False, output="",
                error=(f"不支持的文件类型 {ext or '（无扩展名）'}："
                       f"图片支持 {'/'.join(IMAGE_EXTS)}，视频支持 {'/'.join(VIDEO_EXTS)}"))

        # 只要文字的请求优先本地 OCR（离线、免费、不超时）
        if self._wants_text(question):
            ocr_text = self._ocr_file(target)
            if ocr_text:
                return ToolResult(
                    success=True,
                    output=f"【本地 OCR 识别结果】（未调用视觉模型）\n{ocr_text}",
                    metadata={"path": target, "via": "ocr"})

        if vision is None:
            return self._ocr_file_or_error(target, "视觉模型不可用")
        try:
            with open(target, "rb") as fh:
                image_b64 = base64.b64encode(fh.read()).decode()
        except OSError as e:
            return ToolResult(success=False, output="", error=f"读取图片失败: {e}")

        try:
            analysis = vision.analyze(
                image_b64, question or VISION_IMAGE_FILE_QUESTION,
                image_type=_MIME.get(ext, "image/png"), max_tokens=1500)
        except Exception as e:                      # noqa: BLE001
            return self._ocr_file_or_error(target, f"视觉分析失败: {str(e)[:160]}")

        return ToolResult(
            success=True,
            output=(f"【视觉分析结果】（{name}）"
                    f"{getattr(vision, 'fallback_note', lambda: '')()}\n{analysis}"),
            metadata={"path": target, "via": "vision",
                      "vision_model": getattr(vision, "last_model", "")})

    @staticmethod
    def _analyze_video_file(question: str, target: str, name: str,
                            vision) -> ToolResult:
        """视频：等间隔抽帧 → 一次请求交给视觉模型（帧由 analyze_video 清理）。"""
        if vision is None:
            return ToolResult(
                success=False, output="",
                error=("视频理解需要视觉模型，当前不可用；"
                       "可先用 video_edit 的 probe 看时长/分辨率等基本信息。"))
        try:
            analysis = vision.analyze_video(target, question)
        except Exception as e:                      # noqa: BLE001
            return ToolResult(success=False, output="",
                              error=f"视频分析失败: {str(e)[:200]}")
        return ToolResult(
            success=True,
            output=(f"【视频分析结果】（{name}：等间隔抽帧后交给视觉模型）"
                    f"{getattr(vision, 'fallback_note', lambda: '')()}\n{analysis}"),
            metadata={"path": target, "via": "video_frames",
                      "vision_model": getattr(vision, "last_model", "")})

    @staticmethod
    def _ocr_file(path: str) -> str:
        """对图片文件做本地 OCR（失败返回空串，不抛）。"""
        try:
            from models.ocr import auto_fallback_enabled, recognize_image
            if not auto_fallback_enabled():
                return ""
            result = recognize_image(image_path=path)
            if not result.text.strip():
                return ""
            return f"{result.summary()}\n{result.text}"
        except Exception:                           # noqa: BLE001
            return ""

    def _ocr_file_or_error(self, path: str, why: str) -> ToolResult:
        """视觉不可用 → 尽量用 OCR 兜住；连 OCR 都没字才报错。"""
        ocr_text = self._ocr_file(path)
        if ocr_text:
            return ToolResult(
                success=True,
                output=(f"【本地 OCR 识别结果】（{why}，已自动降级到本地 OCR——"
                        f"只有文字，没有画面理解）\n{ocr_text}"),
                metadata={"path": path, "via": "ocr_fallback"})
        return ToolResult(
            success=False, output="",
            error=(f"{why}；本地 OCR 也不可用或没识别到文字。"
                   f"可改用 ocr 工具的 read 操作单独识别，或检查文件是否损坏。"))

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
