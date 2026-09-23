"""视频理解链路测试（离线）：抽帧 → 多图一次请求 → 临时帧清理。

背景：上游（商汤 / Agnes）`GET /models` 里没有任何模型声明 video 输入模态，
视频只能抽帧成多张图再一次请求交给视觉模型。这里把 ffmpeg 与视觉模型都桩掉，
只验证**接线与清理**是否正确。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.video_edit import VideoEditError, VideoEditor   # noqa: E402
from models.vision import VisionModel                        # noqa: E402
from tools.vision_tool import SeeTool                        # noqa: E402


class _FakeVision:
    """记录调用参数的假视觉模型。"""

    def __init__(self, answer="结果文本", video_error=None):
        self.answer = answer
        self.video_error = video_error
        self.calls = []
        self.video_calls = []
        self.last_model = "fake-vision"

    def analyze(self, image_data, question, image_type="image/png", **kw):
        self.calls.append({"images": image_data, "question": question,
                           "image_type": image_type})
        if isinstance(image_data, list) and not image_data:
            raise AssertionError("多图调用不能是空列表")
        return self.answer

    def analyze_video(self, video_path, question="", frames=6, **kw):
        self.video_calls.append({"path": video_path, "question": question,
                                 "frames": frames})
        if self.video_error:
            raise RuntimeError(self.video_error)
        return self.answer

    def fallback_note(self):
        return ""


class _RealVideoVision(_FakeVision):
    """用**真实**的 analyze_video 实现（只有 analyze 被记录/桩掉）。

    _FakeVision 把 analyze_video 也桩了，直接拿它测 analyze_video 等于测假货。
    """
    analyze_video = VisionModel.analyze_video
    _cleanup_frames = staticmethod(VisionModel._cleanup_frames)


# ============================================================
# VideoEditor.extract_frames
# ============================================================

class TestExtractFrames:

    def _editor(self, tmp_path):
        return VideoEditor(save_dir=str(tmp_path))

    def test_command_uses_even_fps_and_frame_cap(self, tmp_path, monkeypatch):
        """fps = 目标帧数/时长，且用 -frames:v 封顶、定宽缩放。"""
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")
        out_dir = tmp_path / "frames"
        editor = self._editor(tmp_path)
        captured = {}

        def fake_run(args, timeout=None):
            captured["args"] = args
            out_dir.mkdir(exist_ok=True)
            for i in range(1, 7):
                (out_dir / f"f{i:03d}.jpg").write_bytes(b"jpg")
            return ""

        monkeypatch.setattr(editor, "duration", lambda p: 12.0)
        monkeypatch.setattr(editor, "_run", fake_run)

        files = editor.extract_frames(str(video), count=6, out_dir=str(out_dir))
        args = captured["args"]
        assert args[0] == "-i" and args[1] == str(video)
        assert "-frames:v" in args and args[args.index("-frames:v") + 1] == "6"
        vf = args[args.index("-vf") + 1]
        assert vf.startswith("fps=0.500000"), vf       # 6 帧 / 12 秒
        assert "scale=768:-2" in vf
        assert len(files) == 6
        assert files == sorted(files)                  # 时间顺序

    def test_count_is_clamped(self, tmp_path, monkeypatch):
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")
        out_dir = tmp_path / "frames"
        editor = self._editor(tmp_path)
        seen = []

        def fake_run(args, timeout=None):
            seen.append(args[args.index("-frames:v") + 1])
            out_dir.mkdir(exist_ok=True)
            (out_dir / "f001.jpg").write_bytes(b"jpg")
            return ""

        monkeypatch.setattr(editor, "duration", lambda p: 10.0)
        monkeypatch.setattr(editor, "_run", fake_run)

        editor.extract_frames(str(video), count=999, out_dir=str(out_dir))
        editor.extract_frames(str(video), count=0, out_dir=str(out_dir))
        assert seen == [str(VideoEditor.MAX_FRAMES), "1"]

    def test_zero_duration_falls_back_to_one_fps(self, tmp_path, monkeypatch):
        """时长探测失败（0）时不能除零，退回 1fps。"""
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")
        out_dir = tmp_path / "frames"
        editor = self._editor(tmp_path)
        captured = {}

        def fake_run(args, timeout=None):
            captured["vf"] = args[args.index("-vf") + 1]
            out_dir.mkdir(exist_ok=True)
            (out_dir / "f001.jpg").write_bytes(b"jpg")
            return ""

        monkeypatch.setattr(editor, "duration", lambda p: 0.0)
        monkeypatch.setattr(editor, "_run", fake_run)
        editor.extract_frames(str(video), count=4, out_dir=str(out_dir))
        assert captured["vf"].startswith("fps=1.000000")

    def test_missing_video_raises(self, tmp_path):
        with pytest.raises(VideoEditError, match="视频不存在"):
            self._editor(tmp_path).extract_frames(str(tmp_path / "nope.mp4"))

    def test_no_frames_produced_raises(self, tmp_path, monkeypatch):
        """ffmpeg 没报错但一帧都没吐出来（无视频轨/损坏）要明确报错。"""
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")
        editor = self._editor(tmp_path)
        monkeypatch.setattr(editor, "duration", lambda p: 5.0)
        monkeypatch.setattr(editor, "_run", lambda args, timeout=None: "")
        with pytest.raises(VideoEditError, match="没有生成任何帧"):
            editor.extract_frames(str(video), out_dir=str(tmp_path / "empty"))


# ============================================================
# VisionModel.analyze_video
# ============================================================

class TestAnalyzeVideo:

    def _frames(self, tmp_path, n=3):
        d = tmp_path / "vframes"
        d.mkdir(exist_ok=True)
        paths = []
        for i in range(1, n + 1):
            p = d / f"f{i:03d}.jpg"
            p.write_bytes(b"jpeg" + bytes([i]))
            paths.append(str(p))
        return d, paths

    def _patched(self, monkeypatch, tmp_path, n=3):
        d, paths = self._frames(tmp_path, n)
        monkeypatch.setattr("models.video_edit.ffmpeg_path", lambda: "ffmpeg")
        monkeypatch.setattr(VideoEditor, "extract_frames",
                            lambda self, video, count=6, **kw: paths)
        return d, paths

    def test_all_frames_go_in_one_request_in_order(self, tmp_path, monkeypatch):
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")
        d, paths = self._patched(monkeypatch, tmp_path, n=3)
        vm = _RealVideoVision(answer="分析结果")

        out = vm.analyze_video(str(video), "画面有什么变化？", frames=3)
        assert out == "分析结果"
        assert len(vm.calls) == 1                      # 一次请求，不分多次问
        call = vm.calls[0]
        assert isinstance(call["images"], list) and len(call["images"]) == 3
        assert call["image_type"] == "image/jpeg"
        assert "3 张" in call["question"]              # 告知帧数与顺序
        assert "画面有什么变化？" in call["question"]
        assert not d.exists()                          # 临时帧已清理

    def test_empty_question_uses_default_prompt(self, tmp_path, monkeypatch):
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")
        self._patched(monkeypatch, tmp_path, n=2)
        vm = _RealVideoVision()
        vm.analyze_video(str(video), "", frames=2)
        assert "发生了什么" in vm.calls[0]["question"]

    def test_frames_cleaned_up_even_when_vision_fails(self, tmp_path, monkeypatch):
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")
        d, _ = self._patched(monkeypatch, tmp_path, n=2)

        class _Boom(_RealVideoVision):
            def analyze(self, *a, **kw):
                raise RuntimeError("上游挂了")

        with pytest.raises(RuntimeError, match="上游挂了"):
            _Boom().analyze_video(str(video), "看看", frames=2)
        assert not d.exists()

    def test_missing_video_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr("models.video_edit.ffmpeg_path", lambda: "ffmpeg")
        with pytest.raises(RuntimeError, match="视频不存在"):
            VisionModel().analyze_video(str(tmp_path / "nope.mp4"), "看看")

    def test_empty_path_raises(self, monkeypatch):
        with pytest.raises(RuntimeError, match="未提供视频路径"):
            VisionModel().analyze_video("", "看看")

    def test_without_ffmpeg_gives_actionable_error(self, tmp_path, monkeypatch):
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")
        monkeypatch.setattr("models.video_edit.ffmpeg_path", lambda: None)
        with pytest.raises(RuntimeError) as ei:
            VisionModel().analyze_video(str(video), "看看")
        assert "ffmpeg" in str(ei.value) and "FFMPEG_PATH" in str(ei.value)

    def test_extract_failure_is_wrapped(self, tmp_path, monkeypatch):
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")
        monkeypatch.setattr("models.video_edit.ffmpeg_path", lambda: "ffmpeg")

        def boom(self, path, count=6, **kw):
            raise VideoEditError("没有生成任何帧")

        monkeypatch.setattr(VideoEditor, "extract_frames", boom)
        with pytest.raises(RuntimeError, match="抽帧失败"):
            VisionModel().analyze_video(str(video), "看看")


# ============================================================
# SeeTool 的本地文件分支
# ============================================================

class TestSeeToolFiles:

    def test_video_path_routes_to_analyze_video(self, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"fake-video")
        vision = _FakeVision(answer="视频里有只猫")
        tool = SeeTool(vision_model=vision)
        result = tool.execute_json({"path": str(video), "question": "有什么？"})
        assert result.success, result.error
        assert "视频分析结果" in result.output and "视频里有只猫" in result.output
        assert vision.video_calls[0]["path"].endswith("clip.mp4")
        assert not vision.calls                       # 没有走图片分支
        assert result.metadata["via"] == "video_frames"

    def test_video_failure_reports_error(self, tmp_path):
        video = tmp_path / "clip.mov"
        video.write_bytes(b"fake")
        tool = SeeTool(vision_model=_FakeVision(video_error="上游 429"))
        result = tool.execute_json({"path": str(video)})
        assert not result.success and "上游 429" in result.error

    def test_video_without_vision_model(self, tmp_path, monkeypatch):
        video = tmp_path / "clip.webm"
        video.write_bytes(b"fake")
        tool = SeeTool(vision_model=None)
        monkeypatch.setattr(tool, "_get_vision_model", lambda: None)
        result = tool.execute_json({"path": str(video)})
        assert not result.success and "视觉模型" in result.error

    def test_image_path_sends_original_bytes(self, tmp_path):
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nhello")
        vision = _FakeVision(answer="一只狗")
        tool = SeeTool(vision_model=vision)
        result = tool.execute_json({"path": str(img), "question": "图里是什么？"})
        assert result.success, result.error
        assert "一只狗" in result.output
        import base64
        sent = base64.b64decode(vision.calls[0]["images"])
        assert sent == b"\x89PNG\r\n\x1a\nhello"      # 原图字节
        assert vision.calls[0]["image_type"] == "image/png"
        assert result.metadata["via"] == "vision"

    def test_image_without_question_uses_file_default(self, tmp_path):
        img = tmp_path / "pic.jpg"
        img.write_bytes(b"jpeg-bytes")
        vision = _FakeVision()
        SeeTool(vision_model=vision).execute_json({"path": str(img)})
        assert "描述这张图片" in vision.calls[0]["question"]

    def test_missing_file_reports_error(self, tmp_path):
        tool = SeeTool(vision_model=_FakeVision())
        result = tool.execute_json({"path": str(tmp_path / "nope.png")})
        assert not result.success and "文件不存在" in result.error

    def test_unsupported_extension_reports_error(self, tmp_path):
        doc = tmp_path / "note.txt"
        doc.write_text("hello", encoding="utf-8")
        tool = SeeTool(vision_model=_FakeVision())
        result = tool.execute_json({"path": str(doc)})
        assert not result.success and "不支持的文件类型" in result.error

    def test_vision_down_degrades_to_ocr(self, tmp_path, monkeypatch):
        """图片文件在视觉模型挂掉时用本地 OCR 兜底。"""
        import models.ocr as ocr_mod
        img = tmp_path / "pic.png"
        img.write_bytes(b"png")

        class _R:
            text = "发票金额 100 元"

            def summary(self):
                return "OCR 1 行"

        monkeypatch.setattr(ocr_mod, "auto_fallback_enabled", lambda: True)
        monkeypatch.setattr(ocr_mod, "recognize_image",
                            lambda image_path="", image_base64="": _R())
        tool = SeeTool(vision_model=_FakeVision(video_error="x"))

        class _Dead:
            last_model = ""

            def analyze(self, *a, **kw):
                raise RuntimeError("视觉超时")

            def fallback_note(self):
                return ""

        tool._vision_model = _Dead()
        result = tool.execute_json({"path": str(img), "question": "识别内容"})
        # 问题不含"文字"关键词 → 走视觉分支 → 视觉失败 → OCR 兜底
        assert result.success and "发票金额 100 元" in result.output
        assert result.metadata["via"] == "ocr_fallback"

    def test_text_only_question_skips_vision(self, tmp_path, monkeypatch):
        import models.ocr as ocr_mod
        img = tmp_path / "pic.png"
        img.write_bytes(b"png")

        class _R:
            text = "纯文字内容"

            def summary(self):
                return "OCR 1 行"

        monkeypatch.setattr(ocr_mod, "auto_fallback_enabled", lambda: True)
        monkeypatch.setattr(ocr_mod, "recognize_image",
                            lambda image_path="", image_base64="": _R())
        monkeypatch.setattr(SeeTool, "_wants_text", staticmethod(lambda q: True))
        vision = _FakeVision()
        result = SeeTool(vision_model=vision).execute_json(
            {"path": str(img), "question": "提取文字"})
        assert result.success and result.metadata["via"] == "ocr"
        assert not vision.calls                       # 没花视觉配额

    def test_string_interface_accepts_file_path(self, tmp_path):
        img = tmp_path / "pic.png"
        img.write_bytes(b"png")
        vision = _FakeVision(answer="内容")
        result = SeeTool(vision_model=vision).execute(str(img))
        assert result.success
        assert len(vision.calls) == 1                 # 当成文件，不是问句

    def test_string_interface_still_treats_question_as_question(self, tmp_path):
        """不存在的路径不能被误判成文件，仍按"问页面"处理。"""
        tool = SeeTool(browser_tool=None, vision_model=_FakeVision())
        result = tool.execute("D:/nowhere/missing.mp4")
        assert not result.success and "浏览器工具不可用" in result.error
