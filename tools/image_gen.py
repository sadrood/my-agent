"""
文生图工具（ImageGenTool）。

调用商汤 SenseNova U1.5 Lite（sensenova-u1.5-lite）的 OpenAI 兼容
/images/generations 端点生成图片，解码保存到本地目录，返回文件路径。

依赖：models.image_gen.ImageGenModel。
配置：IMAGE_GEN_*（.env），默认端点 https://token.sensenova.cn/v1。
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

# 实测端点支持的标准尺寸
SIZE_CHOICES = ["1024x1024", "768x1024", "1024x768", "1280x720", "720x1280"]


class ImageGenTool(BaseTool):
    """文生图工具：根据文字描述生成图片并保存为本地文件。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"   # 生成图片需写入文件
    parallel_safe: bool = True                  # 输出文件唯一命名，互不冲突，可并行

    def __init__(self, image_model=None):
        """
        Args:
            image_model: ImageGenModel 实例（None 时延迟创建；测试注入用）
        """
        self._model = image_model

    @property
    def name(self) -> str:
        return "image_gen"

    @property
    def description(self) -> str:
        from models.prompts import IMAGE_GEN_PROMPT_GUIDE
        return (
            "文生图工具。调用 SenseNova U1.5 Lite 图像生成模型，根据文字描述"
            "生成图片（插画/海报/信息图/示意图），保存为本地 PNG/JPG 文件并"
            "返回文件路径。适合需要配图、封面、可视化示意图的任务。"
            "提示词要求：" + IMAGE_GEN_PROMPT_GUIDE
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "图片内容描述（中文）。写清主体、场景、风格、构图、光影与配色",
                },
                "size": {
                    "type": "string",
                    "enum": SIZE_CHOICES,
                    "description": "输出尺寸，默认 1024x1024",
                },
                "n": {
                    "type": "integer",
                    "description": "生成数量 1-4，默认 1",
                },
            },
            "required": ["prompt"],
        }

    # ------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------

    def _get_model(self):
        if self._model is not None:
            return self._model
        try:
            from models.image_gen import ImageGenModel
            self._model = ImageGenModel()
        except Exception:
            self._model = None
        return self._model

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        prompt = str(arguments.get("prompt") or "").strip()
        size = arguments.get("size") or None
        try:
            n = int(arguments.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        return self._run(prompt, size=size, n=n)

    def execute(self, input_str: str) -> ToolResult:
        return self._run((input_str or "").strip())

    def _run(self, prompt: str, size: str = None, n: int = 1) -> ToolResult:
        if not prompt:
            return ToolResult(success=False, output="",
                              error="请提供图片描述（prompt），例如：一只橘猫坐在窗台上晒太阳，暖色光线，插画风格。")
        model = self._get_model()
        if model is None:
            return ToolResult(success=False, output="",
                              error="图像生成模块不可用（导入失败）。")
        if not getattr(model, "api_key", ""):
            return ToolResult(success=False, output="",
                              error="未配置图像生成 API key：请在 .env 设置 IMAGE_GEN_API_KEY（或复用主 LLM key）。")
        try:
            result = model.generate(prompt, size=size, n=n)
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"图像生成失败: {str(e)[:200]}")
        paths = result.get("images") or []
        lines = [
            f"已生成 {len(paths)} 张图片（{result.get('model')} · {result.get('size')}）:"
        ]
        lines += [f"- {p}" for p in paths]
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={"images": paths, "prompt": prompt},
        )
