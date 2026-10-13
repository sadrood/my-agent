"""浏览器 worker 线程：把 Playwright 命令投递到专属线程执行，并在会话失效时自愈。

从 BrowserTool 抽出（god class 拆分）：只碰 self 上的 worker/playwright 引用，
不关心具体命令——各命令的处理仍留在 BrowserTool。
"""
import threading

from tools.base import ToolResult


class BrowserWorkerMixin:
    # ================================================================
    # 专属 worker 线程（Playwright 生命周期绑定单线程）
    # ================================================================

    def _start_worker(self) -> None:
        """创建/重建专属 worker 线程。
        Playwright sync API 底层把 asyncio 事件循环绑定到启动它的线程；
        浏览器命令，playwright 对象始终在同一线程创建和使用。"""
        import queue

        self._worker_queue = queue.Queue()
        self._worker_broken = False

        def _loop():
            while True:
                job = self._worker_queue.get()
                if job is None:
                    return
                try:
                    job()
                except Exception:
                    pass  # job 内部已捕获异常并放入结果 box

        self._worker = threading.Thread(
            target=_loop, daemon=True, name="browser-worker")
        self._worker.start()

    # 浏览器会话"已死"的特征串：命中即作废旧 worker/引用并自动重建重试
    _DEAD_SIGNALS = (
        "has been closed",
        "Target closed",
        "Target page, context or browser",
        "Browser closed",
        "Connection closed",
        "Sync API inside the asyncio loop",
        "cannot switch to a different thread",
        "browser has been closed",
        "会话已重建",
    )

    @classmethod
    def _looks_dead(cls, text: str) -> bool:
        t = str(text or "")
        return any(sig.lower() in t.lower() for sig in cls._DEAD_SIGNALS)

    def _stop_playwright(self) -> None:
        """停掉 Playwright 的**驱动进程**（node.exe）。"""
        pw = self._playwright
        self._playwright = None
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    def _invalidate(self, cleanup_residual: bool = True) -> None:
        """作废当前 worker 与 Playwright 引用：下一个命令会重建全新 worker。

        连带清理残留浏览器进程——卡死时驱动进程停不掉 Chromium，只丢引用会泄漏进程。
        """
        with self._state_lock:
            self._gen += 1                     # 作废在飞任务
            had_session = self._had_session()
            self._worker_broken = True
            self._worker = None
            self._worker_queue = None
            self._stop_playwright()
            self._browser = None
            self._context = None
            self._pages = []
            self._current_page_idx = 0
        if cleanup_residual and had_session:
            try:
                self._force_cleanup_residual()
            except Exception:                    # noqa: BLE001
                pass

    def _recover_and_retry(self, command: str, handler, args: str):
        """会话失效后的自愈：重建 worker（+必要时重开浏览器）并重试一次原命令。
        旧 worker 线程的 asyncio loop 无法复用时，不再把错误直接抛给模型。"""
        try:
            self._invalidate()
            if command not in ("launch", "close"):
                try:
                    self._dispatch(self._launch, "")     # 重建后重开浏览器
                except Exception:
                    pass
            res = self._dispatch(handler, args)
            if res is not None and res.success:
                out = (res.output or "").strip()
                note = "(检测到浏览器会话已失效，已自动重建浏览器并重试成功)"
                return ToolResult(
                    success=True,
                    output=((out + chr(10)) if out else "") + note,
                    metadata=getattr(res, "metadata", None),
                )
            return res
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"浏览器会话失效，且自动恢复失败: {str(e)[:200]}")

    def _dispatch(self, fn, *args, wait_timeout: float = None, **kwargs):
        """把浏览器命令投递到 worker 线程执行并等待结果。
        worker（daemon 线程随进程退出），由 _force_cleanup_residual 兜底。"""
        # worker 缺失/已死/上次卡死被废弃 → 重建。附加情况：worker 线程虽然还活着，但绑定的浏览器/上下文引用已经死掉（浏览器进程被杀、CDP 会话关闭等）。
        needs_rebuild = (
            self._worker is None
            or not self._worker.is_alive()
            or self._worker_broken
        )
        if not needs_rebuild:
            try:
                browser_alive = self._is_browser_alive()
            except Exception:
                browser_alive = False
            try:
                context_alive = bool(
                    self._context is not None and self._is_context_alive())
            except Exception:
                context_alive = False
            if not browser_alive and not context_alive:
                needs_rebuild = True
        if needs_rebuild:
            # 防御性丢弃旧 playwright 引用（若未被 reset 清空），避免新 worker 复用绑定在已死线程上的事件循环。
            if self._playwright is not None or self._context is not None:
                self._stop_playwright()
                self._browser = None
                self._context = None
                self._pages = []
                self._current_page_idx = 0
            self._start_worker()

        box: dict = {}
        done = threading.Event()
        gen = self._gen

        def _job():
            # 世代号变了 = 这条命令属于已经作废的会话：直接丢弃，
            # 不让卡死的旧 worker 把结果/状态写回新会话。
            if gen != self._gen:
                box["stale"] = True
                done.set()
                return
            try:
                box["result"] = fn(*args, **kwargs)
            except BaseException as e:   # noqa: BLE001
                box["error"] = e
            finally:
                done.set()

        try:
            self._worker_queue.put(_job)
        except Exception as e:
            self._worker_broken = True
            self._worker = None
            raise RuntimeError(f"浏览器 worker 队列异常: {e}") from e

        if wait_timeout is None:
            done.wait()
        else:
            done.wait(wait_timeout)
            if not done.is_set():
                # 短等待超时（析构场景）：放弃该 worker，交给兜底清理
                self._worker_broken = True
                return None
        if box.get("stale"):
            raise RuntimeError("浏览器会话已重建（上一个 worker 已作废），请重试该命令。")
        if "error" in box:
            raise box["error"]
        return box.get("result")
