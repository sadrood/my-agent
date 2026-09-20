"""
工具管理器模块（v2：JSON Schema + 结构化执行 + 输出截断）。
统一管理所有工具的注册、查找、调用和描述生成。

v2 变化：
- register 支持结构化工具（BaseTool.schema / execute_json）
- execute_json(): 按 JSON Schema 参数执行工具（function calling 入口）
- execute(): 字符串入口（旧接口保留），两个入口都统一做输出截断
- list_openai_schemas(): 输出 OpenAI function calling 格式的工具列表
"""
from typing import Optional

from config import TOOL_CONFIG
from tools.base import BaseTool, ToolResult, truncate_output
from tools.terminal import TerminalTool
from tools.delegate import DelegateTool
from tools.experience import ExperienceTool
from tools.file import FileTool
from tools.python import PythonTool
from tools.browser import BrowserTool
from tools.patch import EditTool
from tools.todo import TodoTool
import os


def _create_browser_tool() -> BaseTool:
    """创建浏览器工具。

    桌面端在运行（环境变量注入或桥健康检查通过）→ 一律用内嵌浏览器：
    Agent 操控的页面就是侧栏里的 <webview>，外部 Playwright 浏览器被禁用。
    桌面端未运行（纯 CLI 场景）→ 才回退独立 Playwright 浏览器。
    """
    from config import BROWSER_CONFIG

    # 仅以实时环境变量为准（config 里 embedded_url 是导入时快照，会被陈旧值
    # 误导：桌面端曾注入过该变量时，即使当前已删除也会误走内嵌分支）。
    if os.getenv("MY_AGENT_EMBEDDED_BROWSER_URL"):
        try:
            from tools.embedded_browser import EmbeddedBrowserTool
            return EmbeddedBrowserTool()
        except Exception:
            pass
    if BROWSER_CONFIG.get("embedded_auto_detect", True):
        try:
            from tools.embedded_browser import probe_bridge
            if probe_bridge():
                from tools.embedded_browser import EmbeddedBrowserTool
                return EmbeddedBrowserTool()
        except Exception:
            pass
    return BrowserTool()


def _create_terminal_tool() -> BaseTool:
    """创建终端工具：桌面端在运行（env 注入或桥探测成功）→ 命令经 Electron 桥
    执行并回显到内置终端面板（Agent 与用户看同一条命令流）；纯 CLI → 本地执行。"""
    from config import BROWSER_CONFIG

    if os.getenv("MY_AGENT_EMBEDDED_TERMINAL_URL"):
        try:
            from tools.embedded_terminal import EmbeddedTerminalTool
            return EmbeddedTerminalTool()
        except Exception:
            pass
    if BROWSER_CONFIG.get("embedded_auto_detect", True):
        try:
            from tools.embedded_browser import probe_bridge
            if probe_bridge():
                from tools.embedded_terminal import EmbeddedTerminalTool
                return EmbeddedTerminalTool()
        except Exception:
            pass
    return TerminalTool()


class ToolManager:
    """工具管理器，负责工具注册、查找和执行调度。"""

    def __init__(self, output_max_chars: int = None):
        self._tools: dict[str, BaseTool] = {}
        self.output_max_chars = output_max_chars or TOOL_CONFIG.get("output_max_chars", 8000)

        # 注册默认内置工具
        browser = _create_browser_tool()
        self.register(_create_terminal_tool())
        self.register(DelegateTool())
        self.register(ExperienceTool())
        self.register(FileTool())
        self.register(PythonTool())
        self.register(browser)
        self.register(EditTool())
        self.register(TodoTool())
        # 任务中心（想法 + 任务状态机）：惰性导入避免循环（agent.tasks → agent → tools）
        try:
            from agent.tasks import TaskTool, ThoughtTool
            self.register(TaskTool())
            self.register(ThoughtTool())
        except Exception:
            pass

        # 文章工坊（多模型互审写作）：需要复用浏览器做事实核查 → 注入 self
        try:
            from tools.article import ArticleTool
            self.register(ArticleTool(tool_manager=self))
        except Exception:
            pass

        # 本地 OCR（截图识字，不依赖视觉模型）：视觉链路的降级通道
        try:
            from tools.ocr import OcrTool
            self.register(OcrTool())
        except Exception:
            pass

        # 桌面操控工具（Windows）：截图/无障碍树/鼠标键盘，高危走审批门
        try:
            from tools.computer_use import DesktopTool
            self.register(DesktopTool())
        except Exception:
            pass

        # 视觉分析工具（需要引用 browser 实例）
        try:
            from tools.vision_tool import SeeTool
            self.register(SeeTool(browser_tool=browser))
        except Exception:
            pass

        # 插件安装器（技能包 / MCP 插件）：Agent 自主安装能力扩展
        try:
            from tools.installer import InstallerTool
            self.register(InstallerTool(tool_manager=self))
        except Exception:
            pass

        # 文生图工具（OpenAI 兼容 images 端点；b64 与 url 两种返回都落盘）
        try:
            from tools.image_gen import ImageGenTool
            self.register(ImageGenTool())
        except Exception:
            pass

        # 文生视频工具（OpenAI Videos 兼容异步任务：创建 → 轮询 → 下载 mp4）
        try:
            from tools.video_gen import VideoGenTool
            self.register(VideoGenTool())
        except Exception:
            pass

        # 视频剪辑工具（ffmpeg：图→运镜、拼接、配音合成、字幕）
        try:
            from tools.video_edit import VideoEditTool
            self.register(VideoEditTool())
        except Exception:
            pass

        # 语音合成工具（edge-tts 配音）
        try:
            from tools.tts import TTSTool
            self.register(TTSTool())
        except Exception:
            pass

        # Toonflow 短剧工厂对接（外部 REST 服务，可选；不在跑也不影响其它工具）
        try:
            from config import TOONFLOW_CONFIG
            if TOONFLOW_CONFIG.get("enabled", True):
                from tools.toonflow import ToonflowTool
                self.register(ToonflowTool())
        except Exception:
            pass

        # 知乎数据开放平台（检索/直答/创作数据/知识库/小工具；需 ZHIHU_ACCESS_SECRET）
        try:
            from config import ZHIHU_CONFIG
            if ZHIHU_CONFIG.get("enabled", True):
                from tools.zhihu import ZhihuTool
                self.register(ZhihuTool())
        except Exception:
            pass

        # 本地工具目录（tools/local/，已 gitignore）：agent 自造的工具/技能放这里，
        # 只在本机生效、不随仓库推送。约定：模块内定义 BaseTool 子类即可。
        self._local_tools: list = []
        if TOOL_CONFIG.get("local_enabled", True):
            self._local_tools = self._load_local_tools()

    # ------------------------------------------------------------
    # 本地工具（agent 自造技能）
    # ------------------------------------------------------------

    @staticmethod
    def _local_dir() -> str:
        """本地工具目录的绝对路径（相对路径锚定项目根，避免 cwd 漂移）。"""
        raw = str(TOOL_CONFIG.get("local_dir") or "./tools/local")
        if os.path.isabs(raw):
            return raw
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.abspath(os.path.join(root, raw))

    def _load_local_tools(self) -> list:
        """扫描并加载本地工具目录里的 BaseTool 子类。

        - 单个模块出错只跳过它（打一行警告），绝不影响其它工具与启动；
        - 以 ``_`` 开头的文件视为私有/临时，跳过；
        - 约定：工具类必须能无参实例化。
        """
        import importlib.util
        import sys

        directory = self._local_dir()
        loaded: list = []
        if not os.path.isdir(directory):
            return loaded
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            path = os.path.join(directory, fname)
            mod_name = "my_agent_local_" + os.path.splitext(fname)[0]
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
            except Exception as e:                      # noqa: BLE001
                print(f"[local_tools] 跳过 {fname}: {type(e).__name__}: {e}",
                      file=sys.stderr)
                continue
            for attr in vars(module).values():
                if (isinstance(attr, type) and issubclass(attr, BaseTool)
                        and attr is not BaseTool
                        and getattr(attr, "__module__", "") == mod_name):
                    try:
                        tool = attr()
                        self.register(tool)
                        loaded.append(tool.name)
                    except Exception as e:              # noqa: BLE001
                        print(f"[local_tools] {fname}:{attr.__name__} 实例化失败: {e}",
                              file=sys.stderr)
        self._local_tools = loaded          # 与 list_local_tools() 保持自洽
        return loaded

    def list_local_tools(self) -> list:
        """本次启动加载进来的本地工具名（供 doctor/自检展示）。"""
        return list(getattr(self, "_local_tools", []))

    def register(self, tool: BaseTool):
        """
        注册一个工具。

        Args:
            tool: 工具实例（需继承 BaseTool）。
        """
        self._tools[tool.name] = tool

    def unregister(self, tool_name: str):
        """
        注销一个工具。

        Args:
            tool_name: 工具名称。
        """
        self._tools.pop(tool_name, None)

    def bind_stop_event(self, event) -> None:
        """把执行器的停止信号注入所有支持的工具（如 terminal 前台命令轮询）。"""
        for tool in self._tools.values():
            setter = getattr(tool, "set_stop_event", None)
            if callable(setter):
                try:
                    setter(event)
                except Exception:
                    pass

    def cancel_active_tools(self) -> None:
        """兜底终止：调用所有工具的 terminate_current()（若实现）。

        用于执行器捕获停止信号后强行打断正在运行的工具，
        即使工具内部没有轮询停止信号也能被杀掉。
        """
        for tool in self._tools.values():
            terminate = getattr(tool, "terminate_current", None)
            if callable(terminate):
                try:
                    terminate()
                except Exception:
                    pass

    def reset_tool(self, tool_name: str):
        """
        重置指定工具状态（工具执行超时/挂起后调用）。

        优先调用工具实例的 reset()（同一实例、引用有效）；实例没有 reset()
        时退回重新实例化。丢弃卡死的底层连接（如 Playwright 的 CDP），
        下次调用从干净状态重新开始。
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return
        reset = getattr(tool, "reset", None)
        if callable(reset):
            try:
                reset()
                return
            except Exception:
                pass
        # 退回：重新实例化（browser 被 SeeTool 引用，需同步指向新实例）
        try:
            new = tool.__class__()
        except Exception:
            return
        self._tools[tool_name] = new
        if tool_name == "browser":
            see = self._tools.get("see")
            if see is not None:
                try:
                    see._browser_tool = new
                except Exception:
                    pass

    def get_tool(self, tool_name: str) -> Optional[BaseTool]:
        """根据名称获取工具实例。"""
        return self._tools.get(tool_name)

    def list_tools(self) -> list[str]:
        """列出所有已注册工具的名称。"""
        return list(self._tools.keys())

    def get_tools_description(self) -> str:
        """
        生成所有工具的文本描述，供 LLM 阅读并决定使用哪个工具。

        Returns:
            工具描述文本。
        """
        if not self._tools:
            return "当前无可用工具。"
        lines = ["可用工具列表："]
        for i, (name, tool) in enumerate(self._tools.items(), 1):
            lines.append(f"\n{i}. {name}")
            lines.append(f"   {tool.description}")
        return "\n".join(lines)

    def get_all_descriptions(self) -> str:
        """兼容旧调用（Team 模块使用）：返回全部工具描述。"""
        return self.get_tools_description()

    # ================================================================
    # v2：JSON Schema 接口
    # ================================================================

    def list_openai_schemas(self) -> list[dict]:
        """全部工具的 OpenAI function calling 格式描述。"""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def get_tool_schema(self, tool_name: str) -> Optional[dict]:
        """获取单个工具的 JSON Schema。"""
        tool = self._tools.get(tool_name)
        return tool.schema if tool else None

    def is_parallel_safe(self, tool_name: str, arguments: dict) -> bool:
        """该工具在本轮参数下是否可以与其他工具并行执行。"""
        tool = self._tools.get(tool_name)
        if tool is None:
            return False
        try:
            return bool(tool.is_parallel_safe(arguments or {}))
        except Exception:
            return False

    # ================================================================
    # 执行（统一截断）
    # ================================================================

    def execute(self, tool_name: str, tool_input: str) -> ToolResult:
        """
        执行指定工具（字符串入口，旧接口）。

        Args:
            tool_name: 工具名称。
            tool_input: 工具输入参数。

        Returns:
            ToolResult 对象。
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult(
                success=False,
                output="",
                error=f"未知工具: '{tool_name}'。可用工具: {', '.join(self._tools.keys())}",
            )
        result = tool.execute(tool_input)
        return self._truncate_result(result)

    def execute_json(self, tool_name: str, arguments: dict) -> ToolResult:
        """
        执行指定工具（结构化入口，function calling 用）。

        Args:
            tool_name: 工具名称。
            arguments: JSON Schema 参数。

        Returns:
            ToolResult 对象。
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult(
                success=False,
                output="",
                error=f"未知工具: '{tool_name}'。可用工具: {', '.join(self._tools.keys())}",
            )
        # 回退判定必须基于**签名**，不能靠捕获 TypeError：
        # 旧实现 `except TypeError:` 会把工具执行体内部抛出的 TypeError 也当成
        # "该工具没实现 execute_json"，于是**把同一个调用再走一遍字符串路径**——
        # 对 terminal / video_gen / file 这类有副作用的工具就是执行两次。
        import inspect

        impl = type(tool).execute_json
        try:
            inspect.signature(impl).bind(tool, arguments or {})
            signature_ok = True
        except (TypeError, ValueError):
            signature_ok = False          # 老式签名的工具 → 走基类字符串转换

        try:
            if signature_ok:
                result = tool.execute_json(arguments or {})
            else:
                result = BaseTool.execute_json(tool, arguments or {})
        except Exception as e:
            return ToolResult(success=False, output="", error=f"工具调用失败: {e}")
        return self._truncate_result(result)

    def _truncate_result(self, result: ToolResult) -> ToolResult:
        """统一输出截断。"""
        if result.output:
            text, truncated, original = truncate_output(result.output, self.output_max_chars)
            result.output = text
            result.truncated = truncated
            result.original_length = original
        return result

    def build_approval_request(self, tool_name: str, arguments: dict):
        """为一次结构化调用生成审批请求。"""
        tool = self._tools.get(tool_name)
        if tool is None:
            return None
        try:
            return tool.build_approval_request(arguments or {})
        except Exception:
            from tools.base import ApprovalRequest
            return ApprovalRequest(
                tool_name=tool_name,
                arguments=arguments or {},
                command=f"{tool_name}({arguments})",
                risk_level=getattr(tool, "risk_level", "medium"),
                min_sandbox_mode=getattr(tool, "min_sandbox_mode", "workspace-write"),
            )

    # ================================================================
    # MCP 集成
    # ================================================================

    def connect_mcp_server(self, server_name: str, command: str,
                           args: list = None, env: dict = None) -> bool:
        """
        连接 MCP 服务器并自动注册其工具。

        Args:
            server_name: 服务器别名
            command: 启动命令
            args: 命令参数
            env: 环境变量

        Returns:
            连接是否成功
        """
        from tools.mcp_client import MCPClient

        client = MCPClient(tool_manager=self)
        result = client.connect_stdio(server_name, command, args or [], env or {})
        if result:
            # 保存客户端引用防止被 GC
            if not hasattr(self, "_mcp_clients"):
                self._mcp_clients = {}
            self._mcp_clients[server_name] = client
        return result

    def disconnect_mcp_server(self, server_name: str = None):
        """断开 MCP 服务器连接。"""
        if not hasattr(self, "_mcp_clients"):
            return
        if server_name:
            client = self._mcp_clients.pop(server_name, None)
            if client:
                client.disconnect(server_name)
        else:
            for name, client in list(self._mcp_clients.items()):
                client.disconnect(name)
            self._mcp_clients.clear()

    def list_mcp_servers(self) -> list:
        """列出已连接的 MCP 服务器。"""
        if hasattr(self, "_mcp_clients"):
            return list(self._mcp_clients.keys())
        return []
