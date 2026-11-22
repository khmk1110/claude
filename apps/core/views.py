from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from apps.accounts import stepup
from apps.attendance import leave
from apps.attendance import services as attendance
from apps.attendance.models import AttendanceDay, AttendanceMonthLock, PaidLeaveRequest
from apps.core.months import month_end, month_start, prev_month
from apps.members.models import Member
from apps.shifts.models import ShiftEntry
from apps.shifts.services import members_for_month


def healthz(request):
    """死活監視用（ログイン不要。中身は返さない）。"""
    from django.db import connection
    from django.http import HttpResponse

    with connection.cursor() as c:
        c.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


def _step(title, desc, url, button, done, started=False):
    return {"title": title, "desc": desc, "url": url, "button": button, "done": done, "started": started}


def monthly_flow(month):
    """今月の業務の流れ（シフト → 勤怠確定 → 経費確認 → 営業件数 → 給与 → 明細・バックアップ）の進み具合。

    金額は含めない（件数・状態のみ）ので、追加パスワードなしで表示してよい。
    """
    from apps.expenses.models import ExpenseRecord, SourceFile
    from apps.payroll.models import PayrollStatement, SalesCount

    pm = prev_month(month)
    members = list(members_for_month(month))
    payroll_members = [m for m in members if m.employment_type in ("contractor", "intern")]
    m_param = f"?month={month:%Y-%m}"

    started = ShiftEntry.objects.filter(member__in=members, date__gte=month, date__lte=month_end(month)).values("member").distinct().count()
    steps = [_step("今月のシフトを作る", f"{started} / {len(members)}人 入力済み", reverse("shifts:grid") + m_param, "シフトを開く",
                   bool(members) and started >= len(members), started > 0)]

    locked = AttendanceMonthLock.objects.filter(month=pm).exists()
    pending_days = AttendanceDay.objects.filter(date__gte=pm, date__lte=month_end(pm), status=AttendanceDay.Status.PLANNED).count()
    steps.append(_step(f"{pm.month}月の勤怠を確定する（給与の「前月出勤日数」になります）",
                       "確定済み" if locked else f"未確定の日 {pending_days}件", reverse("attendance:month") + f"?month={pm:%Y-%m}",
                       "勤怠を開く", locked, pending_days == 0))

    files = SourceFile.objects.filter(target_month=month).count()
    open_recs = ExpenseRecord.objects.filter(status__in=["imported", "unchecked", "pending", "modified"]).filter(
        auto_target_month=month).count()
    steps.append(_step("ガソリン代・交通費などの資料を取り込んで承認する",
                       f"資料 {files}件・確認待ち {open_recs}件" if files else "まだ資料がありません",
                       reverse("expenses:index") + m_param, "経費・資料を開く", files > 0 and open_recs == 0, files > 0))

    conf = SalesCount.objects.filter(month=month, kind="confirmed", member__in=payroll_members).values("member").distinct().count()
    steps.append(_step("営業件数を入力する（前月の獲得件数・今月の入金確定件数）",
                       f"入金確定件数 {conf} / {len(payroll_members)}人 入力済み", reverse("payroll:sales") + m_param,
                       "営業件数を入力", bool(payroll_members) and conf >= len(payroll_members), conf > 0))

    sts = list(PayrollStatement.objects.filter(month=month).values_list("status", flat=True))
    done = sum(1 for s in sts if s == "confirmed")
    steps.append(_step("給与を計算して確定する", f"確定 {done} / {len(sts)}人" if sts else "まだ給与データがありません",
                       reverse("payroll:index") + m_param, "給与を開く", bool(sts) and done == len(sts), bool(sts)))

    steps.append(_step("明細PDFを出して、バックアップを保存する", "全員分の明細PDF・月1回のバックアップ",
                       reverse("payroll:index") + m_param, "明細を出す", False, bool(sts) and done == len(sts)))

    current = next((i for i, s in enumerate(steps) if not s["done"]), None)
    for i, s in enumerate(steps):
        s["no"] = i + 1
        s["state"] = "done" if s["done"] else ("doing" if i == current else "todo")
    return steps


@login_required
def dashboard(request):
    from apps.payroll.models import PayrollStatement

    today = timezone.localdate()
    month = month_start(today)
    members = list(members_for_month(month))
    sums = attendance.month_summaries(members, month)
    under = []
    for m in members:
        std = m.standard_days_for(month)
        if std is not None and sums[m.pk].planned_days < std:
            under.append((m, sums[m.pk].planned_days, std, std - sums[m.pk].planned_days))
    today_work = ShiftEntry.objects.filter(date=today, member__in=members, work_type__attendance_days__gt=0).count()
    needs_check = (
        AttendanceDay.objects.filter(date__lt=today, date__gte=today - timedelta(days=62), status=AttendanceDay.Status.PLANNED)
        .values("member").distinct().count()
    )
    notices = []
    review = PayrollStatement.objects.filter(needs_review=True).count()
    if review:
        notices.append(("確定済みの給与のうち、あとから件数や経費が変わったものが " + str(review) + "件 あります。", reverse("payroll:index")))
    pending_leave = PaidLeaveRequest.objects.filter(status=PaidLeaveRequest.Status.REQUESTED).count()
    if pending_leave:
        notices.append((f"有給の申請が {pending_leave}件 承認待ちです。", reverse("attendance:leave_list")))
    for m in Member.objects.filter(paid_leave_target=True, status=Member.Status.ACTIVE):
        st = leave.leave_status(m, today)
        o = st.obligation
        if st.missing_statutory:
            notices.append((f"{m.name}さんの有給の付与が未登録です（付与日を過ぎた分が {len(st.missing_statutory)}件）。",
                            reverse("attendance:leave_member", args=[m.pk])))
        elif o and not o.get("achieved") and o.get("shortfall", 0) > 0:
            notices.append((f"{m.name}さんは年5日の有給取得まで あと{o['shortfall'].normalize()}日の予定が必要です（期限 {o['deadline']:%Y/%m/%d}）。",
                            reverse("attendance:leave_member", args=[m.pk])))
    if under:
        notices.append((f"出勤基準に届いていない人が {len(under)}人 います（今月のシフトの予定）。", reverse("shifts:grid")))

    payroll = None
    if stepup.is_verified(request, "payroll", touch=False):
        sts = list(PayrollStatement.objects.filter(month=month))
        payroll = {"net": sum(s.net_amount or 0 for s in sts), "count": len(sts)}
    return render(request, "core/dashboard.html", {
        "month": month, "today": today, "flow": monthly_flow(month), "notices": notices, "under": under,
        "member_count": len(members), "today_work": today_work, "needs_check": needs_check, "payroll": payroll,
        "attendance_actual_total": sum((s.confirmed_attendance_days for s in sums.values()), Decimal("0")),
    })
