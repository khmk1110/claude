"""勤怠の同期・確定・集計。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction

from apps.core import audit
from apps.core.months import month_end, month_start
from apps.shifts.models import ShiftEntry, WorkType

from .models import AttendanceDay, AttendanceMonthLock


class MonthLockedError(Exception):
    pass


def is_locked(d: date) -> bool:
    return AttendanceMonthLock.objects.filter(month=month_start(d)).exists()


def ensure_unlocked(d: date) -> None:
    if is_locked(d):
        raise MonthLockedError(f"{d:%Y年%m月}の勤怠は確定済みのため変更できません。")


def sync_day(member, d: date) -> AttendanceDay | None:
    """シフト → 勤怠予定。確定・修正済みの実績は上書きしない。月次確定済みの月は変更しない。"""
    if is_locked(d):
        return None
    entry = ShiftEntry.objects.filter(member=member, date=d).select_related("work_type").first()
    planned = entry.work_type if entry else None
    day = AttendanceDay.objects.filter(member=member, date=d).first()
    if day is None:
        if planned is None:
            return None
        return AttendanceDay.objects.create(member=member, date=d, planned_work_type=planned)
    if day.planned_work_type_id != (planned.pk if planned else None):
        day.planned_work_type = planned
        if day.status == AttendanceDay.Status.PLANNED and planned is None:
            day.delete()
            return None
        day.save(update_fields=["planned_work_type", "updated_at"])
    return day


def _snapshot(day: AttendanceDay) -> dict:
    return {
        "actual": day.actual_work_type.name if day.actual_work_type else None,
        "status": day.get_status_display(),
        "absence": day.is_absence,
        "late": day.is_late,
        "early_leave": day.is_early_leave,
        "note": day.note,
    }


@transaction.atomic
def update_actual(member, d: date, *, work_type: WorkType | None, is_absence=False, is_late=False,
                  is_early_leave=False, note="", user=None, reason="") -> AttendanceDay:
    ensure_unlocked(d)
    day, _ = AttendanceDay.objects.get_or_create(member=member, date=d)
    before = _snapshot(day)
    day.actual_work_type = work_type
    day.is_absence, day.is_late, day.is_early_leave, day.note = is_absence, is_late, is_early_leave, note
    same_as_plan = (
        work_type is not None and work_type.pk == day.planned_work_type_id
        and not (is_absence or is_late or is_early_leave)
    )
    day.status = AttendanceDay.Status.CONFIRMED if same_as_plan else AttendanceDay.Status.MODIFIED
    day.updated_by = user
    day.save()
    audit.log("attendance.update", target=day, before=before, after=_snapshot(day), reason=reason, user=user)
    return day


@transaction.atomic
def confirm_days(member, dates, *, user=None) -> int:
    """予定どおりの実績として確定する（未確定の日のみ）。"""
    count = 0
    for d in dates:
        ensure_unlocked(d)
        day = AttendanceDay.objects.filter(member=member, date=d, status=AttendanceDay.Status.PLANNED).first()
        if day is None:
            continue
        day.actual_work_type = day.planned_work_type
        day.status = AttendanceDay.Status.CONFIRMED
        day.updated_by = user
        day.save()
        count += 1
    if count:
        audit.log("attendance.confirm", target=member, after={"days": [str(d) for d in dates], "count": count}, user=user)
    return count


@transaction.atomic
def lock_month(month: date, *, user=None) -> AttendanceMonthLock:
    """月次確定。未確定の日は予定どおりの実績として確定してからロックする。"""
    month = month_start(month)
    ensure_unlocked(month)
    pending = AttendanceDay.objects.filter(
        date__gte=month, date__lte=month_end(month), status=AttendanceDay.Status.PLANNED
    )
    n = 0
    for day in pending:
        day.actual_work_type = day.planned_work_type
        day.status = AttendanceDay.Status.CONFIRMED
        day.updated_by = user
        day.save()
        n += 1
    lock = AttendanceMonthLock.objects.create(month=month, locked_by=user)
    audit.log("attendance.lock_month", target=lock, after={"auto_confirmed_days": n}, user=user)
    return lock


@transaction.atomic
def unlock_month(month: date, *, user=None, reason: str) -> None:
    if not reason:
        raise ValueError("確定解除の理由を入力してください。")
    lock = AttendanceMonthLock.objects.get(month=month_start(month))
    audit.log("attendance.unlock_month", target=lock, reason=reason, user=user)
    lock.delete()


@dataclass
class MonthSummary:
    planned_days: Decimal = Decimal("0")
    planned_paid_leave: Decimal = Decimal("0")
    attendance_days: Decimal = Decimal("0")  # 実出勤日数（未確定日は予定を採用）
    paid_leave_days: Decimal = Decimal("0")
    confirmed_attendance_days: Decimal = Decimal("0")
    unconfirmed: int = 0
    differences: int = 0
    absence: int = 0
    late: int = 0
    early_leave: int = 0
    days: dict = field(default_factory=dict)


def month_summaries(members, month: date) -> dict[int, MonthSummary]:
    month = month_start(month)
    result = {m.pk: MonthSummary() for m in members}
    qs = AttendanceDay.objects.filter(
        member__in=list(result), date__gte=month, date__lte=month_end(month)
    ).select_related("planned_work_type", "actual_work_type")
    for day in qs:
        s = result[day.member_id]
        s.days[day.date] = day
        if day.planned_work_type:
            s.planned_days += day.planned_work_type.attendance_days
            s.planned_paid_leave += day.planned_work_type.paid_leave_days
        eff = day.effective_work_type
        if eff:
            s.attendance_days += eff.attendance_days
            s.paid_leave_days += eff.paid_leave_days
        if day.status == AttendanceDay.Status.PLANNED:
            s.unconfirmed += 1
        elif eff:
            s.confirmed_attendance_days += eff.attendance_days
        if day.differs_from_plan:
            s.differences += 1
        s.absence += day.is_absence
        s.late += day.is_late
        s.early_leave += day.is_early_leave
    return result


def actual_attendance_days(member, month: date) -> tuple[Decimal | None, str]:
    """給与計算用の前月出勤日数。勤怠データがなければ None（手入力が必要）。"""
    month = month_start(month)
    exists = AttendanceDay.objects.filter(member=member, date__gte=month, date__lte=month_end(month)).exists()
    if not exists:
        return None, ""
    s = month_summaries([member], month)[member.pk]
    locked = is_locked(month)
    source = f"勤怠 {month:%Y年%m月}（{'月次確定済み' if locked else f'未確定 {s.unconfirmed}日を含む'}）"
    return s.attendance_days, source

