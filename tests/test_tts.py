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


@pytest.fixture(autouse=True)
def default_provider_edge(monkeypatch):
    """测试默认走 edge 分支，不跟着开发者 .env 的 TTS_PROVIDER 走。

    TTSModel 在构造时读 TTS_CONFIG，若 .env 设了 TTS_PROVIDER=openrouter，
    下面这些 edge 用例会静默跑到另一条分支上去（联网、断言全变）。
    openrouter 相关用例要么显式传 provider=，要么自己 monkeypatch 覆盖。
    """
    from config import TTS_CONFIG
    monkeypatch.setitem(TTS_CONFIG, "provider", "edge")


@pytest.fixture
def fake_edge_tts(monkeypatch):
    import edge_tts
    FakeCommunicate.calls = []
    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    return FakeCommunicate


def make_model(tmp_path, provider="edge", **kw):
    from models.tts import TTSModel
    return TTSModel(save_dir=str(tmp_path), provider=provider, **kw)


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


# ======================================================================
# OpenRouter 供应商（/api/v1/audio/speech，可挂 fish-audio 等 TTS 模型）
# 全部离线：httpx.post 被替换成假实现
# ======================================================================

class FakeResponse:
    def __init__(self, status_code=200, content=b"", payload=None):
        self.status_code = status_code
        self.content = content
        self._payload = payload
        self.text = str(payload if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeHttpx:
    """记录请求，返回预设响应。"""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def or_model(tmp_path, monkeypatch):
    """构造一个把 httpx.post 拦下来的 openrouter 供应商模型。"""
    import sys
    import types

    from models.tts import TTSModel

    def build(response, **kw):
        fake = FakeHttpx(response)
        mod = types.ModuleType("httpx")
        mod.post = fake.post
        mod.HTTPError = Exception
        monkeypatch.setitem(sys.modules, "httpx", mod)
        m = TTSModel(save_dir=str(tmp_path), provider="openrouter",
                     model="fish-audio/s2.1-pro-free:free")
        m.api_key = "sk-or-test"
        for k, v in kw.items():
            setattr(m, k, v)
        return m, fake

    return build


class TestOpenRouterProvider:
    def test_posts_to_audio_speech_endpoint(self, or_model):
        m, fake = or_model(FakeResponse(200, content=b"\xff\xfb\x90audio"))
        r = m.synthesize("你好")
        call = fake.calls[0]
        assert call["url"] == "https://openrouter.ai/api/v1/audio/speech"
        assert call["json"]["model"] == "fish-audio/s2.1-pro-free:free"
        assert call["json"]["input"] == "你好"
        assert call["json"]["response_format"] == "mp3"
        assert call["headers"]["Authorization"] == "Bearer sk-or-test"
        assert r["provider"] == "openrouter"
        assert open(r["path"], "rb").read().startswith(b"\xff\xfb\x90")

    def test_optional_ranking_headers(self, or_model):
        """官方示例的 HTTP-Referer / X-OpenRouter-Title 归属头。"""
        m, fake = or_model(FakeResponse(200, content=b"x"),
                           referer="https://github.com/me/repo", title="my_agent")
        m.synthesize("hi")
        h = fake.calls[0]["headers"]
        assert h["HTTP-Referer"] == "https://github.com/me/repo"
        assert h["X-OpenRouter-Title"] == "my_agent"

    def test_edge_voice_alias_is_not_sent(self, or_model):
        """回归：TTS_VOICE=xiaoxiao 是 edge 的音色，透传给上游会 400 Invalid voice。"""
        m, fake = or_model(FakeResponse(200, content=b"x"))
        m.voice, m.voice_ignored = m._sanitize_openrouter_voice("xiaoxiao")
        m.synthesize("hi")
        assert "voice" not in fake.calls[0]["json"]
        assert m.voice_ignored == "xiaoxiao"

    def test_model_specific_voice_is_sent(self, or_model):
        m, fake = or_model(FakeResponse(200, content=b"x"))
        m.voice, _ = m._sanitize_openrouter_voice("alloy")
        m.synthesize("hi")
        assert fake.calls[0]["json"]["voice"] == "alloy"

    def test_edge_voice_name_also_dropped(self, or_model):
        """完整音色名 zh-CN-XXXNeural 同样不属于上游命名空间。"""
        m, fake = or_model(FakeResponse(200, content=b"x"))
        m.voice, ignored = m._sanitize_openrouter_voice("zh-CN-YunxiNeural")
        m.synthesize("hi")
        assert "voice" not in fake.calls[0]["json"]
        assert ignored == "zh-CN-YunxiNeural"

    def test_voice_cloning_reference_sent(self, or_model, tmp_path):
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"RIFFfake")
        m, fake = or_model(FakeResponse(200, content=b"x"),
                           reference_audio=str(ref), reference_text="参考文字")
        m.synthesize("克隆这句话")
        refs = fake.calls[0]["json"]["input_references"]
        assert refs[0]["type"] == "input_audio"
        assert refs[0]["input_audio"]["data"].startswith("data:audio/")
        assert refs[1] == {"type": "text", "text": "参考文字"}

    def test_missing_reference_file_ignored(self, or_model):
        m, fake = or_model(FakeResponse(200, content=b"x"),
                           reference_audio="/no/such/file.wav")
        m.synthesize("hi")
        assert "input_references" not in fake.calls[0]["json"]

    def test_empty_audio_is_error(self, or_model):
        m, _ = or_model(FakeResponse(200, content=b""))
        m.fallback_edge = False
        with pytest.raises(RuntimeError, match="空音频"):
            m.synthesize("hi")

    def test_402_explains_credits(self, or_model):
        """付费模型余额不足要给出可行建议，而不是丢一串 JSON。"""
        m, _ = or_model(FakeResponse(402, payload={"error": {"message": "Insufficient credits"}}))
        m.fallback_edge = False
        with pytest.raises(RuntimeError) as e:
            m.synthesize("hi")
        msg = str(e.value)
        assert "402" in msg and "余额" in msg and ":free" in msg

    def test_404_explains_endpoint(self, or_model):
        m, _ = or_model(FakeResponse(404, payload={"error": {"message": "No endpoints"}}))
        m.fallback_edge = False
        with pytest.raises(RuntimeError, match="404"):
            m.synthesize("hi")

    def test_401_explains_key(self, or_model):
        m, _ = or_model(FakeResponse(401, payload={"error": {"message": "bad key"}}))
        m.fallback_edge = False
        with pytest.raises(RuntimeError, match="401"):
            m.synthesize("hi")

    def test_missing_key_fails_fast(self, or_model):
        m, _ = or_model(FakeResponse(200, content=b"x"))
        m.api_key = ""
        m.fallback_edge = False
        with pytest.raises(RuntimeError, match="TTS_OPENROUTER_API_KEY"):
            m.synthesize("hi")

    def test_falls_back_to_edge_on_failure(self, or_model, monkeypatch, tmp_path):
        """OpenRouter 挂了要能兜到 edge-tts，且把降级事实如实回传。"""
        m, _ = or_model(FakeResponse(500, payload={"error": {"message": "boom"}}))
        calls = []

        def fake_edge(text, voice=None, rate=None, volume=None, output=None):
            calls.append(text)
            p = str(tmp_path / "edge.mp3")
            open(p, "w").close()
            return {"path": p, "voice": "zh-CN-XiaoxiaoNeural", "chars": len(text),
                    "text": text, "provider": "edge"}

        monkeypatch.setattr(m, "_synth_edge", fake_edge)
        r = m.synthesize("兜底")
        assert r["provider"] == "edge"
        assert "openrouter" in r["fallback_from"]
        assert calls == ["兜底"]

    def test_fallback_can_be_disabled(self, or_model):
        m, _ = or_model(FakeResponse(500, payload={"error": {"message": "boom"}}))
        m.fallback_edge = False
        with pytest.raises(RuntimeError):
            m.synthesize("hi")

    def test_pcm_wrapped_into_wav(self, or_model):
        """response_format=pcm 是裸 PCM，直接存成 mp3 会变成噪声。"""
        import wave

        pcm = b"\x00\x01" * 240  # 240 帧 16bit 单声道
        m, _ = or_model(FakeResponse(200, content=pcm))
        m.response_format = "pcm"
        r = m.synthesize("hi")
        assert r["path"].endswith(".wav")
        with wave.open(r["path"], "rb") as w:
            assert w.getnframes() == 240

    def test_unknown_provider_rejected(self):
        from models.tts import TTSModel
        with pytest.raises(ValueError, match="未知 TTS 供应商"):
            TTSModel(provider="mystery")

    def test_tool_reports_provider(self, tmp_path, or_model):
        m, _ = or_model(FakeResponse(200, content=b"\xff\xfb\x90x"))
        r = TTSTool(tts_model=m).execute_json({"command": "speak", "text": "你好"})
        assert r.success is True
        assert "openrouter" in r.output
        assert r.metadata["provider"] == "openrouter"

    def test_tool_shows_fallback_warning(self, tmp_path, or_model, monkeypatch):
        """降级到 edge-tts 时必须在输出里写明。

        注意：这里必须把 edge 合成打桩。旧写法让降级路径**真的跑了一遍
        edge-tts**（实测唯一的外部网络调用：GETADDRINFO speech.platform.bing.com），
        而且断言写成 `if r.success:` —— 离线时它会静默变成空操作，
        既依赖网络又失去检出能力。
        """
        m, _ = or_model(FakeResponse(500, payload={"error": {"message": "x"}}))

        def fake_edge(text, voice=None, rate=None, volume=None, output=None):
            p = str(tmp_path / "edge.mp3")
            open(p, "w").close()
            return {"path": p, "voice": "zh-CN-XiaoxiaoNeural", "chars": len(text),
                    "text": text, "provider": "edge"}

        monkeypatch.setattr(m, "_synth_edge", fake_edge)
        r = TTSTool(tts_model=m).execute_json({"command": "speak", "text": "你好"})
        assert r.success is True
        assert "降级" in r.output, "降级必须对用户可见"

    def test_voices_reports_openrouter_provider(self, monkeypatch, tmp_path):
        from config import TTS_CONFIG
        monkeypatch.setitem(TTS_CONFIG, "provider", "openrouter")
        out = TTSTool().execute_json({"command": "voices"}).output
        assert "openrouter" in out and "TTS_REFERENCE_AUDIO" in out
