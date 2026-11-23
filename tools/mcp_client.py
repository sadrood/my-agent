"""MCP (Model Context Protocol) 客户端模块。"""
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
    """把 MCP 启动命令解析成可 Popen 的 (argv_or_string, use_shell)。"""
    if os.name != "nt":
        return [command] + list(args), False
    resolved = shutil.which(command) or command
    if resolved.lower().endswith((".cmd", ".bat")):
        cmdline = subprocess.list2cmdline([resolved] + list(args))
        return cmdline, True
    return [resolved] + list(args), False


def _cache_dir() -> str:
    """按需连接服务器的工具清单缓存目录（与用户级 MCP 配置同级）。"""
    from config import MCP_USER_CONFIG_FILE
    return os.path.join(os.path.dirname(MCP_USER_CONFIG_FILE), "mcp_cache")


def _coerce_timeout(value, server_name: str = "") -> Optional[float]:
    """把配置里的 timeout 解析成秒；非法值告警后按默认值处理，绝不抛异常。

    配置常被手写成 "120s" / "2m"，而 float() 在 Popen 之后才执行 —— 抛错会留下
    没人认领的子进程，且整个 agent 启动随之中断。这里降级而不是拒绝启动。
    """
    if value is None or value == "":
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        print(f"[MCP] 警告：服务器 '{server_name}' 的 timeout={value!r} 不是数字，按默认 15s 处理")
        return None
    if seconds <= 0:
        print(f"[MCP] 警告：服务器 '{server_name}' 的 timeout={value!r} 不为正，按默认 15s 处理")
        return None
    return seconds


def _norm_exclude(value) -> list:
    """exclude 支持列表或逗号分隔字符串；写成字符串时不要被逐字符拆开。"""
    if not value:
        return []
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(s) for s in value if str(s).strip()]
    print(f"[MCP] 警告：exclude={value!r} 类型不支持（应为列表），已忽略")
    return []


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

        if result is None:
            # 旧代码在这里返回 success=True, output="None" —— 超时/进程崩溃/JSON-RPC 报错/管道断全都被伪装成"成功"，模型拿到一个字面量"None" 完全无法判断发生了什么（本仓库反复遇到过的"无用报错"坑）。
            return ToolResult(
                success=False, output="",
                error=("MCP 工具无响应：请求超时、服务器未连接或已崩溃。"
                       "可用 mcp 的 status/重连，或改用其他方式完成该步骤。"),
            )

        return ToolResult(success=True, output=str(result))

    def execute(self, input_str: str) -> ToolResult:
        """旧文本协议入口：input 为 JSON 对象字符串或 key=value 逗号分隔，转 execute_json。
        BaseTool 抽象方法要求实现（之前缺失导致 MCPTool 无法实例化、工具注册崩溃）。"""
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
    """MCP 协议客户端。"""

    def __init__(self, tool_manager=None):
        self._servers: Dict[str, Dict[str, Any]] = {}
        self._tools: Dict[str, MCPTool] = {}
        self._request_id = 0
        self._lock = threading.Lock()
        self._tool_manager = tool_manager
        # 按需连接（lazy）服务器的启动配置：工具已注册但进程未起，首次调用时才连
        self._lazy_configs: Dict[str, Dict[str, Any]] = {}

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
                      env: dict = None, exclude: list = None,
                      timeout: float = None, lazy: bool = False) -> bool:
        """通过 stdio 连接 MCP 服务器。exclude 中的工具名不会被注册。
        timeout 为该服务器每次请求的等待上限（远程 SSH 桥接需要放宽）。
        lazy=True 时不立即起进程：先用缓存的工具清单注册占位工具，首次调用才连接。"""
        args = args or []
        exclude = _norm_exclude(exclude)
        timeout = _coerce_timeout(timeout, server_name)

        if lazy:
            self._lazy_configs[server_name] = {
                "command": command, "args": args, "env": env,
                "exclude": exclude, "timeout": timeout,
            }
            cached = self._load_tool_cache(server_name)
            if cached is None:
                # 无缓存：连一次取工具清单，缓存后立即断开
                if not self.connect_stdio(server_name, command, args, env, exclude, timeout):
                    return False
                self._save_tool_cache(server_name)
                cached = self._load_tool_cache(server_name) or []
                self.disconnect(server_name)
            # disconnect 会反注册工具：这里统一重建占位条目并注册（进程为 None）
            self._servers[server_name] = {
                "type": "stdio", "process": None, "name": server_name,
                "lock": threading.Lock(),
                "exclude": {str(n) for n in exclude},
                "timeout": timeout,
            }
            self._register_tool_infos(server_name, cached, quiet=True)
            return True

        try:
            argv, use_shell = _resolve_argv(command, args)
            process = subprocess.Popen(
                argv,
                shell=use_shell,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # stderr 必须给 DEVNULL：stdio MCP 服务器（尤其 npx/node 系）打日志很勤，管道只开不读的话，写满 ~64KB 缓冲区后子进程会永久阻塞在 write 上。
                stderr=subprocess.DEVNULL,
                text=True,
                # 关键：MCP 走 UTF-8 JSON-RPC；Windows 下 text=True 默认 cp936，不指定会把手套帧解成乱码导致握手失败（之前 playwright-mcp 连不上）
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
            # 每台服务器各自一把锁：之前所有服务器共用实例级 _lock，一台慢服务器会把其它服务器的请求一起卡住（最长 15s/次）
            "name": server_name,
            "lock": threading.Lock(),
            "exclude": {str(name) for name in exclude},
            "timeout": timeout,
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
        found = self._discover_tools(server_name)
        if found is None:
            # 连上了但 tools/list 没成功：不能报"已连接"就完事 —— 安装器会据此把这条配置写进持久化、重启后自动重连，而实际一个工具都没有。
            print(f"[MCP] 已建立连接，但 tools/list 失败：服务器 '{server_name}' 的工具未注册。")
            return False
        print(f"[MCP] 已连接服务器 '{server_name}' ({command} {' '.join(args)})")
        print(f"[MCP] 发现 {found} 个工具")
        return True

    def connect_sse(self, server_name: str, url: str, headers: dict = None) -> bool:
        """通过 SSE 连接 MCP 服务器（实验性）。"""
        try:

            self._servers[server_name] = {
                "type": "sse",
                "url": url,
                "headers": headers or {},
                "session_id": str(uuid.uuid4()),
                "last_event_id": None,
                "name": server_name,
                "lock": threading.Lock(),
            }
            
            print(f"[MCP] 已连接 SSE 服务器 '{server_name}': {url}")
            print("[MCP] SSE 模式为实验性功能，工具发现和调用可能有限。")
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
            
            had_process = server.get("process") is not None
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
            
            if had_process:
                print(f"[MCP] 已断开服务器 '{name}' ({len(removed)} 个工具已卸载)")

    # ================================================================
    # 工具管理
    # ================================================================

    def _discover_tools(self, server_name: str):
        """发现并注册工具。返回注册数量；`tools/list` **调用失败**时返回 None。"""
        result = self._send_request(server_name, "tools/list", {})
        if result is None:
            return None
        tools_list = result.get("tools", [])
        server = self._servers.get(server_name)
        if server is not None:
            server["tools_raw"] = tools_list
        return self._register_tool_infos(server_name, tools_list)

    def _register_tool_infos(self, server_name: str, tools_list: list, quiet: bool = False) -> int:
        """按原始 tools/list 条目注册工具（实时发现与 lazy 缓存共用）。"""
        registered = 0
        skipped = []

        exclude = self._servers.get(server_name, {}).get("exclude") or set()
        for tool_info in tools_list:
            tool_name = tool_info.get("name", "")
            if not tool_name:
                continue
            if tool_name in exclude:
                skipped.append(tool_name)
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
                registered += 1
                if self._tool_manager:
                    self._tool_manager.register(mcp_tool)
            except Exception as e:
                # 单个工具构造/注册失败不阻塞整批（坏 schema 等）
                print(f"[MCP] 跳过工具 '{tool_name}': {e}")
        if skipped and not quiet:
            print(f"[MCP] 已按配置跳过 {len(skipped)} 个工具：{', '.join(skipped)}")
        # exclude 是安全阀（挡掉会二次调用模型的委托工具），拼错一个字母就等于没配。
        # 只在未命中时告警，正常配置下启动依旧安静。
        present = {str(t.get("name")) for t in tools_list if t.get("name")}
        missing = sorted(n for n in exclude if n not in present)
        if missing:
            print(f"[MCP] 警告：服务器 '{server_name}' 的 exclude 里有不存在的工具："
                  f"{', '.join(missing)}（拼写错误？该工具仍会注册）")
        return registered

    def call_tool(self, tool_full_name: str, arguments: dict) -> Any:
        """调用 MCP 工具。"""
        # 从工具名提取服务器名。⚠️ 不能简单按第一个 `_` 切：服务器名本身可能含下划线，而工具的注册名是 `mcp_{server}_{tool}`。
        if not tool_full_name.startswith("mcp_"):
            return {"isError": True,
                    "content": [{"text": f"无效的 MCP 工具名: {tool_full_name}"}]}
        body = tool_full_name[4:]
        server_name = tool_name = ""
        known = set(self._servers) | set(self._lazy_configs)
        for name in sorted(known, key=len, reverse=True):
            prefix = f"{name}_"
            if body.startswith(prefix):
                server_name, tool_name = name, body[len(prefix):]
                break
        if not server_name:
            # 兜底：服务器还没登记（或名字确实不含下划线）时按老办法切
            parts = body.split("_", 1)
            if len(parts) < 2:
                return {"isError": True,
                        "content": [{"text": f"无效的 MCP 工具名格式: {tool_full_name}"}]}
            server_name, tool_name = parts

        if not self._ensure_lazy_connected(server_name):
            return {"isError": True,
                    "content": [{"text": f"MCP 服务器 '{server_name}' 按需连接失败，无法调用 {tool_name}。"}]}

        return self._send_request(server_name, "tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

    def _ensure_lazy_connected(self, server_name: str) -> bool:
        """lazy 服务器首次调用（或断线后重试）时才真正建立连接。"""
        cfg = self._lazy_configs.get(server_name)
        if cfg is None:
            return True
        server = self._servers.get(server_name)
        # check-then-act 必须串行：两个线程同时首调会各起一个进程，
        # 后者覆盖 _servers 条目，前者没人认领（子进程泄漏）。
        lock = (server or {}).get("lock") or self._lock
        with lock:
            server = self._servers.get(server_name)
            proc = server.get("process") if server else None
            if proc is not None and proc.poll() is None:
                return True
            print(f"[MCP] 首次使用，正在连接服务器 '{server_name}' ...")
            ok = self.connect_stdio(server_name, cfg["command"], cfg["args"],
                                    cfg.get("env"), cfg.get("exclude"), cfg.get("timeout"))
            if ok:
                self._save_tool_cache(server_name)
            return ok

    # ------------------------------------------------------------
    # lazy 工具清单缓存（只存 schema，不存命令/token）
    # ------------------------------------------------------------

    @staticmethod
    def _cache_path(server_name: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in server_name)
        return os.path.join(_cache_dir(), f"{safe}.json")

    def _load_tool_cache(self, server_name: str) -> Optional[list]:
        try:
            with open(self._cache_path(server_name), "r", encoding="utf-8") as f:
                data = json.load(f)
            tools = data.get("tools")
            return tools if isinstance(tools, list) else None
        except Exception:
            return None

    def _save_tool_cache(self, server_name: str) -> None:
        tools = (self._servers.get(server_name) or {}).get("tools_raw")
        if not tools:
            return
        try:
            path = self._cache_path(server_name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"tools": tools}, f, ensure_ascii=False)
        except Exception:
            pass

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

    def _stdio_request(self, server: dict, request: dict, timeout: float = None) -> Optional[dict]:
        """通过 stdio 发送请求并获取响应。"""
        # 服务器级超时可配置：SSH 远程桥接握手/调用都可能超过本地默认的 15s
        timeout = float(timeout or server.get("timeout") or 15.0)
        process = server.get("process")
        if process is None or process.poll() is not None:
            print("[MCP] stdio 进程已退出")
            return None

        try:
            lock = server.get("lock") or self._lock
            with lock:
                request_id = request.get("id")
                request_str = json.dumps(request) + "\n"
                process.stdin.write(request_str)
                process.stdin.flush()

                deadline = time.time() + timeout
                while time.time() < deadline:
                    line = self._readline_timeout(process.stdout, deadline)
                    if line is None:
                        # 超时即判定该服务器已不可用并断开：读者线程仍阻塞在 readline 上并持有管道，若不断开，迟到的响应会被这个已废弃的请求吃掉，下一个请求永远等不到自己的回包（协议错位）。
                        print(f"[MCP] 等待响应超时（{timeout}s），断开该服务器")
                        self._mark_dead(server)
                        return None
                    if not line:
                        self._mark_dead(server)
                        return None                      # EOF
                    try:
                        msg = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue                         # 启动横幅/日志，跳过
                    # 只认"回的是我们这条请求"的响应；服务器推送的通知（无 id）或其它消息一律跳过，否则 tools/list 会被通知吃成空结果
                    if msg.get("id") != request_id:
                        continue
                    if msg.get("error"):
                        print(f"[MCP] 错误: {msg['error']}")
                        return None
                    return msg.get("result")
                return None
        except (BrokenPipeError, OSError, json.JSONDecodeError) as e:
            print(f"[MCP] stdio 通信失败: {e}")
            self._mark_dead(server)
            return None

    def _mark_dead(self, server: dict) -> None:
        """把某台服务器标记为不可用并清理其进程（幂等、绝不抛错）。"""
        name = server.get("name")
        try:
            proc = server.get("process")
            if proc is not None and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        if name:
            try:
                self.disconnect(name)
            except Exception:
                pass

    @staticmethod
    def _readline_timeout(stream, deadline: float) -> Optional[str]:
        """带截止时间的 readline：后台线程读取 + 队列等待，超时返回 None。
        （受 _lock 保护），超时后由上层 disconnect 清理，避免协议错位。"""
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
