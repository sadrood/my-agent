"""
视频剪辑工具（VideoEditTool）：ffmpeg 合成能力。

"图 + 运镜 + 配音"路线的执行者——用静态图做漫剧/短视频，不消耗
视频生成配额。与 video_gen（AI 生成视频）职责分离：本工具只管剪辑合成。

命令：
    kenburns  图片 → 运镜短视频（推拉摇移）
    concat    多段视频 → 拼接
    add_audio 视频 + 配音 → 合成
    trim      裁剪片段
    probe     读取时长/分辨率（对齐画面与配音）
    subtitle  烧录字幕
"""
from typing import Any, Dict, List

from tools.base import BaseTool, ToolResult


class VideoEditTool(BaseTool):
    """视频剪辑与合成工具（ffmpeg）。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"   # 需写入视频文件

    def __init__(self, editor=None):
        self._editor = editor

    @property
    def name(self) -> str:
        return "video_edit"

    @property
    def description(self) -> str:
        from config import VIDEO_EDIT_CONFIG
        w = VIDEO_EDIT_CONFIG.get("width", 1280)
        h = VIDEO_EDIT_CONFIG.get("height", 720)
        fps = VIDEO_EDIT_CONFIG.get("fps", 25)
        return (
            "视频剪辑工具（ffmpeg），用于把素材合成为完整视频。\n"
            f"输出统一规格 {w}x{h} @ {fps}fps（便于直接拼接，无需重编码）。\n"
            "命令：\n"
            "  kenburns <图片> [motion] [duration] — 静态图 → 带推拉摇移的短视频\n"
            "      运镜: zoom_in / zoom_out / pan_left / pan_right / pan_up / "
            "pan_down / static\n"
            "      **做漫剧/图文视频的主力命令**：不消耗视频生成配额，画面可控\n"
            "  concat — 按顺序拼接多段视频（自动尝试无损，参数不一致回退重编码）\n"
            "  add_audio — 给视频叠配音/BGM（音轨短于画面时自动补静音，不截断画面）\n"
            "  trim — 裁剪片段；probe — 读时长/分辨率；subtitle — 烧录 SRT 字幕\n\n"
            "典型漫剧流程：image_gen 出图 → 逐镜 kenburns（配时长）→ tts 出配音 → "
            "add_audio 逐镜配音 → concat 合成 → 得到完整视频。\n"
            "注意：concat 要求各段规格一致，kenburns 产出已统一；"
            "若混用 video_gen 的 AI 视频，concat 会自动回退重编码（较慢）。"
        )

    @property
    def schema(self) -> dict:
        from models.video_edit import VideoEditor
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["kenburns", "concat", "add_audio", "trim",
                             "probe", "subtitle"],
                    "description": "要执行的剪辑命令",
                },
                "image": {
                    "type": "string",
                    "description": "kenburns 用：图片路径",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "批量 kenburns：多张图片依次做运镜（各段等长）",
                },
                "videos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "concat 用：按顺序拼接的视频路径列表",
                },
                "video": {
                    "type": "string",
                    "description": "add_audio / trim / probe / subtitle 用：视频路径",
                },
                "audio": {
                    "type": "string",
                    "description": "add_audio 用：配音/音乐路径",
                },
                "srt": {
                    "type": "string",
                    "description": "subtitle 用：SRT 字幕文件路径",
                },
                "motion": {
                    "type": "string",
                    "enum": list(VideoEditor.MOTIONS),
                    "description": "运镜类型（默认 zoom_in）",
                },
                "duration": {
                    "type": "number",
                    "description": "kenburns 单镜时长（秒，默认 4）；trim 的裁剪时长",
                },
                "start": {
                    "type": "number",
                    "description": "trim 起点（秒，默认 0）",
                },
                "end": {
                    "type": "number",
                    "description": "trim 终点（秒）",
                },
                "motions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "批量 kenburns：与 images 一一对应的运镜（可选，"
                                   "默认按顺序轮换不同运镜以增加节奏感）",
                },
                "replace": {
                    "type": "boolean",
                    "description": "add_audio：是否替换原音轨（默认 true）",
                },
                "volume": {
                    "type": "number",
                    "description": "add_audio：音量倍率（默认 1.0）",
                },
                "output": {
                    "type": "string",
                    "description": "输出路径（默认自动命名到生成目录）",
                },
            },
            "required": ["command"],
        }

    # ------------------------------------------------------------

    def _get_editor(self):
        if self._editor is not None:
            return self._editor
        try:
            from models.video_edit import VideoEditor
            self._editor = VideoEditor()
        except Exception:
            self._editor = None
        return self._editor

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        from models.video_edit import VideoEditError, available
        if not available():
            return ToolResult(
                success=False, output="",
                error="未找到 ffmpeg/ffprobe，剪辑功能不可用。安装方式："
                      "winget install --id Gyan.FFmpeg -e --source winget"
                      "（或在 .env 设置 FFMPEG_PATH 指向其 bin 目录）。")

        cmd = str(arguments.get("command") or "").strip().lower()
        editor = self._get_editor()
        if editor is None:
            return ToolResult(success=False, output="",
                              error="视频剪辑模块不可用（导入失败）。")
        try:
            if cmd == "kenburns":
                return self._kenburns(editor, arguments)
            if cmd == "concat":
                r = editor.concat(arguments.get("videos") or [],
                                  output=arguments.get("output") or None)
                return self._ok(f"✅ 已拼接 {r['parts']} 段 → {r['path']}"
                                f"（{r['mode']}，总时长 {r['duration']:.2f}s）", r)
            if cmd == "add_audio":
                r = editor.add_audio(
                    str(arguments.get("video") or ""),
                    str(arguments.get("audio") or ""),
                    output=arguments.get("output") or None,
                    replace=arguments.get("replace", True) is not False,
                    volume=float(arguments.get("volume") or 1.0),
                )
                note = "（音轨已补齐到画面时长）" if r.get("padded") else ""
                return self._ok(f"✅ 已合成配音 → {r['path']}"
                                f"（时长 {r['duration']:.2f}s）{note}", r)
            if cmd == "trim":
                r = editor.trim(str(arguments.get("video") or ""),
                                start=float(arguments.get("start") or 0.0),
                                end=arguments.get("end"),
                                duration=arguments.get("duration"),
                                output=arguments.get("output") or None)
                return self._ok(f"✅ 已裁剪 → {r['path']}"
                                f"（时长 {r['duration']:.2f}s）", r)
            if cmd == "probe":
                info = editor.probe(str(arguments.get("video") or ""))
                return self._ok(
                    f"时长 {info['duration']:.2f}s · {info['width']}x{info['height']}"
                    f" · 音轨: {'有' if info['has_audio'] else '无'}"
                    f" · {info['size_bytes'] / 1024 / 1024:.2f}MB", info)
            if cmd == "subtitle":
                r = editor.subtitle(str(arguments.get("video") or ""),
                                    str(arguments.get("srt") or ""),
                                    output=arguments.get("output") or None)
                return self._ok(f"✅ 已烧录字幕 → {r['path']}", r)
            return ToolResult(success=False, output="",
                              error=f"未知命令: {cmd}（可用: kenburns / concat / "
                                    f"add_audio / trim / probe / subtitle）")
        except VideoEditError as e:
            return ToolResult(success=False, output="", error=str(e)[:400])
        except Exception as e:
            return ToolResult(success=False, output="", error=f"剪辑失败: {str(e)[:300]}")

    def execute(self, input_str: str) -> ToolResult:
        """字符串入口：`kenburns <图片>` 等（简易解析）。"""
        parts = (input_str or "").strip().split(maxsplit=1)
        if not parts:
            return ToolResult(success=False, output="",
                              error="用法: kenburns <图片路径> 或 concat <v1> <v2> ...")
        cmd = parts[0].lower()
        rest = (parts[1] if len(parts) > 1 else "").split()
        args: Dict[str, Any] = {"command": cmd}
        if cmd == "kenburns" and rest:
            args["image"] = rest[0]
            if len(rest) > 1:
                args["motion"] = rest[1]
            if len(rest) > 2:
                try:
                    args["duration"] = float(rest[2])
                except ValueError:
                    pass
        elif cmd == "concat":
            args["videos"] = rest
        elif cmd in ("add_audio", "trim", "probe", "subtitle") and rest:
            args["video"] = rest[0]
            if cmd == "add_audio" and len(rest) > 1:
                args["audio"] = rest[1]
            if cmd == "subtitle" and len(rest) > 1:
                args["srt"] = rest[1]
        return self.execute_json(args)

    # ------------------------------------------------------------

    @staticmethod
    def _ok(text: str, metadata: dict) -> ToolResult:
        return ToolResult(success=True, output=text, metadata=metadata)

    def _kenburns(self, editor, arguments: Dict[str, Any]) -> ToolResult:
        """单图或批量（多图依次运镜后可直接拼接）。"""
        default_motion_cycle = ["zoom_in", "pan_right", "zoom_out", "pan_left"]
        duration = arguments.get("duration")
        images: List[str] = [str(i) for i in (arguments.get("images") or []) if i]
        single = arguments.get("image")
        if single and not images:
            images = [str(single)]
        if not images:
            return ToolResult(success=False, output="",
                              error="请提供 image（单图）或 images（多图）")

        motions = [str(m) for m in (arguments.get("motions") or []) if m]
        results = []
        for i, img in enumerate(images):
            motion = (motions[i] if i < len(motions)
                      else (arguments.get("motion")
                            or default_motion_cycle[i % len(default_motion_cycle)]))
            r = editor.kenburns(img, duration=duration or 4.0, motion=motion)
            results.append(r)

        if len(results) == 1:
            r = results[0]
            return self._ok(
                f"✅ 已生成运镜视频 → {r['path']}"
                f"（{r['motion']}，{r['duration']:.2f}s）", r)

        lines = [f"✅ 已生成 {len(results)} 段运镜视频："]
        for r in results:
            lines.append(f"  - [{r['motion']}] {r['path']} ({r['duration']:.2f}s)")
        paths = [r["path"] for r in results]
        lines.append("可用 video_edit(command=\"concat\", videos=[...]) 拼接为完整视频。")
        return ToolResult(success=True, output="\n".join(lines),
                          metadata={"videos": paths, "parts": len(paths)})
