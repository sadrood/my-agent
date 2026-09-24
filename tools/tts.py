"""
语音合成工具（TTSTool）：把文字变成配音 mp3。

用于漫剧/短视频配音：每镜台词 → 独立音频 → 与画面对齐合成。
底层本地兜底 TTS（微软在线语音，免费、中文多音色）。

命令式接口（与 video_edit / video_gen 一致）：
    speak  — 合成一段语音（text 必填）
    voices — 列出可用中文音色
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

_ROLE_EXTS = (".wav", ".mp3", ".m4a", ".flac")


def _list_role_files(ref_dir: str) -> list:
    """列出角色声线库（TTS_REFERENCE_DIR）里的角色名。"""
    import os
    if not ref_dir or not os.path.isdir(ref_dir):
        return []
    return sorted(
        fn[: -len(ext)] if fn.lower().endswith(ext) else fn
        for fn in os.listdir(ref_dir)
        for ext in _ROLE_EXTS
        if fn.lower().endswith(ext)
    )


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
        from models.tts import ZH_VOICES, provider_summary
        default = TTS_CONFIG.get("voice", "xiaoxiao")
        provider = str(TTS_CONFIG.get("provider") or "edge").strip().lower()
        head = (
            "语音合成（配音）工具。把文字转成音频文件，保存到本地并返回路径与时长。"
            "适合给漫剧/短视频配旁白与角色台词。\n"
            f"当前供应商：{provider_summary()}\n"
        )
        if provider == "openrouter":
            return (
                head
                + "用法：tts(command=\"speak\", text=\"台词\")"
                "（可传 voice 覆盖；模型不支持时该参数会被忽略）。\n"
                "多角色配音：voice 传角色名（如 voice=\"linshen\"），"
                "会自动命中角色声线库里的同名参考样本做克隆绑定，"
                "同一角色音色恒定不偏移（见 TTS_REFERENCE_DIR）。\n"
                "tts(command=\"voices\") 查看当前供应商与音色说明。\n"
                "提示：合成后用 video_edit 的 add_audio 把配音合到画面上；"
                "若配音比画面长，用 video_edit 的 probe 读出两者时长再调整。"
            )
        return (
            head
            + f"常用中文音色：{', '.join(ZH_VOICES.keys())}"
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
        from config import TTS_CONFIG
        from models.tts import ZH_VOICES, provider_summary
        provider = str(TTS_CONFIG.get("provider") or "edge").strip().lower()
        lines = [f"当前供应商: {provider_summary()}"]
        if provider == "openrouter":
            model = TTS_CONFIG.get("model", "")
            lines.append(f"  模型: {model}")
            lines.append("  可用音色: 由模型决定（fish-audio 无预设音色目录，"
                         "默认音色即内置）")
            ref = TTS_CONFIG.get("reference_audio", "")
            lines.append(f"  声音克隆参考样本: {ref or '未配置（TTS_REFERENCE_AUDIO 可开启）'}")
            ref_dir = TTS_CONFIG.get("reference_dir", "")
            roles = _list_role_files(ref_dir)
            if roles:
                lines.append(f"  角色声线库（{len(roles)} 个角色，"
                             "voice=角色名 启用对应克隆）:")
                for role in roles:
                    lines.append(f"    - {role}")
            else:
                lines.append("  角色声线库: 未配置（TTS_REFERENCE_DIR 指向"
                             "参考样本目录即可启用，如 ./output/.../role_refs）")
            lines.append("  提示：若不传 voice，请求会交由模型默认音色合成。")
            return ToolResult(success=True, output="\n".join(lines))
        lines.append("可用中文音色（传简称或完整名均可）:")
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
                 f"供应商: {r.get('provider')} · 音色/模型: {r.get('voice')}"
                 f" · 字数: {r.get('chars')}"
                 + (f" · 时长: {dur:.2f}s" if dur else "")]
        if r.get("reference"):
            # 角色声线库命中：明示这次用的参考样本，便于核对绑定是否偏移
            lines.append(f"🔒 角色声线绑定: {r['reference']}"
                         "（同角色所有台词都用这份参考样本 → 音色恒定）")
        if r.get("fallback_from"):
            # 降级必须可见：免费档不保证可用性，静默兜底会掩盖真实故障
            lines.append(f"⚠️ 已降级：{r['fallback_from']} 失败，改用 edge-tts 兜底"
                         f"（原因: {r.get('fallback_reason', '')}）")
        return ToolResult(success=True, output="\n".join(lines),
                          metadata={"audio_path": path, "duration": dur,
                                    "voice": r.get("voice"), "text": text,
                                    "provider": r.get("provider"),
                                    "fallback_from": r.get("fallback_from")})
