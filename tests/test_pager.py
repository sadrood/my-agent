"""终端分页器：不依赖方向键；非 TTY 时退回一次性打印（不改变脚本行为）。"""
from agent.pager import page


class _Keys:
    def __init__(self, keys):
        self.keys = list(keys)

    def __call__(self):
        return self.keys.pop(0) if self.keys else "q"


def _run(lines, keys, height=3):
    out = []
    rc = page(lines, print_fn=out.append, key_reader=_Keys(keys),
              height=height, interactive=True)
    return rc, out


def test_non_interactive_prints_all_without_reading_keys():
    out = []
    assert page(["a", "b", "c"], print_fn=out.append, interactive=False) is True
    assert out == ["a", "b", "c"]


def test_space_pages_through_and_finishes():
    rc, out = _run([f"L{i}" for i in range(7)], [" ", " "])
    assert rc is True
    assert [x for x in out if x.startswith("L")] == [f"L{i}" for i in range(7)]
    assert sum(1 for x in out if x.startswith("--")) == 2   # 每页后一处提示，最后一页不提示


def test_q_stops_early():
    rc, out = _run([f"L{i}" for i in range(10)], ["q"])
    assert rc is False
    assert sum(1 for x in out if x.startswith("L")) == 3    # 只放了第一页
    assert any("分页退出" in x for x in out)


def test_enter_shows_one_more_line():
    rc, out = _run([f"L{i}" for i in range(6)], ["\r", "q"])
    assert [x for x in out if x.startswith("L")] == ["L0", "L1", "L2", "L3"]
    assert rc is False


def test_other_keys_are_ignored():
    rc, out = _run([f"L{i}" for i in range(4)], ["x", " ", " "])
    assert rc is True
    assert [x for x in out if x.startswith("L")] == ["L0", "L1", "L2", "L3"]


def test_empty_input_is_noop():
    assert page([], print_fn=lambda s: None, interactive=True) is True
