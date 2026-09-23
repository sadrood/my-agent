"""
批次5 回归：Dashboard 的跨站访问防护。

实测背景（2026-09-17 审计）：`allow_origins=["*"]` 的注释写着"只监听 127.0.0.1
所以安全"——但回环绑定**不是**安全边界：浏览器里任何网页都能向 127.0.0.1 发跨域
请求，CORS 正是决定放不放行的那道门。而暴露的接口里有 `POST /api/run`（用用户
自己的 approval=never + danger-full-access 跑任意目标）、`/api/approve`、
`/api/rollback`、`/api/config` —— 组合起来就是本地 RCE。
"""
import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    """直接拿模块级 app（不起 lifespan，避免测试里拉起调度器线程）。"""
    import dashboard.server as srv
    if getattr(srv, "app", None) is None:
        pytest.skip("FastAPI 未安装")
    # base_url 决定默认 Host 头：用真实的回环地址，否则会被 Host 白名单拦下
    return TestClient(srv.app, base_url="http://127.0.0.1:8080")


class TestOriginAllowlist:
    def test_localhost_dev_origin_allowed(self, client):
        r = client.options("/api/run", headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        })
        assert r.headers.get("access-control-allow-origin") in (
            "http://localhost:5173", "*")

    def test_evil_origin_not_allowed(self, client):
        r = client.options("/api/run", headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        })
        acao = r.headers.get("access-control-allow-origin")
        assert acao != "*", "不得对任意来源放行"
        assert acao != "https://evil.example.com", "恶意来源必须被拒"

    def test_null_origin_allowed_for_packaged_desktop(self, client):
        """打包桌面端走 file://，Origin 是 null，必须保留可用。"""
        r = client.options("/api/state", headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
        })
        assert r.headers.get("access-control-allow-origin") in ("null", "*")


class TestWebSocketOriginAllowlist:
    """WebSocket 握手必须自己查 Origin。

    实测漏洞（2026-09-22 审计）：上面两道防线**都只覆盖 http scope** ——
    `@app.middleware("http")` 用的 BaseHTTPMiddleware 与 CORSMiddleware 遇到
    websocket scope 都是直接透传，`websocket_endpoint` 自己也不读 origin。
    于是用户浏览器里任意一个页面 `new WebSocket("ws://127.0.0.1:8090/ws")` 就能：
    ① 收到全部事件流（含审批卡片的 id、工具命令原文、截图）；② 回发
    `approval_response` 直接批准本该由用户裁决的高危命令；③ 发 `stop` 停任务。
    """

    @pytest.mark.parametrize("origin", [
        "https://evil.example.com",
        "http://evil.example.com",
        "http://localhost:9999",        # 回环但不在白名单
    ])
    def test_foreign_origin_rejected(self, client, origin):
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"Origin": origin}):
                pass

    @pytest.mark.parametrize("origin", [
        "http://localhost:5173",        # Vite dev
        "http://127.0.0.1:8080",        # 浏览器版面板
        "null",                         # 打包桌面端 file://
    ])
    def test_allowed_origin_accepted(self, client, origin):
        with client.websocket_connect("/ws", headers={"Origin": origin}):
            pass

    def test_missing_origin_accepted(self, client):
        """无 Origin = 本机进程。浏览器发 WebSocket 必然带 Origin，省不掉。"""
        with client.websocket_connect("/ws"):
            pass


class TestHostAllowlist:
    def test_loopback_host_ok(self, client):
        r = client.get("/api/state", headers={"Host": "127.0.0.1:8080"})
        assert r.status_code != 403

    def test_localhost_host_ok(self, client):
        r = client.get("/api/state", headers={"Host": "localhost:8080"})
        assert r.status_code != 403

    def test_foreign_host_rejected(self, client):
        """DNS rebinding：恶意域名解析到 127.0.0.1 时 Host 是攻击者域名。"""
        r = client.get("/api/state", headers={"Host": "evil.example.com"})
        assert r.status_code == 403
        assert "Host" in r.text or "rebinding" in r.text.lower() or "拒绝" in r.text
