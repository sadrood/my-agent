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

    def test_url_response_auto_downloaded(self, tmp_path, monkeypatch):
        """URL 形式（如 Agnes）应**自动下载落盘**，而不是只回一个链接。

        旧行为是原样返回 URL，导致换到 url 型提供方后图片不在本地
        （与 b64 型提供方体验不一致）。
        """
        import models.image_gen as ig
        monkeypatch.setattr(ig.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse(
                                {"data": [{"url": "https://cdn.example.com/a.png"}]}))

        class DlResp:
            status_code = 200
            content = PNG_1PX_BYTES

        monkeypatch.setattr(ig.httpx, "get",
                            lambda url, timeout=None, follow_redirects=None: DlResp())
        m = make_model(tmp_path)
        r = m.generate("猫")
        p = r["images"][0]
        assert p.startswith(str(tmp_path)) and p.endswith(".png")
        assert open(p, "rb").read() == PNG_1PX_BYTES

    def test_url_download_failure_falls_back_to_url(self, tmp_path, monkeypatch):
        """下载失败时回退给出 URL，不因网络问题丢掉整次生成结果。"""
        import models.image_gen as ig
        monkeypatch.setattr(ig.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse(
                                {"data": [{"url": "https://cdn.example.com/a.png"}]}))

        def _boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(ig.httpx, "get", _boom)
        r = make_model(tmp_path).generate("猫")
        assert r["images"] == ["https://cdn.example.com/a.png"]

    def test_url_not_downloaded_when_save_false(self, tmp_path, monkeypatch):
        """save=False 时保持原样返回 URL（调用方自行处理）。"""
        import models.image_gen as ig
        monkeypatch.setattr(ig.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse(
                                {"data": [{"url": "https://cdn.example.com/a.png"}]}))
        r = make_model(tmp_path).generate("猫", save=False)
        assert r["images"] == ["https://cdn.example.com/a.png"]


class TestOptionalFieldDowngrade:
    """提供方专有字段不认时自动剔除重试（换提供方不用改代码）。

    实例：商汤认 `watermark`，Agnes 报 400
    "watermark 不是文生图队列支持的字段"。
    """

    def test_watermark_rejected_then_retried_without_it(self, tmp_path, monkeypatch):
        import models.image_gen as ig
        seen = []

        class R400:
            status_code = 400
            text = ("{\"error\":{\"message\":\"watermark 不是文生图队列支持的字段\"}}")

        class R200:
            status_code = 200

            def json(self):
                return {"data": [{"b64_json": PNG_1PX_B64}]}

        def _post(url, json=None, headers=None, timeout=None):
            seen.append(dict(json))
            return R400() if "watermark" in (json or {}) else R200()

        monkeypatch.setattr(ig.httpx, "post", _post)
        r = make_model(tmp_path).generate("猫")
        assert len(seen) == 2, "应重试一次"
        assert "watermark" in seen[0] and "watermark" not in seen[1]
        assert r["images"][0].endswith(".png")

    def test_unrelated_400_still_raises(self, tmp_path, monkeypatch):
        """不是字段问题（如 401 鉴权）时不应**对同一端点**吞掉错误反复重试。

        注意：这里显式关掉备用端点——跨提供方兜底是另一回事（见
        tests/test_model_fallback.py：主端点 401 时会去试配了别的 key 的备用端点）。
        """
        import models.image_gen as ig

        class Err:
            status_code = 401
            text = "unauthorized"

        calls = []
        monkeypatch.setitem(ig.IMAGE_GEN_CONFIG, "fallback_models", [])
        monkeypatch.setattr(ig.httpx, "post",
                            lambda url, json=None, headers=None, timeout=None:
                            (calls.append(1), Err())[1])
        with pytest.raises(RuntimeError, match="401"):
            make_model(tmp_path).generate_b64("猫")
        assert len(calls) == 1, "同一端点上的无关错误不应重试"

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
