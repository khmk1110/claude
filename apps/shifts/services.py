"""シフトの入力・集計。変更は必ずここを通し、監査ログと勤怠予定の同期を行う。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from apps.attendance import services as attendance
from apps.core import audit
from apps.core.months import days_in_month, month_end, month_start, prev_month
from apps.members.models import Member

from .models import ShiftEntry, WorkType


def members_for_month(month: date, employment_type: str | None = None):
    """その月に在籍していたメンバー（退職者も退職月までは表示）。"""
    month = month_start(month)
    qs = Member.objects.filter(
        Q(status=Member.Status.ACTIVE) | Q(retired_on__gte=month)
    ).filter(Q(joined_on__isnull=True) | Q(joined_on__lte=month_end(month)))
    if employment_type:
        qs = qs.filter(employment_type=employment_type)
    return qs.order_by("display_order", "id")


def _label(wt: WorkType | None) -> str | None:
    return wt.name if wt else None


@transaction.atomic
def set_shifts(members, dates, work_type: WorkType | None, *, user=None, reason: str = "") -> int:
    """指定したメンバー×日付に勤務区分を設定する（None は未定に戻す）。変更があった件数を返す。"""
    changed = 0
    members = list(members)
    dates = list(dates)
    for m in {month_start(d) for d in dates}:
        attendance.ensure_unlocked(m)  # 勤怠を確定した月のシフトは変更できない（シフト＝勤怠のため）
    existing = {
        (e.member_id, e.date): e
        for e in ShiftEntry.objects.select_for_update()
        .filter(member__in=members, date__in=list(dates))
        .select_related("work_type")
    }
    for member in members:
        for d in dates:
            entry = existing.get((member.pk, d))
            before = _label(entry.work_type) if entry else None
            after = _label(work_type)
            if before == after:
                continue
            if work_type is None:
                entry.delete()
            elif entry is None:
                ShiftEntry.objects.create(member=member, date=d, work_type=work_type, updated_by=user)
            else:
                entry.work_type = work_type
                entry.updated_by = user
                entry.save(update_fields=["work_type", "updated_by", "updated_at"])
            audit.log(
                "shift.update",
                target_type="shift",
                target_id=f"{member.pk}:{d.isoformat()}",
                target_repr=f"{member} {d:%Y-%m-%d}",
                before={"work_type": before},
                after={"work_type": after},
                reason=reason,
                user=user,
            )
            attendance.sync_day(member, d)
            changed += 1
    return changed


@transaction.atomic
def copy_previous_month(month: date, *, user=None, overwrite: bool = False) -> int:
    """前月の同じ日付のシフトを当月にコピーする。既定では未入力の日だけ埋める。"""
    month = month_start(month)
    src = prev_month(month)
    last = month_end(month).day
    current = {(e.member_id, e.date) for e in ShiftEntry.objects.filter(date__gte=month, date__lte=month_end(month))}
    active_ids = set(members_for_month(month).values_list("pk", flat=True))
    changed = 0
    by_type: dict[tuple[int, int], list[date]] = {}
    for e in ShiftEntry.objects.filter(date__gte=src, date__lte=month_end(src)):
        if e.member_id not in active_ids or e.date.day > last:
            continue
        target = month.replace(day=e.date.day)
        if not overwrite and (e.member_id, target) in current:
            continue
        by_type.setdefault((e.member_id, e.work_type_id), []).append(target)
    members = {m.pk: m for m in Member.objects.filter(pk__in={k[0] for k in by_type})}
    types = {w.pk: w for w in WorkType.objects.filter(pk__in={k[1] for k in by_type})}
    for (mid, wid), dates in by_type.items():
        changed += set_shifts([members[mid]], dates, types[wid], user=user, reason="前月のシフトをコピー")
    audit.log("shift.copy_previous_month", target_type="shift_month", target_id=month.isoformat(),
              target_repr=f"{month:%Y年%m月}", after={"changed": changed, "overwrite": overwrite}, user=user)
    return changed


@dataclass
class Row:
    member: Member
    cells: list  # [(date, WorkType|None)]
    planned_days: Decimal
    paid_leave_days: Decimal
    standard: Decimal | None

    @property
    def diff(self):
        return None if self.standard is None else self.planned_days - self.standard


def month_grid(month: date, employment_type: str | None = None) -> list[Row]:
    month = month_start(month)
    members = list(members_for_month(month, employment_type).prefetch_related("monthly_standards"))
    days = days_in_month(month)
    entries = {
        (e.member_id, e.date): e.work_type
        for e in ShiftEntry.objects.filter(member__in=members, date__gte=month, date__lte=month_end(month))
        .select_related("work_type")
    }
    rows = []
    for m in members:
        cells = [(d, entries.get((m.pk, d))) for d in days]
        planned = sum((wt.attendance_days for _, wt in cells if wt), Decimal("0"))
        paid = sum((wt.paid_leave_days for _, wt in cells if wt), Decimal("0"))
        rows.append(Row(m, cells, planned, paid, m.standard_days_for(month)))
    return rows


@transaction.atomic
def reorder_members(ordered_ids: list[int], *, user=None) -> None:
    before = list(Member.objects.order_by("display_order", "id").values_list("pk", flat=True))
    for i, pk in enumerate(ordered_ids, start=1):
        Member.objects.filter(pk=pk).update(display_order=i * 10)
    audit.log("member.reorder", target_type="member", before=before, after=ordered_ids, user=user)
