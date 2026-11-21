"""曜日・祝日の表示用。休み扱いの自動判定には使わない。"""

from datetime import date
from functools import lru_cache

import holidays

WEEKDAYS = "月火水木金土日"


@lru_cache(maxsize=16)
def _jp_holidays(year: int):
    return holidays.country_holidays("JP", years=year, language="ja")


def holiday_name(d: date) -> str:
    return _jp_holidays(d.year).get(d, "") or ""


def weekday_label(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def day_class(d: date) -> str:
    if holiday_name(d) or d.weekday() == 6:
        return "sun"
    if d.weekday() == 5:
        return "sat"
    return ""
