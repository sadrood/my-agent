"""models 包。

采用 PEP 562 惰性导入：`from models import VisionModel` 依然可用，但
**不会**在包初始化时级联加载重依赖。

背景（启动性能）：原先此处 `from .video_analyzer import VideoFrameAnalyzer`
会在任何 `import models.*` 时触发
    models/__init__ → video_analyzer → vision → openai
而 openai SDK 的 __init__ 级联导入大量类型（types.beta / graders / eval 等），
实测 ~1.7s，占 CLI 启动耗时的 84%。改为按需导入后启动从 2.2s 降到 ~0.5s。
"""
from .llm import LLM   # 轻量：llm.py 内部已对 openai 做惰性导入

__all__ = ["LLM", "VisionModel", "VideoFrameAnalyzer"]


def __getattr__(name):
    """PEP 562：首次访问时才导入对应实现。"""
    if name == "VisionModel":
        from .vision import VisionModel
        return VisionModel
    if name == "VideoFrameAnalyzer":
        from .video_analyzer import VideoFrameAnalyzer
        return VideoFrameAnalyzer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
