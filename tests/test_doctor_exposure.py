"""doctor 的「本地暴露面」检查：面板绑定地址 + CDP 调试端口是否敞着。"""
import socket

from agent.doctor import _check_exposure, run_doctor


class TestExposureCheck:
    def test_loopback_default_is_ok(self, monkeypatch):
        for var in ("DASHBOARD_HOST", "MY_AGENT_DASHBOARD_HOST", "HOST"):
            monkeypatch.delenv(var, raising=False)
        r = _check_exposure()
        assert r["ok"] is True
        assert "仅本机可达" in r["message"]

    def test_exposed_bind_mentions_the_credential_file(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
        r = _check_exposure()
        assert r["ok"] is True, "绑非回环本身不算错——但要说明凭据在哪"
        assert "ipc.json" in r["message"]

    def test_open_cdp_port_is_flagged_with_a_fix(self, monkeypatch):
        for var in ("DASHBOARD_HOST", "MY_AGENT_DASHBOARD_HOST", "HOST"):
            monkeypatch.delenv(var, raising=False)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", 9222))
            srv.listen(1)
        except OSError:
            import pytest
            pytest.skip("9222 被占用/不可绑，跳过")
        try:
            r = _check_exposure()
            assert r["ok"] is False
            assert "9222" in r["message"]
            assert "--remote-debugging-pipe" in r["hint"], "报问题必须同时给修法"
        finally:
            srv.close()

    def test_check_is_registered_in_doctor(self):
        import inspect
        assert "_check_exposure" in inspect.getsource(run_doctor)

    def test_never_raises_even_if_probe_fails(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
        assert isinstance(_check_exposure(), dict)
