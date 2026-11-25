"""計算のもとになるデータが変わったら、その月のまだ確定していない給与に「計算し直しが必要」の印を付ける。

印の付いた給与は、給与の画面を開いた時に自動で計算し直す（「全員を計算し直す」を押し忘れて古い金額のまま確定する、
というミスをなくすため）。チームリーダーボーナスのように他の人の件数で決まる項目もあるので、人ではなく月ごとに印を付ける。
"""

from __future__ import annotations

from datetime import date

from django.db.models.signals import m2m_changed, post_delete, post_save


def _month(d) -> date | None:
    if d is None:
        return None
    return date(d.year, d.month, 1)


def _next(m: date) -> date:
    return date(m.year + (m.month == 12), m.month % 12 + 1, 1)


def mark_stale(*months) -> None:
    from .models import PayrollStatement

    months = {m for m in months if m}
    qs = PayrollStatement.objects.exclude(status=PayrollStatement.Status.CONFIRMED).filter(stale=False)
    if months:
        qs = qs.filter(month__in=months)
    qs.update(stale=True)


def _sales(sender, instance, **kw):
    m = _month(instance.month)
    mark_stale(m, _next(m))  # 入金確定はその月、獲得は翌月の単価に使う


def _expense(sender, instance, **kw):
    mark_stale(_month(instance.target_month or instance.auto_target_month))


def _attendance(sender, instance, **kw):
    m = _month(instance.date)
    mark_stale(m, _next(m))  # 当月（インターンリーダー手当）・翌月（前月出勤日数）


def _settings(sender, instance=None, **kw):
    mark_stale()  # 単価表・給与項目・チームの変更は、確定前の全部の月


def connect():
    from apps.attendance.models import AttendanceDay
    from apps.expenses.models import ExpenseRecord

    from .models import PayrollItemDefinition, RateTable, SalesCount, Team, TeamMembership

    for model, fn in ((SalesCount, _sales), (ExpenseRecord, _expense), (AttendanceDay, _attendance),
                      (RateTable, _settings), (PayrollItemDefinition, _settings), (Team, _settings), (TeamMembership, _settings)):
        post_save.connect(fn, sender=model, dispatch_uid=f"stale-save-{model.__name__}")
        post_delete.connect(fn, sender=model, dispatch_uid=f"stale-del-{model.__name__}")
    for field in ("members", "excluded_members"):
        m2m_changed.connect(_settings, sender=getattr(PayrollItemDefinition, field).through,
                            dispatch_uid=f"stale-m2m-{field}")
