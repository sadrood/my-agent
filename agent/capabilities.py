"""本机能力探测：启动时告诉模型这台机器究竟能做什么。

各模块仍各自保留平台分支（行为分叉只能在那里判断），这里只汇总**结果**——
否则模型会把 Windows 专属的桌面操控、AppContainer 沙箱当成通用能力去试。
"""
import platform


def _browser_backend(tool_manager) -> str:
    if tool_manager is None:
        return ""
    try:
        kind = type(tool_manager.get_tool("browser")).__name__
    except Exception:                            # noqa: BLE001
        return ""
    return {
        "EgoBrowserTool": "ego（共享你自己的 Edge/Chrome）",
        "EmbeddedBrowserTool": "内置桥（桌面端托管）",
        "BrowserTool": "Playwright（独立浏览器实例）",
    }.get(kind, kind)


def describe(tool_manager=None) -> str:
    """返回注入系统提示的能力清单（多行文本）。探测失败只影响该行，不影响启动。"""
    lines = [f"- 系统: {platform.system()} {platform.release()}（{platform.machine()}）"]

    try:
        from tools.computer_use import COMPUTER_SUPPORTED
        lines.append("- 桌面操控（computer 工具）: "
                     + ("可用" if COMPUTER_SUPPORTED else "不可用（仅 Windows）"))
    except Exception:                            # noqa: BLE001
        pass

    try:
        from agent.sandbox import appcontainer_available, sandbox_enabled, sandbox_mode
        if not sandbox_enabled():
            lines.append("- OS 级沙箱: 关闭（SANDBOX_EXECUTION=off）")
        elif appcontainer_available():
            lines.append(f"- OS 级沙箱: AppContainer（{sandbox_mode()}）")
        else:
            lines.append(f"- OS 级沙箱: 不可用（{sandbox_mode()} 仅 Windows 支持 AppContainer）")
    except Exception:                            # noqa: BLE001
        pass

    backend = _browser_backend(tool_manager)
    if backend:
        line = f"- 浏览器后端: {backend}"
        if "ego" not in backend:
            try:
                from tools.ego_browser import ego_unavailable_reason
                why = ego_unavailable_reason()
                if why:
                    line += f"；ego 不可用：{why}"
            except Exception:                    # noqa: BLE001
                pass
        lines.append(line)

    return "\n".join(lines)
