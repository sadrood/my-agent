"""pytest 全局隔离配置。
离线时要等连接超时（1.7s/次）——几十个测试累积到分钟级。"""
import asyncio

import pytest

from config import TOOL_CONFIG


@pytest.fixture(autouse=True)
def _keep_main_thread_event_loop():
    """保证主线程始终有一个可用的事件循环。

    `asyncio.run()` 结束会把线程的 loop 置空，而 `botpy.Client` 这类库在构造时要
    `asyncio.get_event_loop()` —— 不补的话，谁先跑谁决定别人的用例能不能过（顺序依赖）。
    """
    def _ensure():
        try:
            asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    _ensure()
    yield
    _ensure()


@pytest.fixture(autouse=True)
def _allow_testclient_host(monkeypatch):
    """放行 TestClient 的默认 Host（testserver）。"""
    monkeypatch.setenv("DASHBOARD_ALLOWED_HOSTS", "testserver")


@pytest.fixture(autouse=True)
def _finite_llm_retry_budget(monkeypatch):
    """测试里重试次数必须有限：开发者 .env 可能配成不限（0），
    那会让恒失败的假 LLM 永远重试、整个测试套件挂死。"""
    from config import TOOL_CONFIG
    monkeypatch.setitem(TOOL_CONFIG, "llm_max_attempts", 6)
    monkeypatch.setitem(TOOL_CONFIG, "llm_quota_retry_budget", 900.0)
    monkeypatch.setitem(TOOL_CONFIG, "llm_quota_min_attempts", 2)


@pytest.fixture(autouse=True)
def _no_llm_fallback_chain(monkeypatch):
    """测试不继承开发者 .env 的备用链（否则会真去打备用端点：联网、慢、不稳定）。"""
    from config import LLM_CONFIG
    monkeypatch.setitem(LLM_CONFIG, "fallback_models", [])


def pytest_configure(config):
    """注册自定义标记。"""
    config.addinivalue_line(
        "markers",
        "real_paths: 该用例专门验证**真实路径锚定**（需要默认存储目录保持原样），"
        "跳过 _isolate_storage_dirs 的重定向",
    )


@pytest.fixture(autouse=True)
def _isolate_storage_dirs(request, monkeypatch, tmp_path):
    """把默认存储目录整体重定向到临时目录——测试**绝不能**写真实数据。"""
    if request.node.get_closest_marker("real_paths"):
        yield
        return
    root = tmp_path / "store"

    try:
        from config import SESSION_CONFIG
        monkeypatch.setitem(SESSION_CONFIG, "dir", str(root / "sessions"))
    except Exception:
        pass

    try:
        import agent.memory as _mem
        monkeypatch.setattr(_mem, "PROJECT_ROOT", str(root))
    except Exception:
        pass

    try:
        import agent.tasks as _tasks
        monkeypatch.setattr(_tasks, "_MEM_DIR", str(root / "memory"))
        monkeypatch.setattr(_tasks, "_TASKS_FILE", str(root / "memory" / "tasks.json"))
        monkeypatch.setattr(_tasks, "_THOUGHTS_FILE", str(root / "memory" / "thoughts.json"))
    except Exception:
        pass

    try:
        import tools.todo as _todo
        monkeypatch.setattr(_todo, "_TODO_DIR", str(root / "memory" / "todos"))
    except Exception:
        pass

    yield


@pytest.fixture(autouse=True)
def _isolate_heavy_runtime_switches(monkeypatch):
    """默认关闭会触发真实副作用的运行时开关（测试可自行覆盖）。"""
    # edit 后不真跑测试：preflight 只应由 test_patch_snapshot 显式开启验证
    monkeypatch.setitem(TOOL_CONFIG, "edit_preflight", False)

    # 不探测内嵌浏览器桥（ToolManager 内是函数级导入，patch 模块属性即可生效）
    try:
        import tools.embedded_browser as _eb
        monkeypatch.setattr(_eb, "probe_bridge", lambda *a, **kw: False)
    except Exception:
        pass

    # 关掉监管者：它默认走**真实 LLM 端点** —— SUPERVISOR_CONFIG["model"] 留空时回退到硬编码的默认模型名，key/base 再回退 GUARDIAN_API_KEY /GUARDIAN_BASE_URL。
    try:
        from config import SUPERVISOR_CONFIG
        monkeypatch.setitem(SUPERVISOR_CONFIG, "enabled", False)
    except Exception:
        pass

    # 同款模式的两处，一并关掉 —— 它们的构造器都是"配置里有 model 就建真 LLM"：；  · build_small_llm：SMALL_MODEL_CONFIG["model"] 非空即建真端点，被**会话标题**
    try:
        from config import SMALL_MODEL_CONFIG
        monkeypatch.setitem(SMALL_MODEL_CONFIG, "enabled", False)
    except Exception:
        pass
    try:
        from config import GUARDIAN_CONFIG
        monkeypatch.setitem(GUARDIAN_CONFIG, "enabled", False)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _reset_computer_ownership():
    """键鼠控制权与输入账本是全局状态：用例之间必须复位，否则前一条的"用户接管"
    会把后面所有写操作都挡掉（表现为莫名其妙的失败）。"""
    try:
        import tools.computer_use as _cu
        _cu.set_owner("agent")
        _cu._HELD_KEYS.clear()
        _cu._HELD_BUTTONS.clear()
    except Exception:
        pass
    yield
    try:
        import tools.computer_use as _cu
        _cu.set_owner("agent")
        _cu._HELD_KEYS.clear()
        _cu._HELD_BUTTONS.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_vision_temporary():
    """会话级临时视觉覆盖是模块级全局状态：/model 切换会写它，用例之间必须复位，
    否则前一条用例设过的临时端点会让后面所有视觉端点断言集体失败。"""
    try:
        from models import vision as _vision
        _vision.set_temporary_primary()
    except Exception:
        pass
    yield
    try:
        from models import vision as _vision
        _vision.set_temporary_primary()
    except Exception:
        pass
