"""
语音合成模块（edge-tts：微软 Edge 在线语音，免费、中文多音色）。

用途：漫剧/短视频配音——把每镜台词合成音频，再与画面合成完整视频。
底层用 edge-tts 的异步接口，这里包成同步方法（工具层是同步调用）。

配置：config.TTS_CONFIG（环境变量 TTS_*）。
"""
import asyncio
import os
from datetime import datetime
from typing import Optional

from config import TTS_CONFIG

# 中文常用音色（edge-tts 命名：zh-CN-<名字>Neural）
ZH_VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",   # 女声·温柔（默认）
    "xiaoyi": "zh-CN-XiaoyiNeural",       # 女声·活泼
    "yunxi": "zh-CN-YunxiNeural",         # 男声·年轻
    "yunjian": "zh-CN-YunjianNeural",     # 男声·沉稳（解说感）
    "yunyang": "zh-CN-YunyangNeural",     # 男声·新闻播报
    "yunxia": "zh-CN-YunxiaNeural",       # 男声·少年
    "liaoning": "zh-CN-liaoning-XiaobeiNeural",  # 东北话女声
    "shaanxi": "zh-CN-shaanxi-XiaoniNeural",     # 陕西话女声
}


def _run_async(coro):
    """在同步环境里跑异步协程（已有事件循环时另起线程，避免嵌套报错）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def resolve_voice(name: str) -> str:
    """把简短别名（yunxi）或完整名（zh-CN-YunxiNeural）解析成 edge-tts 音色名。"""
    if not name:
        return ZH_VOICES["xiaoxiao"]
    key = str(name).strip().lower()
    if key in ZH_VOICES:
        return ZH_VOICES[key]
    # 已是完整音色名（含区域前缀）→ 原样使用
    return str(name).strip()


class TTSModel:
    """语音合成客户端（edge-tts）。"""

    def __init__(
        self,
        voice: str = None,
        rate: str = None,
        volume: str = None,
        save_dir: str = None,
        timeout: float = None,
    ):
        cfg = TTS_CONFIG
        self.voice = resolve_voice(voice or cfg.get("voice"))
        self.rate = rate if rate is not None else cfg.get("rate", "+0%")
        self.volume = volume if volume is not None else cfg.get("volume", "+0%")
        self.save_dir = save_dir or cfg.get("save_dir", "./generated_audio")
        self.timeout = float(timeout or cfg.get("timeout", 60))

    # ------------------------------------------------------------

    def synthesize(self, text: str, voice: str = None, rate: str = None,
                   volume: str = None, output: str = None) -> dict:
        """把文字合成为 mp3。

        Returns:
            {"path": 本地 mp3 路径, "voice": 音色, "chars": 字数, "text": 原文}
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("合成文本为空")

        import edge_tts

        v = resolve_voice(voice) if voice else self.voice
        r = rate if rate is not None else self.rate
        vol = volume if volume is not None else self.volume

        directory = os.path.dirname(output) if output else self.save_dir
        os.makedirs(directory or ".", exist_ok=True)
        if not output:
            # 毫秒精度（%f 前 3 位）：旧实现用 [:17] 只保留微秒第 1 位，
            # 同一秒内两次合成会得到同名文件而互相覆盖。
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:21]
            output = os.path.join(self.save_dir, f"tts-{ts}.mp3")

        communicate = edge_tts.Communicate(text, v, rate=r, volume=vol)
        _run_async(communicate.save(output))

        return {"path": output, "voice": v, "chars": len(text), "text": text}


def is_configured() -> bool:
    """TTS 是否可用（enabled 且 edge-tts 已安装）。"""
    if not TTS_CONFIG.get("enabled"):
        return False
    try:
        import edge_tts  # noqa: F401
        return True
    except ImportError:
        return False
