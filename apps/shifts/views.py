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
    month = month_from_request(request)
    emp = _emp_filter(request)
    days = [
        {"date": d, "iso": d.isoformat(), "wd": weekday_label(d), "cls": day_class(d), "holiday": holiday_name(d)}
        for d in days_in_month(month)
    ]
    return render(
        request,
        "shifts/grid.html",
        {
            **month_context(month),
            "rows": services.month_grid(month, emp or None),
            "days": days,
            "work_types": WorkType.objects.filter(is_active=True),
            "emp": emp,
            "emp_choices": EmploymentType.choices,
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
