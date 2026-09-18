"""
视频剪辑与合成模块（ffmpeg）。

用于"图 + 运镜 + 配音"路线（漫剧/短视频）：
    kenburns   静态图 → 带推拉摇移的短视频（零视频配额消耗）
    concat     多段视频 → 拼接为一个完整视频
    add_audio  给视频叠配音/BGM（并可选补齐时长差）
    trim       裁剪片段
    probe      读取时长/分辨率（用于对齐画面与配音）
    subtitle   烧录字幕（可选）

统一输出规格（保证片段可直接拼接，无需重编码）：
    分辨率 1280x720（可配）、25fps、libx264、yuv420p、aac。

依赖：ffmpeg / ffprobe（PATH 或 FFMPEG_PATH 环境变量）。
配置：config.VIDEO_EDIT_CONFIG（环境变量 VIDEO_EDIT_*）。
"""
import json
import os
import shutil
import subprocess
from datetime import datetime
from typing import List, Optional

from config import VIDEO_EDIT_CONFIG, resolve_under_root

# static-ffmpeg 探测结果缓存（该调用有开销，且可能触发下载）
_STATIC_BIN_CACHE: dict = {}


def _find_binary(name: str) -> Optional[str]:
    """定位 ffmpeg/ffprobe，按可靠性依次尝试：

    1. 显式配置（FFMPEG_PATH 环境变量 / VIDEO_EDIT_CONFIG.ffmpeg_path）
    2. PATH
    3. static-ffmpeg（pip 包，自带二进制；winget 在受限网络下常卡住，这是可靠回退）
    4. winget 安装目录（Gyan.FFmpeg）
    """
    explicit = os.getenv("FFMPEG_PATH", "") or VIDEO_EDIT_CONFIG.get("ffmpeg_path", "")
    if explicit:
        cand = os.path.join(explicit, name + (".exe" if os.name == "nt" else ""))
        if os.path.exists(cand):
            return cand
        if os.path.exists(explicit) and explicit.lower().endswith(name + ".exe"):
            return explicit

    found = shutil.which(name)
    if found:
        return found

    # static-ffmpeg：pip 安装的静态构建（结果缓存，避免重复探测开销）
    cached = _STATIC_BIN_CACHE.get(name)
    if cached:
        return cached
    try:
        from static_ffmpeg import run as _sf
        ff, fp = _sf.get_or_fetch_platform_executables_else_raise()
        cand = ff if name == "ffmpeg" else fp
        if cand and os.path.exists(cand):
            _STATIC_BIN_CACHE[name] = cand
            return cand
    except Exception:
        pass

    # winget 安装位置（Gyan.FFmpeg）
    if os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Microsoft", "WinGet", "Packages")
        if os.path.isdir(base):
            for root, dirs, files in os.walk(base):
                if name + ".exe" in files and "ffmpeg" in root.lower():
                    return os.path.join(root, name + ".exe")
                if root.count(os.sep) - base.count(os.sep) > 4:
                    dirs[:] = []
    return None


def ffmpeg_path() -> Optional[str]:
    return _find_binary("ffmpeg")


def ffprobe_path() -> Optional[str]:
    return _find_binary("ffprobe")


def available() -> bool:
    """剪辑能力是否可用（ffmpeg 与 ffprobe 都在）。"""
    return bool(ffmpeg_path() and ffprobe_path())


class VideoEditError(RuntimeError):
    pass


class VideoEditor:
    """ffmpeg 封装：运镜生成 / 拼接 / 配音合成 / 裁剪 / 探测。"""

    # 运镜类型
    MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right",
               "pan_up", "pan_down", "static")

    def __init__(self, width: int = None, height: int = None,
                 fps: int = None, save_dir: str = None, timeout: float = None):
        cfg = VIDEO_EDIT_CONFIG
        self.width = int(width or cfg.get("width", 1280))
        self.height = int(height or cfg.get("height", 720))
        self.fps = int(fps or cfg.get("fps", 25))
        self.save_dir = resolve_under_root(
            save_dir or cfg.get("save_dir", "./generated_videos"))
        self.timeout = float(timeout or cfg.get("timeout", 600))

    # ------------------------------------------------------------
    # 底层
    # ------------------------------------------------------------

    def _run(self, args: List[str], timeout: float = None) -> str:
        exe = ffmpeg_path()
        if not exe:
            raise VideoEditError(
                "未找到 ffmpeg。请安装（winget install Gyan.FFmpeg）"
                "或在 .env 设置 FFMPEG_PATH 指向其 bin 目录。")
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"] + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout or self.timeout,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise VideoEditError(f"ffmpeg 执行超时（>{timeout or self.timeout}s）") from e
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-6:]
            raise VideoEditError("ffmpeg 失败: " + " | ".join(tail)[:400])
        return proc.stdout or ""

    def _out(self, prefix: str, ext: str = ".mp4") -> str:
        os.makedirs(self.save_dir, exist_ok=True)
        # 毫秒精度：旧实现用 [:17] 只保留微秒第 1 位，同秒内多次生成会互相覆盖
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:21]
        return os.path.join(self.save_dir, f"{prefix}-{ts}{ext}")

    # ------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------

    def probe(self, path: str) -> dict:
        """读取媒体信息：时长(秒)、宽、高、是否有音轨。"""
        exe = ffprobe_path()
        if not exe:
            raise VideoEditError("未找到 ffprobe（随 ffmpeg 一起安装）")
        cmd = [exe, "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", path]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=60, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise VideoEditError("ffprobe 超时") from e
        if proc.returncode != 0:
            raise VideoEditError(f"ffprobe 失败: {(proc.stderr or '')[:200]}")
        data = json.loads(proc.stdout or "{}")
        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        return {
            "duration": float(fmt.get("duration") or 0.0),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "has_audio": bool(audio),
            "size_bytes": int(fmt.get("size") or 0),
            # 音轨规格（T5 校验用：混音后必须是 44100Hz 立体声）
            "audio_sample_rate": int(audio.get("sample_rate") or 0),
            "audio_channels": int(audio.get("channels") or 0),
        }

    def duration(self, path: str) -> float:
        try:
            return float(self.probe(path).get("duration") or 0.0)
        except Exception:
            return 0.0

    # ------------------------------------------------------------
    # 图 → 运镜视频（Ken Burns）
    # ------------------------------------------------------------

    def kenburns(self, image: str, duration: float = 4.0,
                 motion: str = "zoom_in", output: str = None) -> dict:
        """把一张静态图变成带推拉摇移的短视频。

        这是"图 + 运镜"路线的核心：不消耗视频生成配额，成本近乎为零，
        且画面完全由你（图像模型 + 参考图）控制，角色一致性更好。

        motion: zoom_in / zoom_out / pan_left / pan_right / pan_up /
                pan_down / static
        """
        if not os.path.exists(image):
            raise VideoEditError(f"图片不存在: {image}")
        motion = (motion or "zoom_in").strip().lower()
        if motion not in self.MOTIONS:
            raise VideoEditError(
                f"不支持的运镜: {motion}（可选: {', '.join(self.MOTIONS)}）")
        try:
            duration = max(0.5, float(duration))
        except (TypeError, ValueError):
            duration = 4.0

        fps = self.fps
        frames = max(2, int(duration * fps))
        # 按输入图方向自动选输出规格：竖图→竖屏（720x1280），横图→默认（1280x720）。
        # 之前写死 self.width x self.height，竖图会被压扁/裁切成横屏（漫剧全是 9:16）。
        w, h = self.width, self.height
        try:
            info = self.probe(image)
            iw, ih = int(info.get("width") or 0), int(info.get("height") or 0)
            # 竖图 + 当前配置为横屏 → 交换为竖屏规格；其余情况（横图/已竖屏/探测失败）保持原样
            if iw > 0 and ih > 0 and ih > iw and w > h:
                w, h = h, w  # 竖图且默认配置为横屏时，交换得到竖屏规格
        except Exception:
            pass  # 探测失败（如测试假图）回退默认规格

        cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"

        if motion == "zoom_in":
            z = f"min(zoom+{0.5 / frames:.6f},1.5)"
            expr = f"zoompan=z='{z}':x='{cx}':y='{cy}':d={frames}:s={w}x{h}:fps={fps}"
        elif motion == "zoom_out":
            z = f"if(lte(zoom,1.0),1.5,max(1.001,zoom-{0.5 / frames:.6f}))"
            expr = f"zoompan=z='{z}':x='{cx}':y='{cy}':d={frames}:s={w}x{h}:fps={fps}"
        elif motion in ("pan_left", "pan_right", "pan_up", "pan_down"):
            # 先固定放大，再沿线平移（进度 on/(d-1)）
            z = "1.2"
            if motion == "pan_left":
                x, y = f"'(iw-iw/zoom)*(1-on/{frames - 1})'", f"'{cy}'"
            elif motion == "pan_right":
                x, y = f"'(iw-iw/zoom)*on/{frames - 1}'", f"'{cy}'"
            elif motion == "pan_up":
                x, y = f"'{cx}'", f"'(ih-ih/zoom)*(1-on/{frames - 1})'"
            else:  # pan_down
                x, y = f"'{cx}'", f"'(ih-ih/zoom)*on/{frames - 1}'"
            expr = f"zoompan=z='{z}':x={x}:y={y}:d={frames}:s={w}x{h}:fps={fps}"
        else:  # static：仅统一规格，不加运动
            expr = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")

        output = output or self._out("kb")
        self._run([
            "-loop", "1", "-i", image,
            "-vf", f"{expr},format=yuv420p",
            "-t", f"{duration:.3f}",
            "-r", str(fps),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p",
            output,
        ])
        return {"path": output, "duration": self.duration(output),
                "motion": motion, "source": image}

    # ------------------------------------------------------------
    # 拼接 / 配音 / 裁剪 / 字幕
    # ------------------------------------------------------------

    def concat(self, videos: List[str], output: str = None) -> dict:
        """按顺序拼接多段视频（先试无损 concat，失败回退重编码）。"""
        vids = [v for v in (videos or []) if v]
        if len(vids) < 2:
            raise VideoEditError("拼接至少需要 2 段视频")
        for v in vids:
            if not os.path.exists(v):
                raise VideoEditError(f"视频不存在: {v}")
        output = output or self._out("merged")

        # 1) 无损：要求编码参数一致（kenburns 产出的片段满足）
        list_file = output + ".txt"
        try:
            with open(list_file, "w", encoding="utf-8") as f:
                for v in vids:
                    f.write("file '%s'\n" % os.path.abspath(v).replace("'", "'\\''"))
            self._run(["-f", "concat", "-safe", "0", "-i", list_file,
                       "-c", "copy", output])
            if os.path.exists(output) and os.path.getsize(output) > 0:
                return {"path": output, "parts": len(vids),
                        "mode": "copy", "duration": self.duration(output)}
        except VideoEditError:
            pass  # 参数不一致 → 走重编码
        finally:
            try:
                os.remove(list_file)
            except OSError:
                pass

        # 2) 回退：统一规格后重编码拼接
        args: List[str] = []
        for v in vids:
            args += ["-i", v]
        n = len(vids)
        chains = "".join(
            f"[{i}:v]scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={self.fps}[v{i}];" for i in range(n)
        )
        concat_in = "".join(f"[v{i}]" for i in range(n))
        filter_complex = f"{chains}{concat_in}concat=n={n}:v=1:a=0[outv]"
        self._run(args + ["-filter_complex", filter_complex, "-map", "[outv]",
                          "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                          "-pix_fmt", "yuv420p", output])
        return {"path": output, "parts": n, "mode": "reencode",
                "duration": self.duration(output)}

    def add_audio(self, video: str, audio: str, output: str = None,
                  replace: bool = True, volume: float = 1.0,
                  pad_audio: bool = True, fade_out: float = 0.0,
                  bgm: str = None, bgm_volume: float = 0.25,
                  keep_original: bool = False) -> dict:
        """给视频合成音轨：配音（+ 可选 BGM），默认**丢弃原视频声音**。

        2026-09-17 修复三处实测问题：
        - 层次可控（原「混音混入原声」）：`audio` 是主层，`bgm` 是背景层；
          原视频音轨默认丢弃，只有 replace=False 或 keep_original=True 才混入。
          漫剧因此可以「只混配音 + BGM 两层」，不再出现原声/环境音糊在一起。
        - 时长以画面为准（原「音轨长于画面 → 容器被拉长、末尾冻帧」）：
          统一 `-t <画面时长>`；音轨更短且 pad_audio=True 时补静音。
        - 采样统一（原「混音后降级成 24k 单声道」）：所有音轨先
          `aformat=44100/立体声`，输出 `-ar 44100`，避免被 TTS 的 24kHz 单声道拖累。
        """
        if not os.path.exists(video):
            raise VideoEditError(f"视频不存在: {video}")
        if not os.path.exists(audio):
            raise VideoEditError(f"音频不存在: {audio}")
        if bgm and not os.path.exists(bgm):
            raise VideoEditError(f"BGM 不存在: {bgm}")
        output = output or self._out("voiced")

        v_dur = self.duration(video) or 0.0
        a_dur = self.duration(audio) or 0.0
        b_dur = (self.duration(bgm) or 0.0) if bgm else 0.0
        try:
            has_orig = bool(self.probe(video).get("has_audio"))
        except VideoEditError:
            has_orig = False

        afmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
        inputs = ["-i", video, "-i", audio] + (["-i", bgm] if bgm else [])
        chains: List[str] = []
        layers: List[str] = []

        # 主层：配音
        dub = [afmt]
        if abs(float(volume) - 1.0) > 1e-6:
            dub.append(f"volume={float(volume):.2f}")
        padded = bool(pad_audio and v_dur and a_dur and a_dur < v_dur)
        if padded:
            dub.append(f"apad=pad_dur={v_dur - a_dur:.3f}")
        chains.append(f"[1:a]{','.join(dub)}[dub]")
        layers.append("dub")

        # 背景层：BGM（可选）
        if bgm:
            chains.append(f"[2:a]{afmt},volume={max(0.0, float(bgm_volume)):.2f}[bgm]")
            layers.append("bgm")

        # 原声层：默认不混（只有显式要求才保留）
        dropped_original = False
        if has_orig and (keep_original or not replace):
            chains.append(f"[0:a]{afmt}[orig]")
            layers.insert(0, "original")
        elif has_orig:
            dropped_original = True

        if len(layers) > 1:
            mix_in = "".join(f"[{name}]" for name in layers)
            chains.append(f"{mix_in}amix=inputs={len(layers)}:"
                          f"duration=longest:normalize=0[aout]")
        else:
            chains.append(f"[{layers[0]}]anull[aout]")

        if fade_out > 0 and v_dur:
            chains.append(f"[aout]afade=t=out:st={max(0.0, v_dur - fade_out):.3f}:"
                          f"d={float(fade_out):.3f}[aoutf]")
            out_label = "[aoutf]"
        else:
            out_label = "[aout]"

        args = inputs + ["-filter_complex", ";".join(chains),
                         "-map", "0:v:0", "-map", out_label]
        if v_dur:
            args += ["-t", f"{v_dur:.3f}"]     # 画面为准：音轨再长也不拉长容器
        args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                 "-movflags", "+faststart", output]
        self._run(args)
        return {"path": output, "duration": self.duration(output),
                "video_duration": v_dur, "audio_duration": a_dur,
                "bgm_duration": b_dur, "layers": layers,
                "mixed": len(layers) > 1, "padded": padded,
                "dropped_original": dropped_original,
                "bounded_to_video": bool(v_dur)}

    def trim(self, video: str, start: float = 0.0, end: float = None,
             duration: float = None, output: str = None) -> dict:
        """裁剪片段（优先无损）。"""
        if not os.path.exists(video):
            raise VideoEditError(f"视频不存在: {video}")
        output = output or self._out("trim")
        args = ["-ss", f"{max(0.0, float(start)):.3f}", "-i", video]
        if end is not None:
            args += ["-to", f"{float(end):.3f}"]
        elif duration is not None:
            args += ["-t", f"{float(duration):.3f}"]
        self._run(args + ["-c", "copy", output])
        return {"path": output, "duration": self.duration(output)}

    # ------------------------------------------------------------
    # 字幕：SRT → 自建 ASS → ass 滤镜烧录
    # ------------------------------------------------------------
    # 为什么不用 `subtitles` 滤镜直接烧 SRT（2026-09-17 实测结论）：
    #   SRT 没有 PlayRes，libass 按默认 288 高度基准解释 force_style 的 FontSize，
    #   在 1280 高的视频上 FontSize=41 会被放大到约 350px 高、飘到屏幕中间
    #   （正是"竖屏字幕占半屏"）；且 original_size 对 force_style 无效。
    #   自建 ASS 并把 PlayResX/Y 显式设成视频尺寸后，字号/边距就是真实像素，横竖屏一致。
    @staticmethod
    def _srt_to_ass(srt_text: str, width: int, height: int, font_name: str,
                    font_size: int, margin_v: int, outline: int = 2) -> str:
        """极简 SRT → ASS 转换（够用即可：单行/多行文本 + 标准时间轴）。"""
        import re as _re
        blocks = _re.split(r"\n\s*\n", (srt_text or "").replace("\r\n", "\n").strip())
        events = []
        for blk in blocks:
            lines = [ln.rstrip() for ln in blk.split("\n") if ln.strip()]
            if len(lines) < 2:
                continue
            idx = 1 if _re.match(r"^\d+$", lines[0].strip()) else 0
            if idx >= len(lines):
                continue
            m = _re.match(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
                          r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})", lines[idx])
            if not m:
                continue
            g = m.groups()

            def _ts(h, mi, sec, ms):
                cs = int(str(ms).ljust(3, "0")[:3]) // 10
                return f"{int(h):d}:{int(mi):02d}:{int(sec):02d}.{cs:02d}"

            start_t = _ts(g[0], g[1], g[2], g[3])
            end_t = _ts(g[4], g[5], g[6], g[7])
            text = _re.sub(r"<[^>]+>", "", " ".join(lines[idx + 1:])).strip()
            text = text.replace("{", "(").replace("}", ")")
            if text:
                events.append(f"Dialogue: 0,{start_t},{end_t},Default,,0,0,0,,{text}")
        if not events:
            raise VideoEditError("字幕文件没有可用条目（SRT 解析为空）")
        header = [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "ScaledBorderAndShadow: yes",
            "WrapStyle: 0",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,"
            f"&H00000000,0,0,0,0,100,100,0,0,1,{outline},0,2,40,40,{margin_v},1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        return "\n".join(header + events) + "\n"

    def subtitle(self, video: str, srt: str, output: str = None,
                 font_size: int = None, font_name: str = "SimHei",
                 margin_v: int = None, font_size_percent: float = 0.032,
                 outline: int = 2) -> dict:
        """烧录字幕（SRT）。

        - 自建 ASS（PlayRes = 视频尺寸），字号/边距即真实像素：横竖屏观感一致
        - `font_size` 缺省按视频高度百分比换算（默认 3.2%）：720x1280≈41px、1280x720≈23px
        - `margin_v` 缺省为高度 6%（贴底居中，不压画面主体）
        - 字体名带空格导致失败时自动回退去空格字体，并在返回值里记录
        """
        if not os.path.exists(video):
            raise VideoEditError(f"视频不存在: {video}")
        if not os.path.exists(srt):
            raise VideoEditError(f"字幕文件不存在: {srt}")
        output = output or self._out("subbed")

        info = self.probe(video)
        vw = int(info.get("width") or self.width)
        vh = int(info.get("height") or self.height)
        px = int(font_size) if font_size else max(14, int(round(vh * float(font_size_percent))))
        mv = int(margin_v) if margin_v else max(20, int(round(vh * 0.06)))

        with open(srt, encoding="utf-8", errors="replace") as f:
            srt_text = f.read()

        used_name = str(font_name or "SimHei")
        ass_path = os.path.splitext(output)[0] + ".ass"

        def _burn(name: str) -> None:
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(self._srt_to_ass(srt_text, vw, vh, name, px, mv, outline))
            bs = chr(92)
            esc = os.path.abspath(ass_path).replace(bs, "/").replace(":", bs + ":")
            self._run(["-i", video, "-vf", f"ass='{esc}'",
                       "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                       "-c:a", "copy", "-pix_fmt", "yuv420p", output])

        try:
            _burn(used_name)
        except VideoEditError:
            if " " in used_name:
                used_name = used_name.replace(" ", "")
                _burn(used_name)
            else:
                raise
        return {"path": output, "duration": self.duration(output),
                "font_size": px, "margin_v": mv, "font_name": used_name,
                "original_size": f"{vw}x{vh}", "ass_path": ass_path}


def is_configured() -> bool:
    """剪辑是否可用（enabled 且 ffmpeg/ffprobe 就绪）。"""
    if not VIDEO_EDIT_CONFIG.get("enabled"):
        return False
    return available()
