"""
语音合成工具（TTSTool）：把文字变成配音 mp3。

用于漫剧/短视频配音：每镜台词 → 独立音频 → 与画面对齐合成。
底层 edge-tts（微软在线语音，免费、中文多音色）。

命令式接口（与 video_edit / video_gen 一致）：
    speak  — 合成一段语音（text 必填）
    voices — 列出可用中文音色
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult


class TTSTool(BaseTool):
    """文字转语音工具（配音）。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"   # 需写入音频文件
    parallel_safe: bool = True                  # 输出文件唯一命名，可并行

    def __init__(self, tts_model=None):
        self._model = tts_model

    @property
    def name(self) -> str:
        return "tts"

    @property
    def description(self) -> str:
        from config import TTS_CONFIG
        from models.tts import ZH_VOICES
        default = TTS_CONFIG.get("voice", "xiaoxiao")
        return (
            "语音合成（配音）工具。把文字转成 mp3 语音，保存到本地并返回路径与时长。"
            "适合给漫剧/短视频配旁白与角色台词。\n"
            f"常用中文音色：{', '.join(ZH_VOICES.keys())}"
            f"（默认 {default}）；也可传完整音色名如 zh-CN-YunxiNeural。\n"
            "用法：tts(command=\"speak\", text=\"台词\", voice=\"yunxi\", rate=\"+0%\")；"
            "tts(command=\"voices\") 查看全部音色。\n"
            "提示：合成后可用 video_edit 的 add_audio 把配音合到画面上；"
            "若配音比画面长，用 video_edit 的 probe 读出两者时长再调整画面时长。"
        )

    @property
    def schema(self) -> dict:
        from models.tts import ZH_VOICES
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["speak", "voices"],
                    "description": "speak=合成语音；voices=列出可用音色",
                },
                "text": {
                    "type": "string",
                    "description": "要合成的文字（台词/旁白）",
                },
                "voice": {
                    "type": "string",
                    "enum": list(ZH_VOICES.keys()) + ["自定义完整音色名"],
                    "description": "音色（默认取配置）；可传 zh-CN-XXXNeural 完整名",
                },
                "rate": {
                    "type": "string",
                    "description": "语速，如 +20% / -10%（默认 +0%）",
                },
                "volume": {
                    "type": "string",
                    "description": "音量，如 +20%（默认 +0%）",
                },
            },
            "required": ["command"],
        }

    # ------------------------------------------------------------

    def _get_model(self):
        if self._model is not None:
            return self._model
        try:
            from models.tts import TTSModel
            self._model = TTSModel()
        except Exception:
            self._model = None
        return self._model

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        cmd = str(arguments.get("command") or "speak").strip().lower()
        if cmd == "voices":
            return self._list_voices()
        if cmd != "speak":
            return ToolResult(success=False, output="",
                              error=f"未知命令: {cmd}（可用: speak / voices）")
        return self._speak(
            text=str(arguments.get("text") or "").strip(),
            voice=arguments.get("voice") or None,
            rate=arguments.get("rate") or None,
            volume=arguments.get("volume") or None,
        )

    def execute(self, input_str: str) -> ToolResult:
        """字符串入口：`speak <台词>` / `voices`。"""
        text = (input_str or "").strip()
        if not text:
            return ToolResult(success=False, output="",
                              error="用法: speak <台词> 或 voices")
        parts = text.split(maxsplit=1)
        if parts[0].lower() == "voices":
            return self._list_voices()
        if parts[0].lower() == "speak" and len(parts) > 1:
            return self._speak(text=parts[1].strip())
        return self._speak(text=text)

    # ------------------------------------------------------------

    def _list_voices(self) -> ToolResult:
        from models.tts import ZH_VOICES
        lines = ["可用中文音色（传简称或完整名均可）:"]
        for k, v in ZH_VOICES.items():
            lines.append(f"  {k:<10} {v}")
        return ToolResult(success=True, output="\n".join(lines))

    def _speak(self, text: str, voice=None, rate=None, volume=None) -> ToolResult:
        if not text:
            return ToolResult(success=False, output="",
                              error="请提供要合成的文字（text），例如："
                                    "tts(command=\"speak\", text=\"从前有座山\")")
        model = self._get_model()
        if model is None:
            return ToolResult(
                success=False, output="",
                error="语音合成模块不可用：请先安装 edge-tts"
                      "（.venv\\Scripts\\python -m pip install edge-tts）。")
        try:
            r = model.synthesize(text, voice=voice, rate=rate, volume=volume)
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"语音合成失败: {str(e)[:300]}")

        path = r.get("path", "")
        dur = 0.0
        try:
            from models.video_edit import VideoEditor, available as _ff
            if _ff():
                dur = VideoEditor().duration(path)
        except Exception:
            pass
        lines = [f"✅ 配音已生成: {path}",
                 f"音色: {r.get('voice')} · 字数: {r.get('chars')}"
                 + (f" · 时长: {dur:.2f}s" if dur else "")]
        return ToolResult(success=True, output="\n".join(lines),
                          metadata={"audio_path": path, "duration": dur,
                                    "voice": r.get("voice"), "text": text})
