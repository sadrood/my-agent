"""终端单选器：↑/↓ 移动、输入过滤、回车确认、Esc 取消；非 TTY 返回 None。"""
import io

from agent.picker import select


def _run(items, keys, height=12):
    from rich.console import Console
    it = iter(keys)
    out = io.StringIO()
    con = Console(file=out, force_terminal=True, width=80, color_system=None)
    idx = select(items, key_reader=lambda: next(it), console=con,
                 interactive=True, height=height)
    return idx, out.getvalue()


def test_enter_selects_first():
    assert _run(["a", "b"], ["enter"])[0] == 0


def test_down_moves_and_selects():
    assert _run(["a", "b", "c"], ["down", "down", "enter"])[0] == 2


def test_up_stops_at_first_and_down_at_last():
    assert _run(["a", "b"], ["up", "enter"])[0] == 0
    assert _run(["a"], ["down", "enter"])[0] == 0


def test_home_end_and_paging():
    items = [f"item{i}" for i in range(10)]
    assert _run(items, ["end", "enter"])[0] == 9
    assert _run(items, ["pgdn", "enter"], height=3)[0] == 3
    assert _run(items, ["end", "home", "enter"])[0] == 0


def test_typing_filters_and_enter_returns_original_index():
    items = ["alpha", "beta", "gamma"]
    assert _run(items, ["b", "enter"])[0] == 1        # "beta"
    assert _run(items, ["g", "a", "enter"])[0] == 2   # "gamma"


def test_filter_without_match_returns_none():
    assert _run(["alpha"], ["z", "enter"])[0] is None


def test_backspace_restores_list():
    assert _run(["alpha", "beta"], ["b", "backspace", "enter"])[0] == 0


def test_cancel_returns_none():
    assert _run(["a", "b"], ["cancel"])[0] is None


def test_eof_returns_none():
    def boom():
        raise EOFError

    from rich.console import Console
    con = Console(file=io.StringIO(), force_terminal=True, width=80)
    assert select(["a"], key_reader=boom, console=con, interactive=True) is None


def test_non_interactive_or_empty_returns_none():
    assert select(["a"], interactive=False) is None
    assert select([], interactive=True) is None


def test_renders_marker_and_footer():
    _, text = _run(["aaa", "bbb"], ["enter"])
    assert "❯ aaa" in text
    assert "回车打开" in text


def test_long_line_is_not_wrapped_into_multiple_rows():
    """长标题必须被裁成一行：折行会让高亮跨行（之前就是这个毛病）。"""
    long = "很长的标题" * 40
    _, text = _run([long], ["enter"], height=3)
    rendered = [ln for ln in text.splitlines() if "很长的标题" in ln]
    assert rendered, text
    assert all(len(ln) <= 120 for ln in rendered)      # 没被折成很多行


def test_scroll_window_follows_cursor():
    items = [f"item{i}" for i in range(10)]
    idx, text = _run(items, ["down"] * 9 + ["enter"], height=3)
    assert idx == 9
    assert "❯ item9" in text
