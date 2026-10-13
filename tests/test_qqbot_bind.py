"""扫码绑定：任务/轮询/解密/写 .env/过期换码——全离线（HTTP 打桩），不碰真接口。"""
import base64
import os

import pytest

from agent import qqbot_bind as qb


@pytest.fixture
def stub(monkeypatch):
    """把 _post_json 换成脚本化的假服务端，并记录调用。"""
    calls = []

    def fake(url, payload, timeout=10.0):
        calls.append((url, payload))
        return fake.reply.pop(0)

    fake.reply = []
    fake.calls = calls
    monkeypatch.setattr(qb, "_post_json", fake)
    return fake


class TestTasks:
    def test_key_is_32_bytes_and_random(self):
        a_b64, a_raw = qb.make_bind_key()
        b_b64, _ = qb.make_bind_key()
        assert len(a_raw) == 32 and len(base64.b64decode(a_b64)) == 32
        assert a_b64 != b_b64

    def test_create_task_returns_id(self, stub):
        stub.reply = [{"retcode": 0, "data": {"task_id": "T-1"}}]
        assert qb.create_bind_task("KEY") == "T-1"
        assert stub.calls[0][0] == "https://q.qq.com/lite/create_bind_task"
        assert stub.calls[0][1] == {"key": "KEY"}

    def test_create_task_rejects_bad_retcode(self, stub):
        stub.reply = [{"retcode": 1001, "msg": "频率超限"}]
        with pytest.raises(RuntimeError, match="频率超限"):
            qb.create_bind_task("KEY")

    def test_create_task_requires_task_id(self, stub):
        stub.reply = [{"retcode": 0, "data": {}}]
        with pytest.raises(RuntimeError, match="task_id"):
            qb.create_bind_task("KEY")

    def test_connect_url_shape(self):
        url = qb.build_connect_url("T-1", source="my_agent")
        assert url.startswith("https://q.qq.com/qqbot/openclaw/connect.html?")
        assert "task_id=T-1" in url and "source=my_agent" in url and "_wv=2" in url

    def test_poll_parses_status(self, stub):
        stub.reply = [{"retcode": 0, "data": {"status": 2, "bot_appid": "190",
                                              "bot_encrypt_secret": "E", "user_openid": "U"}}]
        got = qb.poll_bind_result("T-1")
        assert got["status"] is qb.BindStatus.COMPLETED
        assert (got["app_id"], got["encrypt_secret"], got["user_openid"]) == ("190", "E", "U")

    def test_unknown_env(self):
        with pytest.raises(ValueError):
            qb.host_of("staging")


class TestSecret:
    def test_decrypt_matches_official_layout(self):
        """IV 12B + 密文 + tag 16B：自造一份密文，验证解出来的就是原文。"""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = os.urandom(32)
        iv = os.urandom(12)
        blob = AESGCM(key).encrypt(iv, b"SECRET-XYZ", None)
        enc = base64.b64encode(iv + blob).decode()
        assert qb.decrypt_secret(enc, key) == "SECRET-XYZ"


class TestEnvFile:
    def test_appends_when_missing(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        qb.save_env("190", "SEC", str(path))
        text = path.read_text(encoding="utf-8")
        assert "QQBOT_APP_ID=190" in text and "QQBOT_APP_SECRET=SEC" in text
        assert text.startswith("A=1\n")

    def test_replaces_existing_and_keeps_rest(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("A=1\nQQBOT_APP_ID=old\nQQBOT_APP_SECRET=old\nB=2\n", encoding="utf-8")
        qb.save_env("new", "NEWSEC", str(path))
        assert path.read_text(encoding="utf-8").splitlines() == [
            "A=1", "QQBOT_APP_ID=new", "QQBOT_APP_SECRET=NEWSEC", "B=2"]

    def test_fills_placeholder_comment(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("# QQBOT_APP_ID=\n# QQBOT_APP_SECRET=\n", encoding="utf-8")
        qb.save_env("190", "SEC", str(path))
        assert path.read_text(encoding="utf-8").splitlines() == [
            "QQBOT_APP_ID=190", "QQBOT_APP_SECRET=SEC"]

    def test_extra_keys_are_written_too(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        qb.save_env("190", "SEC", str(path), extra={"QQBOT_ALLOW_FROM": "OPENID-1"})
        assert "QQBOT_ALLOW_FROM=OPENID-1" in path.read_text(encoding="utf-8")

    def test_read_env_value_skips_comments_and_blank(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("# QQBOT_ALLOW_FROM=old\n\nQQBOT_APP_ID=190\nQQBOT_ALLOW_FROM=u1\n",
                        encoding="utf-8")
        assert qb.read_env_value(str(path), "QQBOT_ALLOW_FROM") == "u1"
        assert qb.read_env_value(str(path), "QQBOT_APP_SECRET") == ""
        assert qb.read_env_value(str(tmp_path / "nope"), "QQBOT_ALLOW_FROM") == ""


class TestCliOutput:
    """CLI 的落盘/回显策略：默认不回显密钥，选择不落盘时必须给出出口。"""

    def _run(self, monkeypatch, capsys, argv, secret="SECRET-XYZ", allow_from=""):
        import sys
        import types
        monkeypatch.setitem(sys.modules, "qrcode", types.ModuleType("qrcode"))
        monkeypatch.setattr(qb, "qr_png", lambda url, path: path)
        monkeypatch.setattr(qb, "qr_text", lambda url: "<QR>")
        monkeypatch.setattr(qb, "bind", lambda **kw: {
            "app_id": "1903204272", "app_secret": secret, "user_openid": "u"})
        written = []
        monkeypatch.setattr(qb, "save_env",
                            lambda a, s, p, extra=None: written.append((a, s, p, extra)) or p)
        monkeypatch.setattr(qb, "read_env_value", lambda p, k: allow_from)
        code = qb.main(argv)
        return code, capsys.readouterr().out, written

    def test_default_writes_env_and_hides_secret(self, monkeypatch, capsys):
        code, out, written = self._run(monkeypatch, capsys, [])
        assert code == 0 and written and written[0][1] == "SECRET-XYZ"
        assert "AppID=1903204272" in out
        assert "SECRET-XYZ" not in out, "默认不回显密钥"

    def test_no_write_env_without_print_secret_says_so(self, monkeypatch, capsys):
        code, out, written = self._run(monkeypatch, capsys,
                                       ["--no-write-env"])
        assert code == 0 and written == [], "选了不落盘就不能偷偷写"
        assert "SECRET-XYZ" not in out
        assert "--print-secret" in out, "要告诉人这份密钥还能怎么拿到"

    def test_print_secret_gives_the_escape_hatch(self, monkeypatch, capsys):
        code, out, written = self._run(monkeypatch, capsys,
                                       ["--no-write-env", "--print-secret"])
        assert code == 0 and written == []
        assert "SECRET-XYZ" in out, "明确要了就一定给"
        assert "日志" in out or "保存" in out, "同时提醒别泄露"

    def test_flag_is_registered(self):
        import inspect
        src = inspect.getsource(qb.main)
        assert "--print-secret" in src

    def test_bind_writes_openid_into_allow_from(self, monkeypatch, capsys):
        code, out, written = self._run(monkeypatch, capsys, [])
        assert code == 0 and written[0][3] == {"QQBOT_ALLOW_FROM": "u"}, "绑定人应自动进白名单"
        assert "白名单" in out

    def test_existing_allow_from_is_not_overwritten(self, monkeypatch, capsys):
        code, out, written = self._run(monkeypatch, capsys, [], allow_from="someone-else")
        assert code == 0 and written[0][3] == {}, "已有白名单不许被绑定流程覆盖"


class TestBindFlow:
    def _patch(self, monkeypatch, results):
        """按顺序喂 poll 结果；返回记录下来的 (create, poll) 次数。"""
        seen = {"create": 0, "poll": 0}

        def fake_create(key_b64, env="production", timeout=10.0):
            seen["create"] += 1
            return f"T-{seen['create']}"

        def fake_poll(task_id, env="production", timeout=10.0):
            seen["poll"] += 1
            return results[min(seen["poll"] - 1, len(results) - 1)]

        monkeypatch.setattr(qb, "create_bind_task", fake_create)
        monkeypatch.setattr(qb, "poll_bind_result", fake_poll)
        monkeypatch.setattr(qb, "decrypt_secret", lambda enc, key: f"PLAIN:{enc}")
        return seen

    def test_waits_then_succeeds(self, monkeypatch):
        seen = self._patch(monkeypatch, [
            {"status": qb.BindStatus.PENDING},
            {"status": qb.BindStatus.COMPLETED, "app_id": "190",
             "encrypt_secret": "ENC", "user_openid": "U"},
        ])
        got = qb.bind(sleep=lambda _s: None)
        assert got == {"app_id": "190", "app_secret": "PLAIN:ENC", "user_openid": "U"}
        assert seen["poll"] == 2

    def test_expired_gets_fresh_qr(self, monkeypatch):
        seen = self._patch(monkeypatch, [
            {"status": qb.BindStatus.EXPIRED},
            {"status": qb.BindStatus.COMPLETED, "app_id": "190",
             "encrypt_secret": "ENC", "user_openid": ""},
        ])
        expired = []
        qr_urls = []
        qb.bind(sleep=lambda _s: None, on_qr=qr_urls.append, on_expired=lambda: expired.append(1))
        assert seen["create"] == 2 and expired == [1] and len(qr_urls) == 2

    def test_poll_error_is_retried(self, monkeypatch):
        calls = {"n": 0}

        def fake_poll(task_id, env="production", timeout=10.0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("网络抖了")
            return {"status": qb.BindStatus.COMPLETED, "app_id": "190",
                    "encrypt_secret": "E", "user_openid": ""}

        monkeypatch.setattr(qb, "create_bind_task", lambda *a, **k: "T-1")
        monkeypatch.setattr(qb, "poll_bind_result", fake_poll)
        monkeypatch.setattr(qb, "decrypt_secret", lambda enc, key: "SEC")
        assert qb.bind(sleep=lambda _s: None)["app_secret"] == "SEC"

    def test_timeout(self, monkeypatch):
        self._patch(monkeypatch, [{"status": qb.BindStatus.PENDING}])
        with pytest.raises(TimeoutError):
            qb.bind(total_timeout=-1, sleep=lambda _s: None)


class TestQrAndCli:
    def test_qr_text_is_not_empty(self):
        assert len(qb.qr_text("https://q.qq.com/x").strip()) > 50

    def test_qr_png_written(self, tmp_path):
        path = qb.qr_png("https://q.qq.com/x", str(tmp_path / "sub" / "qr.png"))
        assert os.path.getsize(path) > 100

    def test_main_success_writes_env(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(qb, "bind", lambda **kw: {"app_id": "190", "app_secret": "SEC",
                                                      "user_openid": ""})
        env_file = tmp_path / ".env"
        assert qb.main(["--env-file", str(env_file), "--png", str(tmp_path / "q.png")]) == 0
        assert "QQBOT_APP_ID=190" in env_file.read_text(encoding="utf-8")
        assert "SEC" not in capsys.readouterr().out        # 密钥不回显

    def test_main_dry_run_and_failure(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(qb, "bind", lambda **kw: {"app_id": "190", "app_secret": "SEC",
                                                      "user_openid": ""})
        env_file = tmp_path / ".env"
        assert qb.main(["--no-write-env", "--env-file", str(env_file)]) == 0
        assert not env_file.exists()

        def boom(**kw):
            raise TimeoutError("等待扫码超时")

        monkeypatch.setattr(qb, "bind", boom)
        assert qb.main(["--env-file", str(env_file)]) == 1
        assert "绑定失败" in capsys.readouterr().out
