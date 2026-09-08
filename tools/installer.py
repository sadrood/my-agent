"""
插件安装工具：让 Agent 自主安装「技能包（Skills）」与「MCP 插件」。

- 技能包：复用 agent.skills.SkillPackManager——目标目录白名单（project/user
  技能目录）、MANIFEST.sha256 完整性校验、默认拒绝未签名包，全部安全边界沿用。
  来源是本地目录；网络获取交给 terminal 工具（git clone 等，走审批门），
  本工具只负责「校验 + 落位」。安装后技能即出现在提示词的可用技能列表。
- MCP 插件：把 {"name","command","args","env"} 写入用户级配置
  （~/.my_agent/mcp_servers.json，重启自动恢复连接），并立即连接注册工具。
  连接意味着本机会执行该 command——高风险操作，审批策略会介入（on-failure
  策略下需要人工确认）。

命令（execute_json）：
  skill_install  {pack_path, target: user|project, allow_unsigned, overwrite}
  skill_list     {}
  mcp_add        {name, command, args, env, overwrite}
  mcp_list       {}
  mcp_remove     {name}
"""
import json
import os
import shlex
import tempfile

from tools.base import BaseTool, ToolResult


class InstallerTool(BaseTool):
    """技能包 / MCP 插件安装器（安全边界：SkillPackManager 白名单 + 验签）。"""

    def __init__(self, tool_manager=None):
        self._tool_manager = tool_manager

    # ================================================================
    # 元数据
    # ================================================================

    @property
    def name(self) -> str:
        return "installer"

    @property
    def description(self) -> str:
        return (
            "插件安装器：安装技能包（Skills）与 MCP 插件（工具扩展）。\n"
            "\n【技能包安装】\n"
            "  skill_install：安装本地技能包目录（含若干 <技能名>/SKILL.md 子目录）。\n"
            "    - 默认拒绝未签名包（无 MANIFEST.sha256），需 allow_unsigned=true 放行；\n"
            "    - 同名技能已存在时拒绝，需 overwrite=true 覆盖更新；\n"
            "    - 推荐流程：terminal 里 git clone 技能包仓库 → 本工具安装 → 立即生效。\n"
            "  skill_list：列出已安装技能（名称/描述/触发词）。\n"
            "\n【MCP 插件】\n"
            "  mcp_add：安装并立即连接一个 MCP 服务器（stdio），重启后自动恢复。\n"
            "    例：mcp_add {name:\"fs\", command:\"npx\", args:[\"-y\",\"@modelcontextprotocol/server-filesystem\",\"/tmp\"]}\n"
            "    注意：连接会在本机执行 command，属高风险操作，可能需要用户确认。\n"
            "  mcp_list：列出已安装的 MCP 插件（标注是否已连接）。\n"
            "  mcp_remove：卸载插件（断开连接 + 移除持久化配置）。"
        )

    risk_level: str = "medium"
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["skill_install", "skill_list", "mcp_add", "mcp_list", "mcp_remove"],
                    "description": "要执行的安装器命令",
                },
                "pack_path": {"type": "string", "description": "skill_install：技能包本地目录路径"},
                "target": {"type": "string", "enum": ["user", "project"],
                            "description": "skill_install：安装目标（默认 project）"},
                "allow_unsigned": {"type": "boolean", "description": "skill_install：放行未签名包（默认 false）"},
                "overwrite": {"type": "boolean", "description": "覆盖同名技能/插件（默认 false）"},
                "name": {"type": "string", "description": "mcp_add/mcp_remove：插件名"},
                "mcp_command": {"type": "string", "description": "mcp_add：MCP 服务器启动命令（如 npx / uvx）"},
                "args": {"description": "mcp_add：命令参数（数组或空格分隔字符串）"},
                "env": {"type": "object", "description": "mcp_add：额外环境变量（可选）"},
            },
            "required": ["command"],
        }

    def build_approval_request(self, arguments):
        from tools.base import ApprovalRequest
        command = str(arguments.get("command", ""))
        # mcp_add 意味着允许在本机执行一个新命令（连接时 spawn），高风险；
        # skill_install 受 SkillPackManager 白名单+验签约束，medium；其余只读/删除配置，low。
        if command == "mcp_add":
            risk, sandbox = "high", "workspace-write"
        elif command in ("skill_install",):
            risk, sandbox = "medium", "workspace-write"
        else:
            risk, sandbox = "low", "read-only"
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=f"installer {command}".strip(),
            risk_level=risk,
            min_sandbox_mode=sandbox,
        )

    # ================================================================
    # 入口
    # ================================================================

    def execute_json(self, arguments):
        command = str(arguments.get("command", "")).strip()
        try:
            if command == "skill_install":
                return self._skill_install(
                    pack_path=str(arguments.get("pack_path", "")),
                    target=str(arguments.get("target", "project")),
                    allow_unsigned=bool(arguments.get("allow_unsigned", False)),
                    overwrite=bool(arguments.get("overwrite", False)),
                )
            if command == "skill_list":
                return self._skill_list()
            if command == "mcp_add":
                return self._mcp_add(
                    name=str(arguments.get("name", "")).strip(),
                    command=str(arguments.get("mcp_command", "")).strip(),
                    args=arguments.get("args"),
                    env=arguments.get("env"),
                    overwrite=bool(arguments.get("overwrite", False)),
                )
            if command == "mcp_list":
                return self._mcp_list()
            if command == "mcp_remove":
                return self._mcp_remove(str(arguments.get("name", "")).strip())
        except Exception as e:
            return ToolResult(success=False, output="", error=f"安装器执行失败: {str(e)[:200]}")
        return ToolResult(success=False, output="", error=f"未知安装器命令: '{command}'")

    def execute(self, input_str: str) -> ToolResult:
        """旧文本协议：installer <command> [位置参数/标志]"""
        parts = input_str.strip().split()
        if not parts:
            return ToolResult(success=False, output="", error="安装器命令为空。")
        cmd = parts[0].lower().replace("-", "_")
        rest = parts[1:]

        if cmd == "skill_install":
            if not rest:
                return ToolResult(success=False, output="", error="格式: skill_install <技能包路径> [user|project] [--allow-unsigned] [--overwrite]")
            pack_path = rest[0]
            target = "project"
            allow_unsigned = "--allow-unsigned" in rest
            overwrite = "--overwrite" in rest
            for tok in rest[1:]:
                if tok in ("user", "project"):
                    target = tok
            return self.execute_json({"command": "skill_install", "pack_path": pack_path,
                                      "target": target, "allow_unsigned": allow_unsigned,
                                      "overwrite": overwrite})
        if cmd == "skill_list":
            return self._skill_list()
        if cmd == "mcp_add":
            if len(rest) < 2:
                return ToolResult(success=False, output="", error="格式: mcp_add <name> <command> [args...]")
            name, command = rest[0], rest[1]
            args = rest[2:]
            return self.execute_json({"command": "mcp_add", "name": name, "mcp_command": command, "args": args})
        if cmd == "mcp_list":
            return self._mcp_list()
        if cmd == "mcp_remove":
            if not rest:
                return ToolResult(success=False, output="", error="格式: mcp_remove <name>")
            return self._mcp_remove(rest[0])
        return ToolResult(success=False, output="", error=f"未知安装器命令: '{cmd}'")

    # ================================================================
    # 技能包
    # ================================================================

    @staticmethod
    def _skills_target_dir(target: str) -> str:
        from config import SKILLS_CONFIG
        key = "user_dir" if str(target).lower() == "user" else "project_dir"
        return SKILLS_CONFIG.get(key) or SKILLS_CONFIG["project_dir"]

    def _skill_install(self, pack_path: str, target: str,
                       allow_unsigned: bool, overwrite: bool) -> ToolResult:
        from agent.skills import SkillPackManager

        pack_path = pack_path.strip()
        if not pack_path or not os.path.isdir(pack_path):
            return ToolResult(success=False, output="",
                              error=f"技能包目录不存在: '{pack_path}'。"
                                    "先用 terminal 把技能包下载/克隆到本地再安装。")
        target_dir = self._skills_target_dir(target)
        mgr = SkillPackManager()
        try:
            result = mgr.install_pack(
                pack_path, target_dir,
                overwrite=overwrite, allow_unsigned=allow_unsigned,
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"技能包安装失败: {str(e)[:200]}")

        lines = [f"技能包安装{'成功' if result.ok else '未完成'}（status={result.status}）→ {target_dir}"]
        for r in result.results:
            lines.append(f"  - {r.name}: {r.status}")
        if result.conflict:
            lines.append(f"  冲突: {', '.join(result.conflict)}（覆盖更新需 overwrite=true）")
        for reason in result.reasons[:5]:
            lines.append(f"  原因: {reason}")
        if result.ok:
            lines.append("技能已生效：新任务的提示词会自动包含该技能（按触发词匹配）。")
        return ToolResult(success=result.ok, output="\n".join(lines),
                          error=None if result.ok else "；".join(result.reasons[:2]))

    def _skill_list(self) -> ToolResult:
        from agent.skills import SkillManager
        from config import SKILLS_CONFIG
        mgr = SkillManager(
            project_dir=SKILLS_CONFIG.get("project_dir"),
            user_dir=SKILLS_CONFIG.get("user_dir"),
        )
        skills = mgr.discover()
        if not skills:
            return ToolResult(success=True, output="当前没有已安装的技能。"
                              "可用 skill_install 安装技能包（含 SKILL.md 的目录）。")
        lines = [f"共 {len(skills)} 个技能:"]
        for s in skills:
            triggers = f"（触发: {', '.join(s.triggers[:4])}）" if s.triggers else ""
            lines.append(f"  - {s.name}: {(s.description or '')[:80]}{triggers}")
        return ToolResult(success=True, output="\n".join(lines))

    # ================================================================
    # MCP 插件
    # ================================================================

    @staticmethod
    def _user_config_file() -> str:
        import config
        return config.MCP_USER_CONFIG_FILE

    @classmethod
    def _read_installed(cls) -> list:
        import config
        return config._load_user_mcp_servers()

    @classmethod
    def _write_installed(cls, servers: list) -> None:
        """原子写入用户级插件配置（tempfile + os.replace）。"""
        path = cls._user_config_file()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(servers, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _norm_args(args) -> list:
        if args is None:
            return []
        if isinstance(args, list):
            return [str(a) for a in args]
        if isinstance(args, str):
            return shlex.split(args) if args.strip() else []
        return [str(args)]

    def _mcp_add(self, name: str, command: str, args, env, overwrite: bool) -> ToolResult:
        if not name or not command:
            return ToolResult(success=False, output="",
                              error="mcp_add 需要 name 与 command（command 是 MCP 服务器启动命令）。")
        servers = self._read_installed()
        if any(str(s.get("name")) == name for s in servers) and not overwrite:
            return ToolResult(
                success=False, output="",
                error=f"插件 '{name}' 已安装。更新需 overwrite=true（或先 mcp_remove）。",
            )
        entry = {"name": name, "command": command,
                 "args": self._norm_args(args), "env": dict(env or {})}
        ok = True
        err = ""
        if self._tool_manager is not None:
            ok = bool(self._tool_manager.connect_mcp_server(
                name, command, entry["args"], entry["env"]))
            if not ok:
                err = "（连接失败：请检查 command/args 是否正确、依赖是否已安装）"
        if ok:
            servers = [s for s in servers if str(s.get("name")) != name]
            servers.append(entry)
            self._write_installed(servers)
            tool_names = ""
            if self._tool_manager is not None:
                try:
                    listed = self._tool_manager.list_tools() or []
                    mcp_names = [t for t in listed if str(t).startswith(f"{name}_") or str(t).startswith(f"mcp_{name}")]
                    tool_names = f"，注册工具: {', '.join(mcp_names[:8])}" if mcp_names else ""
                except Exception:
                    pass
            return ToolResult(success=True,
                              output=f"MCP 插件 '{name}' 已安装并连接{tool_names}。\n"
                                     f"配置已持久化到 {self._user_config_file()}，重启自动恢复。")
        return ToolResult(success=False, output="",
                          error=f"MCP 插件 '{name}' 连接失败{err}。未写入持久化配置。")

    def _mcp_list(self) -> ToolResult:
        servers = self._read_installed()
        if not servers:
            return ToolResult(success=True, output="当前没有已安装的 MCP 插件。"
                              "可用 mcp_add 安装（例：playwright-mcp / filesystem 等社区服务器）。")
        connected = set()
        if self._tool_manager is not None:
            try:
                connected = set(self._tool_manager.list_mcp_servers() or [])
            except Exception:
                pass
        lines = [f"共 {len(servers)} 个 MCP 插件:"]
        for s in servers:
            state = "已连接" if str(s.get("name")) in connected else "未连接"
            args_str = " ".join(s.get("args") or [])
            lines.append(f"  - {s.get('name')} [{state}] {s.get('command')} {args_str}".rstrip())
        return ToolResult(success=True, output="\n".join(lines))

    def _mcp_remove(self, name: str) -> ToolResult:
        if not name:
            return ToolResult(success=False, output="", error="mcp_remove 需要 name。")
        servers = self._read_installed()
        kept = [s for s in servers if str(s.get("name")) != name]
        if len(kept) == len(servers):
            return ToolResult(success=False, output="", error=f"未找到插件 '{name}'。")
        if self._tool_manager is not None:
            try:
                self._tool_manager.disconnect_mcp_server(name)
            except Exception:
                pass
        self._write_installed(kept)
        return ToolResult(success=True, output=f"MCP 插件 '{name}' 已卸载（断开连接并移除配置）。")
