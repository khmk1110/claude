"""有給休暇の管理（正社員）。

会社独自の出勤基準とは別に、労働基準法に沿った付与・期限・年5日取得義務を確認する。
- 取得済み: 勤怠が確定（または修正済み）している有給の日
- 取得予定: シフトに有給が入っていて、勤怠が未確定の日
- 申請中  : PaidLeaveRequest が申請中のもの（承認するまで残日数から引かない）
残日数は古い付与分から先に消化する（先入れ先出し）。付与から2年で時効。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core import audit

from .models import AttendanceDay, PaidLeaveAdjustment, PaidLeaveGrant, PaidLeaveRequest

# 通常の労働者（週5日以上または週30時間以上）の法定付与日数: (入社からの月数, 日数)
STATUTORY_SCHEDULE = [(6, 10), (18, 11), (30, 12), (42, 14), (54, 16), (66, 18), (78, 20)]
OBLIGATION_MIN_GRANT = Decimal("10")  # 10日以上付与された人が年5日取得義務の対象
OBLIGATION_DAYS = Decimal("5")


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    import calendar

    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def statutory_grants(joined_on: date, until: date) -> list[tuple[date, Decimal]]:
    """入社日から until までの法定付与（出勤率8割以上を前提。実際の付与前に確認すること）。"""
    out = []
    for months, days in STATUTORY_SCHEDULE:
        g = add_months(joined_on, months)
        if g > until:
            return out
        out.append((g, Decimal(days)))
    months = STATUTORY_SCHEDULE[-1][0]
    while True:
        months += 12
        g = add_months(joined_on, months)
        if g > until:
            return out
        out.append((g, Decimal(20)))


def expiry_for(granted_on: date) -> date:
    return add_months(granted_on, 24) - timedelta(days=1)


def next_statutory_grant(joined_on: date | None, today: date) -> tuple[date, Decimal] | None:
    if not joined_on:
        return None
    for g, d in statutory_grants(joined_on, add_months(today, 13)):
        if g > today:
            return g, d
    return None


@transaction.atomic
def create_statutory_grants(member, *, until: date | None = None, user=None) -> int:
    """入社日から今日までの法定付与のうち、未登録のものを作成する。"""
    until = until or timezone.localdate()
    if not member.joined_on:
        raise ValueError("入社日が登録されていません。")
    n = 0
    for g, days in statutory_grants(member.joined_on, until):
        obj, created = PaidLeaveGrant.objects.get_or_create(
            member=member, granted_on=g, defaults={"days": days, "expires_on": expiry_for(g), "is_statutory": True}
        )
        if created:
            n += 1
            audit.log("leave.grant_create", target=member, after={"granted_on": g, "days": days}, user=user)
    return n


def leave_days(member):
    """有給が含まれる勤怠日を (日付, 日数, 確定済みか) で返す。"""
    qs = (
        AttendanceDay.objects.filter(member=member)
        .select_related("planned_work_type", "actual_work_type")
        .order_by("date")
    )
    out = []
    for day in qs:
        wt = day.effective_work_type
        if wt and wt.paid_leave_days > 0:
            out.append((day.date, wt.paid_leave_days, day.status != AttendanceDay.Status.PLANNED))
    return out


@dataclass
class GrantStatus:
    grant: PaidLeaveGrant
    used: Decimal = Decimal("0")

    @property
    def remaining(self) -> Decimal:
        return self.grant.days - self.used


@dataclass
class LeaveStatus:
    granted_total: Decimal = Decimal("0")
    taken: Decimal = Decimal("0")
    planned: Decimal = Decimal("0")
    requested: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")
    remaining_after_planned: Decimal = Decimal("0")
    expiring_soon: list = field(default_factory=list)  # [(期限, 日数)]
    next_grant: tuple | None = None
    obligation: dict | None = None
    grants: list = field(default_factory=list)
    history: list = field(default_factory=list)
    shortage: Decimal = Decimal("0")
    missing_statutory: list = field(default_factory=list)  # 付与日を過ぎているのに未登録の法定付与


def _allocate(grants: list[GrantStatus], d: date, days: Decimal) -> Decimal:
    left = days
    for gs in grants:
        if left <= 0:
            break
        if gs.grant.granted_on <= d <= gs.grant.expires_on and gs.remaining > 0:
            take = min(gs.remaining, left)
            gs.used += take
            left -= take
    return left  # 割り当てできなかった日数（残日数不足）


def leave_status(member, as_of: date | None = None) -> LeaveStatus:
    as_of = as_of or timezone.localdate()
    st = LeaveStatus()
    grants = [GrantStatus(g) for g in member.paid_leave_grants.order_by("granted_on")]
    days = leave_days(member)
    for d, n, confirmed in days:
        if confirmed and d <= as_of:
            st.shortage += _allocate(grants, d, n)
            st.taken += n
        else:
            st.planned += n
        if confirmed and d <= as_of:
            label = "取得済み"
        elif d <= as_of:
            label = "取得予定（勤怠が未確定）"
        else:
            label = "取得予定"
        st.history.append((d, n, label))
    adjustments = sum(
        (a.days for a in PaidLeaveAdjustment.objects.filter(member=member, date__lte=as_of)), Decimal("0")
    )
    valid = [gs for gs in grants if gs.grant.granted_on <= as_of <= gs.grant.expires_on]
    st.granted_total = sum((gs.grant.days for gs in grants), Decimal("0"))
    st.remaining = sum((gs.remaining for gs in valid), Decimal("0")) + adjustments - st.shortage
    st.remaining_after_planned = st.remaining - st.planned
    st.requested = sum(
        (r.days for r in PaidLeaveRequest.objects.filter(member=member, status=PaidLeaveRequest.Status.REQUESTED)),
        Decimal("0"),
    )
    st.expiring_soon = [
        (gs.grant.expires_on, gs.remaining)
        for gs in valid
        if gs.remaining > 0 and gs.grant.expires_on <= add_months(as_of, 3)
    ]
    st.grants = grants
    st.next_grant = next_statutory_grant(member.joined_on, as_of)
    if member.joined_on:
        registered = {gs.grant.granted_on for gs in grants}
        st.missing_statutory = [
            (g, d) for g, d in statutory_grants(member.joined_on, as_of) if g not in registered
        ]

    # 年5日取得義務: 直近の「10日以上付与」から1年間
    target = [gs.grant for gs in grants if gs.grant.days >= OBLIGATION_MIN_GRANT and gs.grant.granted_on <= as_of]
    if target:
        g = target[-1]
        end = add_months(g.granted_on, 12) - timedelta(days=1)
        taken = sum((n for d, n, c in days if c and g.granted_on <= d <= min(end, as_of)), Decimal("0"))
        planned = sum((n for d, n, c in days if not c and g.granted_on <= d <= end), Decimal("0"))
        st.obligation = {
            "start": g.granted_on,
            "deadline": end,
            "taken": taken,
            "planned": planned,
            "achieved": taken >= OBLIGATION_DAYS,
            "shortfall": max(OBLIGATION_DAYS - taken - planned, Decimal("0")),
        }
    return st


@transaction.atomic
def decide_request(req: PaidLeaveRequest, *, approve: bool, user=None) -> None:
    """承認するとシフトに有給（半日なら『半日稼働＋半日有給』）を入れる。"""
    from apps.shifts import services as shifts
    from apps.shifts.models import WorkType

    if req.status != PaidLeaveRequest.Status.REQUESTED:
        raise ValueError("申請中のものだけ承認・却下できます。")
    req.status = PaidLeaveRequest.Status.APPROVED if approve else PaidLeaveRequest.Status.REJECTED
    req.decided_at = timezone.now()
    req.decided_by = user
    req.save()
    if approve:
        wt = WorkType.objects.get(code="paid" if req.days == 1 else "half_paid")
        shifts.set_shifts([req.member], [req.date], wt, user=user, reason="有給申請の承認")
    audit.log("leave.approve" if approve else "leave.reject", target=req.member,
              after={"date": req.date, "days": req.days}, user=user)
