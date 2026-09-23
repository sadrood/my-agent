from .hub import DashboardHub, get_dashboard_hub

try:
    from .server import app, start_server
except ImportError as e:
    # 只把「FastAPI / uvicorn 没装」当成缺依赖。其余 ImportError（本项目某个模块
    # 写错名字、语法错误、循环导入）必须**原样抛出** —— 旧实现用裸 except Exception
    # 全部吞掉、统一报「FastAPI 未安装」，桌面端后端拉起失败时日志指向完全错误的
    # 方向（2026-09-22 审计）。
    if "fastapi" not in str(e).lower() and "uvicorn" not in str(e).lower():
        raise
    app = None

    def start_server(*args, **kwargs):
        print("[Dashboard] FastAPI 未安装: pip install fastapi uvicorn")

__all__ = ["DashboardHub", "get_dashboard_hub", "app", "start_server"]
