"""対象年月の扱い。年月は「その月の1日」の date で表す。"""

import calendar
from datetime import date

from django.core.exceptions import ValidationError


def month_start(d: date) -> date:
    return d.replace(day=1)


def validate_month_start(value: date) -> None:
    if value is not None and value.day != 1:
        raise ValidationError("対象年月は月の1日で指定してください。")


def prev_month(m: date) -> date:
    m = month_start(m)
    return date(m.year - 1, 12, 1) if m.month == 1 else date(m.year, m.month - 1, 1)


def next_month(m: date) -> date:
    m = month_start(m)
    return date(m.year + 1, 1, 1) if m.month == 12 else date(m.year, m.month + 1, 1)


def month_end(m: date) -> date:
    return date(m.year, m.month, calendar.monthrange(m.year, m.month)[1])


def days_in_month(m: date) -> list[date]:
    return [date(m.year, m.month, d) for d in range(1, month_end(m).day + 1)]


def parse_month(text: str) -> date:
    """'2026-10' / '2026/10' / '202610' を受け付ける。"""
    t = text.strip().replace("/", "-")
    if "-" in t:
        y, mo = t.split("-")[:2]
    else:
        y, mo = t[:4], t[4:6]
    return date(int(y), int(mo), 1)
