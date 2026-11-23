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


def grid(request):
    """シフト表（見るだけならログイン不要。変更はログインした人だけ）。"""
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
    from apps.attendance.models import AttendanceMonthLock

    extra = {"lock": AttendanceMonthLock.objects.filter(month=month).first()}
    if view == "person":
        sel = next((r for r in rows if str(r.member.pk) == request.GET.get("member")), rows[0] if rows else None)
        cal = []
        if sel:
            lead = (days[0]["date"].weekday() + 1) % 7  # 日曜はじまり
            cal = [None] * lead + [
                {"date": d["date"], "wt": sel.cells[i][1], "cls": d["cls"], "holiday": d["holiday"], "today": d["today"]}
                for i, d in enumerate(days)
            ]
        extra.update({"sel": sel, "cal": cal})
    template = {"day": "shifts/day.html", "person": "shifts/person.html"}.get(view, "shifts/grid.html")
    return render(
        request,
        template,
        {
            **extra,
            **month_context(month),
            "rows": rows,
            "days": days,
            "work_types": WorkType.objects.filter(is_active=True),
            "emp": emp,
            "emp_choices": EmploymentType.choices,
            "view": view,
            "can_edit": request.user.is_authenticated,
            "public_page": not request.user.is_authenticated,
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
    from apps.attendance.services import MonthLockedError

    try:
        for mid, dates in cells.items():
            changed += services.set_shifts([members[mid]], dates, work_type, user=request.user)
    except MonthLockedError as e:
        return JsonResponse({"detail": str(e) + "変更するには、先に確定を解除してください。"}, status=400)
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
    from apps.attendance.services import MonthLockedError

    try:
        n = services.copy_previous_month(month, user=request.user, overwrite=overwrite)
        messages.success(request, f"前月のシフトをコピーしました（{n}件）。")
    except MonthLockedError as e:
        messages.error(request, str(e))
    return redirect(f"/shifts/?month={month:%Y-%m}")


def share(request):
    month = month_from_request(request)
    return render(request, "shifts/share.html", {**month_context(month), "emp": _emp_filter(request),
                                                 "public_page": not request.user.is_authenticated})


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
        from apps.attendance.services import MonthLockedError

        try:
            n = apply_shift_rows(month, rows, user=request.user, save_alias=bool(request.POST.get("save_alias")))
        except MonthLockedError as e:
            messages.error(request, str(e))
            return redirect("shifts:import")
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


@login_required
def numbers(request):
    """LINEで届く「出勤日」または「休みの日」の数字を、人ごとに貼り付けて入力する（従来のスプレッドシートと同じ入れ方）。

    - 出勤日モード: 入れた日＝出勤、それ以外＝休み / 休みモード: 入れた日＝休み、それ以外＝出勤
    - すでに半日・有給・半日＋半日有給にしている日はそのまま残す
    """
    from django.db import transaction

    from apps.attendance.models import AttendanceMonthLock
    from apps.core.months import month_end

    from .legacy import DayListError, parse_day_list
    from .models import ShiftEntry

    month = month_from_request(request)
    last = month_end(month).day
    members = list(services.members_for_month(month))
    entries = {}
    for e in ShiftEntry.objects.filter(member__in=members, date__gte=month, date__lte=month_end(month)).select_related("work_type"):
        entries.setdefault(e.member_id, {})[e.date.day] = e.work_type.code
    locked = AttendanceMonthLock.objects.filter(month=month).exists()
    special = {"half", "paid", "half_paid"}

    def current_text(m, mode):
        codes = entries.get(m.pk, {})
        if not codes:
            return ""
        target = "work" if mode == "work" else "off"
        return ",".join(str(d) for d in sorted(codes) if codes[d] == target)

    rows, errors = [], {}
    if request.method == "POST" and not locked:
        plan = []
        for m in members:
            mode = request.POST.get(f"mode_{m.pk}", m.shift_input_mode)
            text = request.POST.get(f"days_{m.pk}", "").strip()
            initial = request.POST.get(f"initial_{m.pk}", "")
            initial_mode = request.POST.get(f"initial_mode_{m.pk}", "")
            if text == initial and mode == initial_mode:
                continue  # 変更なし
            try:
                listed = set(parse_day_list(text, last))
            except DayListError as e:
                errors[m.pk] = str(e)
                continue
            plan.append((m, mode, listed))
        if not errors and plan:
            work, off = WorkType.objects.get(code="work"), WorkType.objects.get(code="off")
            with transaction.atomic():
                n = 0
                for m, mode, listed in plan:
                    work_days = listed if mode == "work" else set(range(1, last + 1)) - listed
                    keep = {d for d, c in entries.get(m.pk, {}).items() if c in special}  # 半日・有給はそのまま
                    dates = [month.replace(day=d) for d in range(1, last + 1) if d not in keep]
                    n += services.set_shifts([m], [d for d in dates if d.day in work_days], work, user=request.user, reason="LINEの数字から入力")
                    n += services.set_shifts([m], [d for d in dates if d.day not in work_days], off, user=request.user, reason="LINEの数字から入力")
                    if m.shift_input_mode != mode:
                        Member.objects.filter(pk=m.pk).update(shift_input_mode=mode)
            messages.success(request, f"{len(plan)}人分のシフトを保存しました。")
            return redirect(f"/shifts/?month={month:%Y-%m}")
        if errors:
            messages.error(request, "読み取れない数字があります。赤い行を直してから、もう一度保存してください（まだ何も保存していません）。")
        elif not plan:
            messages.info(request, "変更はありませんでした。")
    for m in members:
        mode = request.POST.get(f"mode_{m.pk}") if request.method == "POST" else None
        mode = mode or m.shift_input_mode
        init = current_text(m, mode)
        text = request.POST.get(f"days_{m.pk}", init) if request.method == "POST" else init
        specials = sorted(d for d, c in entries.get(m.pk, {}).items() if c in special)
        rows.append({"member": m, "mode": mode, "text": text, "initial": init, "error": errors.get(m.pk, ""),
                     "specials": specials, "has": bool(entries.get(m.pk))})
    return render(request, "shifts/numbers.html", {
        **month_context(month), "rows": rows, "last": last, "locked": locked, "view": "numbers",
        "lock": AttendanceMonthLock.objects.filter(month=month).first(),
        "first_weekday": (month.weekday() + 1) % 7,
    })
