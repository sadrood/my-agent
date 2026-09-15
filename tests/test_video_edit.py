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

    def test_replace_maps_new_audio(self, tmp_path, fake_ffmpeg):
        video = touch(tmp_path / "v.mp4")
        audio = touch(tmp_path / "a.mp3")
        make_editor(tmp_path).add_audio(video, audio, replace=True)
        cmd = fake_ffmpeg["ffmpeg_cmds"][-1]
        assert "-map" in cmd and "1:a:0" in " ".join(cmd)

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
