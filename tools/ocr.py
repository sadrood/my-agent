"""OCR 工具（ocr）：从截图/图片里读文字，**不经过视觉大模型**。

为什么单独做一个工具：用户的原话是"缺少截图识别文字的能力，总是依赖视觉模型，
视觉模型无响应就废了"。视觉模型擅长理解版面与语义，但识字这件事本地引擎就能做，
而且离线、免费、不会因为上游超时或"该模型不支持图片输入"而整个卡死。

用法：
    ocr <图片路径>      识别指定图片里的文字
    ocr                 识别最近一张截图（screenshots/ 等目录下最新的 png/jpg）
    ocr engines         列出本机可用的 OCR 后端
    ocr lang            列出 Windows OCR 支持的语言

产物：识别文本直接返回（不落盘），中文空格已归一化。
"""
import glob
import os
from typing import Any, Dict, List

from tools.base import BaseTool, ToolResult

_HELP = """OCR 工具（本地识字，不依赖视觉模型）：
  ocr <图片路径>     识别图片里的文字（png/jpg/bmp/webp）
  ocr                识别最近一张截图（自动找 screenshots/ 与 generated_images/ 下最新的图）
  ocr engines        本机可用的 OCR 后端
  ocr lang           Windows OCR 支持的语言
说明：中文结果已做空格归一（"系 统 提 示" → "系统提示"）；英文单词间空格保留。
      需要"看懂版面/找按钮坐标"时仍用 see（视觉模型），本工具只负责把字读出来。"""

#: 找"最近一张截图"时按顺序扫的目录（相对项目根/绝对路径都支持）
_SCREENSHOT_DIRS = ("screenshots", "generated_images", "generated_images/computer", "output")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")


class OcrTool(BaseTool):
    """本地 OCR：截图/图片取字（零依赖优先用系统引擎）。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"
    parallel_safe: bool = True          # 只读本地图片，无共享状态

    @property
    def name(self) -> str:
        return "ocr"

    @property
    def description(self) -> str:
        return (
            "OCR 工具：把截图/图片里的**文字**读出来（本地引擎，不调用视觉大模型）。\n"
            "  ocr <图片路径>   识别指定图片\n"
            "  ocr              识别最近一张截图\n"
            "  ocr engines      查看可用后端\n"
            "什么时候用它：需要图片/截图里的文字内容（报错信息、表格、按钮名字、弹窗文案）时，"
            "先用它——快、离线、不耗视觉模型配额；只有当你要**看懂版面、定位元素坐标、判断界面状态**"
            "时才用 see（视觉模型）。视觉模型不可用或超时时，也应该改用它把字读出来。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["read", "engines", "lang"],
                              "description": "read=识别图片文字；engines=可用后端；lang=支持的语言"},
                "path": {"type": "string", "description": "read 的图片路径（可省略=用最近一张截图）"},
            },
            "required": ["operation"],
        }

    # ------------------------------------------------------------

    @staticmethod
    def _latest_screenshot() -> str:
        from config import resolve_under_root

        candidates: List[tuple] = []
        for d in _SCREENSHOT_DIRS:
            full = resolve_under_root(d)
            if not os.path.isdir(full):
                continue
            for ext in _IMAGE_EXT:
                for p in glob.glob(os.path.join(full, f"*{ext}")):
                    try:
                        candidates.append((os.path.getmtime(p), p))
                    except OSError:
                        continue
        if not candidates:
            return ""
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _engines_text(self) -> str:
        from models.ocr import OcrEngine, auto_fallback_enabled, ocr_available

        engine = OcrEngine()
        usable = engine.available_backends()
        lines = ["本地 OCR 后端："]
        for name in OcrEngine.BACKENDS:
            mark = "✓ 可用" if name in usable else "✗ 不可用"
            hint = {
                "windows": "Windows.Media.Ocr（系统自带，零安装）",
                "rapidocr": "rapidocr-onnxruntime（pip 安装即启用）",
                "tesseract": "tesseract 可执行文件 + pytesseract",
            }.get(name, "")
            lines.append(f"  [{mark}] {name:10s} {hint}")
        lines.append(f"  解析后端: {engine.resolve_backend() if usable else '（无可用后端）'}")
        lines.append(f"  识别语言: {engine.languages}")
        lines.append(f"  自动降级（视觉失败→OCR）: "
                     f"{'开' if auto_fallback_enabled() else '关'}")
        if not ocr_available():
            lines.append("  提示：Windows 上若不可用，检查是否装了「中文(简体)」语言的可选功能；"
                         "或 pip install rapidocr-onnxruntime")
        return "\n".join(lines)

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        from models.ocr import OcrError, OcrEngine

        op = str(arguments.get("operation", "read") or "read").strip().lower()
        if op in ("engines", "backends"):
            return ToolResult(success=True, output=self._engines_text())
        if op == "lang":
            langs = OcrEngine().languages_available()
            return ToolResult(
                success=True,
                output="Windows OCR 可用语言: " + (", ".join(langs) if langs else
                                                  "（非 Windows 或不可用；其它后端按自身模型）"))
        if op not in ("read", ""):
            return ToolResult(success=False, output="",
                              error=f"未知操作 {op!r}（read / engines / lang）")

        path = str(arguments.get("path", "") or "").strip()
        if not path:
            path = self._latest_screenshot()
            if not path:
                return ToolResult(
                    success=False, output="",
                    error="没找到可识别的图片：请给出 path，或先截图"
                          "（browser screenshot / computer screenshot）。")
        try:
            result = OcrEngine().recognize(path)
        except OcrError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:                  # noqa: BLE001
            return ToolResult(success=False, output="",
                              error=f"OCR 异常: {type(e).__name__}: {str(e)[:200]}")

        if not result.text.strip():
            return ToolResult(
                success=False, output="",
                error=f"没有识别到文字（{result.summary()}）。若图里确实有字，"
                      f"可能是语言包不匹配（当前 {OcrEngine().languages}）或图片太小。")
        return ToolResult(
            success=True,
            output=f"【OCR 识别结果】{result.summary()}\n{result.text}",
            metadata={"engine": result.engine, "chars": result.chars,
                      "lines": result.lines, "image": result.image},
        )

    def execute(self, input_str: str) -> ToolResult:
        """文本协议：ocr <路径> / ocr engines / ocr lang / ocr"""
        text = (input_str or "").strip()
        if not text:
            return self.execute_json({"operation": "read"})
        first, _, rest = text.partition(" ")
        if first.lower() in ("engines", "backends"):
            return self.execute_json({"operation": "engines"})
        if first.lower() in ("lang", "languages"):
            return self.execute_json({"operation": "lang"})
        if first.lower() in ("help", "?"):
            return ToolResult(success=True, output=_HELP)
        if first.lower() == "read":
            text = rest.strip()
            if not text:
                return self.execute_json({"operation": "read"})
        return self.execute_json({"operation": "read", "path": text})
