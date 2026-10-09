"""粘贴感知的多行输入读取器（借鉴主流 CLI 的输入体验）。
否则残余行会卡在 Python 的输入缓冲区里被吞掉）；POSIX 用 select + readline。"""
import codecs
import os
import sys
import time

# 排干上限（防异常输入导致失控）
MAX_DRAIN_LINES = 400
MAX_DRAIN_CHARS = 20000


def _write_prompt(text: str) -> None:
    """只显示提示符（不读取输入）——读取由 _StdinReader 独占。"""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


def _split_lines(text: str) -> list:
    """把原始字符流按 CR/LF 拆行（兼容 CRLF / LF / 孤立 CR）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


# ============================================================
# 平台相关：stdin 待读检测与排干
# ============================================================

def _pending_posix(stdin, timeout: float = 0.05) -> bool:
    """POSIX：select 探测 stdin 是否还有待读字节。"""
    import select
    try:
        r, _, _ = select.select([stdin], [], [], timeout)
    except (ValueError, OSError):
        return False
    return bool(r)


def _drain_posix(stdin) -> list:
    """POSIX：逐行排干（canonical 模式下 read 一次最多一行，不会过度读取）。"""
    # 必须用 os.read 取「当前可读的字节」，不能用 readline()：管道输入（`echo x | my-agent`）的半行数据会让 select 报可读、而 readline() 一直等到换行或 EOF —— CLI 看起来像卡死。
    lines, total = [], 0
    fd = stdin.fileno()
    # ↑ 跨块增量解码：以前对每个 chunk 单独 decode，汉字/emoji 被 4096 字节边界切断就会解出半截字节
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while _pending_posix(stdin, 0.02):
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        text = decoder.decode(chunk)               # ↑ 未完成的多字节序列留到下一块继续
        if text:
            lines.extend(text.splitlines() or [""])
            total += len(text)
        if len(lines) >= MAX_DRAIN_LINES or total >= MAX_DRAIN_CHARS:
            break
    tail = decoder.decode(b"", True)               # ↑ 收尾：冲出解码器里残留的半截序列
    if tail:
        lines.append(tail)
    return lines


class _StdinReader:
    """POSIX 唯一的 stdin 读取者：共享缓冲 + 跨块增量解码。

    ↑ 一个 fd 只能有一个读取者：以前「readline 式 input()」（rich → sys.stdin 文本层）
      与「os.read 字节排干」同时读同一个 stdin，谁先取走字节，另一个的解码器就会撞上
      残缺的多字节序列（UnicodeDecodeError: invalid continuation byte），异常从 rich 里
      冒出来把整个进程打挂，残留粘贴内容随后被 shell 逐行执行。
    """

    def __init__(self, stdin=None):
        self.stdin = stdin if stdin is not None else sys.stdin
        self.fd = self.stdin.fileno()
        # ↑ 解码器与缓冲都是这一份：提示符行与粘贴排干共用，不再有第二个读取者
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.buffer = ""

    def pending(self, timeout: float = 0.05) -> bool:
        """缓冲里已有整行，或 fd 上还有待读字节。"""
        if "\n" in self.buffer:
            return True
        return _pending_posix(self.stdin, timeout)

    def _fill(self, timeout=None) -> bool:
        """读一块字节并增量解码进缓冲。timeout=None 表示阻塞读（等用户回车）。"""
        if timeout is not None and not self.pending(timeout):
            return False
        try:
            chunk = os.read(self.fd, 4096)
        except OSError:
            return False
        if not chunk:
            self.buffer += self.decoder.decode(b"", True)   # ↑ EOF：补 final，冲出未完成序列
            return False
        self.buffer += self.decoder.decode(chunk)           # ↑ 增量解码，绝不逐块独立 decode
        return True

    def read_line(self, timeout=None):
        """读一行；EOF 返回 None。阻塞读时 Ctrl+C 直接抛 KeyboardInterrupt（保持可中断）。"""
        while "\n" not in self.buffer:
            if not self._fill(timeout):
                if self.buffer:
                    line, self.buffer = self.buffer, ""
                    return line
                return None
        line, self.buffer = self.buffer.split("\n", 1)
        return line.rstrip("\r")

    def drain_lines(self, max_lines: int = MAX_DRAIN_LINES,
                    max_chars: int = MAX_DRAIN_CHARS) -> list:
        """把当前可读的剩余内容按行收齐（粘贴的后继行）；半行留在缓冲里等下一轮。"""
        lines, total = [], 0
        while "\n" in self.buffer or self.pending(0.02):
            if "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                line = line.rstrip("\r")
                lines.append(line)
                total += len(line)
            elif not self._fill(0.02):
                break
            if len(lines) >= max_lines or total >= max_chars:
                break
        return lines


def _drain_windows() -> list:
    """Windows：逐字符从控制台输入队列取剩余粘贴内容。"""
    try:
        import ctypes
        libc = ctypes.CDLL("msvcrt")

        def getch() -> str:
            # 不设 restype：_getwch 返回 wint_t，按 int 取回再转字符。（曾设 restype=c_wchar → ctypes 已返回 str，再 chr(str) 崩溃）
            value = libc._getwch()
            if value in (-1, 0xFFFF):      # WEOF / 读取错误 → 视为 EOF
                return "\x1a"
            try:
                return chr(value)
            except (ValueError, OverflowError):
                return "\x1a"
    except Exception:
        # 退化路径：msvcrt.getwch（带系统回显，CR 不会转换，视觉略差但内容完整）
        import msvcrt

        def getch() -> str:
            return msvcrt.getwch()

    try:
        import msvcrt
        kbhit = msvcrt.kbhit
    except ImportError:
        return []

    chars = []
    try:
        while kbhit() and len(chars) < MAX_DRAIN_CHARS:
            ch = getch()
            if ch == "\x03":      # Ctrl+C：保持可中断语义
                raise KeyboardInterrupt
            if ch == "\x1a":      # Ctrl+Z：视为 EOF
                break
            chars.append(ch)
            # 行数上限：POSIX 路径一直有这个保护，Windows 侧之前漏了
            if chars.count("\n") + chars.count("\r") >= MAX_DRAIN_LINES:
                break
            # 手工回显：把回车换成换行，粘贴内容在屏幕上按行呈现
            try:
                sys.stdout.write("\n" if ch == "\r" else ch)
            except Exception:
                pass
    except KeyboardInterrupt:
        raise
    except Exception:
        # 排干失败绝不能拖垮主循环：放弃收尾，已收集部分保留
        pass
    try:
        sys.stdout.flush()
    except Exception:
        pass
    return _split_lines("".join(chars))


def _drain_with_settle(drain_once, pending_fn, settle_delay: float = 0.05,
                       max_rounds: int = 6) -> list:
    """排干 + 短等待复检：粘贴可能分多个突发写入，等一小会儿确认收完。
    （需要按原始字符流累积才能彻底消除，属低频场景）。"""
    out = []
    for _ in range(max_rounds):
        if pending_fn():
            out.extend(drain_once())
            time.sleep(settle_delay)   # 等下一个突发到达
            continue
        break
    else:
        if pending_fn():
            try:
                sys.stderr.write(
                    "\n[输入] 粘贴内容过大，已达到单次读入上限；剩余内容仍在终端"
                    "队列里，会被当成下一条消息。建议把长文本存成文件后用 file 工具读取。\n")
                sys.stderr.flush()
            except Exception:
                pass
    return out


# ============================================================
# 纯逻辑：续行合并
# ============================================================

def resolve_continuation(raw_lines: list) -> list:
    r"""把以 ``\`` 结尾的行与其后续行合并（``\``+Enter 续行约定）。"""
    blocks, buf = [], []
    for line in raw_lines:
        stripped = line.rstrip()
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            buf.append(stripped[:-1].rstrip())
            continue
        buf.append(line)
        blocks.append("\n".join(buf))
        buf = []
    if buf:
        blocks.append("\n".join(buf))
    return blocks


# ============================================================
# 主入口：读取一条目标输入
# ============================================================

def _read_goal_posix(stdin, prompt_primary, prompt_continuation) -> str:
    """POSIX 默认路径：提示符只负责**显示**，读取全部走同一个字节读取者（_StdinReader）。"""
    reader = _StdinReader(stdin)
    blocks = []
    while True:
        try:
            (prompt_primary if not blocks else prompt_continuation)()
            line = reader.read_line()          # ↑ 唯一的读取入口；Ctrl+C 直接抛 KeyboardInterrupt
        except (EOFError, KeyboardInterrupt):
            return None
        if line is None:
            return None

        if line == "" and not blocks:
            # 空行：若此刻粘贴剩余内容刚到（粘贴以空行开头），先收下再定
            if reader.pending():
                extra = reader.drain_lines()
                if extra:
                    blocks.append(line)
                    blocks.extend(extra)
                    break
            return ""

        if line.rstrip().endswith("\\") and not line.rstrip().endswith("\\\\"):
            blocks.append(line.rstrip()[:-1].rstrip())
            continue
        blocks.append(line)
        break

    # 主行已结束：把粘贴突发的剩余行一次性收齐（人工打字不会在几十毫秒内完成下一行）
    try:
        is_tty = stdin.isatty()
    except Exception:
        is_tty = True
    if is_tty and reader.pending():
        blocks.extend(reader.drain_lines())

    return "\n".join(blocks).strip()


def read_goal(prompt_primary=None, prompt_continuation=None,
              stdin=None, pending_fn=None, drain_fn=None) -> str:
    """读取一条目标输入（交互模式）。

    提示符期间开**输出闸门**：别的线程的输出先排队，回车后按原顺序放出来，
    避免把用户正在敲的那一行覆盖掉。
    """
    from agent import output_gate
    output_gate.begin_prompt()
    try:
        return _read_goal_inner(prompt_primary, prompt_continuation, stdin,
                                pending_fn, drain_fn)
    finally:
        output_gate.end_prompt()
        _flush_deferred()


def _flush_deferred() -> None:
    """把闸门里排队的输出按顺序真正打印（不在提示符状态时调用）。"""
    from agent import output_gate
    try:
        from agent.ui_theme import _console, _legacy_console
    except Exception:                                # noqa: BLE001
        return
    items = output_gate.drain()
    if not items:
        return
    for text, args, kwargs in items:
        console = _console or _legacy_console
        if console is None:
            try:
                sys.stdout.write(text + "\n")
            except Exception:                        # noqa: BLE001
                pass
            continue
        try:
            console.print(*(args or (text,)), **(kwargs or {}))
        except Exception:                            # noqa: BLE001
            pass


def _read_goal_inner(prompt_primary=None, prompt_continuation=None,
                     stdin=None, pending_fn=None, drain_fn=None) -> str:
    stdin = stdin if stdin is not None else sys.stdin

    # ↑ POSIX 默认路径统一成单一字节读取者；显式注入 pending_fn/drain_fn（测试、嵌入方）
    #   时保持原有流程，避免破坏既有调用约定。
    if sys.platform != "win32" and pending_fn is None and drain_fn is None:
        if prompt_primary is None:
            prompt_primary = lambda: _write_prompt("> ")
        if prompt_continuation is None:
            prompt_continuation = lambda: _write_prompt("… ")
        return _read_goal_posix(stdin, prompt_primary, prompt_continuation)

    if prompt_primary is None:
        prompt_primary = lambda: input("> ")
    if prompt_continuation is None:
        prompt_continuation = lambda: input("… ")

    if pending_fn is None:
        if sys.platform == "win32":
            def pending_fn() -> bool:
                try:
                    import msvcrt
                    return bool(msvcrt.kbhit())
                except (ImportError, OSError):
                    return False
        else:
            def pending_fn() -> bool:
                return _pending_posix(stdin)

    if drain_fn is None:
        def drain_fn() -> list:
            if sys.platform == "win32":
                return _drain_with_settle(_drain_windows, pending_fn)
            return _drain_with_settle(lambda: _drain_posix(stdin), pending_fn)

    blocks = []
    while True:
        try:
            line = prompt_primary() if not blocks else prompt_continuation()
        except (EOFError, KeyboardInterrupt):
            return None
        # ↑ 提示符回调可能只负责"显示"（返回 None）：这时当成本次没有输入，别拿 None 去 rstrip
        if line is None:
            return None

        if line == "" and not blocks:
            # 空行：若此刻粘贴剩余内容刚到（粘贴以空行开头），先收下再定
            if pending_fn():
                extra = drain_fn()
                if extra:
                    blocks.append(line)
                    blocks.extend(extra)
                    break
            return ""

        if line.rstrip().endswith("\\") and not line.rstrip().endswith("\\\\"):
            blocks.append(line.rstrip()[:-1].rstrip())
            continue
        blocks.append(line)
        break

    # 主行已结束：把粘贴突发的剩余行一次性收齐（人工打字不会在几十毫秒内完成下一行）
    try:
        is_tty = stdin.isatty()
    except Exception:
        is_tty = True
    if is_tty and pending_fn():
        blocks.extend(drain_fn())

    return "\n".join(blocks).strip()
