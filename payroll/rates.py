"""個人事業主の単価表（システム固定）。

単価は「前月獲得件数」と「当月出勤日数」の組み合わせで決まる。
この表は担当者が画面から変更できないよう、コード上で固定している。
変更が必要な場合は、このファイルを修正し RATE_TABLE_VERSION を更新すること。
"""

RATE_TABLE_VERSION = "2026-09"

# 列: 前月獲得件数の区分 (下限, 上限 or None, 表示ラベル)
COUNT_BANDS = [
    (0, 39, "～39件"),
    (40, 49, "40～49件"),
    (50, 59, "50～59件"),
    (60, 79, "60～79件"),
    (80, 99, "80～99件"),
    (100, 119, "100～119件"),
    (120, None, "120件～"),
]

# 行: 当月出勤日数の区分 (下限, 上限, 表示ラベル)
DAY_BANDS = [
    (0, 10, "～10日"),
    (11, 15, "11～15日"),
    (16, 18, "16～18日"),
    (19, 21, "19～21日"),
    (22, 24, "22～24日"),
    (25, 27, "25～27日"),
    (28, 31, "28～31日"),
]

# RATES[行=出勤日数区分][列=前月件数区分]
RATES = [
    [5000, 5250, 5750, 6000, 6250, 6500, 7000],
    [5250, 5500, 6000, 6250, 6500, 6750, 7250],
    [5500, 5750, 6250, 6500, 6750, 7000, 7500],
    [5750, 6000, 6500, 6750, 7000, 7250, 7750],
    [6000, 6250, 6750, 7000, 7250, 7500, 8000],
    [6250, 6500, 7000, 7250, 7500, 7750, 8250],
    [6500, 6750, 7250, 7500, 7750, 8000, 8500],
]

MAX_WORK_DAYS = 31


def count_band_index(prev_count):
    if prev_count is None or prev_count < 0:
        raise ValueError("前月獲得件数が不正です")
    for i, (lo, hi, _) in enumerate(COUNT_BANDS):
        if prev_count >= lo and (hi is None or prev_count <= hi):
            return i
    raise ValueError("前月獲得件数が単価表の範囲外です")


def day_band_index(work_days):
    if work_days is None or work_days < 0 or work_days > MAX_WORK_DAYS:
        raise ValueError("出勤日数は0～31日で入力してください")
    for i, (lo, hi, _) in enumerate(DAY_BANDS):
        if lo <= work_days <= hi:
            return i
    raise ValueError("出勤日数が単価表の範囲外です")


def lookup_rate(prev_count, work_days):
    """前月獲得件数 + 当月出勤日数 → 当月単価（円）。

    注意: ここで使う件数は「前月」の獲得件数。報酬計算に使う「当月」件数とは別物。
    """
    return RATES[day_band_index(work_days)][count_band_index(prev_count)]
