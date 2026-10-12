"""面板的进出面收敛：非回环绑定要凭据、凭据文件随进程生命周期、可选零端口传输。

对照：只绑本机时保持零摩擦（桌面端/CLI 不用改），一旦绑到 0.0.0.0/网卡 IP，
面板就不再是"谁都能调"。
"""
import json
import os
import stat

import pytest

import dashboard.server as srv
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clean_state():
    saved = srv._IPC_TOKEN
    yield
    srv._IPC_TOKEN = saved
    os.environ.pop("DASHBOARD_HOST", None)
    os.environ.pop("DASHBOARD_NO_AUTH", None)


def _client():
    return TestClient(srv.app, base_url="http://127.0.0.1:8080")


class TestLoopbackStaysFrictionless:
    def test_no_token_needed_on_loopback(self):
        srv._IPC_TOKEN = "tok"
        assert _client().get("/api/state").status_code == 200

    def test_no_token_configured_means_no_gate(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
        srv._IPC_TOKEN = ""                       # 未生成凭据（例如直接 import app 跑测试）
        assert _client().get("/api/state").status_code == 200


class TestExposedBindRequiresToken:
    def _exposed(self):
        os.environ["DASHBOARD_HOST"] = "0.0.0.0"
        srv._IPC_TOKEN = "tok-abc"
        return _client()

    def test_missing_token_is_401(self):
        r = self._exposed().get("/api/state")
        assert r.status_code == 401
        assert "ipc.json" in r.json()["error"], "拒绝时要告诉人凭据在哪"

    def test_wrong_token_is_401(self):
        assert self._exposed().get("/api/state",
                                   headers={"X-Agent-Token": "nope"}).status_code == 401

    def test_x_agent_token_header_is_accepted(self):
        assert self._exposed().get("/api/state",
                                   headers={"X-Agent-Token": "tok-abc"}).status_code == 200

    def test_bearer_header_is_accepted(self):
        assert self._exposed().get(
            "/api/state", headers={"Authorization": "Bearer tok-abc"}).status_code == 200

    def test_first_visit_with_query_token_sets_cookie(self):
        c = self._exposed()
        r = c.get("/?token=tok-abc")
        assert r.status_code == 200
        assert c.cookies.get(srv._IPC_COOKIE), "首次带 token 打开要换成 cookie"
        assert c.get("/api/state").status_code == 200, "之后不再需要带 token"

    def test_escape_hatch_is_explicit(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_NO_AUTH", "1")
        assert self._exposed().get("/api/state").status_code == 200


class TestIpcCredentialFile:
    def test_write_and_clear(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SESSION_DIR", str(tmp_path))       # 让 config.PROJECT_ROOT 落到 tmp
        import config
        monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
        srv._IPC_TOKEN = "tok-file"
        path = srv.write_ipc_file("0.0.0.0", 8080, token="tok-file")
        assert os.path.isfile(path), "凭据文件必须落盘，桌面端/CLI 靠它连接"
        payload = json.loads(open(path, encoding="utf-8").read())
        assert payload["token"] == "tok-file"
        assert payload["host"] == "0.0.0.0" and payload["port"] == 8080
        assert payload["pid"] == os.getpid()
        assert payload["started_at"]
        srv.clear_ipc_file()
        assert not os.path.exists(path), "进程退出后凭据不该留着"

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不看 mode 位")
    def test_file_mode_is_600(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
        srv._IPC_TOKEN = "tok-mode"
        path = srv.write_ipc_file("127.0.0.1", 8080)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_file_lives_under_gitignored_memory_dir(self):
        assert "memory/ipc.json" in srv.ipc_file_path().replace("\\", "/")
