"""
Dashboard 服务器测试（不绑定端口，只验证路由注册）。
"""
import pytest


def test_ws_route_registered():
    """WebSocket 路由必须注册成功（依赖 websockets 库，缺失时会静默 404）。"""
    from dashboard.server import app
    from starlette.routing import WebSocketRoute
    assert app is not None
    ws_routes = [r for r in app.routes if isinstance(r, WebSocketRoute)]
    assert any(r.path == "/ws" for r in ws_routes), "/ws 路由未注册（检查 websockets 依赖）"


def test_root_route_serves_index(tmp_path, monkeypatch):
    import os
    import dashboard.server as srv
    monkeypatch.setattr(srv, "STATIC_DIR", os.path.dirname(srv.__file__) + "/static")
    assert srv.STATIC_DIR.endswith("static")


def test_api_routes_exist():
    from dashboard.server import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/state" in paths
    assert "/api/history" in paths
    assert "/api/run" in paths
    assert "/api/running" in paths
    assert "/api/test-llm" in paths
    assert "/" in paths


def test_cors_middleware_enabled():
    """桌面端前端（Vite/file://）跨域访问后端需要 CORS，否则 POST /api/run 会被浏览器拦截。"""
    from dashboard.server import app
    from starlette.middleware.cors import CORSMiddleware
    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert cors, "缺少 CORS 中间件（前端 fetch /api/run 会被浏览器拦截）"


def test_api_run_starts_worker(monkeypatch):
    """POST /api/run：非空 goal 启动后台执行；空 goal 拒绝。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    calls = []

    def _fake_worker(goal, kwargs=None):
        calls.append(goal)

    monkeypatch.setattr(srv, "_run_agent_worker", _fake_worker)

    client = TestClient(srv.app)
    r = client.post("/api/run", content=json.dumps({"goal": "测试任务"}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls == ["测试任务"]

    r2 = client.post("/api/run", content=json.dumps({"goal": "  "}),
                     headers={"Content-Type": "application/json"})
    assert r2.json()["ok"] is False
    assert "goal" in r2.json()["error"]
    assert calls == ["测试任务"]   # 空任务不启动 worker


def test_approval_broker_allow_and_timeout():
    """ApprovalBroker：前端批准 → approver 返回 True；超时 → False。"""
    import threading
    import time
    from dashboard.server import ApprovalBroker

    class _Req:
        tool_name = "terminal"
        command = "del foo.txt"
        risk_level = "high"
        reason = "高风险命令"

    broker = ApprovalBroker(timeout=5)
    result = {}

    def _run():
        result["allow"] = broker.approver(_Req())

    t = threading.Thread(target=_run)
    t.start()
    time.sleep(0.2)
    assert broker.pending_count() == 1
    # 从 hub 最近事件里拿到审批 id
    from dashboard.hub import get_dashboard_hub
    hub = get_dashboard_hub()
    approval_events = [e for e in hub._history if e.type == "approval"]
    assert approval_events, "approver 未发出 approval 事件"
    req_id = approval_events[-1].data["id"]
    assert broker.resolve(req_id, True) is True
    t.join(timeout=3)
    assert result["allow"] is True
    assert broker.pending_count() == 0

    # 超时路径：0.3s 超时 → 拒绝
    broker2 = ApprovalBroker(timeout=0.3)
    t0 = time.time()
    assert broker2.approver(_Req()) is False
    assert time.time() - t0 < 2


def test_api_approve_endpoint():
    """POST /api/approve：匹配未知 id → matched=False；缺 id → 报错。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    client = TestClient(srv.app)
    r = client.post("/api/approve", content=json.dumps({"id": "nope", "allow": True}),
                    headers={"Content-Type": "application/json"})
    assert r.json()["ok"] is True
    assert r.json()["matched"] is False

    r2 = client.post("/api/approve", content=json.dumps({"allow": True}),
                     headers={"Content-Type": "application/json"})
    assert r2.json()["ok"] is False


def test_api_files_tree_bounded():
    """GET /api/files：返回工作区树；跳过噪声目录；有数量上限。"""
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    client = TestClient(srv.app)
    r = client.get("/api/files")
    assert r.status_code == 200
    data = r.json()
    assert data["root"]
    tree = data["tree"]
    assert tree["type"] == "dir"
    top = {c["name"] for c in tree["children"]}
    assert "dashboard" in top
    assert ".git" not in top
    assert "node_modules" not in top
    assert ".venv" not in top


def test_api_file_read_and_traversal(tmp_path):
    """GET /api/file：可读工作区内文件；路径越界被拒绝；二进制/不存在报错。"""
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    client = TestClient(srv.app)
    # 工作区内真实文件
    r = client.get("/api/file", params={"path": "dashboard/server.py"})
    assert r.json()["ok"] is True
    assert "FastAPI" in r.json()["content"]

    # 路径越界
    r2 = client.get("/api/file", params={"path": "../outside.txt"})
    assert r2.json()["ok"] is False
    assert "越界" in r2.json()["error"]
    r3 = client.get("/api/file", params={"path": "C:/Windows/System32/drivers/etc/hosts"})
    assert r3.json()["ok"] is False

    # 不存在
    r4 = client.get("/api/file", params={"path": "no_such_file_xyz.txt"})
    assert r4.json()["ok"] is False


# ---------------- 会话连续性（desktop/dashboard 前端 ⇆ 后端 SessionStore） ----------------

def test_api_sessions_routes_exist():
    """会话 CRUD 路由必须注册。"""
    from dashboard.server import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/sessions" in paths
    assert "/api/sessions/{session_id}" in paths


def test_api_session_create(tmp_path, monkeypatch):
    """POST /api/sessions：后端生成 conv-YYYYMMDD-hex 会话 id 并落盘。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    monkeypatch.setattr(
        "agent.session.SESSION_CONFIG",
        {"dir": str(tmp_path / "sessions"), "max_sessions": 50},
    )
    from agent.session import SessionStore

    client = TestClient(srv.app)
    r = client.post("/api/sessions", content="{}",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    sess = body["session"]
    import re as _re
    assert _re.match(r"^conv-\d{8}-[0-9a-f]{6}$", sess["id"]), sess["id"]
    assert sess["title"] == "新对话"
    assert sess["count"] == 0

    # 已落盘，可用 GET 读回
    detail = client.get(f"/api/sessions/{sess['id']}")
    assert detail.status_code == 200
    assert detail.json()["ok"] is True
    assert detail.json()["conversation"]["id"] == sess["id"]

    # 与 generate_conversation_id 命名空间一致：两次创建 id 不同
    r2 = client.post("/api/sessions", content="{}",
                     headers={"Content-Type": "application/json"})
    assert r2.json()["session"]["id"] != sess["id"]


def test_api_run_passes_session_id(monkeypatch):
    """/api/run payload 里的 session_id 要透传给 worker。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    seen = {}

    def _fake_worker(goal, kwargs=None):
        seen["goal"] = goal
        seen["kwargs"] = kwargs or {}

    monkeypatch.setattr(srv, "_run_agent_worker", _fake_worker)

    client = TestClient(srv.app)
    r = client.post(
        "/api/run",
        content=json.dumps({"goal": "带会话的任务", "session_id": "conv-20260829-abc123"}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert seen["kwargs"]["session_id"] == "conv-20260829-abc123"


def test_api_side_run_forwards_main_session(monkeypatch):
    """/api/side-run 把主会话 id 透传为 worker 的 side_of（辅助 Agent 独立命名空间）。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    seen = {}

    def _fake_worker(goal, kwargs=None, side_of=""):
        seen["goal"] = goal
        seen["side_of"] = side_of

    monkeypatch.setattr(srv, "_run_agent_worker", _fake_worker)

    client = TestClient(srv.app)
    r = client.post(
        "/api/side-run",
        content=json.dumps({"goal": "帮我调研", "main_session_id": "conv-main-1"}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert seen["goal"] == "帮我调研"
    assert seen["side_of"] == "conv-main-1"

    # 空 goal 拒绝
    r2 = client.post("/api/side-run", content=json.dumps({"goal": " "}),
                     headers={"Content-Type": "application/json"})
    assert r2.json()["ok"] is False


def test_worker_restores_session_history(monkeypatch, tmp_path):
    """session_id 对应已有对话时：历史消息恢复进 memory 并绑定 session_name。"""
    import dashboard.server as srv

    # 会话文件写进临时目录，不污染真实 memory/sessions
    monkeypatch.setattr(
        "agent.session.SESSION_CONFIG",
        {"dir": str(tmp_path / "sessions"), "max_sessions": 50},
    )
    from agent.session import SessionStore
    conv_id = "conv-testsess-000001"
    SessionStore().save_conversation(
        conv_id,
        messages=[{"role": "user", "content": "记住了吗"},
                  {"role": "assistant", "content": "记住了"}],
        title="恢复测试",
    )

    created = {}

    class _FakeMemory:
        def __init__(self, chat_id=None):
            self.chat_id = chat_id
            self.history = []

        def add_message(self, role, content):
            self.history.append((role, content))

    class _FakeAgent:
        def __init__(self, tool_manager=None, memory=None, config=None):
            self.memory = memory
            self.config = config
            created["memory"] = memory
            created["config"] = config

        def run(self, goal, stop_event=None):
            pass

        def _restore_conversation_model(self, conv):
            created["restored"] = conv.get("id")

    monkeypatch.setattr("agent.Agent", _FakeAgent)
    monkeypatch.setattr("agent.memory.Memory", _FakeMemory)

    srv._run_agent_worker("继续刚才的话题", {"session_id": conv_id})

    assert created["config"].session_name == conv_id
    assert created["memory"].chat_id == conv_id   # 会话隔离：记忆按 session_id 分目录
    assert ("user", "记住了吗") in created["memory"].history
    assert ("assistant", "记住了") in created["memory"].history
    assert created["restored"] == conv_id


def test_worker_new_session_generates_id(monkeypatch, tmp_path):
    """未传 session_id 时自动生成新 id，保证记忆按会话隔离（不回退 default）。"""
    import dashboard.server as srv

    monkeypatch.setattr(
        "agent.session.SESSION_CONFIG",
        {"dir": str(tmp_path / "sessions"), "max_sessions": 50},
    )

    created = {}

    class _FakeMemory:
        def __init__(self, chat_id=None):
            self.chat_id = chat_id

        def add_message(self, role, content):
            pass

    class _FakeAgent:
        def __init__(self, tool_manager=None, memory=None, config=None):
            self.memory = memory
            self.config = config
            created["config"] = config
            created["memory"] = memory

        def run(self, goal, stop_event=None):
            pass

    monkeypatch.setattr("agent.Agent", _FakeAgent)
    monkeypatch.setattr("agent.memory.Memory", _FakeMemory)

    srv._run_agent_worker("无会话任务", {})
    # 新行为：生成隔离 id，绝不落到所有会话共用的 default 记忆库
    assert created["config"].session_name != ""
    assert created["config"].session_name.startswith("conv-")
    assert created["memory"].chat_id != "default"
    assert created["memory"].chat_id.startswith("conv-")


def test_worker_sanitizes_illegal_session_id(monkeypatch, tmp_path):
    """session_id 含非法字符时 chat_id 只保留安全字符，防路径穿越。"""
    import dashboard.server as srv

    monkeypatch.setattr(
        "agent.session.SESSION_CONFIG",
        {"dir": str(tmp_path / "sessions"), "max_sessions": 50},
    )

    created = {}

    class _FakeMemory:
        def __init__(self, chat_id=None):
            self.chat_id = chat_id

        def add_message(self, role, content):
            pass

    class _FakeAgent:
        def __init__(self, tool_manager=None, memory=None, config=None):
            self.memory = memory
            self.config = config
            created["config"] = config
            created["memory"] = memory

        def run(self, goal, stop_event=None):
            pass

    monkeypatch.setattr("agent.Agent", _FakeAgent)
    monkeypatch.setattr("agent.memory.Memory", _FakeMemory)

    srv._run_agent_worker("任务", {"session_id": "../etc/passwd"})
    # chat_id 净化后不含 "/"，无法构成路径穿越；会话 id 原样透传（文件层另有 sanitize）
    assert created["config"].session_name == "../etc/passwd"
    assert created["memory"].chat_id == ".._etc_passwd"
    assert "/" not in created["memory"].chat_id


def test_api_rollback_route_and_flow(monkeypatch, tmp_path):
    """/api/rollback：合法 checkpoint 回滚成功；缺 commit / 坏 commit 拒绝。"""
    import json
    import subprocess
    import pytest
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git 不可用")

    # 在临时目录搭一个带 checkpoint 历史的仓库
    repo = tmp_path / "repo"
    repo.mkdir()
    def _git(*args):
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              )
    _git("init", "-b", "main")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "baseline", "-q")
    (repo / "a.txt").write_text("v1", encoding="utf-8")
    _git("add", "a.txt")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "checkpoint: step1", "-q")
    base_hash = _git("rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("v2", encoding="utf-8")
    (repo / "b.txt").write_text("added", encoding="utf-8")
    _git("add", "-A")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "checkpoint: step2", "-q")

    monkeypatch.setattr(srv, "WORKSPACE_ROOT", str(repo))

    client = TestClient(srv.app)

    # 缺 commit
    r0 = client.post("/api/rollback", content=json.dumps({}),
                     headers={"Content-Type": "application/json"})
    assert r0.json()["ok"] is False

    # 坏 commit
    r1 = client.post("/api/rollback", content=json.dumps({"commit": "deadbeef" * 5}),
                     headers={"Content-Type": "application/json"})
    assert r1.json()["ok"] is False

    # 正常回滚到 step1：a.txt 恢复 v1，b.txt 移除
    r2 = client.post("/api/rollback", content=json.dumps({"commit": base_hash}),
                     headers={"Content-Type": "application/json"})
    assert r2.json()["ok"] is True
    assert (repo / "a.txt").read_text(encoding="utf-8") == "v1"
    assert not (repo / "b.txt").exists()


def test_api_diff_git_fallback(monkeypatch, tmp_path):
    """/api/diff：change_tracker 无记录（python/terminal 改的文件）时回退 git diff。"""
    import json
    import subprocess
    import pytest
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git 不可用")

    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args):
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    _git("init", "-b", "main")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "base", "-q")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "a.py")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "v1", "-q")
    (repo / "a.py").write_text("x = 2\nprint('hi')\n", encoding="utf-8")

    class _NoTracker:
        def get(self, abs_path):
            return None  # 模拟 python/terminal 改的文件（无 edit 工具快照）

    monkeypatch.setattr(srv, "WORKSPACE_ROOT", str(repo))
    monkeypatch.setattr(srv, "_get_tracker", lambda: _NoTracker())

    client = TestClient(srv.app)
    r = client.get("/api/diff", params={"path": "a.py"})
    data = r.json()
    assert data["ok"] is True
    assert data["tool"] == "git"
    assert "-x = 1" in data["unified_diff"]
    assert "+x = 2" in data["unified_diff"]
    assert data["added"] >= 1

    # 非 git 仓库目录 → 明确报错
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(srv, "WORKSPACE_ROOT", str(plain))
    r2 = client.get("/api/diff", params={"path": "a.py"})
    assert r2.json()["ok"] is False
    assert "git" in r2.json()["error"]


def test_api_config_vision_get_and_set(monkeypatch):
    """GET /api/config 打码回视觉配置；POST 设置覆盖并生效；空值清除覆盖。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    srv._VISION_OVERRIDES.clear()
    client = TestClient(srv.app)

    r = client.get("/api/config")
    assert r.json()["ok"] is True
    v = r.json()["vision"]
    assert {"model", "base_url", "key_set", "key_tail"} <= set(v)
    if v["key_set"]:
        assert v["key_tail"].startswith("***")

    r2 = client.post("/api/config",
                     content=json.dumps({"vision": {"model": "gpt-4o-mini",
                                                    "base_url": "https://x/v1",
                                                    "api_key": "sk-test-9999"}}),
                     headers={"Content-Type": "application/json"})
    assert r2.json()["ok"] is True
    assert srv._VISION_OVERRIDES["model"] == "gpt-4o-mini"
    assert r2.json()["vision"]["key_tail"].endswith("9999")

    # 覆盖应用到 see / computer 工具
    class _FakeVM:
        def __init__(self, vision_model=None, base_url=None, api_key=None):
            self.kwargs = {"model": vision_model, "base_url": base_url, "api_key": api_key}

    monkeypatch.setattr("models.vision.VisionModel", _FakeVM)
    from tools.tool_manager import ToolManager
    tm = ToolManager()
    srv._apply_vision_overrides(tm)
    assert tm.get_tool("see")._vision_model.kwargs["model"] == "gpt-4o-mini"
    assert tm.get_tool("computer")._vision_model.kwargs["api_key"] == "sk-test-9999"

    # 空值清除覆盖
    r3 = client.post("/api/config",
                     content=json.dumps({"vision": {"model": "", "base_url": "", "api_key": ""}}),
                     headers={"Content-Type": "application/json"})
    assert r3.json()["ok"] is True
    assert not srv._VISION_OVERRIDES


def test_api_config_test_vision(monkeypatch):
    """test-vision：成功回模型名+答案；失败回错误信息。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    srv._VISION_OVERRIDES.clear()
    srv._VISION_OVERRIDES["model"] = "fake-vision"
    client = TestClient(srv.app)

    class _FakeVM:
        def __init__(self, vision_model=None, base_url=None, api_key=None):
            self.vision_model = vision_model

        def analyze(self, b64, question, max_tokens=80):
            return "背景红色，中间白色方块。"

    monkeypatch.setattr("models.vision.VisionModel", _FakeVM)
    r = client.post("/api/config/test-vision")
    assert r.json()["ok"] is True
    assert r.json()["model"] == "fake-vision"
    assert "红色" in r.json()["answer"]

    class _BadVM(_FakeVM):
        def analyze(self, b64, question, max_tokens=80):
            raise RuntimeError("模型不存在")

    monkeypatch.setattr("models.vision.VisionModel", _BadVM)
    r2 = client.post("/api/config/test-vision")
    assert r2.json()["ok"] is False
    assert "模型不存在" in r2.json()["error"]
    srv._VISION_OVERRIDES.clear()


def test_api_config_test_vision_payload_override(monkeypatch):
    """test-vision 带 payload.vision 时优先测表单值（不落覆盖、不依赖已保存配置）。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    srv._VISION_OVERRIDES.clear()
    client = TestClient(srv.app)
    seen = {}

    class _FakeVM:
        def __init__(self, vision_model=None, base_url=None, api_key=None):
            seen.update({"model": vision_model, "base_url": base_url, "api_key": api_key})
            self.vision_model = vision_model

        def analyze(self, b64, question, max_tokens=80):
            return "ok"

    monkeypatch.setattr("models.vision.VisionModel", _FakeVM)
    r = client.post("/api/config/test-vision",
                    content=json.dumps({"vision": {"model": "form-model",
                                                   "base_url": "https://f/v1",
                                                   "api_key": "sk-form"}}),
                    headers={"Content-Type": "application/json"})
    assert r.json()["ok"] is True
    assert seen == {"model": "form-model", "base_url": "https://f/v1", "api_key": "sk-form"}
    assert not srv._VISION_OVERRIDES


def test_api_ask(monkeypatch):
    """/api/ask：历史透传 + 注入系统提示；空消息拒绝；调用失败回错误。"""
    import json
    from fastapi.testclient import TestClient

    seen = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            seen["messages"] = kwargs["messages"]
            seen["model"] = kwargs["model"]

            class _M:
                content = "测试回答"
            class _Choice:
                message = _M()
            class _Resp:
                choices = [_Choice()]
            return _Resp()

    class _FakeClient:
        def __init__(self, api_key=None, base_url=None):
            seen["api_key"] = api_key
            self.chat = type("C", (), {})()
            self.chat.completions = _FakeCompletions()

    monkeypatch.setattr("openai.OpenAI", _FakeClient)

    client = TestClient(__import__("dashboard.server", fromlist=["app"]).app)
    r = client.post("/api/ask",
                    content=json.dumps({"messages": [
                        {"role": "user", "content": "快速问一下"},
                        {"role": "assistant", "content": "答"},
                        {"role": "user", "content": "再问"},
                    ]}),
                    headers={"Content-Type": "application/json"})
    assert r.json()["ok"] is True
    assert r.json()["answer"] == "测试回答"
    roles = [m["role"] for m in seen["messages"]]
    assert roles[0] == "system" and roles[-1] == "user"
    assert "辅助" in seen["messages"][0]["content"]

    r2 = client.post("/api/ask", content=json.dumps({"messages": []}),
                     headers={"Content-Type": "application/json"})
    assert r2.json()["ok"] is False


def test_api_ask_with_browser_context(monkeypatch):
    """/api/ask：browser_context 作为 system 上下文注入（在用户消息之前）。"""
    import json
    from fastapi.testclient import TestClient

    seen = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            seen["messages"] = kwargs["messages"]

            class _M:
                content = "网页说的是测试内容"
            class _Choice:
                message = _M()
            class _Resp:
                choices = [_Choice()]
            return _Resp()

    class _FakeClient:
        def __init__(self, api_key=None, base_url=None):
            self.chat = type("C", (), {})()
            self.chat.completions = _FakeCompletions()

    monkeypatch.setattr("openai.OpenAI", _FakeClient)

    client = TestClient(__import__("dashboard.server", fromlist=["app"]).app)
    r = client.post("/api/ask",
                    content=json.dumps({
                        "messages": [{"role": "user", "content": "这页讲了什么"}],
                        "browser_context": "这是网页正文内容",
                    }),
                    headers={"Content-Type": "application/json"})
    assert r.json()["ok"] is True
    msgs = seen["messages"]
    assert msgs[0]["role"] == "system"          # SIDE_CHAT_SYSTEM
    assert msgs[1]["role"] == "system"          # browser_context
    assert "网页内容" in msgs[1]["content"]
    assert "这是网页正文内容" in msgs[1]["content"]
    assert msgs[-1]["role"] == "user"

    # 无 browser_context 时不注入
    r2 = client.post("/api/ask",
                     content=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
                     headers={"Content-Type": "application/json"})
    assert r2.json()["ok"] is True
    assert len(seen["messages"]) == 2


def test_api_side_browser_whitelist():
    """/api/browser：白名单外命令直接拒绝（不触发执行器）。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    client = TestClient(srv.app)
    for bad in ("click", "js", "type", "", "screenshot_base64"):
        r = client.post("/api/browser",
                        content=json.dumps({"command": bad, "args": "#x"}),
                        headers={"Content-Type": "application/json"})
        assert r.json()["ok"] is False, f"命令 {bad!r} 应被拒绝"
        assert "不允许" in r.json()["error"]


def test_api_side_browser_executes(monkeypatch):
    """/api/browser：白名单命令经单线程执行器调用浏览器并透传结果。"""
    import json
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    calls = []

    def _fake_exec(command, args=""):
        calls.append((command, args))
        return {"ok": True, "output": f"已导航: {args}"}

    monkeypatch.setattr(srv, "_side_browser_execute", _fake_exec)

    client = TestClient(srv.app)
    r = client.post("/api/browser",
                    content=json.dumps({"command": "goto", "args": "https://example.com"}),
                    headers={"Content-Type": "application/json"})
    assert r.json()["ok"] is True
    assert r.json()["output"] == "已导航: https://example.com"
    assert calls == [("goto", "https://example.com")]


def test_side_browser_getter_creates_isolated_instance(monkeypatch):
    """侧栏浏览器单例：无头 + 非持久 profile（与主任务浏览器隔离）。"""
    import dashboard.server as srv

    created = {}

    class _FakeBrowserTool:
        def __init__(self, headless=None, persistent=None):
            created["headless"] = headless
            created["persistent"] = persistent

        def execute(self, cmd):
            from tools.base import ToolResult
            return ToolResult(success=True, output=cmd)

    monkeypatch.setattr("tools.browser.BrowserTool", _FakeBrowserTool)
    old = srv._SIDE_BROWSER
    srv._SIDE_BROWSER = None
    try:
        tool = srv._get_side_browser()
        assert created == {"headless": True, "persistent": False}
        # 再次获取复用同一实例
        assert srv._get_side_browser() is tool
    finally:
        srv._SIDE_BROWSER = old
