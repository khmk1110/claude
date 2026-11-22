"""給与・報酬管理の画面。すべてのビューに追加認証（payroll）を必須にする（ミドルウェアと二重）。"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.decorators import stepup_required
from apps.attendance import services as attendance
from apps.core import audit
from apps.core.models import ImmutableAfterConfirmError
from apps.core.monthnav import month_context, month_from_request
from apps.core.models import AuditLog
from apps.core.months import next_month, prev_month
from apps.members.matching import match_member
from apps.members.models import EmploymentType, Member
from apps.shifts.services import members_for_month

from . import calc
from .models import (
    BaseSalary,
    PayrollInput,
    PayrollItemDefinition,
    PayrollLine,
    PayrollStatement,
    Product,
    SalesCount,
    SalesEvidence,
)
from .pdf import render_pdf

S = PayrollStatement.Status


def payroll_view(view):
    return login_required(stepup_required("payroll")(view))


def _int_or_none(text):
    text = unicodedata.normalize("NFKC", str(text or "")).replace(",", "").strip()
    if text == "":
        return None
    if not re.fullmatch(r"-?\d+", text):
        raise ValueError(f"「{text}」は整数で入力してください。")
    return int(text)


def _dec_or_none(text):
    text = unicodedata.normalize("NFKC", str(text or "")).replace(",", "").strip()
    if text == "":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"「{text}」は数値で入力してください。")


# ---------------------------------------------------------------------------
# 月次一覧
# ---------------------------------------------------------------------------
@payroll_view
def index(request):
    month = month_from_request(request)
    statements = list(
        PayrollStatement.objects.filter(month=month).select_related("member").prefetch_related("inputs")
        .order_by("member__display_order", "member_id")
    )
    rows = []
    for st in statements:
        inputs = {i.key: i for i in st.inputs.all()}
        rows.append({"st": st, "in": {k: (inputs[k].value if k in inputs else None) for k in PayrollInput.Key.values},
                     "overridden": any(i.is_overridden for i in inputs.values())})
    counts = {s: sum(1 for st in statements if st.status == s) for s in S.values}
    pending_members = members_for_month(month).filter(employment_type__in=calc.PAYROLL_TYPES).exclude(
        payroll_statements__month=month
    ).count()
    return render(request, "payroll/index.html", {
        **month_context(month), "rows": rows, "counts": counts, "status_choices": S.choices,
        "pending_members": pending_members, "payment_date": calc.ensure_month(month).payment_date if statements else None,
        "total_net": sum((st.net_amount or 0) for st in statements),
        "unconfirmed": sum(1 for st in statements if st.status != S.CONFIRMED),
    })


@payroll_view
@require_POST
def month_action(request):
    month = month_from_request(request)
    action = request.POST.get("action")
    if action == "prepare":
        n = calc.prepare_month(month, user=request.user)
        messages.success(request, f"{n}人分の給与データを作成しました。")
    elif action == "calculate_all":
        n = errors = 0
        for st in PayrollStatement.objects.filter(month=month).exclude(status=S.CONFIRMED):
            st = calc.calculate(st, user=request.user)
            n += 1
            errors += bool(st.blocking_errors)
        msg = f"{n}人分を計算しました。"
        if errors:
            messages.warning(request, msg + f"うち{errors}人は入力が足りないため「入力途中」です。")
        else:
            messages.success(request, msg)
    elif action == "payment_date":
        pm = calc.ensure_month(month)
        try:
            new = date.fromisoformat(request.POST.get("payment_date", ""))
        except ValueError:
            messages.error(request, "支払日が正しくありません。")
        else:
            audit.log("payroll.payment_date", target=pm, before={"payment_date": pm.payment_date},
                      after={"payment_date": new}, user=request.user)
            pm.payment_date = new
            pm.save()
            messages.success(request, "支払日を変更しました（確定済みの明細は確定時の支払日のままです）。")
    return redirect(f"/payroll/?month={month:%Y-%m}")


# ---------------------------------------------------------------------------
# 個人別
# ---------------------------------------------------------------------------
@payroll_view
def detail(request, pk):
    st = get_object_or_404(PayrollStatement.objects.select_related("member"), pk=pk)
    audit.log("payroll.view", target=st, user=request.user)
    inputs = {i.key: i for i in st.inputs.all()}
    input_rows = [(k, label, inputs.get(k)) for k, label in PayrollInput.Key.choices]
    lines = list(st.lines.all())
    history = list(
        AuditLog.objects.filter(target_type="payroll.PayrollStatement", target_id=str(st.pk))
        .exclude(action="payroll.view").order_by("-at")[:50]
    )
    prev_st = PayrollStatement.objects.filter(member=st.member, month__lt=st.month).order_by("-month").first()
    next_st = PayrollStatement.objects.filter(member=st.member, month__gt=st.month).order_by("month").first()
    return render(request, "payroll/detail.html", {
        "st": st, "input_rows": input_rows, "lines": lines, "history": history,
        "earnings": [l for l in lines if l.kind == "earning"], "deductions": [l for l in lines if l.kind == "deduction"],
        "kind_choices": PayrollItemDefinition.Kind.choices, "prev_st": prev_st, "next_st": next_st,
        "snapshot": st.snapshot if st.is_confirmed else None,
    })


@payroll_view
@require_POST
def detail_action(request, pk):
    st = get_object_or_404(PayrollStatement, pk=pk)
    p, user = request.POST, request.user
    action = p.get("action")
    try:
        with transaction.atomic():
            if action == "calculate":
                st = calc.calculate(st, user=user)
                if st.blocking_errors:
                    messages.warning(request, "計算しましたが、入力が足りない項目があります。")
                else:
                    messages.success(request, "再計算しました。")
            elif action == "input":
                calc.set_input_override(st, p["key"], _dec_or_none(p.get("value")), reason=p.get("reason", "").strip(), user=user)
                messages.success(request, "修正して再計算しました。")
            elif action == "input_clear":
                calc.set_input_override(st, p["key"], None, reason="", user=user)
                messages.success(request, "自動取得値に戻して再計算しました。")
            elif action == "line":
                line = get_object_or_404(PayrollLine, pk=p["line_id"], statement=st)
                calc.set_line_override(line, _int_or_none(p.get("amount")), reason=p.get("reason", "").strip(), user=user)
                messages.success(request, "金額を修正しました。")
            elif action == "line_clear":
                line = get_object_or_404(PayrollLine, pk=p["line_id"], statement=st)
                calc.set_line_override(line, None, reason="", user=user)
                messages.success(request, "自動計算額に戻しました。")
            elif action == "adhoc_add":
                amount = _int_or_none(p.get("amount"))
                if amount is None:
                    raise ValueError("金額を入力してください。")
                calc.add_adhoc_line(st, name=p.get("name", "").strip(), kind=p.get("kind"), amount=amount,
                                    detail=p.get("detail", "").strip(), reason=p.get("reason", "").strip(), user=user)
                messages.success(request, "当月のみの項目を追加しました。")
            elif action == "adhoc_remove":
                line = get_object_or_404(PayrollLine, pk=p["line_id"], statement=st)
                calc.remove_adhoc_line(line, user=user, reason=p.get("reason", ""))
                messages.success(request, "項目を削除しました。")
            elif action == "review":
                calc.mark_review(st, user=user)
                messages.success(request, "確認待ちにしました。")
            elif action == "confirm":
                calc.confirm(st, user=user)
                messages.success(request, "確定しました。以後、この給与は変更されません。")
            elif action == "reopen":
                calc.reopen(st, user=user, reason=p.get("reason", "").strip())
                messages.success(request, "確定を取り消しました。修正後、再計算して確定してください。")
            else:
                raise ValueError("不明な操作です。")
    except ImmutableAfterConfirmError as e:
        messages.error(request, str(e))
    except (ValueError, KeyError) as e:
        messages.error(request, str(e) or "入力内容が正しくありません。")
    return redirect("payroll:detail", pk=st.pk)


# ---------------------------------------------------------------------------
# PDF・CSV・Excel
# ---------------------------------------------------------------------------
def _snapshot_for(st: PayrollStatement) -> tuple[dict, bool]:
    if st.is_confirmed and st.snapshot:
        return st.snapshot, False
    return calc.build_snapshot(st), True


@payroll_view
def statement_pdf(request, pk):
    st = get_object_or_404(PayrollStatement, pk=pk)
    snap, draft = _snapshot_for(st)
    audit.log("payroll.pdf", target=st, after={"draft": draft}, user=request.user)
    resp = HttpResponse(render_pdf([(snap, draft)]), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="statement-{st.month:%Y-%m}-{st.member.member_no}.pdf"'
    return resp


@payroll_view
def month_pdf(request):
    month = month_from_request(request)
    only_confirmed = request.GET.get("all") != "1"
    qs = PayrollStatement.objects.filter(month=month).select_related("member").order_by("member__display_order", "member_id")
    if only_confirmed:
        qs = qs.filter(status=S.CONFIRMED)
    items = [_snapshot_for(st) for st in qs]
    if not items:
        messages.error(request, "出力できる明細がありません（確定済みの給与がありません）。")
        return redirect(f"/payroll/?month={month:%Y-%m}")
    audit.log("payroll.pdf_bulk", target_type="payroll_month", target_id=month.isoformat(),
              after={"count": len(items), "only_confirmed": only_confirmed}, user=request.user)
    resp = HttpResponse(render_pdf(items), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="statements-{month:%Y-%m}.pdf"'
    return resp


def _export_table(month):
    sts = list(PayrollStatement.objects.filter(month=month).select_related("member").prefetch_related("inputs", "lines")
               .order_by("member__display_order", "member_id"))
    line_names: list[str] = []
    for st in sts:
        for l in (st.snapshot["lines"] if st.is_confirmed and st.snapshot else st.lines.all()):
            name = l["name"] if isinstance(l, dict) else l.name
            if name not in line_names:
                line_names.append(name)
    header = ["メンバー番号", "氏名", "雇用区分", "前月出勤日数", "前月獲得件数", "当月獲得件数", "入金確定件数", "単価",
              *line_names, "支給合計", "控除合計", "差引支給額", "計算状況"]
    rows = []
    for st in sts:
        inputs = {i.key: i.value for i in st.inputs.all()}
        amounts: dict[str, int] = {}
        src = st.snapshot["lines"] if st.is_confirmed and st.snapshot else [
            {"name": l.name, "kind": l.kind, "amount": l.amount} for l in st.lines.all()]
        for l in src:
            sign = 1 if l["kind"] == "earning" else -1
            amounts[l["name"]] = amounts.get(l["name"], 0) + sign * int(l["amount"] or 0)
        g = lambda k: "" if inputs.get(k) is None else calc.num(inputs[k])  # noqa: E731
        rows.append([st.member.member_no, st.member.name, st.get_employment_type_display(),
                     g("prev_attendance_days"), g("prev_acquired_count"), g("current_acquired_count"), g("confirmed_count"),
                     g("unit_price"), *[amounts.get(n, "") for n in line_names],
                     st.earnings_total or 0, st.deductions_total or 0, st.net_amount or 0, st.get_status_display()])
    return header, rows


@payroll_view
def export(request, fmt):
    if fmt not in ("csv", "xlsx"):
        from django.http import Http404

        raise Http404
    month = month_from_request(request)
    header, rows = _export_table(month)
    audit.log("payroll.export", target_type="payroll_month", target_id=month.isoformat(), after={"format": fmt, "rows": len(rows)},
              user=request.user)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        resp = HttpResponse(buf.getvalue().encode("utf-8-sig"), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="payroll-{month:%Y-%m}.csv"'
        return resp
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{month:%Y-%m}"
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append(r)
    ws.freeze_panes = "C2"
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="payroll-{month:%Y-%m}.xlsx"'
    return resp


# ---------------------------------------------------------------------------
# 営業件数の入力
# ---------------------------------------------------------------------------
_NUM = re.compile(r"\d+")


def parse_counts_text(text: str):
    """『青木一郎 12』や、表をコピーした『青木一郎\t0\t4\t…\t12』を読み取る（行末の数字＝件数）。"""
    results = []
    for raw in text.splitlines():
        line = unicodedata.normalize("NFKC", raw).strip()
        if not line or line.split()[0].endswith("合計") or line.startswith(("営業", "■")):
            continue
        m = re.match(r"^([^\d\s,:：]+(?:[ \u3000][^\d\s,:：]+)?)[\s,:：]*(.*)$", line)
        if not m:
            continue
        name, rest = m.group(1).strip(), m.group(2)
        nums = _NUM.findall(rest)
        if not name or not nums:
            continue
        match = match_member(name)
        results.append({"text": raw.strip(), "name": name, "count": int(nums[-1]), "member": match.member,
                        "candidates": match.candidates, "method": match.method})
    return results


def _product_members(month):
    items = PayrollItemDefinition.objects.filter(is_active=True, method=PayrollItemDefinition.Method.PRODUCT_COMMISSION)
    out: dict[int, list[Product]] = {}
    codes = {i.params.get("product") for i in items}
    products = {p.code: p for p in Product.objects.filter(code__in=codes)}
    for item in items.prefetch_related("members"):
        p = products.get(item.params.get("product"))
        for m in item.members.all():
            if p and p not in out.setdefault(m.pk, []):
                out[m.pk].append(p)
    for v in out.values():
        v.sort(key=lambda p: p.display_order)
    return out


def _flag_confirmed(member, month, note, user):
    st = PayrollStatement.objects.filter(member=member, month=month, status=S.CONFIRMED).first()
    if st:
        PayrollStatement.objects.filter(pk=st.pk).update(
            needs_review=True, review_note=(st.review_note + "\n" if st.review_note else "") + note)
        audit.log("payroll.flag_review", target=st, after={"note": note}, user=user)


@payroll_view
def sales(request):
    month = month_from_request(request)
    members = list(members_for_month(month))
    prod_map = _product_members(month)
    existing = {(s.member_id, s.kind, s.product_id): s for s in SalesCount.objects.filter(month=month)}
    prefill: dict[str, int] = {}
    parsed = None

    if request.method == "POST" and request.POST.get("action") == "parse":
        kind = request.POST.get("kind", "confirmed")
        parsed = parse_counts_text(request.POST.get("text", ""))
        for r in parsed:
            if r["member"]:
                prefill[f"{'conf' if kind == 'confirmed' else 'acq'}_{r['member'].pk}"] = r["count"]
        messages.info(request, "読み取った件数をフォームに入れました。内容を確認して「保存」を押してください（まだ保存されていません）。")

    elif request.method == "POST" and request.POST.get("action") == "save":
        evidence_file = request.FILES.get("evidence")
        source = SalesCount.Source.IMAGE if evidence_file or request.POST.get("evidence_text") else SalesCount.Source.MANUAL
        try:
            with transaction.atomic():
                if evidence_file or request.POST.get("evidence_text"):
                    SalesEvidence.objects.create(month=month, kind=request.POST.get("evidence_kind", "confirmed"),
                                                 file=evidence_file, text=request.POST.get("evidence_text", ""),
                                                 uploaded_by=request.user)
                changed = 0
                for m in members:
                    fields = [("acq", SalesCount.Kind.ACQUIRED, None), ("conf", SalesCount.Kind.CONFIRMED, None)]
                    fields += [(f"prod_{p.code}", SalesCount.Kind.CONFIRMED, p) for p in prod_map.get(m.pk, [])]
                    for prefix, kind, product in fields:
                        name = f"{prefix}_{m.pk}"
                        if name not in request.POST:
                            continue
                        value = _int_or_none(request.POST.get(name))
                        if value is not None and value < 0:
                            raise ValueError(f"{m.name}: 件数に負の数は入力できません。")
                        row = existing.get((m.pk, kind, product.pk if product else None))
                        before = row.count if row else None
                        if before == value:
                            continue
                        if value is None:
                            row.delete()
                        elif row:
                            row.count, row.source, row.updated_by = value, source, request.user
                            row.save()
                        else:
                            SalesCount.objects.create(member=m, month=month, kind=kind, product=product, count=value,
                                                      source=source, updated_by=request.user)
                        label = f"{SalesCount.Kind(kind).label}{'（' + product.name + '）' if product else ''}"
                        audit.log("payroll.sales_count", target=m, before={"count": before},
                                  after={"month": month, "kind": label, "count": value}, user=request.user)
                        affected = [month] if kind == SalesCount.Kind.CONFIRMED else [month, next_month(month)]
                        for am in affected:
                            _flag_confirmed(m, am, f"{month:%Y年%m月}の{label}が {before} → {value} に変更", request.user)
                        changed += 1
        except ValueError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"{changed}件を保存しました。給与に反映するには、給与画面で再計算してください。")
            return redirect(f"/payroll/sales/?month={month:%Y-%m}")

    rows = []
    for m in members:
        def val(kind, product=None, prefix=None):
            key = f"{prefix}_{m.pk}"
            if key in prefill:
                return prefill[key]
            row = existing.get((m.pk, kind, product.pk if product else None))
            return row.count if row else ""
        rows.append({
            "member": m, "acq": val(SalesCount.Kind.ACQUIRED, prefix="acq"), "conf": val(SalesCount.Kind.CONFIRMED, prefix="conf"),
            "products": [(p, val(SalesCount.Kind.CONFIRMED, p, f"prod_{p.code}")) for p in prod_map.get(m.pk, [])],
        })
    return render(request, "payroll/sales.html", {
        **month_context(month), "rows": rows, "parsed": parsed,
        "evidence": SalesEvidence.objects.filter(month=month)[:20],
    })


@payroll_view
def evidence_file(request, pk):
    ev = get_object_or_404(SalesEvidence, pk=pk)
    audit.log("payroll.evidence_view", target=ev, user=request.user)
    from django.http import FileResponse

    return FileResponse(ev.file.open("rb"), filename=ev.file.name.rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# 正社員（明細なし。前月出勤日数・獲得件数・基本給の参照）
# ---------------------------------------------------------------------------
@payroll_view
def employees(request):
    month = month_from_request(request)
    if request.method == "POST":
        try:
            m = get_object_or_404(Member, pk=request.POST.get("member_id"), employment_type=EmploymentType.EMPLOYEE)
            eff = date.fromisoformat(request.POST.get("effective_from", "") + "-01")
            amount = _int_or_none(request.POST.get("amount"))
            if amount is None or amount <= 0:
                raise ValueError("基本給を入力してください。")
            with transaction.atomic():
                old = BaseSalary.objects.filter(member=m, effective_from=eff).first()
                BaseSalary.objects.update_or_create(member=m, effective_from=eff, defaults={"amount": amount,
                                                    "note": request.POST.get("note", "")[:255]})
                audit.log("payroll.base_salary", target=m, before={"amount": old.amount if old else None},
                          after={"effective_from": eff, "amount": amount}, user=request.user)
            messages.success(request, f"{m.name}の基本給を登録しました。")
        except ValueError as e:
            messages.error(request, str(e) or "入力内容が正しくありません。")
        return redirect(f"/payroll/employees/?month={month:%Y-%m}")
    pm = prev_month(month)
    rows = []
    for m in members_for_month(month, EmploymentType.EMPLOYEE):
        days, src = attendance.actual_attendance_days(m, pm)
        bs = BaseSalary.for_month(m, month)
        third = m.joined_on and (m.joined_on.year * 12 + m.joined_on.month + 2) == (month.year * 12 + month.month)
        rows.append({"member": m, "prev_days": days, "prev_src": src, "prev_acq": calc.sales_total(m, pm, SalesCount.Kind.ACQUIRED),
                     "base": bs, "third_month": third, "history": m.base_salaries.all()[:5]})
    audit.log("payroll.employees_view", target_type="payroll_month", target_id=month.isoformat(), user=request.user)
    return render(request, "payroll/employees.html", {**month_context(month), "rows": rows, "prev": pm})


# ---------------------------------------------------------------------------
# チーム（チーム達成ボーナス・インターンリーダー手当）
# ---------------------------------------------------------------------------
@payroll_view
def teams(request):
    from django.utils import timezone

    from apps.core.months import month_start

    from .models import Team, TeamMembership

    this_month = month_start(timezone.localdate())
    if request.method == "POST":
        p, user = request.POST, request.user
        action = p.get("action")
        try:
            with transaction.atomic():
                if action == "create":
                    leader = get_object_or_404(Member, pk=p.get("leader"))
                    name = p.get("name", "").strip() or f"{leader.name.split()[0]}チーム"
                    team = Team.objects.create(name=name, leader=leader, kind=p.get("kind", Team.Kind.SALES))
                    audit.log("team.create", target=team, after={"leader": leader.name, "kind": team.get_kind_display()}, user=user)
                    messages.success(request, f"チーム「{team.name}」を作りました。メンバーを追加してください。")
                else:
                    team = get_object_or_404(Team, pk=p.get("team"))
                    if action == "leader":
                        leader = get_object_or_404(Member, pk=p.get("leader"))
                        audit.log("team.leader", target=team, before={"leader": team.leader.name}, after={"leader": leader.name}, user=user)
                        team.leader = leader
                        team.save()
                        messages.success(request, f"リーダーを{leader.name}さんに変更しました（未確定の給与は再計算で反映されます）。")
                    elif action == "add":
                        m = get_object_or_404(Member, pk=p.get("member"))
                        start = parse_month_input(p.get("start_month")) or this_month
                        if TeamMembership.objects.filter(team=team, member=m, end_month__isnull=True).exists():
                            raise ValueError(f"{m.name}さんはすでにメンバーです。")
                        TeamMembership.objects.create(team=team, member=m, start_month=start)
                        audit.log("team.member_add", target=team, after={"member": m.name, "start": start}, user=user)
                        messages.success(request, f"{m.name}さんを{start:%Y年%m月}からメンバーにしました。")
                    elif action == "end":
                        ms = get_object_or_404(TeamMembership, pk=p.get("membership"), team=team)
                        end = parse_month_input(p.get("end_month")) or this_month
                        if end < ms.start_month:
                            raise ValueError("脱退月は加入月以降にしてください。")
                        ms.end_month = end
                        ms.save()
                        audit.log("team.member_end", target=team, after={"member": ms.member.name, "end": end}, user=user)
                        messages.success(request, f"{ms.member.name}さんは{end:%Y年%m月}までのメンバーにしました。")
                    elif action == "rename":
                        audit.log("team.rename", target=team, before={"name": team.name}, after={"name": p.get("name")}, user=user)
                        team.name = p.get("name", "").strip() or team.name
                        team.save()
                        messages.success(request, "チーム名を変更しました。")
                    elif action == "toggle":
                        team.is_active = not team.is_active
                        team.save()
                        audit.log("team.toggle", target=team, after={"is_active": team.is_active}, user=user)
                        messages.success(request, "チームを" + ("有効" if team.is_active else "停止") + "にしました。")
                    else:
                        raise ValueError("不明な操作です。")
        except ValueError as e:
            messages.error(request, str(e))
        return redirect("payroll:teams")

    rows = []
    for team in Team.objects.select_related("leader").order_by("-is_active", "kind", "name"):
        current = [ms for ms in team.memberships.select_related("member").order_by("start_month")
                   if ms.end_month is None or ms.end_month >= this_month]
        past = [ms for ms in team.memberships.select_related("member") if ms.end_month and ms.end_month < this_month]
        rows.append({"team": team, "current": current, "past": past})
    return render(request, "payroll/teams.html", {
        "rows": rows, "members": Member.objects.filter(status=Member.Status.ACTIVE), "kinds": Team.Kind.choices,
        "month_param": f"{this_month:%Y-%m}",
    })


def parse_month_input(text):
    from apps.core.months import parse_month

    return parse_month(text) if text else None
