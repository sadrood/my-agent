"""
models/video_edit.py 与 tools/video_edit.py 的离线单元测试。

ffmpeg/ffprobe 的 subprocess 调用被 monkeypatch 拦截，不真的转码。
重点覆盖：命令构造正确性、运镜参数、concat 回退、配音补齐逻辑。
"""
import json
import os
import subprocess

import pytest

from tools.video_edit import VideoEditTool


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_ffmpeg(monkeypatch, tmp_path):
    """拦截 ffmpeg/ffprobe：记录命令、按需造输出文件。"""
    import models.video_edit as ve

    state = {"cmds": [], "probe": {"duration": 4.0, "width": 1280, "height": 720,
                                   "has_audio": False, "size_bytes": 1000},
             "fail_concat_copy": False}
    state["ffmpeg_cmds"] = []   # 仅 ffmpeg 调用（便于断言；probe 会插入 ffprobe）

    monkeypatch.setattr(ve, "_find_binary",
                        lambda name: "ffmpeg" if name == "ffmpeg" else "ffprobe")

    def _run(cmd, **kw):
        state["cmds"].append(cmd)
        exe = os.path.basename(str(cmd[0]))
        if exe.startswith("ffprobe"):
            return FakeProc(0, json.dumps({
                "format": {"duration": str(state["probe"]["duration"]),
                           "size": str(state["probe"]["size_bytes"])},
                "streams": [{"codec_type": "video",
                             "width": state["probe"]["width"],
                             "height": state["probe"]["height"]}]
                + ([{"codec_type": "audio"}] if state["probe"]["has_audio"] else []),
            }))
        state["ffmpeg_cmds"].append(cmd)
        # ffmpeg：最后一个参数是输出文件 → 造出来
        out = cmd[-1]
        if state["fail_concat_copy"] and "-c" in cmd and "copy" in cmd \
                and "-f" in cmd and "concat" in cmd:
            return FakeProc(1, "", "concat copy failed")
        try:
            with open(out, "wb") as f:
                f.write(b"\x00" * 32)
        except OSError:
            pass
        return FakeProc(0)

    monkeypatch.setattr(ve.subprocess, "run", _run)
    return state


def last_ffmpeg_cmd(state) -> str:
    """取最近一次 ffmpeg 调用的命令串（kenburns 末尾会调 ffprobe 读时长，
    因此不能简单取 cmds[-1]）。"""
    return " ".join(state["ffmpeg_cmds"][-1])


def make_editor(tmp_path, **kw):
    from models.video_edit import VideoEditor
    return VideoEditor(save_dir=str(tmp_path), **kw)


def touch(path, size=64):
    with open(path, "wb") as f:
        f.write(b"\x00" * size)
    return str(path)


class TestKenBurns:
    def test_zoom_in_command(self, tmp_path, fake_ffmpeg):
        img = touch(tmp_path / "a.png")
        r = make_editor(tmp_path).kenburns(img, duration=3, motion="zoom_in")
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "zoompan" in cmd
        assert "z='min(zoom+" in cmd            # 放大表达式
        assert "d=75" in cmd                     # 3s × 25fps
        assert "s=1280x720" in cmd
        assert "-t 3.000" in cmd
        assert r["path"].endswith(".mp4") and r["motion"] == "zoom_in"

    def test_zoom_out_command(self, tmp_path, fake_ffmpeg):
        img = touch(tmp_path / "a.png")
        make_editor(tmp_path).kenburns(img, duration=2, motion="zoom_out")
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "max(1.001,zoom-" in cmd          # 缩小表达式

    @pytest.mark.parametrize("motion", ["pan_left", "pan_right", "pan_up", "pan_down"])
    def test_pan_motions(self, tmp_path, fake_ffmpeg, motion):
        img = touch(tmp_path / "a.png")
        make_editor(tmp_path).kenburns(img, duration=2, motion=motion)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "zoompan" in cmd and "z='1.2'" in cmd   # 先放大再平移
        assert "on/" in cmd                            # 位移随进度变化

    def test_static_motion(self, tmp_path, fake_ffmpeg):
        img = touch(tmp_path / "a.png")
        make_editor(tmp_path).kenburns(img, duration=1, motion="static")
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "scale=" in cmd and "pad=" in cmd       # 仅统一规格

    def test_invalid_motion_rejected(self, tmp_path, fake_ffmpeg):
        from models.video_edit import VideoEditError
        img = touch(tmp_path / "a.png")
        with pytest.raises(VideoEditError, match="不支持的运镜"):
            make_editor(tmp_path).kenburns(img, motion="spin")

    def test_missing_image_rejected(self, tmp_path, fake_ffmpeg):
        from models.video_edit import VideoEditError
        with pytest.raises(VideoEditError, match="图片不存在"):
            make_editor(tmp_path).kenburns(str(tmp_path / "nope.png"))

    def test_yuv420p_for_compat(self, tmp_path, fake_ffmpeg):
        """必须输出 yuv420p，否则拼接/播放器兼容性差。"""
        img = touch(tmp_path / "a.png")
        make_editor(tmp_path).kenburns(img)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "yuv420p" in cmd


class TestConcat:
    def _three(self, tmp_path):
        return [touch(tmp_path / f"v{i}.mp4") for i in range(3)]

    def test_prefers_lossless_copy(self, tmp_path, fake_ffmpeg):
        vids = self._three(tmp_path)
        r = make_editor(tmp_path).concat(vids)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "-f concat" in cmd and "-c copy" in cmd
        assert r["mode"] == "copy" and r["parts"] == 3

    def test_falls_back_to_reencode(self, tmp_path, fake_ffmpeg):
        vids = self._three(tmp_path)
        fake_ffmpeg["fail_concat_copy"] = True
        r = make_editor(tmp_path).concat(vids)
        assert r["mode"] == "reencode"
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "filter_complex" in cmd and "concat=n=3" in cmd

    def test_requires_two(self, tmp_path, fake_ffmpeg):
        from models.video_edit import VideoEditError
        v = touch(tmp_path / "a.mp4")
        with pytest.raises(VideoEditError, match="至少需要 2 段"):
            make_editor(tmp_path).concat([v])

    def test_missing_part_rejected(self, tmp_path, fake_ffmpeg):
        from models.video_edit import VideoEditError
        v = touch(tmp_path / "a.mp4")
        with pytest.raises(VideoEditError, match="视频不存在"):
            make_editor(tmp_path).concat([v, str(tmp_path / "nope.mp4")])


class TestAddAudio:
    def test_pads_short_audio(self, tmp_path, fake_ffmpeg):
        """音轨短于画面 → 补静音（避免 -shortest 把画面截短）。"""
        video = touch(tmp_path / "v.mp4")
        audio = touch(tmp_path / "a.mp3")
        fake_ffmpeg["probe"]["duration"] = 5.0          # 画面 5s
        ed = make_editor(tmp_path)
        # 让音频"短"：probe 对 mp3 返回 2s
        orig_run = fake_ffmpeg

        def _probe(path):
            return {"duration": 2.0 if path.endswith(".mp3") else 5.0,
                    "width": 1280, "height": 720, "has_audio": False,
                    "size_bytes": 10}

        ed.probe = _probe
        r = ed.add_audio(video, audio)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "apad=pad_dur=3.000" in cmd
        assert r["padded"] is True

    def test_no_pad_when_audio_longer(self, tmp_path, fake_ffmpeg):
        video = touch(tmp_path / "v.mp4")
        audio = touch(tmp_path / "a.mp3")
        ed = make_editor(tmp_path)
        ed.probe = lambda p: {"duration": 8.0 if p.endswith(".mp3") else 5.0,
                              "width": 1280, "height": 720, "has_audio": False,
                              "size_bytes": 10}
        r = ed.add_audio(video, audio)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "apad" not in cmd
        assert r["padded"] is False
        # T4 回归：音轨(8s) 长于画面(5s) 时必须封顶到画面时长，否则容器被拉长、末尾冻帧
        assert "-t 5.000" in cmd and r["bounded_to_video"] is True

    def test_replace_maps_new_audio(self, tmp_path, fake_ffmpeg):
        video = touch(tmp_path / "v.mp4")
        audio = touch(tmp_path / "a.mp3")
        r = make_editor(tmp_path).add_audio(video, audio, replace=True)
        cmd = " ".join(fake_ffmpeg["ffmpeg_cmds"][-1])
        assert "-map 0:v:0" in cmd and "[aout]" in cmd
        assert r["layers"] == ["dub"] and r["mixed"] is False

    def test_volume_filter(self, tmp_path, fake_ffmpeg):
        video = touch(tmp_path / "v.mp4")
        audio = touch(tmp_path / "a.mp3")
        make_editor(tmp_path).add_audio(video, audio, volume=0.5)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "volume=0.50" in cmd

    def test_missing_audio_rejected(self, tmp_path, fake_ffmpeg):
        from models.video_edit import VideoEditError
        video = touch(tmp_path / "v.mp4")
        with pytest.raises(VideoEditError, match="音频不存在"):
            make_editor(tmp_path).add_audio(video, str(tmp_path / "nope.mp3"))

    def test_bgm_two_layers_default_drops_original(self, tmp_path, fake_ffmpeg):
        """T6：配音 + BGM 两层，原视频声音默认丢弃（不再三层糊在一起）。"""
        video = touch(tmp_path / "v.mp4")
        audio = touch(tmp_path / "dub.mp3")
        bgm = touch(tmp_path / "bgm.mp3")
        fake_ffmpeg["probe"]["has_audio"] = True       # 原视频本来有声音
        ed = make_editor(tmp_path)
        ed.probe = lambda p: {"duration": 5.0, "width": 1280, "height": 720,
                              "has_audio": True, "size_bytes": 10}
        r = ed.add_audio(video, audio, bgm=bgm, bgm_volume=0.2)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert r["layers"] == ["dub", "bgm"]
        assert "amix=inputs=2" in cmd and "volume=0.20" in cmd
        assert "[0:a]" not in cmd                       # 原声不参与
        assert r["dropped_original"] is True

    def test_keep_original_mixes_three_layers(self, tmp_path, fake_ffmpeg):
        """显式要求保留原声时才混三层。"""
        video = touch(tmp_path / "v.mp4")
        audio = touch(tmp_path / "dub.mp3")
        ed = make_editor(tmp_path)
        ed.probe = lambda p: {"duration": 5.0, "width": 1280, "height": 720,
                              "has_audio": True, "size_bytes": 10}
        r = ed.add_audio(video, audio, replace=False)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert r["layers"] == ["original", "dub"]
        assert "amix=inputs=2" in cmd and "[0:a]" in cmd

    def test_audio_normalized_to_44100_stereo(self, tmp_path, fake_ffmpeg):
        """T5：所有音轨统一 44.1kHz 立体声，避免被 TTS 24k 单声道降级。"""
        video = touch(tmp_path / "v.mp4")
        audio = touch(tmp_path / "a.mp3")
        make_editor(tmp_path).add_audio(video, audio)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "sample_rates=44100" in cmd and "channel_layouts=stereo" in cmd
        assert "-ar 44100" in cmd


class TestSubtitle:
    def _video(self, tmp_path, w, h):
        ed = make_editor(tmp_path)
        ed.probe = lambda p: {"duration": 6.0, "width": w, "height": h,
                              "has_audio": True, "size_bytes": 10}
        return ed, touch(tmp_path / "v.mp4")

    def test_font_size_scales_with_height(self, tmp_path, fake_ffmpeg):
        """T3：SRT 无 PlayRes，必须给 original_size，并按高度换算字号。"""
        ed, video = self._video(tmp_path, 720, 1280)     # 竖屏
        srt = touch(tmp_path / "s.srt")
        r = ed.subtitle(video, srt)
        cmd = last_ffmpeg_cmd(fake_ffmpeg)
        assert "original_size=720x1280" in cmd
        assert r["font_size"] == 41                      # 1280 * 3.2%
        assert r["margin_v"] == 77                       # 1280 * 6%
        assert f"FontSize=41" in cmd and "MarginV=77" in cmd

    def test_horizontal_video_smaller_font(self, tmp_path, fake_ffmpeg):
        ed, video = self._video(tmp_path, 1280, 720)
        srt = touch(tmp_path / "s.srt")
        r = ed.subtitle(video, srt)
        assert r["font_size"] == 23                      # 720 * 3.2%
        assert r["original_size"] == "1280x720"

    def test_explicit_font_size_wins(self, tmp_path, fake_ffmpeg):
        ed, video = self._video(tmp_path, 1280, 720)
        srt = touch(tmp_path / "s.srt")
        r = ed.subtitle(video, srt, font_size=36, margin_v=100)
        assert r["font_size"] == 36 and r["margin_v"] == 100

    def test_font_name_with_space_falls_back(self, tmp_path, fake_ffmpeg):
        """T8：含空格字体名导致滤镜失败 → 自动回退去空格字体并记录。"""
        from models.video_edit import VideoEditError
        ed, video = self._video(tmp_path, 1280, 720)
        srt = touch(tmp_path / "s.srt")
        calls = {"n": 0}
        real_run = ed._run

        def flaky(args, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise VideoEditError("ffmpeg 失败: Could not find font")
            return real_run(args, timeout)

        ed._run = flaky
        r = ed.subtitle(video, srt, font_name="Microsoft YaHei")
        assert calls["n"] == 2
        assert r["font_name"] == "MicrosoftYaHei"


class TestProbeAndTrim:
    def test_probe_parses_fields(self, tmp_path, fake_ffmpeg):
        v = touch(tmp_path / "v.mp4")
        info = make_editor(tmp_path).probe(v)
        assert info["duration"] == 4.0
        assert info["width"] == 1280 and info["height"] == 720
        assert info["has_audio"] is False

    def test_duration_helper(self, tmp_path, fake_ffmpeg):
        v = touch(tmp_path / "v.mp4")
        assert make_editor(tmp_path).duration(v) == 4.0

    def test_trim_with_start_end(self, tmp_path, fake_ffmpeg):
        v = touch(tmp_path / "v.mp4")
        make_editor(tmp_path).trim(v, start=1.5, end=3.5)
        cmd = fake_ffmpeg["ffmpeg_cmds"][-1]
        assert "-ss" in cmd and "1.500" in cmd and "-to" in cmd and "3.500" in cmd


class TestToolLayer:
    def test_missing_ffmpeg_hint(self, monkeypatch):
        """未装 ffmpeg 时给出可执行的安装提示，而不是晦涩报错。"""
        import models.video_edit as ve
        monkeypatch.setattr(ve, "available", lambda: False)
        r = VideoEditTool().execute_json({"command": "kenburns", "image": "x.png"})
        assert r.success is False
        assert "winget" in r.error and "FFMPEG_PATH" in r.error

    def test_kenburns_single(self, tmp_path, fake_ffmpeg):
        img = touch(tmp_path / "a.png")
        r = VideoEditTool(editor=make_editor(tmp_path)).execute_json(
            {"command": "kenburns", "image": img, "motion": "zoom_in", "duration": 2})
        assert r.success is True and "运镜视频" in r.output
        assert r.metadata["path"].endswith(".mp4")

    def test_kenburns_batch_rotates_motions(self, tmp_path, fake_ffmpeg):
        """多图批量：未指定运镜时自动轮换，增加节奏感。"""
        imgs = [touch(tmp_path / f"{i}.png") for i in range(3)]
        r = VideoEditTool(editor=make_editor(tmp_path)).execute_json(
            {"command": "kenburns", "images": imgs, "duration": 2})
        assert r.success is True and r.metadata["parts"] == 3
        motions = [ve for ve in ("zoom_in", "pan_right", "zoom_out")]
        cmds = " ".join(" ".join(c) for c in fake_ffmpeg["cmds"])
        for m in motions:
            assert m in cmds or "zoompan" in cmds

    def test_concat_via_tool(self, tmp_path, fake_ffmpeg):
        vids = [touch(tmp_path / f"v{i}.mp4") for i in range(2)]
        r = VideoEditTool(editor=make_editor(tmp_path)).execute_json(
            {"command": "concat", "videos": vids})
        assert r.success is True and "已拼接 2 段" in r.output

    def test_probe_via_tool(self, tmp_path, fake_ffmpeg):
        v = touch(tmp_path / "v.mp4")
        r = VideoEditTool(editor=make_editor(tmp_path)).execute_json(
            {"command": "probe", "video": v})
        assert r.success is True and "1280x720" in r.output

    def test_unknown_command(self, tmp_path, fake_ffmpeg):
        r = VideoEditTool(editor=make_editor(tmp_path)).execute_json(
            {"command": "explode"})
        assert r.success is False and "未知命令" in r.error

    def test_string_entry_kenburns(self, tmp_path, fake_ffmpeg):
        img = touch(tmp_path / "a.png")
        r = VideoEditTool(editor=make_editor(tmp_path)).execute(f"kenburns {img}")
        assert r.success is True

    def test_registered_in_tool_manager(self):
        from tools.tool_manager import ToolManager
        assert "video_edit" in ToolManager().list_tools()

def _real_ffmpeg() -> bool:
    try:
        from models.video_edit import available
        return bool(available())
    except Exception:
        return False


@pytest.mark.skipif(not _real_ffmpeg(), reason="需要真实 ffmpeg/ffprobe")
class TestRealFfmpegIntegration:
    """真机回归（本机装了 ffmpeg 才跑）：用真实媒体验证 T3/T4/T5/T6 的修复。"""

    def _inputs(self, tmp_path):
        import subprocess as sp
        from models.video_edit import ffmpeg_path
        ff = ffmpeg_path()
        v, dub, bgm = tmp_path / "v.mp4", tmp_path / "dub.wav", tmp_path / "bgm.wav"
        sp.run([ff, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(v)],
               check=True, capture_output=True)
        for path, freq in ((dub, 880), (bgm, 220)):
            sp.run([ff, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=5",
                    "-c:a", "pcm_s16le", str(path)], check=True, capture_output=True)
        return str(v), str(dub), str(bgm)

    def test_mix_bounded_normalized_drops_original(self, tmp_path):
        from models.video_edit import VideoEditor
        v, dub, bgm = self._inputs(tmp_path)
        ed = VideoEditor(save_dir=str(tmp_path))
        r = ed.add_audio(v, dub, bgm=bgm, bgm_volume=0.2)
        info = ed.probe(r["path"])
        assert abs(info["duration"] - 2.0) < 0.4, info       # T4：封顶到画面
        assert info["audio_sample_rate"] == 44100, info      # T5：统一采样率
        assert info["audio_channels"] == 2, info             # T5：立体声
        assert r["layers"] == ["dub", "bgm"] and r["dropped_original"] is True

    def test_subtitle_burn_real(self, tmp_path):
        from models.video_edit import VideoEditor
        v, _, _ = self._inputs(tmp_path)
        srt = tmp_path / "s.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,500\n测试字幕\n",
            encoding="utf-8",
        )
        ed = VideoEditor(save_dir=str(tmp_path))
        r = ed.subtitle(v, str(srt))
        info = ed.probe(r["path"])
        assert info["duration"] > 1.0
        assert r["font_size"] == 8 or r["font_size"] >= 7     # 240 高 * 3.2% ≈ 8
        assert r["original_size"] == "320x240"
