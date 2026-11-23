from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
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
                   reverse("shifts:grid") + q, "シフト表を開く", bool(members) and started >= len(members), started > 0,
                   [("LINEの数字でまとめて入力", reverse("shifts:numbers") + q)])]

    locked = AttendanceMonthLock.objects.filter(month=pm).exists()
    acq = SalesCount.objects.filter(month=pm, kind="acquired", member__in=payroll_members).values("member").distinct().count()
    conf = SalesCount.objects.filter(month=month, kind="confirmed", member__in=payroll_members).values("member").distinct().count()
    open_recs = ExpenseRecord.objects.filter(status__in=["imported", "unchecked", "pending", "modified"], auto_target_month=month).count()
    files = SourceFile.objects.filter(target_month=month).count()
    ready = locked and bool(payroll_members) and acq >= len(payroll_members) and conf >= len(payroll_members) and files > 0 and open_recs == 0
    steps.append(_step("25日頃", "件数と経費を取り込む",
                       f"{pm.month}月の勤怠 {'確定' if locked else '未確定'}・件数 {min(acq, conf)}/{len(payroll_members)}人・経費の確認待ち {open_recs}件",
                       reverse("payroll:index") + q, "給与を開く", ready, locked or acq > 0 or conf > 0 or files > 0,
                       [(f"{pm.month}月の勤怠を確定", reverse("attendance:month") + pq)]))

    sts = list(PayrollStatement.objects.filter(month=month).values_list("status", flat=True))
    done = sum(1 for s in sts if s == "confirmed")
    steps.append(_step("月末まで", "明細を確認して確定する", f"確定 {done}/{len(sts)}人" if sts else "まだ計算していません",
                       reverse("payroll:review") + q, "1人ずつ確認", bool(sts) and done == len(sts), done > 0,
                       [("バックアップを保存", reverse("setup"))]))

    current = next((i for i, s in enumerate(steps) if not s["done"]), None)
    for i, s in enumerate(steps):
        s["no"] = i + 1
        s["state"] = "done" if s["done"] else ("doing" if i == current else "todo")
    return steps


def dashboard(request):
    if not request.user.is_authenticated:
        return redirect("shifts:grid")  # ログインしていない人には、まずシフト表（見るだけ）を出す
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


def csrf_failure(request, reason=""):
    """セキュリティ確認（CSRF）で止まったとき。古い画面から送信した場合がほとんどなので、画面を開き直してもらう。

    ログイン画面なら自動で開き直す。それ以外は分かりやすい説明と「開き直す」ボタンを出す。
    """
    import logging

    from django.contrib import messages as dj_messages
    from django.shortcuts import redirect

    logging.getLogger("django.security.csrf").warning("CSRF failure: %s %s", request.path, reason)
    if request.path.startswith("/login/"):
        dj_messages.info(request, "画面の有効期限が切れていたため、開き直しました。もう一度ログインしてください。")
        nxt = request.POST.get("next") or request.GET.get("next") or ""
        return redirect("/login/" + (f"?next={nxt}" if nxt.startswith("/") and not nxt.startswith("//") else ""))
    return render(request, "core/csrf_failure.html", {"back": request.path}, status=403)
