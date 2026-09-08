"""打包后的后端入口：等价 `python -m dashboard.server`，但可静音控制台日志。

PyInstaller onefile 以本文件为入口（windowed 模式无控制台）：
- 通过子进程环境变量传入端口（MY_AGENT_BACKEND_PORT）
- MY_AGENT_QUIET=1 时把 uvicorn 日志降到 warning，避免无控制台时刷屏
"""
import os
import sys


def main() -> int:
    port = os.environ.get("MY_AGENT_BACKEND_PORT", "8090")
    quiet = os.environ.get("MY_AGENT_QUIET", "") == "1"
    log_level = "warning" if quiet else "info"
    # 与 dashboard.server.__main__ 保持一致（该模块自身解析 argv）
    sys.argv = ["myagent-backend", "--port", port]
    if quiet:
        sys.argv += ["--log-level", log_level]
    import dashboard.server  # noqa: F401  触发路由注册
    from dashboard.server import app
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(port), log_level=log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
