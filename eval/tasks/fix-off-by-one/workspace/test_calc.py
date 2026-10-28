from calc import sum_to


def test_sum_to_five():
    assert sum_to(5) == 15


def test_sum_to_zero():
    assert sum_to(0) == 0
