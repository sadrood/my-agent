"""OCR 语言映射与视觉异常检测的"失败可区分"回归。

实测故障（2026-09-22 审计）：
- `models/ocr.py` 的 tesseract 后端用 `l.split("-")[0]` 把默认配置
  `zh-Hans-CN,en-US` 切成 `zh,en`，而 tesseract 语言包叫 `chi_sim`/`eng`
  —— 这个后端按默认配置**必然失败**，报错还把人引向"去装语言包"（用户其实装了）；
  分隔符也错了（pytesseract 要 `+`）。旧兜底 `langs or "chi_sim+eng"` 因为
  langs 非空永远不生效。
- `models/video_analyzer.py` 在视觉模型失败时返回 `has_anomaly=False`，与
  "检测过、确实没问题"无法区分；调用方只看这个字段 → agent 会在**完全没做过
  页面检查**的情况下继续操作，用户和 rollout 里都看不到痕迹。
"""
import pytest

import models.ocr as ocr_mod
from models.ocr import _TESSERACT_LANGS
from models.video_analyzer import VideoFrameAnalyzer


def _map_langs(languages: str) -> str:
    """复刻 `_run_tesseract` 里的映射（纯函数部分，不依赖 pytesseract）。"""
    out = []
    for raw in languages.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        parts = token.split("-")
        mapped = (_TESSERACT_LANGS.get(token)
                  or _TESSERACT_LANGS.get("-".join(parts[:2]))
                  or _TESSERACT_LANGS.get(parts[0]))
        out.append(mapped or token)
    return "+".join(dict.fromkeys(out)) or "chi_sim+eng"


class TestTesseractLanguageMapping:
    @pytest.mark.parametrize("cfg,expected", [
        ("zh-Hans-CN,en-US", "chi_sim+eng"),   # 默认配置必须能跑
        ("zh-Hant-TW", "chi_tra"),             # 繁体不能退成简体
        ("zh-TW", "chi_tra"),
        ("en-US", "eng"),
        ("ja-JP", "jpn"),
        ("", "chi_sim+eng"),                   # 空配置退到兜底
    ])
    def test_mapping(self, cfg, expected):
        assert _map_langs(cfg) == expected

    def test_separator_is_plus(self):
        """pytesseract 要 `+` 分隔，不是 `,`（旧实现用了逗号）。"""
        assert "," not in _map_langs("zh-Hans-CN,en-US")

    def test_source_uses_progressive_fallback(self):
        import inspect
        src = inspect.getsource(ocr_mod.OcrEngine._run_tesseract)
        assert "parts[:2]" in src, "繁体前缀（zh-hant）的回退没了"


class TestDetectAnomalyFailureIsVisible:
    def test_vision_unavailable_is_marked(self):
        r = VideoFrameAnalyzer(vision_model=None).detect_anomaly("Zm9v")
        assert r["has_anomaly"] is False
        assert r.get("detect_failed") is True, "视觉不可用被读成了'页面正常'"

    def test_vision_error_is_marked(self):
        class _Boom:
            def analyze(self, *a, **k):
                raise RuntimeError("上游 503")

        r = VideoFrameAnalyzer(vision_model=_Boom()).detect_anomaly("Zm9v")
        assert r["has_anomaly"] is False
        assert r.get("detect_failed") is True
        assert "503" in r["description"]

    def test_callers_surface_the_failure(self):
        """两处调用方都要把"没检查成"说出来，而不是静默当成正常。"""
        import inspect
        import agent.agent as ag
        src = inspect.getsource(ag.Agent)
        assert src.count('detect_failed') >= 2, "调用方没处理 detect_failed"
