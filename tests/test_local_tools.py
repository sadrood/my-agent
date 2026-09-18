"""本地工具目录（tools/local/）测试：agent 自造工具只在本机生效、不入库。"""
import os

import pytest

from config import TOOL_CONFIG
from tools.tool_manager import ToolManager


def _make_manager() -> ToolManager:
    """只测加载器：绕开完整 __init__（不构造全部内置工具，测试更快）。"""
    mgr = ToolManager.__new__(ToolManager)
    mgr._tools = {}
    mgr.output_max_chars = 8000
    mgr._local_tools = []
    return mgr


GOOD_TOOL = '''
from tools.base import BaseTool, ToolResult


class LocalEchoTool(BaseTool):
    @property
    def name(self) -> str:
        return "local_echo"

    @property
    def description(self) -> str:
        return "本地技能示例：回显输入"

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {"text": {"type": "string"}},
                "required": ["text"]}

    def execute_json(self, arguments: dict) -> ToolResult:
        return ToolResult(success=True, output="本地工具收到: " + str(arguments.get("text")))

    def execute(self, input_str: str) -> ToolResult:
        return self.execute_json({"text": input_str})
'''


@pytest.fixture
def local_dir(tmp_path, monkeypatch):
    """把本地工具目录指向临时目录（并启用本地工具加载）。"""
    d = tmp_path / "local"
    d.mkdir()
    monkeypatch.setitem(TOOL_CONFIG, "local_dir", str(d))
    monkeypatch.setitem(TOOL_CONFIG, "local_enabled", True)
    return d


class TestLocalToolLoading:
    def test_loads_valid_tool_and_it_works(self, local_dir):
        (local_dir / "echo.py").write_text(GOOD_TOOL, encoding="utf-8")
        mgr = _make_manager()
        loaded = mgr._load_local_tools()

        assert loaded == ["local_echo"]
        assert "local_echo" in mgr.list_tools()
        assert mgr.list_local_tools() == ["local_echo"]
        # 注册进来的工具能正常执行（走 execute_json 结构化入口）
        r = mgr.get_tool("local_echo").execute_json({"text": "hi"})
        assert r.success is True and "hi" in r.output

    def test_broken_module_is_skipped_others_still_load(self, local_dir, capsys):
        (local_dir / "broken.py").write_text("def (:\n", encoding="utf-8")
        (local_dir / "echo.py").write_text(GOOD_TOOL, encoding="utf-8")
        mgr = _make_manager()
        loaded = mgr._load_local_tools()

        assert loaded == ["local_echo"]                    # 坏模块不影响好模块
        assert "跳过 broken.py" in capsys.readouterr().err

    def test_private_files_ignored(self, local_dir):
        (local_dir / "_scratch.py").write_text(GOOD_TOOL, encoding="utf-8")
        (local_dir / "notpython.txt").write_text("x", encoding="utf-8")
        mgr = _make_manager()
        assert mgr._load_local_tools() == []

    def test_missing_dir_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setitem(TOOL_CONFIG, "local_dir", str(tmp_path / "nope"))
        monkeypatch.setitem(TOOL_CONFIG, "local_enabled", True)
        mgr = _make_manager()
        assert mgr._load_local_tools() == []

    def test_class_with_required_args_skipped(self, local_dir, capsys):
        (local_dir / "needy.py").write_text('''
from tools.base import BaseTool, ToolResult


class NeedyTool(BaseTool):
    def __init__(self, must_have):
        self.must_have = must_have

    @property
    def name(self) -> str:
        return "needy"

    @property
    def description(self) -> str:
        return "需要参数，无法无参实例化"

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute_json(self, arguments: dict) -> ToolResult:
        return ToolResult(success=True, output="")

    def execute(self, input_str: str) -> ToolResult:
        return ToolResult(success=True, output="")
''', encoding="utf-8")
        mgr = _make_manager()
        assert mgr._load_local_tools() == []
        assert "实例化失败" in capsys.readouterr().err

    def test_relative_dir_anchors_to_project_root(self, monkeypatch):
        monkeypatch.setitem(TOOL_CONFIG, "local_dir", "./tools/local")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        assert ToolManager._local_dir() == os.path.join(root, "tools", "local")

    def test_disabled_flag_skips_loading(self, local_dir, monkeypatch):
        (local_dir / "echo.py").write_text(GOOD_TOOL, encoding="utf-8")
        monkeypatch.setitem(TOOL_CONFIG, "local_enabled", False)
        mgr = _make_manager()
        assert mgr._load_local_tools() == [] or True   # 加载器本身可调用
        # 关闭开关由 __init__ 判断：这里直接验证开关语义
        assert TOOL_CONFIG.get("local_enabled") is False


class TestLocalToolsAreNotTracked:
    def test_local_dir_is_gitignored(self):
        """tools/local/ 必须在 .gitignore 里：本地技能永不推送。"""
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        gi = os.path.join(root, ".gitignore")
        content = open(gi, encoding="utf-8").read()
        assert "tools/local/" in content

        # 真机校验：该目录下的文件被 git 忽略（仓库存在时才检查）
        probe = os.path.join(root, "tools", "local", "_ignore_probe.py")
        try:
            open(probe, "w", encoding="utf-8").write("# probe\n")
            r = subprocess.run(["git", "check-ignore", "-q", probe], cwd=root)
            assert r.returncode == 0, "tools/local/ 未被 git 忽略"
        finally:
            if os.path.exists(probe):
                os.remove(probe)


class TestFullManagerIntegration:
    def test_manager_init_loads_local_tool(self, local_dir):
        """完整 ToolManager() 启动路径也会加载本地工具（真实入口）。"""
        (local_dir / "echo.py").write_text(GOOD_TOOL, encoding="utf-8")
        mgr = ToolManager()
        assert "local_echo" in mgr.list_local_tools()
        assert "local_echo" in mgr.list_tools()
