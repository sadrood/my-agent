"""
models/image_gen.py 与 tools/image_gen.py 的离线单元测试。

httpx.post 被 monkeypatch 拦截，不发起真实网络请求。
"""
import base64

import pytest

from tools.image_gen import ImageGenTool

# 1x1 透明 PNG（标准最小 PNG，可用于校验保存文件内容）
PNG_1PX_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
PNG_1PX_BYTES = base64.b64decode(PNG_1PX_B64)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


@pytest.fixture
def fake_post(monkeypatch):
    """拦截 httpx.post，返回脚本化的 images 响应，并记录请求。"""
    import models.image_gen as ig
    calls = {}

    def _post(url, json=None, headers=None, timeout=None):
        calls["url"] = url
        calls["payload"] = json
        calls["headers"] = headers
        calls["timeout"] = timeout
        return FakeResponse({"created": 1, "data": [{"b64_json": PNG_1PX_B64}]})

    monkeypatch.setattr(ig.httpx, "post", _post)
    return calls


def make_model(tmp_path, **kw):
    from models.image_gen import ImageGenModel
    return ImageGenModel(
        api_key="sk-test", base_url="https://example.com/v1",
        model="sensenova-u1.5-lite", size="1024x1024",
        save_dir=str(tmp_path), timeout=30, **kw,
    )


class TestImageGenModel:
    def test_generate_saves_png(self, tmp_path, fake_post):
        m = make_model(tmp_path)
        r = m.generate("一只猫")
        assert r["model"] == "sensenova-u1.5-lite"
        assert r["size"] == "1024x1024"
        assert len(r["images"]) == 1
        p = r["images"][0]
        assert p.startswith(str(tmp_path)) and p.endswith(".png")
        assert open(p, "rb").read() == PNG_1PX_BYTES

    def test_payload_contract(self, tmp_path, fake_post):
        m = make_model(tmp_path)
        m.generate_b64("猫", size="1280x720", n=2)
        pl = fake_post["payload"]
        assert pl["model"] == "sensenova-u1.5-lite"
        assert pl["prompt"] == "猫"
        assert pl["size"] == "1280x720"
        assert pl["n"] == 2
        assert pl["response_format"] == "b64_json"
        # 公测期去水印：默认关闭水印
        assert pl["watermark"] is False
        assert fake_post["url"] == "https://example.com/v1/images/generations"
        assert fake_post["headers"]["Authorization"] == "Bearer sk-test"

    def test_watermark_configurable(self, tmp_path, fake_post):
        make_model(tmp_path, watermark=True).generate_b64("猫")
        assert fake_post["payload"]["watermark"] is True

    def test_url_response_passthrough(self, tmp_path, monkeypatch):
        import models.image_gen as ig
        monkeypatch.setattr(ig.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse(
                                {"data": [{"url": "https://cdn.example.com/a.png"}]}))
        m = make_model(tmp_path)
        r = m.generate("猫")
        assert r["images"] == ["https://cdn.example.com/a.png"]

    def test_http_error_raises(self, tmp_path, monkeypatch):
        import models.image_gen as ig

        class Err:
            status_code = 401
            text = "unauthorized"

        monkeypatch.setattr(ig.httpx, "post",
                            lambda url, json=None, headers=None, timeout=None: Err())
        with pytest.raises(RuntimeError, match="401"):
            make_model(tmp_path).generate_b64("猫")

    def test_bad_magic_raises(self, tmp_path, monkeypatch):
        import models.image_gen as ig
        bad = base64.b64encode(b"not an image").decode()
        monkeypatch.setattr(ig.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse({"data": [{"b64_json": bad}]}))
        with pytest.raises(RuntimeError, match="魔数"):
            make_model(tmp_path).generate("猫")

    def test_n_clamped(self, tmp_path, fake_post):
        make_model(tmp_path).generate_b64("猫", n=99)
        assert fake_post["payload"]["n"] == 4


class TestImageGenTool:
    def _fake_model(self, result):
        class Fake:
            api_key = "sk-test"

            def generate(self, prompt, size=None, n=1):
                self.last = (prompt, size, n)
                return result

        return Fake()

    def test_schema_requires_prompt(self):
        s = ImageGenTool().schema
        assert s["required"] == ["prompt"]
        assert "size" in s["properties"]
        assert "n" in s["properties"]

    def test_empty_prompt_fails(self):
        r = ImageGenTool(image_model=self._fake_model({})).execute_json({"prompt": "  "})
        assert not r.success and "prompt" in r.error

    def test_missing_key_fails(self):
        class Fake:
            api_key = ""

        r = ImageGenTool(image_model=Fake()).execute("画一只猫")
        assert not r.success and "IMAGE_GEN_API_KEY" in r.error

    def test_happy_path(self):
        model = self._fake_model({
            "images": ["D:/out/a.png"],
            "model": "sensenova-u1.5-lite",
            "size": "1024x1024",
        })
        r = ImageGenTool(image_model=model).execute_json(
            {"prompt": "画一只猫", "size": "1024x1024"})
        assert r.success
        assert "D:/out/a.png" in r.output
        assert "sensenova-u1.5-lite" in r.output
        assert model.last == ("画一只猫", "1024x1024", 1)

    def test_error_bubbles(self):
        class Fake:
            api_key = "sk-test"

            def generate(self, prompt, size=None, n=1):
                raise RuntimeError("HTTP 429")

        r = ImageGenTool(image_model=Fake()).execute("画猫")
        assert not r.success and "429" in r.error
