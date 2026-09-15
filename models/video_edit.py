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

from config import VIDEO_EDIT_CONFIG

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
        self.save_dir = save_dir or cfg.get("save_dir", "./generated_videos")
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
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        return {
            "duration": float(fmt.get("duration") or 0.0),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "has_audio": has_audio,
            "size_bytes": int(fmt.get("size") or 0),
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
        w, h = self.width, self.height
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
                  pad_audio: bool = True, fade_out: float = 0.0) -> dict:
        """给视频叠加配音/BGM。

        replace=True 丢弃原音轨（漫剧通常如此）；pad_audio=True 时若音轨比
        画面短则补静音（到画面结束），保证整段都有声音、不被截断。
        """
        if not os.path.exists(video):
            raise VideoEditError(f"视频不存在: {video}")
        if not os.path.exists(audio):
            raise VideoEditError(f"音频不存在: {audio}")
        output = output or self._out("voiced")

        v_dur = self.duration(video)
        a_dur = self.duration(audio)
        args = ["-i", video, "-i", audio]
        filters = []
        if abs(volume - 1.0) > 1e-6:
            filters.append(f"volume={volume:.2f}")
        if pad_audio and v_dur and a_dur and a_dur < v_dur:
            # 音轨不足 → 补静音（避免 -shortest 把画面截短）
            filters.append(f"apad=pad_dur={v_dur - a_dur:.3f}")
        if fade_out > 0:
            filters.append(f"afade=t=out:st={max(0.0, v_dur - fade_out):.3f}:d={fade_out:.3f}")

        if replace:
            args += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            # 混音：原音轨 + 新音轨
            filters.insert(0, "[0:a][1:a]amix=inputs=2:duration=longest[aout]")
            args += ["-filter_complex", ";".join(filters)]
            args += ["-map", "0:v:0", "-map", "[aout]"]
            filters = []
            self._run(args + ["-c:v", "copy", "-c:a", "aac", "-shortest", output])
            return {"path": output, "duration": self.duration(output),
                    "video_duration": v_dur, "audio_duration": a_dur,
                    "mixed": True}

        if filters:
            args += ["-af", ",".join(filters)]
        args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
        # 画面与音轨取长者（pad_audio 时音轨已补齐，故等价于画面时长）
        args += ["-shortest"] if not pad_audio else []
        self._run(args + [output])
        return {"path": output, "duration": self.duration(output),
                "video_duration": v_dur, "audio_duration": a_dur,
                "padded": bool(pad_audio and a_dur and v_dur and a_dur < v_dur)}

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

    def subtitle(self, video: str, srt: str, output: str = None,
                 font_size: int = 24) -> dict:
        """烧录字幕（SRT）。注意：需重编码，比 copy 慢。"""
        if not os.path.exists(video):
            raise VideoEditError(f"视频不存在: {video}")
        if not os.path.exists(srt):
            raise VideoEditError(f"字幕文件不存在: {srt}")
        output = output or self._out("subbed")
        # Windows 路径需转义给 subtitles 滤镜
        sub = os.path.abspath(srt).replace("\\", "/").replace(":", "\\:")
        style = (f"FontSize={font_size},PrimaryColour=&H00FFFFFF,"
                 f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0")
        self._run(["-i", video, "-vf",
                   f"subtitles='{sub}':force_style='{style}'",
                   "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                   "-c:a", "copy", "-pix_fmt", "yuv420p", output])
        return {"path": output, "duration": self.duration(output)}


def is_configured() -> bool:
    """剪辑是否可用（enabled 且 ffmpeg/ffprobe 就绪）。"""
    if not VIDEO_EDIT_CONFIG.get("enabled"):
        return False
    return available()
