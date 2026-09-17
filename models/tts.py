"""
语音合成模块（配音：多供应商）。

用途：漫剧/短视频配音——把每镜台词合成音频，再与画面合成完整视频。

供应商（config.TTS_CONFIG["provider"]）：
- ``edge``       微软 Edge 在线语音（edge-tts）：免费、免 key、中文多音色。
- ``openrouter`` OpenRouter 的 ``/api/v1/audio/speech``（OpenAI 兼容端点），
  可挂 fish-audio 等 TTS 模型。按字符计费，``:free`` 变体 0 元。
  支持**声音克隆**（input_references）的模型可传参考音频。

工具层是同步调用，这里的异步（edge-tts）包成同步。
"""
import base64
import mimetypes
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

SUPPORTED_PROVIDERS = ("edge", "openrouter")


def _run_async(coro):
    """在同步环境里跑异步协程（已有事件循环时另起线程，避免嵌套报错）。"""
    import asyncio
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


def _timestamp() -> str:
    """毫秒精度时间戳。

    旧实现用 [:17] 只保留微秒第 1 位，同一秒内两次合成会得到同名文件互相覆盖。
    """
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:21]


def _data_uri(path: str) -> str:
    """把本地音频读成 ``data:audio/...;base64,...``（OpenRouter 克隆参考样本）。"""
    mime = mimetypes.guess_type(path)[0] or "audio/wav"
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")


class TTSModel:
    """语音合成客户端（edge / openrouter，按配置分派）。"""

    def __init__(
        self,
        voice: str = None,
        rate: str = None,
        volume: str = None,
        save_dir: str = None,
        timeout: float = None,
        provider: str = None,
        model: str = None,
    ):
        cfg = TTS_CONFIG
        self.provider = str(provider or cfg.get("provider") or "edge").strip().lower()
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"未知 TTS 供应商 '{self.provider}'，可选: "
                + " / ".join(SUPPORTED_PROVIDERS))
        # edge 需要把别名解析成完整音色名；openrouter 的音色由模型决定。
        # 关键：openrouter 不能用 TTS_VOICE——那是 edge 的音色别名，
        # 直接透传会被上游拒绝（实测 fish-audio 报 "Invalid voice 'xiaoxiao'"）。
        if self.provider == "edge":
            raw_voice = voice or cfg.get("voice")
            self.voice = resolve_voice(raw_voice)
        else:
            raw_voice = voice or cfg.get("openrouter_voice") or ""
            self.voice, self.voice_ignored = self._sanitize_openrouter_voice(raw_voice)
        self.rate = rate if rate is not None else cfg.get("rate", "+0%")
        self.volume = volume if volume is not None else cfg.get("volume", "+0%")
        self.save_dir = save_dir or cfg.get("save_dir", "./generated_audio")
        self.timeout = float(timeout or cfg.get("timeout", 60))

        self.api_key = cfg.get("openrouter_api_key", "")
        self.base_url = str(cfg.get("openrouter_base_url",
                                    "https://openrouter.ai/api/v1")).rstrip("/")
        self.model = model or cfg.get("model", "fish-audio/s2.1-pro-free:free")
        self.response_format = cfg.get("response_format", "mp3")
        self.referer = cfg.get("referer", "")
        self.title = cfg.get("title", "")
        self.reference_audio = cfg.get("reference_audio", "")
        self.reference_text = cfg.get("reference_text", "")
        self.fallback_edge = bool(cfg.get("fallback_edge", True))

    # ------------------------------------------------------------

    @staticmethod
    def _sanitize_openrouter_voice(name: str):
        """剔除误填的 edge 音色（那是另一套供应商的命名空间）。

        返回 (可用音色, 被忽略的音色)。edge 的 xiaoxiao / zh-CN-XXXNeural 送给
        OpenRouter 只会换来 "Invalid voice" 硬失败；这里直接忽略并留痕，
        让请求按模型默认音色走通，同时把事实回传给调用方，不静默。
        """
        v = (name or "").strip()
        if not v:
            return "", ""
        if v.lower() in ZH_VOICES or v in ZH_VOICES.values():
            return "", v
        return v, ""

    def _target_path(self, output: str = None) -> str:
        if output:
            directory = os.path.dirname(output)
        else:
            directory = self.save_dir
            ext = self.response_format if self.response_format in ("mp3", "wav") else "mp3"
            output = os.path.join(self.save_dir, f"tts-{_timestamp()}.{ext}")
        os.makedirs(directory or ".", exist_ok=True)
        return output

    def synthesize(self, text: str, voice: str = None, rate: str = None,
                   volume: str = None, output: str = None) -> dict:
        """把文字合成为音频文件。

        Returns:
            {"path": 本地音频路径, "voice": 音色/模型, "chars": 字数,
             "text": 原文, "provider": 实际使用的供应商}
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("合成文本为空")

        if self.provider == "openrouter":
            try:
                return self._synth_openrouter(text, voice=voice, output=output)
            except Exception as e:
                if not self.fallback_edge:
                    raise
                # 免费档"不保证生产可用性"，失败时兜底到 edge-tts，
                # 但把降级事实如实回传，不静默掩盖。
                r = self._synth_edge(text, voice=None, rate=rate, volume=volume,
                                     output=output)
                r["fallback_from"] = f"openrouter({self.model})"
                r["fallback_reason"] = str(e)[:200]
                return r
        return self._synth_edge(text, voice=voice, rate=rate, volume=volume,
                                output=output)

    # ------------------------------------------------------------
    # edge-tts
    # ------------------------------------------------------------

    def _synth_edge(self, text: str, voice=None, rate=None, volume=None,
                    output=None) -> dict:
        import edge_tts

        v = resolve_voice(voice) if voice else self.voice or ZH_VOICES["xiaoxiao"]
        r = rate if rate is not None else self.rate
        vol = volume if volume is not None else self.volume
        if not output:
            output = os.path.join(self.save_dir, f"tts-{_timestamp()}.mp3")
        os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

        communicate = edge_tts.Communicate(text, v, rate=r, volume=vol)
        _run_async(communicate.save(output))

        return {"path": output, "voice": v, "chars": len(text), "text": text,
                "provider": "edge"}

    # ------------------------------------------------------------
    # openrouter（/api/v1/audio/speech）
    # ------------------------------------------------------------

    def _payload(self, text: str, voice: str = None) -> dict:
        payload = {
            "model": self.model,
            "input": text,
            "response_format": self.response_format,
        }
        # 音色：只有显式指定才带。fish-audio 这类模型没有预设音色目录，
        # 文档要求"仅在提供方有默认音色时才可省略 voice"——它的默认音色即内置。
        v = voice if voice is not None else self.voice
        if v:
            payload["voice"] = v
        if self.reference_audio and os.path.isfile(self.reference_audio):
            refs = [{"type": "input_audio",
                     "input_audio": {"data": _data_uri(self.reference_audio)}}]
            if self.reference_text:
                refs.append({"type": "text", "text": self.reference_text})
            payload["input_references"] = refs
        return payload

    def _synth_openrouter(self, text: str, voice: str = None,
                          output: str = None) -> dict:
        if not self.api_key:
            raise RuntimeError(
                "未配置 OpenRouter key：请在 .env 设置 TTS_OPENROUTER_API_KEY"
                "（或 OPENROUTER_API_KEY），并确认 TTS_PROVIDER=openrouter")
        import httpx

        output = self._target_path(output)
        url = f"{self.base_url}/audio/speech"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # 官方示例的可选归属头：仅影响 openrouter.ai 排行榜统计，取值随意
        if self.referer:
            headers["HTTP-Referer"] = self.referer
        if self.title:
            headers["X-OpenRouter-Title"] = self.title
        try:
            resp = httpx.post(url, json=self._payload(text, voice), headers=headers,
                              timeout=self.timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"TTS 请求失败: {str(e)[:200]}") from e

        if resp.status_code >= 400:
            raise RuntimeError(self._explain_http_error(resp))

        audio = resp.content
        if not audio:
            raise RuntimeError("TTS 返回空音频（200 但无内容）")
        with open(output, "wb") as f:
            f.write(audio)
        fmt = self.response_format
        if fmt == "pcm":
            # 裸 PCM 不能直接当 mp3 用（播放器会当成噪声），先封成 wav
            output = self._wrap_pcm(output)
        result = {"path": output, "voice": self.model if not self.voice else self.voice,
                  "chars": len(text), "text": text, "provider": "openrouter",
                  "bytes": len(audio)}
        if self.voice_ignored:
            result["voice_ignored"] = self.voice_ignored
        return result

    @staticmethod
    def _explain_http_error(resp) -> str:
        """把 OpenRouter 的错误翻成人话（402/404/401 各有各的坑）。"""
        detail = ""
        try:
            data = resp.json()
            detail = str((data.get("error") or {}).get("message") or data)[:300]
        except Exception:
            detail = resp.text[:300]
        code = resp.status_code
        if code == 402:
            return (f"TTS 失败 HTTP 402（余额不足）: {detail} —— "
                    "付费模型需要账户有额度；可改用 :free 变体"
                    "（如 fish-audio/s2.1-pro-free:free）")
        if code == 404:
            return (f"TTS 失败 HTTP 404: {detail} —— "
                    "模型名可能有误，或该模型当前没有可用供应商端点")
        if code == 401:
            return f"TTS 失败 HTTP 401（认证失败）: {detail} —— 检查 OpenRouter key"
        if code == 429:
            return (f"TTS 失败 HTTP 429（限流）: {detail} —— "
                    "免费档限流较紧，可稍后重试或降低并发")
        return f"TTS 失败 HTTP {code}: {detail}"

    def _wrap_pcm(self, path: str) -> str:
        """把裸 PCM 包成 wav（response_format=pcm 时避免产出"假 mp3"）。"""
        import wave
        raw = path[:-4] + ".pcm" if path.endswith(".mp3") else path + ".pcm"
        os.replace(path, raw)
        wav_path = raw[:-4] + ".wav"
        with open(raw, "rb") as f:
            data = f.read()
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(data)
        try:
            os.remove(raw)
        except OSError:
            pass
        return wav_path


def is_configured() -> bool:
    """TTS 是否可用（按当前供应商判断依赖是否齐备）。"""
    if not TTS_CONFIG.get("enabled"):
        return False
    provider = str(TTS_CONFIG.get("provider") or "edge").strip().lower()
    if provider == "openrouter":
        return bool(TTS_CONFIG.get("openrouter_api_key"))
    try:
        import edge_tts  # noqa: F401
        return True
    except ImportError:
        return False


def provider_summary() -> str:
    """一行描述当前供应商（工具 description / voices 命令复用）。"""
    provider = str(TTS_CONFIG.get("provider") or "edge").strip().lower()
    if provider == "openrouter":
        model = TTS_CONFIG.get("model", "")
        return f"openrouter（模型 {model}，按字符计费；:free 变体 0 元）"
    return "edge（微软 Edge 在线语音，免费多音色）"
