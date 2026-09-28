"""models 包。
**不会**在包初始化时级联加载重依赖。"""
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
