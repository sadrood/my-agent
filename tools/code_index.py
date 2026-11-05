"""代码索引工具：符号大纲 / 定义查找 / 引用扫描（只读，无外部依赖）。

Python 用标准库 ast 精确解析；JS/TS 用轻量正则（未装 tree-sitter 也能用）。
目的是让模型在改代码前先定位，而不是逐个文件读。
"""
import ast
import os
import re
from typing import Any, Dict, Iterable, List, Tuple

from tools.base import BaseTool, ToolResult

_SKIP_DIRS = {
    ".venv", ".git", "__pycache__", ".pytest_cache", ".idea", ".vscode",
    "node_modules", "screenshots", "rollouts", "memory", "output",
    "generated_audio", "generated_images", "generated_videos",
    "dist", "build", "release",
}
_CODE_EXTS = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_MAX_FILES = 2000
_MAX_REF_HITS = 50
_MAX_FILE_BYTES = 1_000_000

_JS_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(class|function|const|let|var)\s+([A-Za-z_$][\w$]*)\s*(\([^)]*\))?",
    re.MULTILINE,
)
_TS_TYPE = re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(interface|type|enum)\s+([A-Za-z_$][\w$]*)",
                      re.MULTILINE)


def _is_code(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _CODE_EXTS


def _iter_files(root: str) -> Iterable[str]:
    """遍历代码文件（目录会跳过依赖/产物目录）。"""
    if os.path.isfile(root):
        if _is_code(root):
            yield root
        return
    count = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if count >= _MAX_FILES:
                return
            full = os.path.join(base, name)
            if _is_code(full):
                count += 1
                yield full


def _read(path: str) -> str:
    try:
        if os.path.getsize(path) > _MAX_FILE_BYTES:
            return ""
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def _py_signature(node: ast.AST) -> str:
    args = getattr(node, "args", None)
    if args is None:
        return ""
    names = [a.arg for a in getattr(args, "posonlyargs", []) + args.args]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    names += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return f"({', '.join(names)})"


def _py_symbols(text: str) -> List[Tuple[int, str, str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out: List[Tuple[int, str, str]] = []

    def walk(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                out.append((child.lineno, "class", prefix + child.name))
                walk(child, prefix + child.name + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                out.append((child.lineno, kind, prefix + child.name + _py_signature(child)))
                walk(child, prefix + child.name + ".")

    walk(tree)
    return sorted(out)


def _js_symbols(text: str) -> List[Tuple[int, str, str]]:
    out: List[Tuple[int, str, str]] = []
    for m in _JS_DEF.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        kind, name, args = m.group(1), m.group(2), m.group(3) or ""
        out.append((line, kind, name + args))
    for m in _TS_TYPE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        out.append((line, m.group(1), m.group(2)))
    return sorted(out)


def symbols_of(path: str) -> List[Tuple[int, str, str]]:
    """返回 (行号, 类型, 名称/签名)。不支持的扩展名返回空。"""
    text = _read(path)
    if not text:
        return []
    ext = os.path.splitext(path)[1].lower()
    if ext in (".py", ".pyi"):
        return _py_symbols(text)
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return _js_symbols(text)
    return []


def _matches(symbol_name: str, wanted: str) -> bool:
    return symbol_name == wanted or symbol_name.endswith("." + wanted)


class CodeIndexTool(BaseTool):
    """代码索引：outline / find / refs / map。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"
    parallel_safe: bool = True

    @property
    def name(self) -> str:
        return "code_index"

    @property
    def description(self) -> str:
        return (
            "代码索引（只读，改代码前先定位）：\n"
            "  code_index outline <文件|目录>   列出类/函数/方法、行号与参数\n"
            "  code_index find <符号> [目录]    查定义位置\n"
            "  code_index refs <符号> [目录]    查引用（file:line + 整行）\n"
            "  code_index map [目录]            每个文件的符号数量概览\n"
            "Python 精确解析（ast），JS/TS 用轻量正则；比逐个文件读更快。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["outline", "find", "refs", "map"],
                    "description": "要执行的操作",
                },
                "query": {
                    "type": "string",
                    "description": "outline 的文件/目录；find/refs 的符号名；map 可省略",
                },
                "path": {
                    "type": "string",
                    "description": "find/refs 的搜索目录（默认当前目录）",
                },
            },
            "required": ["operation"],
        }

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        op = str(arguments.get("operation", "")).strip().lower()
        query = str(arguments.get("query", "")).strip()
        path = str(arguments.get("path", "") or "").strip()
        if op == "outline":
            return self._outline(query or ".")
        if op == "find":
            return self._find(query, path or ".")
        if op == "refs":
            return self._refs(query, path or ".")
        if op == "map":
            return self._map(query or ".")
        return ToolResult(success=False, output="",
                          error=f"未知操作: '{op}'。支持: outline / find / refs / map")

    def execute(self, input_str: str) -> ToolResult:
        parts = (input_str or "").strip().split(maxsplit=2)
        if not parts:
            return ToolResult(success=False, output="", error="用法: outline <路径> | find <符号> [目录] | refs <符号> [目录] | map [目录]")
        op = parts[0].lower()
        query = parts[1] if len(parts) > 1 else ""
        path = parts[2] if len(parts) > 2 else ""
        return self.execute_json({"operation": op, "query": query, "path": path})

    # ------------------------------------------------------------------
    def _outline(self, target: str) -> ToolResult:
        root = os.path.abspath(target)
        if not os.path.exists(root):
            return ToolResult(success=False, output="", error=f"路径不存在: {target}")
        files = [root] if os.path.isfile(root) else list(_iter_files(root))[:80]
        lines: List[str] = []
        for path in files:
            symbols = symbols_of(path)
            if not symbols:
                continue
            rel = os.path.relpath(path)
            lines.append(f"{rel}")
            for line_no, kind, name in symbols:
                lines.append(f"  {line_no:>5}  {kind:<9} {name}")
        if not lines:
            return ToolResult(success=True, output="（没有找到可解析的符号）")
        return ToolResult(success=True, output="\n".join(lines)[:12000])

    def _find(self, name: str, root: str) -> ToolResult:
        if not name:
            return ToolResult(success=False, output="", error="find 需要符号名")
        hits: List[str] = []
        for path in _iter_files(os.path.abspath(root)):
            for line_no, kind, symbol in symbols_of(path):
                if _matches(symbol.split("(", 1)[0], name):
                    hits.append(f"{os.path.relpath(path)}:{line_no}  {kind} {symbol}")
                    if len(hits) >= _MAX_REF_HITS:
                        break
            if len(hits) >= _MAX_REF_HITS:
                break
        if not hits:
            return ToolResult(success=True, output=f"（没有找到定义: {name}）")
        return ToolResult(success=True, output="\n".join(hits))

    def _refs(self, name: str, root: str) -> ToolResult:
        if not name:
            return ToolResult(success=False, output="", error="refs 需要符号名")
        pattern = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
        hits: List[str] = []
        for path in _iter_files(os.path.abspath(root)):
            text = _read(path)
            if not text:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{os.path.relpath(path)}:{line_no}: {line.strip()[:160]}")
                    if len(hits) >= _MAX_REF_HITS:
                        break
            if len(hits) >= _MAX_REF_HITS:
                break
        if not hits:
            return ToolResult(success=True, output=f"（没有找到引用: {name}）")
        suffix = "" if len(hits) < _MAX_REF_HITS else f"\n…（超过 {_MAX_REF_HITS} 条，已截断）"
        return ToolResult(success=True, output="\n".join(hits) + suffix)

    def _map(self, root: str) -> ToolResult:
        rows: List[str] = []
        total = 0
        for path in _iter_files(os.path.abspath(root)):
            symbols = symbols_of(path)
            if not symbols:
                continue
            kinds: Dict[str, int] = {}
            for _line, kind, _name in symbols:
                key = "class" if kind == "class" else "def"
                kinds[key] = kinds.get(key, 0) + 1
            total += len(symbols)
            detail = "、".join(f"{k} {v}" for k, v in sorted(kinds.items()))
            rows.append(f"{os.path.relpath(path)}（{detail}）")
            if len(rows) >= 100:
                rows.append("…（文件过多，已截断）")
                break
        if not rows:
            return ToolResult(success=True, output="（没有找到可解析的代码文件）")
        return ToolResult(success=True, output=f"共 {total} 个符号\n" + "\n".join(rows))
