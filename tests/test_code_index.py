# -*- coding: utf-8 -*-
"""code_index：符号大纲 / 定义查找 / 引用扫描（测试是纯离线文件）。"""
from tools.code_index import CodeIndexTool, symbols_of


def _project(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text(
        "class Foo:\n"
        "    def bar(self, x, y=1):\n"
        "        return x\n"
        "\n"
        "def baz(x):\n"
        "    return x\n",
        encoding="utf-8",
    )
    (pkg / "use.py").write_text(
        "from pkg.a import baz, Foo\n"
        "baz(1)\n"
        "Foo().bar(2)\n",
        encoding="utf-8",
    )
    web = tmp_path / "web"
    web.mkdir()
    (web / "app.ts").write_text(
        "export function render(el: HTMLElement) {}\n"
        "class Store {}\n"
        "interface Props {}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_symbols_of_python(tmp_path):
    root = _project(tmp_path)
    symbols = symbols_of(str(root / "pkg" / "a.py"))
    names = [name for _line, _kind, name in symbols]
    assert "class Foo" not in names
    assert "Foo" in names
    assert any(name.startswith("Foo.bar(") for name in names)
    assert any(name.startswith("baz(") for name in names)


def test_symbols_of_ts(tmp_path):
    root = _project(tmp_path)
    symbols = symbols_of(str(root / "web" / "app.ts"))
    names = [name for _line, _kind, name in symbols]
    assert any(n.startswith("render(") for n in names)
    assert "Store" in names and "Props" in names


def test_outline_find_refs_map(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    tool = CodeIndexTool()

    outline = tool.execute("outline pkg")
    assert outline.success
    assert "class" in outline.output and "Foo" in outline.output
    assert "def" in outline.output and "baz" in outline.output

    found = tool.execute_json({"operation": "find", "query": "baz"})
    assert found.success and "a.py:" in found.output and "def baz" in found.output

    refs = tool.execute_json({"operation": "refs", "query": "baz", "path": "."})
    assert refs.success
    assert "a.py:" in refs.output and "use.py:" in refs.output

    summary = tool.execute_json({"operation": "map", "path": "."})
    assert summary.success and "def" in summary.output and "class" in summary.output


def test_unknown_operation_and_missing_path(tmp_path):
    tool = CodeIndexTool()
    assert not tool.execute("nope").success
    assert not tool.execute_json({"operation": "outline", "query": str(tmp_path / "missing")}).success


def test_registered_in_tool_manager():
    from tools.tool_manager import ToolManager
    assert "code_index" in ToolManager().list_tools()


def test_skips_dependency_dirs(tmp_path, monkeypatch):
    root = _project(tmp_path)
    junk = root / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "x.py").write_text("def should_not_appear():\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(root)
    out = CodeIndexTool().execute("map").output
    assert "node_modules" not in out
