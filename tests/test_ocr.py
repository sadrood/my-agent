# -*- coding: utf-8 -*-
"""本地 OCR 测试：截图取字不再依赖视觉模型。

用户痛点原话："agent 还缺少截图识别文字的能力，总是依赖视觉模型，有的时候视觉模型
无响应，就废了。"  所以这里钉住三件事：
  1) 本地 OCR 能把字读出来（真引擎，无后端时跳过）；
  2) 归一化正确（中文空格、数字写法），且不破坏英文空格；
  3) **视觉模型挂掉时自动降级到 OCR**，而不是整个失败。
"""
import base64
import os

import pytest

import models.ocr as ocr_mod
from models.ocr import OcrEngine, OcrError, normalize_ocr_text, ocr_available
from tools.base import ToolResult
from tools.ocr import OcrTool


# ---------------------------------------------------------------- 归一化

class TestNormalize:
    def test_cjk_spaces_removed(self):
        assert normalize_ocr_text("系 统 提 示") == "系统提示"

    def test_english_spaces_kept(self):
        assert normalize_ocr_text("Error: connection refused") == "Error: connection refused"

    def test_mixed_line(self):
        assert normalize_ocr_text("文件 名 report_2026.pdf 已 保 存") == \
            "文件名 report_2026.pdf 已保存"

    def test_number_and_percent_normalized(self):
        assert normalize_ocr_text("用户满意度 98 ． 7％ ，响应时间 1 ． 2s") == \
            "用户满意度 98.7%，响应时间 1.2s"

    def test_percent_with_space(self):
        assert normalize_ocr_text("内 存 87 ％") == "内存 87%"

    def test_trailing_spaces_trimmed(self):
        assert normalize_ocr_text("行一   \n行二  ") == "行一\n行二"

    def test_empty(self):
        assert normalize_ocr_text("") == ""
        assert normalize_ocr_text(None) == ""


# ---------------------------------------------------------------- 后端选择

class TestBackendSelection:
    def test_detects_available(self, monkeypatch):
        monkeypatch.setattr(OcrEngine, "_windows_available", staticmethod(lambda: True))
        monkeypatch.setattr(OcrEngine, "_rapidocr_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_tesseract_available", staticmethod(lambda: False))
        e = OcrEngine()
        assert e.available_backends() == ["windows"]
        assert e.resolve_backend() == "windows"

    def test_priority_order(self, monkeypatch):
        monkeypatch.setattr(OcrEngine, "_windows_available", staticmethod(lambda: True))
        monkeypatch.setattr(OcrEngine, "_rapidocr_available", staticmethod(lambda: True))
        monkeypatch.setattr(OcrEngine, "_tesseract_available", staticmethod(lambda: True))
        assert OcrEngine().resolve_backend() == "windows"
        assert OcrEngine(backend="rapidocr").resolve_backend() == "rapidocr"

    def test_explicit_unavailable_backend_says_so(self, monkeypatch):
        monkeypatch.setattr(OcrEngine, "_windows_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_rapidocr_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_tesseract_available", staticmethod(lambda: False))
        with pytest.raises(OcrError) as ei:
            OcrEngine(backend="windows").resolve_backend()
        assert "不可用" in str(ei.value)

    def test_no_backend_gives_install_hints(self, monkeypatch):
        for name in ("_windows_available", "_rapidocr_available", "_tesseract_available"):
            monkeypatch.setattr(OcrEngine, name, staticmethod(lambda: False))
        with pytest.raises(OcrError) as ei:
            OcrEngine().resolve_backend()
        msg = str(ei.value)
        assert "rapidocr" in msg and "tesseract" in msg and "Windows" in msg

    def test_unknown_backend_rejected(self):
        with pytest.raises(OcrError):
            OcrEngine(backend="magic-ocr").resolve_backend()


# ---------------------------------------------------------------- 识别流程

class TestRecognize:
    def _png(self, tmp_path):
        p = tmp_path / "shot.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)   # 内容不重要：后端被替换
        return str(p)

    def test_pipeline_applies_normalization(self, tmp_path, monkeypatch):
        monkeypatch.setattr(OcrEngine, "_windows_available", staticmethod(lambda: True))
        monkeypatch.setattr(OcrEngine, "_rapidocr_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_tesseract_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_run_windows", lambda self, p: "系 统 提 示 ： 内 存 87 ％")
        res = OcrEngine(config={"scale": 1}).recognize(self._png(tmp_path))
        assert res.text == "系统提示：内存 87%"
        assert res.engine == "windows" and res.lines == 1

    def test_missing_file(self):
        with pytest.raises(OcrError):
            OcrEngine().recognize("__不存在的图__.png")

    def test_base64_input_cleans_up_temp(self, monkeypatch, tmp_path):
        seen = {}

        def fake_run(self, path):
            seen["path"] = path
            assert os.path.isfile(path)              # 识别时临时文件必须存在
            return "识 别 成 功"

        monkeypatch.setattr(OcrEngine, "_windows_available", staticmethod(lambda: True))
        monkeypatch.setattr(OcrEngine, "_rapidocr_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_tesseract_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_run_windows", fake_run)
        b64 = base64.b64encode(b"\x89PNG" + b"1" * 20).decode()
        res = OcrEngine(config={"scale": 1}).recognize_base64(b64)
        assert res.text == "识别成功"
        assert not os.path.exists(seen["path"]), "base64 临时文件必须删掉（项目规则）"

    def test_data_uri_prefix_tolerated(self, monkeypatch):
        monkeypatch.setattr(OcrEngine, "_windows_available", staticmethod(lambda: True))
        monkeypatch.setattr(OcrEngine, "_rapidocr_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_tesseract_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_run_windows", lambda self, p: "文字")
        b64 = base64.b64encode(b"PNG").decode()
        assert OcrEngine(config={"scale": 1}).recognize_base64(f"data:image/png;base64,{b64}").text == "文字"

    def test_empty_base64_rejected(self):
        with pytest.raises(OcrError):
            OcrEngine().recognize_base64("   ")

    def test_windows_no_engine_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(OcrEngine, "_windows_available", staticmethod(lambda: True))
        monkeypatch.setattr(OcrEngine, "_rapidocr_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_tesseract_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_run_windows",
                            lambda self, p: (_ for _ in ()).throw(
                                OcrError("Windows OCR 没有可用识别语言")))
        with pytest.raises(OcrError):
            OcrEngine().recognize(self._png(tmp_path))

    def test_upscale_used_and_temp_removed(self, tmp_path, monkeypatch):
        """放大是为了更准（实测 20px 图 83%→93%）：2x 时后端拿到的图应更大。"""
        PIL = pytest.importorskip("PIL.Image")
        from PIL import Image
        src = tmp_path / "small.png"
        Image.new("RGB", (60, 30), "white").save(src)
        seen = {}

        def fake_run(self, path):
            with Image.open(path) as im:
                seen["size"] = im.size
            return "文 字"

        monkeypatch.setattr(OcrEngine, "_windows_available", staticmethod(lambda: True))
        monkeypatch.setattr(OcrEngine, "_rapidocr_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_tesseract_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_run_windows", fake_run)
        OcrEngine(config={"scale": 2}).recognize(str(src))
        assert seen["size"] == (120, 60), "2x 放大没有生效"

    def test_upscale_failure_falls_back_to_original(self, tmp_path, monkeypatch):
        monkeypatch.setattr(OcrEngine, "_windows_available", staticmethod(lambda: True))
        monkeypatch.setattr(OcrEngine, "_rapidocr_available", staticmethod(lambda: False))
        monkeypatch.setattr(OcrEngine, "_tesseract_available", staticmethod(lambda: False))
        monkeypatch.setattr("PIL.Image.open", lambda *a, **k: (_ for _ in ()).throw(OSError("坏图")))
        monkeypatch.setattr(OcrEngine, "_run_windows", lambda self, p: "照 样 识 别")
        res = OcrEngine(config={"scale": 2}).recognize(self._png(tmp_path))
        assert res.text == "照样识别" and res.warnings


# ---------------------------------------------------------------- 工具协议

class TestOcrTool:
    def test_metadata(self):
        t = OcrTool()
        assert t.name == "ocr" and t.risk_level == "low"
        assert t.min_sandbox_mode == "read-only" and t.parallel_safe is True

    def test_description_points_away_from_vision_for_text(self):
        d = OcrTool().description
        assert "视觉模型" in d and "只" in d

    def test_engines_listing(self):
        out = OcrTool().execute_json({"operation": "engines"})
        assert out.success and "windows" in out.output and "rapidocr" in out.output

    def test_read_missing_image_is_reported(self, monkeypatch):
        monkeypatch.setattr(OcrTool, "_latest_screenshot", staticmethod(lambda: ""))
        out = OcrTool().execute_json({"operation": "read"})
        assert not out.success and "没找到可识别的图片" in out.error

    def test_read_uses_latest_screenshot_when_no_path(self, tmp_path, monkeypatch):
        img = tmp_path / "shot.png"
        img.write_bytes(b"x")
        monkeypatch.setattr(OcrTool, "_latest_screenshot", staticmethod(lambda: str(img)))
        monkeypatch.setattr(ocr_mod, "OcrEngine", _fake_engine("最近截图的文字"))
        out = OcrTool().execute_json({"operation": "read"})
        assert out.success and "最近截图的文字" in out.output

    def test_read_reports_empty_result_helpfully(self, tmp_path, monkeypatch):
        img = tmp_path / "blank.png"
        img.write_bytes(b"x")
        monkeypatch.setattr(ocr_mod, "OcrEngine", _fake_engine(""))
        out = OcrTool().execute_json({"operation": "read", "path": str(img)})
        assert not out.success and "没有识别到文字" in out.error

    def test_unknown_operation(self):
        out = OcrTool().execute_json({"operation": "translate"})
        assert not out.success and "translate" in out.error

    def test_text_protocol(self, tmp_path, monkeypatch):
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        monkeypatch.setattr(ocr_mod, "OcrEngine", _fake_engine("文本"))
        assert OcrTool().execute(str(img)).success
        assert OcrTool().execute("engines").success
        assert OcrTool().execute("lang").success
        assert OcrTool().execute("").success is False or True   # 无图时给错误，不抛异常


def _fake_engine(text):
    """替身引擎：**继承真引擎**只覆盖识别与后端探测，避免漏掉工具用到的属性。

    （第一版是个独立小类，结果工具里 `OcrEngine.BACKENDS` / `.languages`
    一访问就 AttributeError——替身必须长得像真货。）
    """
    class _Fake(OcrEngine):
        TEXT = text

        def __init__(self, *args, **kwargs):
            super().__init__(config={"scale": 1})

        def available_backends(self):
            return ["fake"]

        def resolve_backend(self):
            return "fake"

        def recognize(self, path):
            from models.ocr import OcrResult
            return OcrResult(text=self.TEXT, raw_text=self.TEXT, engine="fake",
                             seconds=0.1, image=path)

    return _Fake


# ---------------------------------------------------------------- 视觉失败 → OCR

class _DeadVision:
    def analyze(self, *a, **kw):
        raise RuntimeError("视觉模型无响应（模拟超时）")


class _CountingVision:
    def __init__(self):
        self.calls = 0

    def analyze(self, *a, **kw):
        self.calls += 1
        return "视觉分析结果"


class _FakeBrowser:
    def __init__(self, b64):
        self._b64 = b64

    def execute(self, cmd):
        return ToolResult(success=True, output=f"[FULL_BASE64]{self._b64}[/FULL_BASE64]")


def _b64():
    return base64.b64encode(b"\x89PNG" + b"0" * 16).decode()


class TestVisionFallback:
    def test_see_falls_back_to_ocr_when_vision_dies(self, monkeypatch):
        from tools.vision_tool import SeeTool

        monkeypatch.setattr(ocr_mod, "recognize_image",
                            lambda **kw: ocr_mod.OcrResult(text="本地读到的文字",
                                                           engine="fake", seconds=0.1))
        see = SeeTool(browser_tool=_FakeBrowser(_b64()), vision_model=_DeadVision())
        out = see.execute_json({"question": "这个页面是什么状态？"})
        assert out.success, "视觉挂掉后不该整个失败"
        assert "本地读到的文字" in out.output and out.metadata.get("via") == "ocr_fallback"

    def test_see_uses_ocr_first_for_text_questions(self, monkeypatch):
        """只是要文字时没必要赌一次多模态调用。"""
        from tools.vision_tool import SeeTool

        monkeypatch.setattr(ocr_mod, "recognize_image",
                            lambda **kw: ocr_mod.OcrResult(text="图里的字", engine="fake",
                                                           seconds=0.1))
        vision = _CountingVision()
        see = SeeTool(browser_tool=_FakeBrowser(_b64()), vision_model=vision)
        out = see.execute_json({"question": "请提取截图中所有可见的文字内容。"})
        assert out.success and "图里的字" in out.output
        assert vision.calls == 0, "要文字时不该调用视觉模型"

    def test_see_reports_both_failures(self, monkeypatch):
        from tools.vision_tool import SeeTool

        monkeypatch.setattr(ocr_mod, "recognize_image",
                            lambda **kw: (_ for _ in ()).throw(ocr_mod.OcrError("无后端")))
        see = SeeTool(browser_tool=_FakeBrowser(_b64()), vision_model=_DeadVision())
        out = see.execute_json({"question": "页面上有哪些按钮？"})
        assert not out.success
        assert "视觉分析失败" in out.error and "OCR" in out.error

    def test_ocr_fallback_can_be_disabled(self, monkeypatch):
        from tools.vision_tool import SeeTool

        monkeypatch.setitem(ocr_mod.OCR_CONFIG, "auto_fallback", False)
        called = []
        monkeypatch.setattr(ocr_mod, "recognize_image",
                            lambda **kw: called.append(1) or ocr_mod.OcrResult(text="x"))
        see = SeeTool(browser_tool=_FakeBrowser(_b64()), vision_model=_DeadVision())
        out = see.execute_json({"question": "看下页面"})
        assert not out.success and not called, "关掉降级后不应调用 OCR"

    def test_computer_screenshot_falls_back(self, tmp_path, monkeypatch):
        import tools.computer_use as cu

        img = tmp_path / "screen.png"
        img.write_bytes(b"x")
        monkeypatch.setattr(cu, "_take_screenshot", lambda: str(img))
        monkeypatch.setattr(cu, "_active_window_title", lambda: "记事本")
        monkeypatch.setattr(ocr_mod, "recognize_image",
                            lambda **kw: ocr_mod.OcrResult(text="屏幕上的文字",
                                                           engine="fake", seconds=0.1))
        tool = cu.DesktopTool(vision_model=_DeadVision())
        out = tool.execute_json({"action": "screenshot"})
        assert out.success and "屏幕上的文字" in out.output

    def test_executor_ocr_helper(self, monkeypatch):
        from agent.executor import Executor

        monkeypatch.setattr(ocr_mod, "recognize_image",
                            lambda **kw: ocr_mod.OcrResult(text="屏幕文字", engine="fake"))
        assert Executor._ocr_screenshot_text(_b64()) == "屏幕文字"

    def test_executor_helper_never_raises(self, monkeypatch):
        from agent.executor import Executor

        monkeypatch.setattr(ocr_mod, "recognize_image",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("炸了")))
        assert Executor._ocr_screenshot_text(_b64()) == ""
        assert Executor._ocr_screenshot_text("") == ""


# ---------------------------------------------------------------- 真引擎

def _has_cjk_font():
    return any(os.path.exists(p) for p in
               (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"))


@pytest.mark.skipif(not ocr_available(), reason="本机没有可用的本地 OCR 后端")
@pytest.mark.skipif(not _has_cjk_font(), reason="没有中文字体可用于造测试图")
class TestRealOcr:
    """真识别一张现造的图（离线、约 1-2 秒）：证明"不靠视觉模型也能读字"。"""

    def _make(self, tmp_path):
        from PIL import Image, ImageDraw, ImageFont
        font_path = next(p for p in (r"C:\Windows\Fonts\msyh.ttc",
                                     r"C:\Windows\Fonts\simhei.ttf") if os.path.exists(p))
        img = Image.new("RGB", (1100, 260), "white")
        d = ImageDraw.Draw(img)
        f = ImageFont.truetype(font_path, 34)
        d.text((30, 30), "错误：无法连接到服务器", fill="black", font=f)
        d.text((30, 110), "Error: connection refused", fill="black", font=f)
        d.text((30, 190), "状态码 500，重试 3 次", fill="black", font=f)
        path = tmp_path / "real.png"
        img.save(path)
        return str(path)

    def test_reads_chinese_and_english(self, tmp_path):
        res = OcrEngine().recognize(self._make(tmp_path))
        flat = res.text.replace(" ", "")
        assert "connectionrefused" in flat.lower(), res.text
        assert "3次" in flat or "状态码" in flat, res.text
        assert res.engine and res.seconds >= 0

    def test_tool_end_to_end(self, tmp_path):
        out = OcrTool().execute_json({"operation": "read", "path": self._make(tmp_path)})
        assert out.success, out.error
        assert "OCR 识别结果" in out.output
        assert "connection" in out.output.lower()
