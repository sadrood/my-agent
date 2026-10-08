"""浏览器实时画面：有观看者才抓帧、限最小间隔、只保留最新一帧（慢客户端自然丢帧）。

Playwright 同步 API 的对象绑定在 worker 线程上，做不了后台 CDP 事件泵，故采用拉取式。
"""
import threading
import time

DEFAULT_MIN_GAP_MS = 1000


class FrameThrottle:
    """限帧：两次抓帧间隔不小于 min_gap_ms，被丢掉的那次计数。"""

    def __init__(self, min_gap_ms: int = DEFAULT_MIN_GAP_MS):
        self.min_gap = max(0.0, float(min_gap_ms) / 1000.0)
        self.emitted = 0
        self.dropped = 0
        self._last = 0.0

    def allow(self, now: float = None) -> bool:
        now = time.time() if now is None else float(now)
        if now - self._last < self.min_gap:
            self.dropped += 1
            return False
        self._last = now
        self.emitted += 1
        return True


class FrameStore:
    """只保留最新一帧（带自增序号，便于 SSE 判重与等待新帧）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._frame = None

    def put(self, data: str, fmt: str = "jpeg") -> int:
        with self._lock:
            self._seq += 1
            self._frame = {"seq": self._seq, "data": data, "format": fmt,
                           "at": time.time()}
            return self._seq

    def latest(self):
        with self._lock:
            return None if self._frame is None else dict(self._frame)

    def wait_next(self, seq: int, timeout: float = 1.0):
        """等出现比 seq 更新的帧（超时返回 None）；供需要阻塞语义的调用方用。"""
        deadline = time.time() + max(0.0, timeout)
        while True:
            frame = self.latest()
            if frame and frame["seq"] > seq:
                return frame
            if time.time() >= deadline:
                return None
            time.sleep(0.05)


class LiveStream:
    """观看者计数 + 抓帧器注册。没有观看者时**不抓帧**（不白烧 CPU）。"""

    def __init__(self, min_gap_ms: int = DEFAULT_MIN_GAP_MS, store=None, throttle=None):
        self.store = store or FrameStore()
        self.throttle = throttle or FrameThrottle(min_gap_ms)
        self._lock = threading.Lock()
        self._capturer = None
        self._viewers = 0

    def set_capturer(self, fn) -> None:
        with self._lock:
            self._capturer = fn

    def acquire(self) -> int:
        with self._lock:
            self._viewers += 1
            return self._viewers

    def release(self) -> int:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            return self._viewers

    def viewers(self) -> int:
        with self._lock:
            return self._viewers

    def capture_now(self):
        """按限流抓一帧并返回最新帧（无抓帧器/无观看者/抓帧失败都返回已有的最新帧）。"""
        with self._lock:
            capturer, viewers = self._capturer, self._viewers
        if capturer is None or viewers <= 0:
            return self.store.latest()
        if not self.throttle.allow():
            return self.store.latest()
        try:
            data = capturer()
        except Exception:                        # noqa: BLE001
            return self.store.latest()
        if data:
            self.store.put(data)
        return self.store.latest()

    def stats(self) -> dict:
        return {"viewers": self.viewers(), "emitted": self.throttle.emitted,
                "dropped": self.throttle.dropped, "min_gap_ms": self.throttle.min_gap * 1000}


def _configured_min_gap_ms() -> int:
    """最小抓帧间隔（毫秒）：可经 BROWSER_LIVE_MIN_GAP_MS 调整，默认 1 秒。"""
    try:
        from config import BROWSER_CONFIG
        return int(BROWSER_CONFIG.get("live_min_gap_ms", DEFAULT_MIN_GAP_MS))
    except Exception:                            # noqa: BLE001
        return DEFAULT_MIN_GAP_MS


#: 进程内单例：browser 工具注册抓帧器，dashboard 的 SSE 端点消费帧
ACTIVE = LiveStream(min_gap_ms=_configured_min_gap_ms())
