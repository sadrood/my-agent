"""
MCP (Model Context Protocol) 客户端模块。
让 Agent 能够连接外部 MCP 服务器，动态发现和调用远程工具。

支持传输方式：
- stdio: 启动子进程通信
- SSE (Server-Sent Events): HTTP 流式传输

设计原则：
- MCP 工具自动注册到 ToolManager，Agent 像使用本地工具一样使用 MCP 工具
- 自动重连机制
- 支持多 MCP 服务器同时连接
"""
import json
import os
import shutil
import subprocess
import threading
import queue
import uuid
import time
from typing import Optional, Callable, Dict, Any

from tools.base import BaseTool, ToolResult


def _resolve_argv(command: str, args: list) -> tuple:
    """把 MCP 启动命令解析成可 Popen 的 (argv_or_string, use_shell)。

    Windows 下 CreateProcess 不能直接跑 .cmd/.bat（npx 实际是 npx.cmd），
    必须经 cmd.exe 执行。这里用 shell=True + list2cmdline 引号化字符串，
    交给 cmd.exe /c 处理——比手动拼 ['cmd.exe','/d','/s','/c',cmdline]
    更稳（后者会因反斜杠转义引号而报"不是内部或外部命令"）。
    list2cmdline 对参数正确引号化，参数侧无注入风险。
    """
    if os.name != "nt":
        return [command] + list(args), False
    resolved = shutil.which(command) or command
    if resolved.lower().endswith((".cmd", ".bat")):
        cmdline = subprocess.list2cmdline([resolved] + list(args))
        return cmdline, True
    return [resolved] + list(args), False


class MCPTool(BaseTool):
    """MCP 远程工具包装器。将 MCP 服务器的工具包装为本地 BaseTool。"""

    risk_level: str = "medium"
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"

    def __init__(self, name: str, description: str, input_schema: dict,
                 call_fn: Callable, server_name: str):
        self._name = f"mcp_{server_name}_{name}"
        self._description = self._build_description(name, description, input_schema)
        self._call_fn = call_fn
        self._input_schema = input_schema

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def schema(self) -> dict:
        """透传 MCP 服务器的 input_schema，让 function calling 拿到真实参数结构。"""
        schema = dict(self._input_schema or {})
        schema.setdefault("type", "object")
        if "properties" not in schema:
            schema["properties"] = {
                "input": {"type": "string", "description": self._description[:300]}
            }
        return schema

    def execute_json(self, arguments: dict) -> ToolResult:
        """结构化入口：直接以 JSON 参数调用 MCP 工具。"""
        try:
            result = self._call_fn(self._name, arguments or {})
        except Exception as e:
            return ToolResult(success=False, output="", error=f"MCP 工具调用失败: {str(e)}")

        if isinstance(result, dict):
            if result.get("isError"):
                content = result.get("content", [])
                text = (
                    content[0].get("text", "MCP 工具调用失败")
                    if isinstance(content, list) and content and isinstance(content[0], dict)
                    else str(result)
                )
                return ToolResult(success=False, output="", error=text)
            content = result.get("content", [])
            if isinstance(content, list):
                texts = [c.get("text", str(c)) for c in content if isinstance(c, dict)]
                return ToolResult(success=True, output="\n".join(texts) or str(result))

        return ToolResult(success=True, output=str(result))

    def execute(self, input_str: str) -> ToolResult:
        """旧文本协议入口：input 为 JSON 对象字符串或 key=value 逗号分隔，转 execute_json。

        BaseTool 抽象方法要求实现（此前缺失导致 MCPTool 无法实例化、工具注册崩溃）。
        """
        try:
            if not input_str.strip():
                return ToolResult(success=False, output="", error="参数为空。")
            stripped = input_str.strip()
            if stripped.startswith("{"):
                args = json.loads(stripped)
            else:
                args = {}
                for kv in stripped.split(","):
                    kv = kv.strip()
                    if not kv:
                        continue
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        args[k.strip()] = v.strip()
                    else:
                        args["input"] = kv
            return self.execute_json(args if isinstance(args, dict) else {"input": args})
        except Exception as e:
            return ToolResult(success=False, output="", error=f"MCP 工具参数解析失败: {str(e)[:200]}")

    @staticmethod
    def _build_description(name: str, description: str, schema: dict) -> str:
        """构建工具描述文本。"""
        desc_parts = [f"[MCP] {description or name}"]
        
        props = schema.get("properties", {})
        required = schema.get("required", [])
        
        if props:
            desc_parts.append("\n  参数:")
            for param_name, param_info in props.items():
                req_mark = " (必填)" if param_name in required else ""
                param_type = param_info.get("type", "string")
                param_desc = param_info.get("description", "")
                desc_parts.append(
                    f"    - {param_name}{req_mark}: {param_type}"
                    + (f" - {param_desc}" if param_desc else "")
                )
        
        return "\n".join(desc_parts)


class MCPClient:
    """
    MCP 协议客户端。
    
    用法:
        client = MCPClient()
        client.connect_stdio("my-server", "python", ["server.py"])
        tools = client.list_tools()
        result = client.call_tool("tool_name", {"arg": "value"})
    """

    def __init__(self, tool_manager=None):
        self._servers: Dict[str, Dict[str, Any]] = {}
        self._tools: Dict[str, MCPTool] = {}
        self._request_id = 0
        self._lock = threading.Lock()
        self._tool_manager = tool_manager

    @property
    def is_connected(self) -> bool:
        return len(self._servers) > 0

    @property
    def server_names(self) -> list:
        return list(self._servers.keys())

    # ================================================================
    # 连接管理
    # ================================================================

    def connect_stdio(self, server_name: str, command: str, args: list = None,
                      env: dict = None) -> bool:
        """
        通过 stdio 连接 MCP 服务器。
        
        Args:
            server_name: 服务器别名
            command: 启动命令（如 "python" / "node" / "npx"）
            args: 命令参数列表
            env: 额外环境变量
        
        Returns:
            连接是否成功
        """
        args = args or []
        
        try:
            argv, use_shell = _resolve_argv(command, args)
            process = subprocess.Popen(
                argv,
                shell=use_shell,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # 关键：MCP 走 UTF-8 JSON-RPC；Windows 下 text=True 默认 cp936，
                # 不指定会把手套帧解成乱码导致握手失败（此前 playwright-mcp 连不上）
                encoding="utf-8",
                errors="replace",
                env={**os.environ, **(env or {})},
            )
        except Exception as e:
            print(f"[MCP] 启动服务器 '{server_name}' 失败: {e}")
            return False

        self._servers[server_name] = {
            "type": "stdio",
            "process": process,
            "command": command,
            "args": args,
        }

        # 初始化握手
        init_result = self._send_request(server_name, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
            },
            "clientInfo": {
                "name": "my-agent-mcp-client",
                "version": "1.0.0",
            },
        })

        if init_result is None:
            print(f"[MCP] 与服务器 '{server_name}' 初始化握手失败")
            self.disconnect(server_name)
            return False

        # 发送 initialized 通知
        self._send_notification(server_name, "notifications/initialized", {})

        # 发现工具
        self._discover_tools(server_name)
        
        print(f"[MCP] 已连接服务器 '{server_name}' ({command} {' '.join(args)})")
        print(f"[MCP] 发现 {len([t for t in self._tools if t.startswith(f'mcp_{server_name}_')])} 个工具")
        return True

    def connect_sse(self, server_name: str, url: str, headers: dict = None) -> bool:
        """
        通过 SSE 连接 MCP 服务器（实验性）。
        
        Args:
            server_name: 服务器别名
            url: SSE 端点 URL
            headers: 额外 HTTP 头
        """
        try:
            import urllib.request
            import ssl
            
            self._servers[server_name] = {
                "type": "sse",
                "url": url,
                "headers": headers or {},
                "session_id": str(uuid.uuid4()),
                "last_event_id": None,
            }
            
            print(f"[MCP] 已连接 SSE 服务器 '{server_name}': {url}")
            print(f"[MCP] SSE 模式为实验性功能，工具发现和调用可能有限。")
            return True
            
        except Exception as e:
            print(f"[MCP] SSE 连接 '{server_name}' 失败: {e}")
            return False

    def disconnect(self, server_name: str = None):
        """断开指定或全部 MCP 服务器连接。"""
        names = [server_name] if server_name else list(self._servers.keys())
        
        for name in names:
            server = self._servers.pop(name, None)
            if server is None:
                continue
            
            # 反注册工具
            prefix = f"mcp_{name}_"
            removed = [k for k in self._tools if k.startswith(prefix)]
            for k in removed:
                if self._tool_manager:
                    self._tool_manager.unregister(k)
                self._tools.pop(k, None)
            
            if server["type"] == "stdio":
                process = server.get("process")
                if process:
                    try:
                        process.stdin.close()
                        process.stdout.close()
                        process.terminate()
                        process.wait(timeout=5)
                    except Exception:
                        process.kill()
            
            print(f"[MCP] 已断开服务器 '{name}' ({len(removed)} 个工具已卸载)")

    # ================================================================
    # 工具管理
    # ================================================================

    def _discover_tools(self, server_name: str):
        """发现并注册 MCP 服务器的工具。"""
        result = self._send_request(server_name, "tools/list", {})
        if result is None:
            return

        tools_list = result.get("tools", [])
        for tool_info in tools_list:
            tool_name = tool_info.get("name", "")
            if not tool_name:
                continue
            try:
                mcp_tool = MCPTool(
                    name=tool_name,
                    description=tool_info.get("description", ""),
                    input_schema=tool_info.get("inputSchema", {}),
                    call_fn=self.call_tool,
                    server_name=server_name,
                )
                self._tools[mcp_tool.name] = mcp_tool
                if self._tool_manager:
                    self._tool_manager.register(mcp_tool)
            except Exception as e:
                # 单个工具构造/注册失败不阻塞整批（坏 schema 等）
                print(f"[MCP] 跳过工具 '{tool_name}': {e}")

    def call_tool(self, tool_full_name: str, arguments: dict) -> Any:
        """
        调用 MCP 工具。
        
        Args:
            tool_full_name: 完整工具名（mcp_<server>_<tool>）
            arguments: 工具参数
        """
        # 从工具名提取服务器名
        if not tool_full_name.startswith("mcp_"):
            return {"isError": True, "content": [{"text": f"无效的 MCP 工具名: {tool_full_name}"}]}
        
        parts = tool_full_name[4:].split("_", 1)
        if len(parts) < 2:
            return {"isError": True, "content": [{"text": f"无效的 MCP 工具名格式: {tool_full_name}"}]}
        
        server_name = parts[0]
        tool_name = parts[1]
        
        return self._send_request(server_name, "tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

    def list_tools(self) -> Dict[str, MCPTool]:
        """列出所有已发现的 MCP 工具。"""
        return self._tools.copy()

    # ================================================================
    # 底层通信
    # ================================================================

    def _send_request(self, server_name: str, method: str, params: dict) -> Optional[dict]:
        """发送 JSON-RPC 请求并等待响应。"""
        server = self._servers.get(server_name)
        if server is None:
            print(f"[MCP] 服务器 '{server_name}' 未连接")
            return None

        request_id = self._next_id()
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        if server["type"] == "stdio":
            return self._stdio_request(server, request)
        elif server["type"] == "sse":
            return self._sse_request(server, request)
        
        return None

    def _send_notification(self, server_name: str, method: str, params: dict):
        """发送 JSON-RPC 通知（无需响应）。"""
        server = self._servers.get(server_name)
        if server is None:
            return
        
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        
        if server["type"] == "stdio":
            try:
                process = server["process"]
                process.stdin.write(json.dumps(notification) + "\n")
                process.stdin.flush()
            except Exception as e:
                print(f"[MCP] 发送通知失败: {e}")

    def _stdio_request(self, server: dict, request: dict, timeout: float = 15.0) -> Optional[dict]:
        """通过 stdio 发送请求并获取响应。

        健壮性（此前 playwright-mcp 等连不上的根因）：
        - 跳过服务器启动横幅/日志等非 JSON 行，直到读到合法 JSON-RPC 响应；
        - 读响应带超时（首次 npx 下载包 / 服务器慢时不再无限阻塞）。
        """
        process = server.get("process")
        if process is None or process.poll() is not None:
            print("[MCP] stdio 进程已退出")
            return None

        try:
            with self._lock:
                request_id = request.get("id")
                request_str = json.dumps(request) + "\n"
                process.stdin.write(request_str)
                process.stdin.flush()

                deadline = time.time() + timeout
                while time.time() < deadline:
                    line = self._readline_timeout(process.stdout, deadline)
                    if line is None:
                        print(f"[MCP] 等待响应超时（{timeout}s）")
                        return None
                    if not line:
                        return None                      # EOF
                    try:
                        msg = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue                         # 启动横幅/日志，跳过
                    # 只认"回的是我们这条请求"的响应；服务器推送的通知（无 id）
                    # 或其它消息一律跳过，否则 tools/list 会被通知吃成空结果
                    if msg.get("id") != request_id:
                        continue
                    if msg.get("error"):
                        print(f"[MCP] 错误: {msg['error']}")
                        return None
                    return msg.get("result")
                return None
        except (BrokenPipeError, OSError, json.JSONDecodeError) as e:
            print(f"[MCP] stdio 通信失败: {e}")
            return None

    @staticmethod
    def _readline_timeout(stream, deadline: float) -> Optional[str]:
        """带截止时间的 readline：后台线程读取 + 队列等待，超时返回 None。

        Windows 管道没有非阻塞 readline，只能用读者线程；MCP 请求串行
        （受 _lock 保护），超时后由上层 disconnect 清理，避免协议错位。
        """
        q = queue.Queue(maxsize=1)

        def _read():
            try:
                q.put(stream.readline())
            except Exception as e:  # noqa: BLE001
                q.put(e)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        try:
            result = q.get(timeout=max(0.05, deadline - time.time()))
        except queue.Empty:
            return None
        if isinstance(result, BaseException):
            raise result
        return result

    def _sse_request(self, server: dict, request: dict) -> Optional[dict]:
        """通过 SSE 发送请求并获取响应（简化实现）。"""
        print("[MCP] SSE 请求功能为实验性，可能不完整。")
        
        try:
            import urllib.request
            
            url = server["url"]
            headers = {
                "Content-Type": "application/json",
                **server.get("headers", {}),
            }
            
            data = json.dumps(request).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("result")
                
        except Exception as e:
            print(f"[MCP] SSE 请求失败: {e}")
            return None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def __del__(self):
        self.disconnect()
