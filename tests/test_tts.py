"""
models/tts.py 与 tools/tts.py 的离线单元测试（不联网）。

edge-tts 的合成被 monkeypatch 拦截。
"""
import pytest

from tools.tts import TTSTool


class FakeCommunicate:
    """假 edge-tts：记录参数并把内容写进目标文件。"""

    calls = []

    def __init__(self, text, voice, rate=None, volume=None):
        FakeCommunicate.calls.append(
            {"text": text, "voice": voice, "rate": rate, "volume": volume})
        self._text = text

    async def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._text)


@pytest.fixture
def fake_edge_tts(monkeypatch):
    import edge_tts
    FakeCommunicate.calls = []
    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    return FakeCommunicate


def make_model(tmp_path, **kw):
    from models.tts import TTSModel
    return TTSModel(save_dir=str(tmp_path), **kw)


class TestVoiceResolve:
    def test_alias_resolved(self):
        from models.tts import resolve_voice, ZH_VOICES
        assert resolve_voice("yunxi") == ZH_VOICES["yunxi"]
        assert resolve_voice("XIAOXIAO") == ZH_VOICES["xiaoxiao"]

    def test_full_name_passthrough(self):
        from models.tts import resolve_voice
        assert resolve_voice("zh-CN-YunxiNeural") == "zh-CN-YunxiNeural"
        assert resolve_voice("en-US-AriaNeural") == "en-US-AriaNeural"

    def test_default_when_empty(self):
        from models.tts import resolve_voice, ZH_VOICES
        assert resolve_voice("") == ZH_VOICES["xiaoxiao"]
        assert resolve_voice(None) == ZH_VOICES["xiaoxiao"]


class TestTTSModel:
    def test_synthesize_creates_file(self, tmp_path, fake_edge_tts):
        m = make_model(tmp_path)
        r = m.synthesize("从前有座山")
        assert r["path"].startswith(str(tmp_path)) and r["path"].endswith(".mp3")
        assert open(r["path"], encoding="utf-8").read() == "从前有座山"
        assert r["chars"] == 5
        assert r["voice"] == "zh-CN-XiaoxiaoNeural"

    def test_voice_and_rate_forwarded(self, tmp_path, fake_edge_tts):
        m = make_model(tmp_path)
        m.synthesize("测试", voice="yunxi", rate="+20%")
        call = FakeCommunicate.calls[-1]
        assert call["voice"] == "zh-CN-YunxiNeural"
        assert call["rate"] == "+20%"

    def test_empty_text_rejected(self, tmp_path, fake_edge_tts):
        with pytest.raises(ValueError, match="为空"):
            make_model(tmp_path).synthesize("   ")

    def test_custom_output_path(self, tmp_path, fake_edge_tts):
        out = tmp_path / "sub" / "a.mp3"
        r = make_model(tmp_path).synthesize("hi", output=str(out))
        assert r["path"] == str(out) and out.exists()

    def test_unique_filenames(self, tmp_path, fake_edge_tts):
        m = make_model(tmp_path)
        p1 = m.synthesize("一")["path"]
        p2 = m.synthesize("二")["path"]
        assert p1 != p2   # 唯一命名，便于并行


class TestTTSTool:
    def test_schema_commands(self):
        s = TTSTool().schema
        assert s["properties"]["command"]["enum"] == ["speak", "voices"]
        assert s["required"] == ["command"]

    def test_voices_listed(self):
        r = TTSTool().execute_json({"command": "voices"})
        assert r.success is True and "xiaoxiao" in r.output

    def test_empty_text_hint(self):
        r = TTSTool().execute_json({"command": "speak"})
        assert r.success is False and "文字" in r.error

    def test_speak_success(self, tmp_path, fake_edge_tts):
        t = TTSTool(tts_model=make_model(tmp_path))
        r = t.execute_json({"command": "speak", "text": "你好世界", "voice": "yunyang"})
        assert r.success is True
        assert "配音已生成" in r.output
        assert r.metadata["audio_path"].endswith(".mp3")
        assert r.metadata["voice"] == "zh-CN-YunyangNeural"

    def test_speak_failure_reported(self, tmp_path, monkeypatch):
        from models.tts import TTSModel

        def _boom(*a, **kw):
            raise RuntimeError("网络不可达")

        monkeypatch.setattr(TTSModel, "synthesize", _boom)
        r = TTSTool(tts_model=TTSModel(save_dir=str(tmp_path))).execute_json(
            {"command": "speak", "text": "你好"})
        assert r.success is False and "网络不可达" in r.error

    def test_unknown_command(self):
        r = TTSTool().execute_json({"command": "sing"})
        assert r.success is False and "未知命令" in r.error

    def test_string_entry(self, tmp_path, fake_edge_tts):
        t = TTSTool(tts_model=make_model(tmp_path))
        assert t.execute("speak 你好").success is True
        assert t.execute("voices").success is True

    def test_registered_in_tool_manager(self):
        from tools.tool_manager import ToolManager
        assert "tts" in ToolManager().list_tools()
