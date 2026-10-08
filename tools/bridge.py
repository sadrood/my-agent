"""桌面端内嵌浏览器桥的客户端：健康探测 + 命令串行化 + 瞬态重试 + 错误分类。

桥是 Electron 侧的 HTTP 服务，一次只处理一条命令：并发调用必须串行；"桥刚起/刚挂"
这类连接错误值得重试，命令本身的错误立刻回给模型。
"""
import json
import threading
import time
import urllib.request

#: 只有这些信号才重试（桥未就绪/正在重启）；其余错误立刻抛，不掩盖真 bug
TRANSIENT_SIGNS = (
    "connection refused", "connectionrefused", "econnrefused",
    "connection reset", "connectionreseterror", "broken pipe",
    "timed out", "timeout", "temporarily unavailable",
    "remote end closed", "10054", "10061",
)

DEFAULT_PROBE_TTL = 3.0
DEFAULT_PROBE_TIMEOUT = 0.8
DEFAULT_RETRIES = 3
RETRY_BASE_DELAY = 0.25


def is_transient(exc) -> bool:
    """这个错误是不是"桥暂时不可达"（可重试），而不是命令/参数错了。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(sign in text for sign in TRANSIENT_SIGNS)


def describe_error(exc) -> str:
    """把异常翻成模型能据此决策的一句话。"""
    if is_transient(exc):
        return (f"内嵌浏览器桥不可达（暂时性：{str(exc)[:100]}）——"
                "桥可能刚启动或正在重启，稍后重试；频繁出现请确认桌面端在运行。")
    return f"内嵌浏览器桥调用失败（{type(exc).__name__}: {str(exc)[:120]}）"


class BridgeClient:
    """串行化的桥客户端（探测结果带 TTL 缓存，调用失败立即失效）。"""

    def __init__(self, url: str, timeout: float = 60.0, retries: int = DEFAULT_RETRIES,
                 probe_ttl: float = DEFAULT_PROBE_TTL,
                 probe_timeout: float = DEFAULT_PROBE_TIMEOUT):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.probe_ttl = probe_ttl
        self.probe_timeout = probe_timeout
        self._lock = threading.RLock()      # 命令串行化；探测会在 call 内发生，故可重入
        self._probe_at = 0.0
        self._probe_ok = False

    @property
    def health_url(self) -> str:
        return self.url.rsplit("/", 1)[0] + "/health"

    def probe(self, use_cache: bool = True) -> bool:
        """桥是否在线（健康检查）。缓存 TTL 很短：桥刚挂时不能继续被判成在线。"""
        now = time.time()
        with self._lock:
            if use_cache and (now - self._probe_at) < self.probe_ttl:
                return self._probe_ok
        ok = False
        try:
            with urllib.request.urlopen(self.health_url, timeout=self.probe_timeout) as resp:
                ok = bool(json.loads(resp.read().decode("utf-8")).get("ok"))
        except Exception:                       # noqa: BLE001
            ok = False
        with self._lock:
            self._probe_at = time.time()
            self._probe_ok = ok
        return ok

    def _invalidate_probe(self) -> None:
        self._probe_at = 0.0

    def call(self, action: str, **params) -> dict:
        """POST 一条动作，返回 {"ok": bool, "output"/"base64"/"error"}；不抛异常。"""
        payload = json.dumps({"action": action, **params}).encode("utf-8")
        last_error = None
        with self._lock:                        # 桥是单队列：并发调用必须排队
            for attempt in range(self.retries):
                req = urllib.request.Request(
                    self.url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                except Exception as e:          # noqa: BLE001
                    last_error = e
                    self._invalidate_probe()
                    if attempt + 1 >= self.retries or not is_transient(e):
                        break
                    time.sleep(RETRY_BASE_DELAY * (attempt + 1))
                    continue
                if not data.get("ok"):
                    err = str(data.get("error", ""))
                    if attempt + 1 < self.retries and is_transient(RuntimeError(err)):
                        last_error = RuntimeError(err)
                        self._invalidate_probe()
                        time.sleep(RETRY_BASE_DELAY * (attempt + 1))
                        continue
                return data
        return {"ok": False, "error": describe_error(last_error)}
