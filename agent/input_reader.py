"""
粘贴感知的多行输入读取器（借鉴主流 CLI 的输入体验）。

主流 agent CLI 都是全屏文本框：
- 粘贴经终端 bracketed-paste 协议整块进入输入缓冲区，回车一次提交整块；
- 手工换行用 Shift+Enter（部分 CLI 也支持行尾 ``\\`` + Enter）；
- Web GUI 用 textarea，粘贴天然保留换行。

my_agent 是行式 stdio CLI（rich Prompt 单行读取），没有全屏 TUI。
等价近似方案（本模块）：
1. 主行读取结束后立即检查 stdin 是否还有待读内容——粘贴是一整块突发到达，
   人工逐行输入则行与行之间隔着按键延迟——有待读内容则全部收下合并成一条。
   Windows 用无回显宽字符 _getwch 逐字符直取控制台队列（绕过 TextIOWrapper，
   否则残余行会卡在 Python 的输入缓冲区里被吞掉）；POSIX 用 select + readline。
2. 行尾 ``\\`` 表示续行（主流 CLI 同款约定），续行提示符 ``…``。
3. 非 TTY（管道/CI）不做排干，保持"逐行一条消息"的脚本行为。
"""
import sys
import time

# 排干上限（防异常输入导致失控）
MAX_DRAIN_LINES = 400
MAX_DRAIN_CHARS = 20000


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
    lines, total = [], 0
    while _pending_posix(stdin, 0.02):
        line = stdin.readline()
        if line == "":
            break
        lines.append(line.rstrip("\r\n"))
        total += len(line)
        if len(lines) >= MAX_DRAIN_LINES or total >= MAX_DRAIN_CHARS:
            break
    return lines


def _drain_windows() -> list:
    """Windows：逐字符从控制台输入队列取剩余粘贴内容。

    必须绕过 TextIOWrapper：input() 走 C 运行时 ReadConsoleW，而
    sys.stdin.readline() 走 FileIO/TextIOWrapper，两者缓冲相互独立——
    若后者一次性把队列读空，残余行会卡在它的缓冲区里"消失"。
    这里用无回显宽字符 _getwch 直取队列，再手工回显（CR 转成换行）。
    """
    try:
        import ctypes
        libc = ctypes.CDLL("msvcrt")

        def getch() -> str:
            # 不设 restype：_getwch 返回 wint_t，按 int 取回再转字符。
            # （曾设 restype=c_wchar → ctypes 已返回 str，再 chr(str) 崩溃）
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
                       max_rounds: int = 3) -> list:
    """排干 + 短等待复检：粘贴可能分多个突发写入，等一小会儿确认收完。

    正常单行输入（无待读内容）时第一次检查即返回，不引入额外延迟。
    """
    out = []
    for _ in range(max_rounds):
        if pending_fn():
            out.extend(drain_once())
            time.sleep(settle_delay)   # 等下一个突发到达
            continue
        break
    return out


# ============================================================
# 纯逻辑：续行合并
# ============================================================

def resolve_continuation(raw_lines: list) -> list:
    """把以 ``\\`` 结尾的行与其后续行合并（``\\``+Enter 续行约定）。

    规则：
    - 行尾是 ``\\`` 且不是 ``\\\\``（转义）→ 去掉续行符，与下一行合并；
    - 其余行原样保留。
    返回合并后的段落列表。
    """
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

def read_goal(prompt_primary=None, prompt_continuation=None,
              stdin=None, pending_fn=None, drain_fn=None) -> str:
    """
    读取一条目标输入（交互模式）。

    Args:
        prompt_primary: 无参数可调用 → 主提示符下输入的一行 str
        prompt_continuation: 无参数可调用 → 续行提示符下输入的一行 str
        stdin: 输入流（默认 sys.stdin；测试注入用）
        pending_fn: 无参数可调用 → bool（stdin 是否还有待读内容；测试注入用）
        drain_fn: 无参数可调用 → list[str]（排干剩余输入；测试注入用）

    Returns:
        合并后的输入文本（两端空白已去除，内部换行保留）；
        EOF / Ctrl+C 时返回 None。
    """
    stdin = stdin if stdin is not None else sys.stdin
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
