import pytest

from payroll import rates


def test_spec_example():
    # 前月61件 + 当月出勤20日 → 6,750円
    assert rates.lookup_rate(61, 20) == 6750


@pytest.mark.parametrize("prev,days,expected", [
    (0, 0, 5000), (39, 10, 5000), (40, 10, 5250), (49, 11, 5500), (50, 15, 6000),
    (59, 16, 6250), (60, 18, 6500), (79, 19, 6750), (80, 21, 7000), (99, 22, 7250),
    (100, 24, 7500), (119, 25, 7750), (120, 27, 8250), (500, 28, 8500), (120, 31, 8500),
    (39, 31, 6500), (0, 11, 5250),
])
def test_boundaries(prev, days, expected):
    assert rates.lookup_rate(prev, days) == expected


def test_table_is_complete_and_monotonic():
    assert len(rates.RATES) == len(rates.DAY_BANDS)
    for row in rates.RATES:
        assert len(row) == len(rates.COUNT_BANDS)
        assert row == sorted(row)
    for col in zip(*rates.RATES):
        assert list(col) == sorted(col)


@pytest.mark.parametrize("prev,days", [(-1, 10), (10, -1), (10, 32), (None, 10), (10, None)])
def test_invalid(prev, days):
    with pytest.raises(ValueError):
        rates.lookup_rate(prev, days)
