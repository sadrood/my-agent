"""
原生桌面 GUI（Tkinter，Python 自带，零第三方依赖）。

独立桌面窗口：输入目标 → 后台线程跑 Agent → 事件经 event_sink 推入队列 →
主线程轮询渲染（工具调用 / 结果 / 最终回答）。非 Web、非浏览器壳。
"""
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import scrolledtext

_WIN_TITLE = "my_agent · 小悟"


def _wake_existing_instance() -> bool:
    """Windows: 若已有同名窗口，唤醒它并置前，返回 True。

    防止重复启动堆积多个桌面窗口。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        EnumWindows = user32.EnumWindows
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextLengthW = user32.GetWindowTextLengthW
        IsWindowVisible = user32.IsWindowVisible
        ShowWindow = user32.ShowWindow
        SetForegroundWindow = user32.SetForegroundWindow
    except Exception:
        return False

    found = []

    def cb(hwnd, lparam):
        if found:
            return True
        if IsWindowVisible(hwnd):
            n = GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == _WIN_TITLE:
                    found.append(hwnd)
        return True

    try:
        EnumWindows(EnumWindowsProc(cb), 0)
    except Exception:
        return False

    if found:
        try:
            ShowWindow(found[0], 9)         # SW_RESTORE
            SetForegroundWindow(found[0])
        except Exception:
            pass
        return True
    return False


def apply_event(out, event_type: str, data: dict, pending_cards: list) -> None:
    """把一条 Agent 事件渲染到输出区（独立函数，便于脱离 Tk 单测）。

    工具调用渲染为"卡片"：头部（工具名 + 运行状态 + 耗时）+ 参数摘要 + 结果行，
    通过 Text 的 tag 机制（card/card_ok/card_err）形成块状底色。
    """

    def _insert(text: str, tag: str = "meta") -> int:
        out.config(state="normal")
        out.insert("end", text.rstrip() + "\n", tag)
        line = int(out.index("end-1c").split(".")[0])
        out.see("end")
        out.config(state="disabled")
        return line

    def _compact(args) -> str:
        if not args:
            return ""
        try:
            s = json.dumps(args, ensure_ascii=False)
        except Exception:
            s = str(args)
        return s[:160] + ("…" if len(s) > 160 else "")

    if event_type == "tool_call":
        tool = str(data.get("tool", ""))
        line = _insert(f"⏺ {tool}   ⏳ 运行中…", "tool")
        out.tag_add("card", f"{line}.0", f"{line}.end")
        body = _compact(data.get("arguments") or data.get("args"))
        if body:
            body_line = _insert(f"     {body}", "tool")
            out.tag_add("card", f"{body_line}.0", f"{body_line}.end")
        pending_cards.append({"tool": tool, "line": line, "t0": time.time()})
    elif event_type == "tool_result":
        tool = str(data.get("tool", ""))
        ok = bool(data.get("success", False))
        text = str(data.get("output") or data.get("error") or "")[:400]
        # 回写对应卡片的状态行：名称 + 成功/失败 + 耗时
        idx = next((i for i, c in enumerate(pending_cards) if c["tool"] == tool), None)
        if idx is not None:
            card = pending_cards.pop(idx)
            elapse = time.time() - card["t0"]
            mark = ("✅ 成功" if ok else "❌ 失败") + f"（{elapse:.1f}s）"
            bg_tag = "card_ok" if ok else "card_err"
            out.config(state="normal")
            out.delete(f"{card['line']}.0", f"{card['line']}.end")
            out.insert(f"{card['line']}.0", f"⏺ {tool}  {mark}", "ok" if ok else "err")
            out.tag_add(bg_tag, f"{card['line']}.0", f"{card['line']}.end")
            out.config(state="disabled")
            out.see("end")
        # 结果摘要行（同样带卡片底色）
        res_line = _insert(("⎿ " if ok else "⎿ ✗ ") + text, "ok" if ok else "err")
        out.tag_add("card_ok" if ok else "card_err", f"{res_line}.0", f"{res_line}.end")
    elif event_type == "answer":
        _insert("─" * 46, "meta")
        _insert(str(data.get("output") or "(无输出)"), "answer")


def launch_desktop():
    """启动原生桌面窗口（阻塞，窗口关闭后返回）。"""
    if _wake_existing_instance():
        return
    root = tk.Tk()
    root.title(_WIN_TITLE)
    root.geometry("920x680")
    root.configure(bg="#1e1e2e")

    # 窗口居中
    root.update_idletasks()
    _w, _h = 920, 680
    _sw = root.winfo_screenwidth()
    _sh = root.winfo_screenheight()
    root.geometry(f"{_w}x{_h}+{max((_sw - _w) // 2, 0)}+{max((_sh - _h) // 2 - 40, 0)}")

    # 兜底：确保窗口立即可见且置前（避免后台启动时落在隐藏位置/最小化状态）
    try:
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(600, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass

    events = queue.Queue()
    running = {"v": False}
    stop_event = threading.Event()       # 停止信号：点击"停止"按钮置位

    # ---- 顶部：输入区 ----
    top = tk.Frame(root, bg="#1e1e2e")
    top.pack(fill="x", padx=10, pady=(10, 4))
    entry = tk.Text(
        top, height=3, wrap="word", bg="#28283a", fg="#e6e6f0",
        insertbackground="#e6e6f0", font=("Consolas", 11),
        relief="flat", highlightthickness=1, highlightbackground="#3b3b55",
    )
    entry.pack(side="left", fill="both", expand=True)

    btn_frame = tk.Frame(top, bg="#1e1e2e")
    btn_frame.pack(side="right", padx=(8, 0))
    send_btn = tk.Button(
        btn_frame, text="发送\nCtrl+Enter", width=11, height=3,
        bg="#4a6cf7", fg="#ffffff", activebackground="#3a5ce0",
        relief="flat", font=("Microsoft YaHei", 10),
    )
    send_btn.pack()
    stop_btn = tk.Button(
        btn_frame, text="停止", width=11, height=1,
        bg="#e05c5c", fg="#ffffff", activebackground="#c94f4f",
        relief="flat", font=("Microsoft YaHei", 10),
        state="disabled", command=lambda: do_stop(),
    )
    stop_btn.pack(pady=(8, 0))

    # 状态栏：运行状态灯（●）+ 状态文本
    status_bar = tk.Frame(root, bg="#1e1e2e")
    status_bar.pack(fill="x", padx=12)
    lamp = tk.Label(
        status_bar, text="●", bg="#1e1e2e", fg="#6fcf97",
        font=("Consolas", 11),
    )
    lamp.pack(side="left")
    status = tk.Label(
        status_bar, text="就绪", anchor="w", bg="#1e1e2e", fg="#9aa0b5",
        font=("Microsoft YaHei", 9),
    )
    status.pack(side="left", padx=(6, 0))

    # ---- 输出区 ----
    out = scrolledtext.ScrolledText(
        root, bg="#14141f", fg="#d0d0dd", insertbackground="#d0d0dd",
        font=("Consolas", 10), relief="flat", wrap="word", state="disabled",
    )
    out.pack(fill="both", expand=True, padx=10, pady=(4, 10))

    out.tag_config("user", foreground="#4a6cf7")
    out.tag_config("tool", foreground="#7f7f95")
    out.tag_config("ok", foreground="#6fcf97")
    out.tag_config("err", foreground="#eb5757")
    out.tag_config("answer", foreground="#e6e6f0", font=("Microsoft YaHei", 11))
    out.tag_config("meta", foreground="#9aa0b5")
    out.tag_config("card", foreground="#c8c8dd", background="#232336")      # 工具卡片：进行中
    out.tag_config("card_ok", foreground="#c8c8dd", background="#1d2b23")  # 卡片：成功
    out.tag_config("card_err", foreground="#c8c8dd", background="#2b1d1f")  # 卡片：失败

    # ---- 工具函数 ----
    pending_cards: list = []   # 进行中的工具调用卡片（头部行号 + 开始时间）

    def append(text: str, tag: str = "meta") -> int:
        """追加一行输出，返回该行行号（1-based）。"""
        out.config(state="normal")
        out.insert("end", text.rstrip() + "\n", tag)
        line = int(out.index("end-1c").split(".")[0])
        out.see("end")
        out.config(state="disabled")
        return line

    def render(event_type: str, data: dict):
        """事件渲染委托给模块级 apply_event（便于单测）。"""
        apply_event(out, event_type, data, pending_cards)

    def do_stop():
        """点击"停止"：置位停止信号；执行循环会在下一个检查点优雅退出。"""
        if not running["v"]:
            return
        stop_event.set()
        stop_btn.config(state="disabled")
        lamp.config(fg="#e0a458")     # 橙灯：正在请求停止
        status.config(text="停止中…")

    def send():
        if running["v"]:
            return
        goal = entry.get("1.0", "end").strip()
        if not goal:
            return
        entry.delete("1.0", "end")
        append("▸ " + goal, "user")
        running["v"] = True
        stop_event.clear()            # 每次新任务重置停止信号
        send_btn.config(state="disabled")
        stop_btn.config(state="normal")   # 运行期间可停止
        lamp.config(fg="#f0c674")     # 黄灯：运行中
        status.config(text="运行中…")
        threading.Thread(target=worker, args=(goal,), daemon=True).start()

    def worker(goal: str):
        try:
            from agent import Agent, AgentConfig
            from agent.memory import Memory
            from tools import ToolManager
            agent = Agent(
                tool_manager=ToolManager(),
                memory=Memory(),
                config=AgentConfig(verbose=False, stream_enabled=False),
            )
            final = agent.run(
                goal,
                event_sink=lambda et, data: events.put((et, data)),
                stop_event=stop_event,
            )
            events.put(("answer", {"output": final}))
        except Exception as e:
            events.put(("error", {"error": str(e)}))
        finally:
            ended = "stopped" if stop_event.is_set() else "completed"
            events.put(("run_end", {"status": ended}))

    def poll():
        try:
            while True:
                et, data = events.get_nowait()
                if et == "error":
                    append("✗ " + str(data.get("error"))[:500], "err")
                    status.config(text="出错")
                    lamp.config(fg="#eb5757")
                    running["v"] = False
                    send_btn.config(state="normal")
                    stop_btn.config(state="disabled")
                elif et == "run_end":
                    ended = data.get("status", "completed")
                    status.config(text="已停止" if ended == "stopped" else "完成")
                    lamp.config(fg="#6fcf97")
                    running["v"] = False
                    send_btn.config(state="normal")
                    stop_btn.config(state="disabled")
                else:
                    render(et, data)
        except queue.Empty:
            pass
        root.after(100, poll)

    def on_key(event):
        if event.state & 0x4 and event.keysym == "Return":   # Ctrl+Enter
            send()
            return "break"

    entry.bind("<KeyPress>", on_key)
    send_btn.config(command=send)

    root.after(100, poll)
    root.mainloop()
