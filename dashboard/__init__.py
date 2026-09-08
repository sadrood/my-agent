from .hub import DashboardHub, get_dashboard_hub

try:
    from .server import app, start_server
except Exception:
    app = None
    def start_server(*args, **kwargs):
        print("[Dashboard] FastAPI 未安装: pip install fastapi uvicorn")

__all__ = ["DashboardHub", "get_dashboard_hub", "app", "start_server"]
