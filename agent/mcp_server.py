"""
MCP Server 模块（借鉴同类实现的 mcp-server 模式）。

同类框架除了消费 MCP 工具，还可以把自己作为 MCP server 暴露出去，
让任何 MCP 宿主（IDE、其他 Agent、宿主平台等）以工具的方式调用它。

本模块同样把 my_agent 包装成 MCP server：
- run_agent: 让宿主把一个目标交给 my_agent 执行（单Agent / Team / Research）
- list_agent_tools: 查看 my_agent 当前可用的工具

用法:
    python main.py --mcp-server            # stdio 传输（默认）
    python main.py --mcp-server --mcp-port 8080   # 可选: SSE（暂不支持，保留参数）

兼容性: 自动适配 mcp SDK 1.x（mcp.server.fastmcp.FastMCP）与 2.x
（mcp.server.mcpserver.MCPServer）。
"""
from typing import List

from config import MCP_CONFIG


def _create_server():
    """创建 MCP server 实例（兼容 mcp SDK 1.x / 2.x）。"""
    try:
        from mcp.server.fastmcp import FastMCP
        return FastMCP(
            "my_agent",
            instructions=(
                "my_agent 是一个通用 AI Agent（借鉴开源 agent 框架设计）："
                "具备思考、规划、工具调用（终端/文件/Python/浏览器/MCP/视觉）、"
                "安全审批、Guardian 审校与 Rollout 追踪能力。"
                "用 run_agent 把任务交给它执行。"
            ),
        )
    except ImportError:
        pass
    try:
        from mcp.server.mcpserver import MCPServer
        return MCPServer(
            "my_agent",
            instructions=(
                "my_agent 是一个通用 AI Agent（借鉴开源 agent 框架设计）："
                "具备思考、规划、工具调用（终端/文件/Python/浏览器/MCP/视觉）、"
                "安全审批、Guardian 审校与 Rollout 追踪能力。"
                "用 run_agent 把任务交给它执行。"
            ),
        )
    except ImportError:
        raise ImportError(
            "未找到可用的 MCP SDK。请安装官方 SDK: pip install 'mcp>=1.0'"
        )


def _run_agent(
    goal: str,
    mode: str = "single",
    max_steps: int = 20,
    approval_policy: str = "never",
    sandbox_mode: str = "workspace-write",
    guardian: bool = True,
    max_step_ops: int = 12,
) -> str:
    """
    执行一个 Agent 任务。

    Args:
        goal: 任务目标描述
        mode: single（单Agent）/ team（团队协作）/ research（深度研究）
        max_steps: 最大计划步骤数
        approval_policy: untrusted / on-failure / on-request / never
                         （MCP 无人值守环境默认 never）
        sandbox_mode: read-only / workspace-write / danger-full-access
        guardian: 是否启用 Guardian 安全审校
        max_step_ops: 单步骤内最大工具操作数

    Returns:
        执行结果文本
    """
    from agent import Agent, AgentConfig
    from agent.team import Team
    from tools import ToolManager

    if mode == "team":
        team = Team(tool_manager=ToolManager())
        result = team.run(goal, enable_review=True)
        return (
            f"[Team] 成功: {result.success}\n"
            f"最终答案:\n{result.final_answer}\n\n"
            + (f"审校建议:\n{result.review_feedback}" if result.review_feedback else "")
        )

    if mode == "research":
        from tools.research import DeepResearcher
        from models import LLM
        tool_manager = ToolManager()
        researcher = DeepResearcher(tool_manager=tool_manager, llm=LLM())
        report = researcher.research(goal, depth=3)
        sections = "\n\n".join(
            f"### {s.get('title', '')}\n{s.get('content', '')[:1500]}"
            for s in report.sections
        )
        return (
            f"[Research] 置信度: {report.confidence:.0%}\n"
            f"摘要:\n{report.summary[:800]}\n\n{sections}"
        )

    # 默认 single 模式
    config = AgentConfig(
        max_steps=max_steps,
        verbose=False,
        enable_vision=True,
        approval_policy=approval_policy,
        sandbox_mode=sandbox_mode,
        approval_interactive=False,          # MCP 环境无人值守
        guardian_enabled=guardian,
        max_step_ops=max_step_ops,
    )
    agent = Agent(
        tool_manager=ToolManager(),
        config=config,
    )
    return agent.run(goal)


def _list_agent_tools() -> List[str]:
    """列出 my_agent 当前注册的工具（含 MCP 远程工具）。"""
    from tools import ToolManager
    tm = ToolManager()
    tools = tm.list_tools()
    mcp_servers = tm.list_mcp_servers()
    if mcp_servers:
        tools.append(f"(已连接 MCP 服务器: {', '.join(mcp_servers)})")
    return tools


def register_tools(server):
    """把 my_agent 的工具注册到 MCP server（兼容 FastMCP / MCPServer 装饰器）。"""
    server.tool(
        name="run_agent",
        description=(
            "把任务交给 my_agent 执行。my_agent 是一个通用 AI Agent："
            "会自己规划步骤并调用终端、文件、Python、浏览器、视觉分析、"
            "外部 MCP 等工具完成任务，内置安全审批与 Guardian 审校。"
            "适合网页操作、生成文件（Excel/CSV/文档）、数据处理、信息检索等任务。"
            "注意：这是长时运行任务，返回的是最终总结。"
        ),
    )(_run_agent)

    server.tool(
        name="list_agent_tools",
        description="查看 my_agent 当前可用的工具列表。",
    )(_list_agent_tools)

    return server


def run_server(transport: str = "stdio"):
    """
    启动 MCP server。

    Args:
        transport: stdio（默认）。mcp 2.x 亦支持 sse / streamable-http。
    """
    server = register_tools(_create_server())
    server.run(transport=transport)


if __name__ == "__main__":
    run_server()
