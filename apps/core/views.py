from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from apps.accounts import stepup
from apps.attendance import leave
from apps.attendance import services as attendance
from apps.attendance.models import AttendanceDay, PaidLeaveRequest
from apps.core.months import month_end, month_start
from apps.members.models import Member
from apps.shifts.models import ShiftEntry
from apps.shifts.services import members_for_month


@login_required
def dashboard(request):
    today = timezone.localdate()
    month = month_start(today)
    members = list(members_for_month(month))
    days_count = month_end(month).day
    entries = ShiftEntry.objects.filter(date__gte=month, date__lte=month_end(month), member__in=members)
    filled = entries.count()
    sums = attendance.month_summaries(members, month)
    under = []
    for m in members:
        std = m.standard_days_for(month)
        if std is not None and sums[m.pk].planned_days < std:
            under.append((m, sums[m.pk].planned_days, std, std - sums[m.pk].planned_days))
    today_work = (
        ShiftEntry.objects.filter(date=today, member__in=members, work_type__attendance_days__gt=0)
        .select_related("member")
        .count()
    )
    needs_check = (
        AttendanceDay.objects.filter(date__lt=today, date__gte=today - timedelta(days=62),
                                     status=AttendanceDay.Status.PLANNED)
        .values("member").distinct().count()
    )
    leave_alerts = []
    for m in Member.objects.filter(paid_leave_target=True, status=Member.Status.ACTIVE):
        st = leave.leave_status(m, today)
        o = st.obligation
        if (o and not o.get("achieved") and o.get("shortfall") > 0) or st.expiring_soon or st.missing_statutory:
            leave_alerts.append((m, st))
    ctx = {
        "month": month,
        "member_count": len(members),
        "shift_filled": filled,
        "shift_total": len(members) * days_count,
        "shift_members_started": entries.values("member").distinct().count(),
        "today_work": today_work,
        "under": under,
        "needs_check": needs_check,
        "pending_leave": PaidLeaveRequest.objects.filter(status=PaidLeaveRequest.Status.REQUESTED).count(),
        "leave_alerts": leave_alerts,
        "attendance_actual_total": sum((s.confirmed_attendance_days for s in sums.values()), Decimal("0")),
        # 給与・経費の金額は、それぞれ追加認証済みの場合のみ表示（STEP 4・5 で中身を追加）
        "payroll_verified": stepup.is_verified(request, "payroll", touch=False),
        "expenses_verified": stepup.is_verified(request, "expenses", touch=False),
    }
    return render(request, "core/dashboard.html", ctx)
