import json
from datetime import date

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.calendar_jp import day_class, holiday_name, weekday_label
from apps.core.models import AuditLog
from apps.core.monthnav import month_context, month_from_request
from apps.core.months import days_in_month, month_end, month_start
from apps.members.models import EmploymentType, Member

from . import services
from .image import render_png
from .models import WorkType


def _emp_filter(request):
    t = request.GET.get("type") or ""
    return t if t in EmploymentType.values else ""


@login_required
def grid(request):
    from django.utils import timezone

    month = month_from_request(request)
    emp = _emp_filter(request)
    view = request.GET.get("view", "table")
    today = timezone.localdate()
    rows = services.month_grid(month, emp or None)
    days = []
    for i, d in enumerate(days_in_month(month)):
        working = []
        for r in rows:
            wt = r.cells[i][1]
            if wt and (wt.attendance_days > 0 or wt.paid_leave_days > 0):
                working.append((r.member, wt))
        count = sum(1 for _, wt in working if wt.attendance_days > 0)  # 出勤する人数（半日も1人）
        days.append({"date": d, "iso": d.isoformat(), "wd": weekday_label(d), "cls": day_class(d), "holiday": holiday_name(d),
                     "today": d == today, "working": working, "count": count})
    for r in rows:  # セルごとに土日祝・今日の色分け用のクラスを付ける
        r.view_cells = [(d, wt, days[i]["cls"] + (" today" if days[i]["today"] else "")) for i, (d, wt) in enumerate(r.cells)]
    return render(
        request,
        "shifts/grid.html" if view != "day" else "shifts/day.html",
        {
            **month_context(month),
            "rows": rows,
            "days": days,
            "work_types": WorkType.objects.filter(is_active=True),
            "emp": emp,
            "emp_choices": EmploymentType.choices,
            "view": view,
        },
    )


@login_required
@require_POST
def api_set(request):
    """選択したセルに勤務区分を一括設定する。body: {"cells": [[member_id, "YYYY-MM-DD"], ...], "work_type": code|null}"""
    try:
        body = json.loads(request.body)
        code = body.get("work_type")
        work_type = WorkType.objects.get(code=code, is_active=True) if code else None
        cells: dict[int, list[date]] = {}
        for mid, iso in body["cells"]:
            cells.setdefault(int(mid), []).append(date.fromisoformat(iso))
        if not cells or sum(len(v) for v in cells.values()) > 5000:
            raise ValueError
    except (ValueError, KeyError, TypeError, WorkType.DoesNotExist, json.JSONDecodeError):
        return JsonResponse({"detail": "入力内容が正しくありません。"}, status=400)
    members = {m.pk: m for m in Member.objects.filter(pk__in=cells)}
    if len(members) != len(cells):
        return JsonResponse({"detail": "存在しないメンバーが含まれています。"}, status=400)
    changed = 0
    for mid, dates in cells.items():
        changed += services.set_shifts([members[mid]], dates, work_type, user=request.user)
    # 変更後の行の集計を返す
    month = month_start(next(iter(cells.values()))[0])
    rows = {
        r.member.pk: {
            "planned": f"{r.planned_days.normalize():f}",
            "diff": None if r.diff is None else f"{r.diff.normalize():+f}" if r.diff else "±0",
            "short": r.diff is not None and r.diff < 0,
        }
        for r in services.month_grid(month)
        if r.member.pk in cells
    }
    return JsonResponse({"changed": changed, "rows": rows})


@login_required
@require_POST
def copy_previous(request):
    month = month_from_request(request)
    overwrite = request.POST.get("overwrite") == "1"
    n = services.copy_previous_month(month, user=request.user, overwrite=overwrite)
    messages.success(request, f"前月のシフトをコピーしました（{n}件）。")
    return redirect(f"/shifts/?month={month:%Y-%m}")


@login_required
def share(request):
    month = month_from_request(request)
    return render(request, "shifts/share.html", {**month_context(month), "emp": _emp_filter(request)})


@login_required
def image_png(request):
    month = month_from_request(request)
    png = render_png(month, settings.COMPANY_NAME, _emp_filter(request) or None)
    resp = HttpResponse(png, content_type="image/png")
    if request.GET.get("download"):
        resp["Content-Disposition"] = f'attachment; filename="shift-{month:%Y-%m}.png"'
    return resp


@login_required
def history(request):
    month = month_from_request(request)
    logs = AuditLog.objects.filter(
        target_type="shift", target_id__contains=f":{month:%Y-%m}-"
    ).order_by("-at")[:500]
    return render(request, "shifts/history.html", {**month_context(month), "logs": logs})


@login_required
def import_view(request):
    """既存シフト表（Excel）の取り込み。確認画面で、特定できない名前は対象者を選ぶか取り込まないかを決める。"""
    from datetime import date as _date

    from .legacy import DayListError, apply_shift_rows, read_shift_sheet
    from .models import ShiftEntry

    ctx = {}
    if request.method == "POST" and request.POST.get("step") == "upload":
        f = request.FILES.get("file")
        if not f or not f.name.lower().endswith(".xlsx"):
            messages.error(request, "シフト表の Excel（.xlsx）を選んでください。")
            return redirect("shifts:import")
        try:
            month, rows = read_shift_sheet(f.read())
        except DayListError as e:
            messages.error(request, str(e))
            return redirect("shifts:import")
        request.session["shift_import"] = {"month": month.isoformat(), "rows": rows}
        return redirect("shifts:import")
    plan = request.session.get("shift_import")
    if request.method == "POST" and request.POST.get("step") == "apply" and plan:
        month = _date.fromisoformat(plan["month"])
        rows = plan["rows"]
        for item in rows:
            choice = request.POST.get(f"member_{item['row']}", "skip")
            if choice.isdigit():
                # 自動で特定できなかった名前（姓だけ・別表記）を選んだ場合は、別名として覚える対象にする
                item["chosen"] = item["method"] != "exact"
                item["member_id"] = int(choice)
            else:
                item["member_id"] = None
        n = apply_shift_rows(month, rows, user=request.user, save_alias=bool(request.POST.get("save_alias")))
        request.session.pop("shift_import", None)
        messages.success(request, f"{month:%Y年%m月}のシフトを取り込みました（{n}件）。")
        return redirect(f"/shifts/?month={month:%Y-%m}")
    if request.method == "POST" and request.POST.get("step") == "cancel":
        request.session.pop("shift_import", None)
        return redirect("shifts:import")
    if plan:
        month = _date.fromisoformat(plan["month"])
        ctx.update({"month": month, "rows": plan["rows"],
                    "existing": ShiftEntry.objects.filter(date__year=month.year, date__month=month.month).count(),
                    "members": Member.objects.filter(status=Member.Status.ACTIVE)})
    return render(request, "shifts/import.html", ctx)
