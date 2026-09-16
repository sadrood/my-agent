"""用户优先（yield to user）判定逻辑测试：Agent 控制键鼠时用户可随时抢回。"""
from tools.computer_use import should_yield_to_user


def test_no_input_recently_never_yields():
    # 最近没有任何输入（0 = 未知）→ 不让路
    assert should_yield_to_user(0, 0, 10000) is False
    # 最后一次输入在 5 秒前 → 用户没在操作
    assert should_yield_to_user(4000, 5000, 10000) is False


def test_user_input_after_ours_yields():
    # 我们在 9000ms 注入过；9500ms 又有输入，而我们的注入已过阈值 → 是用户在动 → 让路
    assert should_yield_to_user(9000, 9900, 10000) is True


def test_our_own_injection_does_not_yield():
    # 刚注入完 100ms：这次"最后输入"就是我们自己 → 不让路
    assert should_yield_to_user(9900, 9900, 10000) is False


def test_tick_wraparound_handled():
    # 32 位 tick 回绕：last_input 接近 2^32，now 很小 → 实际只过了 1200ms
    now = 1000
    last_input = (2 ** 32) - 200
    last_synthetic = now - 2000           # 我们的注入已是 2 秒前
    assert should_yield_to_user(last_synthetic, last_input, now, threshold_ms=1300) is True
    assert should_yield_to_user(last_synthetic, last_input, now, threshold_ms=1000) is False
