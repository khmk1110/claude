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


def _step(when, title, desc, url, button, done, started=False, extra=None):
    return {"when": when, "title": title, "desc": desc, "url": url, "button": button, "done": done, "started": started,
            "extra": extra or []}


def monthly_flow(month):
    """毎月の流れ（給与管理マニュアルの順番）の進み具合。金額は含めない（件数・状態のみ）。

    1日: その月のシフトが届く → 入力
    25日頃: 前月の出勤日数・獲得件数でインセンティブ単価を確認 → 入金確定件数×単価で基本給
            → ガソリン代・交通費 → その他の経費・立替・手当・当月だけの項目 → 確定・明細PDF
    """
    from apps.expenses.models import ExpenseRecord, SourceFile
    from apps.payroll.models import PayrollStatement, SalesCount

    pm = prev_month(month)
    members = list(members_for_month(month))
    payroll_members = [m for m in members if m.employment_type in ("contractor", "intern")]
    q, pq = f"?month={month:%Y-%m}", f"?month={pm:%Y-%m}"

    started = ShiftEntry.objects.filter(member__in=members, date__gte=month, date__lte=month_end(month)).values("member").distinct().count()
    steps = [_step("1日", f"{month.month}月のシフトを入力する", f"LINEで届いた数字を入力（{started} / {len(members)}人 入力済み）",
                   reverse("shifts:numbers") + q, "シフトを入力", bool(members) and started >= len(members), started > 0,
                   [("前月のシフトをコピー", reverse("shifts:grid") + q)])]

    locked = AttendanceMonthLock.objects.filter(month=pm).exists()
    acq = SalesCount.objects.filter(month=pm, kind="acquired", member__in=payroll_members).values("member").distinct().count()
    steps.append(_step("25日頃", "前月の出勤日数・獲得件数を確認して、インセンティブ単価を確認する",
                       f"{pm.month}月の勤怠 {'確定済み' if locked else '未確定'}・{pm.month}月の獲得件数 {acq} / {len(payroll_members)}人",
                       reverse("payroll:sales") + pq, f"{pm.month}月の獲得件数を入力",
                       locked and bool(payroll_members) and acq >= len(payroll_members), locked or acq > 0,
                       [(f"{pm.month}月の勤怠を確定", reverse("attendance:month") + pq)]))

    conf = SalesCount.objects.filter(month=month, kind="confirmed", member__in=payroll_members, product__isnull=True).values("member").distinct().count()
    sts = list(PayrollStatement.objects.filter(month=month).values_list("status", flat=True))
    calculated = sum(1 for s in sts if s in ("calculated", "review", "confirmed"))
    steps.append(_step("25日頃", "入金確定件数を入力して、基本給（件数×単価）を計算する",
                       f"入金確定件数 {conf} / {len(payroll_members)}人・計算済み {calculated} / {len(sts)}人",
                       reverse("payroll:sales") + q, "入金確定件数を入力",
                       bool(sts) and calculated == len(sts) and conf >= len(payroll_members), conf > 0,
                       [("給与を計算する", reverse("payroll:index") + q)]))

    files = SourceFile.objects.filter(target_month=month).exclude(file_format="manual").count()
    open_recs = ExpenseRecord.objects.filter(status__in=["imported", "unchecked", "pending", "modified"], auto_target_month=month).count()
    steps.append(_step("25日頃", "ガソリン代・交通費の資料を取り込んで、金額を確認する",
                       f"資料 {files}件・確認待ち {open_recs}件" if files else "ENEOSの請求書・交通費の請求書を取り込みます",
                       reverse("expenses:index") + q, "資料を取り込む", files > 0 and open_recs == 0, files > 0))

    reviewed = sum(1 for s in sts if s in ("review", "confirmed"))
    steps.append(_step("25日頃", "その他の経費・立替・手当・その月だけの項目を確認する",
                       "LINEで届いた経費、チームボーナス・リーダー手当、都市ガス・プロパンなど" + (f"（確認済み {reviewed} / {len(sts)}人）" if sts else ""),
                       reverse("payroll:index") + q, "給与を確認する", bool(sts) and reviewed == len(sts), reviewed > 0,
                       [("LINEの経費を入力", reverse("expenses:index") + q)]))

    done = sum(1 for s in sts if s == "confirmed")
    steps.append(_step("月末まで", "給与を確定して、明細PDFを発行する", f"確定 {done} / {len(sts)}人" if sts else "まだ給与データがありません",
                       reverse("payroll:index") + q, "確定・明細PDF", bool(sts) and done == len(sts), done > 0,
                       [("バックアップを保存", reverse("setup"))]))

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

    payroll = None
    if stepup.is_verified(request, "payroll", touch=False):
        sts = list(PayrollStatement.objects.filter(month=month))
        payroll = {"net": sum(s.net_amount or 0 for s in sts), "count": len(sts)}
    return render(request, "core/dashboard.html", {
        "month": month, "today": today, "flow": monthly_flow(month), "notices": notices, "under": under,
        "member_count": len(members), "today_work": today_work, "needs_check": needs_check, "payroll": payroll,
        "attendance_actual_total": sum((s.confirmed_attendance_days for s in sums.values()), Decimal("0")),
    })
