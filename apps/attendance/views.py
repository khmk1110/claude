from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.calendar_jp import day_class, holiday_name, weekday_label
from apps.core.monthnav import month_context, month_from_request
from apps.core.months import days_in_month
from apps.members.models import EmploymentType, Member
from apps.shifts.models import WorkType
from apps.shifts.services import members_for_month

from . import leave, services
from .models import AttendanceMonthLock, PaidLeaveAdjustment, PaidLeaveGrant, PaidLeaveRequest


@login_required
def month_view(request):
    month = month_from_request(request)
    emp = request.GET.get("type", "")
    members = list(members_for_month(month, emp if emp in EmploymentType.values else None))
    sums = services.month_summaries(members, month)
    rows = []
    for m in members:
        s = sums[m.pk]
        std = m.standard_days_for(month)
        rows.append({"member": m, "s": s, "standard": std, "diff": None if std is None else s.attendance_days - std})
    return render(
        request,
        "attendance/month.html",
        {
            **month_context(month),
            "rows": rows,
            "lock": AttendanceMonthLock.objects.filter(month=month).first(),
            "emp": emp,
            "emp_choices": EmploymentType.choices,
        },
    )


@login_required
def member_view(request, member_id):
    month = month_from_request(request)
    member = get_object_or_404(Member, pk=member_id)
    s = services.month_summaries([member], month)[member.pk]
    days = []
    for d in days_in_month(month):
        days.append({"date": d, "wd": weekday_label(d), "cls": day_class(d), "holiday": holiday_name(d), "a": s.days.get(d)})
    return render(
        request,
        "attendance/member.html",
        {
            **month_context(month),
            "member": member,
            "s": s,
            "days": days,
            "standard": member.standard_days_for(month),
            "locked": services.is_locked(month),
            "work_types": WorkType.objects.filter(is_active=True),
            "today": timezone.localdate(),
        },
    )


@login_required
@require_POST
def member_update(request, member_id):
    member = get_object_or_404(Member, pk=member_id)
    month = month_from_request(request)
    back = redirect(f"/attendance/{member.pk}/?month={month:%Y-%m}")
    try:
        if request.POST.get("action") == "confirm":
            dates = [date.fromisoformat(x) for x in request.POST.getlist("dates")]
            n = services.confirm_days(member, dates, user=request.user)
            messages.success(request, f"{n}日分を予定どおりの実績として確定しました。")
        else:
            d = date.fromisoformat(request.POST["date"])
            code = request.POST.get("actual") or ""
            wt = WorkType.objects.get(code=code) if code else None
            services.update_actual(
                member, d, work_type=wt,
                is_absence=bool(request.POST.get("absence")), is_late=bool(request.POST.get("late")),
                is_early_leave=bool(request.POST.get("early")), note=request.POST.get("note", "")[:255],
                user=request.user, reason=request.POST.get("reason", "")[:255],
            )
            messages.success(request, f"{d:%m/%d} の実績を保存しました。")
    except services.MonthLockedError as e:
        messages.error(request, str(e))
    except (ValueError, KeyError, WorkType.DoesNotExist):
        messages.error(request, "入力内容が正しくありません。")
    return back


@login_required
@require_POST
def lock_toggle(request):
    month = month_from_request(request)
    try:
        if request.POST.get("action") == "unlock":
            services.unlock_month(month, user=request.user, reason=request.POST.get("reason", "").strip())
            messages.success(request, f"{month:%Y年%m月}の勤怠の確定を解除しました。")
        else:
            services.lock_month(month, user=request.user)
            messages.success(request, f"{month:%Y年%m月}の勤怠を確定しました。")
    except (ValueError, services.MonthLockedError) as e:
        messages.error(request, str(e))
    nxt = request.POST.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(f"/attendance/?month={month:%Y-%m}")


# --- 有給 ---------------------------------------------------------------
@login_required
def leave_list(request):
    today = timezone.localdate()
    rows = [
        {"member": m, "st": leave.leave_status(m, today)}
        for m in Member.objects.filter(paid_leave_target=True, status=Member.Status.ACTIVE)
    ]
    pending = PaidLeaveRequest.objects.filter(status=PaidLeaveRequest.Status.REQUESTED).select_related("member")
    return render(request, "attendance/leave_list.html", {"rows": rows, "pending": pending, "today": today})


@login_required
def leave_member(request, member_id):
    member = get_object_or_404(Member, pk=member_id)
    today = timezone.localdate()
    return render(
        request,
        "attendance/leave_member.html",
        {
            "member": member,
            "st": leave.leave_status(member, today),
            "requests": member.paid_leave_requests.order_by("-date")[:50],
            "adjustments": member.paid_leave_adjustments.order_by("-date"),
            "today": today,
        },
    )


def _dec(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        raise ValueError


@login_required
@require_POST
def leave_action(request, member_id):
    member = get_object_or_404(Member, pk=member_id)
    action = request.POST.get("action")
    p = request.POST
    try:
        with transaction.atomic():
            if action == "statutory":
                n = leave.create_statutory_grants(member, user=request.user)
                messages.success(request, f"法定付与を{n}件作成しました。出勤率8割以上を満たしているか確認してください。")
            elif action == "grant":
                g = date.fromisoformat(p["granted_on"])
                days = _dec(p["days"])
                if days <= 0:
                    raise ValueError
                PaidLeaveGrant.objects.create(member=member, granted_on=g, days=days,
                                              expires_on=leave.expiry_for(g), is_statutory=False, note=p.get("note", "")[:255])
                audit.log("leave.grant_create", target=member, after={"granted_on": g, "days": days}, user=request.user)
                messages.success(request, "付与を登録しました。")
            elif action == "adjust":
                days, reason = _dec(p["days"]), p.get("reason", "").strip()
                if not reason:
                    messages.error(request, "修正理由を入力してください。")
                else:
                    PaidLeaveAdjustment.objects.create(member=member, date=date.fromisoformat(p["date"]), days=days,
                                                       reason=reason[:255], created_by=request.user)
                    audit.log("leave.adjust", target=member, after={"days": days}, reason=reason, user=request.user)
                    messages.success(request, "手動修正を登録しました。")
            elif action == "request":
                days = _dec(p["days"])
                if days not in (Decimal("1"), Decimal("0.5")):
                    raise ValueError
                req = PaidLeaveRequest.objects.create(member=member, date=date.fromisoformat(p["date"]), days=days,
                                                      note=p.get("note", "")[:255])
                audit.log("leave.request", target=member, after={"date": req.date, "days": days}, user=request.user)
                messages.success(request, "有給の申請を登録しました（承認するまで取得済みには数えません）。")
            elif action in ("approve", "reject"):
                req = get_object_or_404(PaidLeaveRequest, pk=p.get("request_id"), member=member)
                leave.decide_request(req, approve=action == "approve", user=request.user)
                messages.success(request, "承認し、シフトに有給を入れました。" if action == "approve" else "却下しました。")
            else:
                raise ValueError
    except IntegrityError:
        messages.error(request, "同じ日付のデータがすでにあります。")
    except (ValueError, KeyError) as e:
        messages.error(request, str(e) if str(e) else "入力内容が正しくありません。")
    return redirect("attendance:leave_member", member_id=member.pk)
