"""正社員の給与の合計（明細は作らない）。給与＋交通費＋その他の経費を1人ずつ出し、支出の合計にも使う。"""

from __future__ import annotations

from django.db.models import Q

from apps.members.models import EmploymentType, Member
from apps.shifts.services import members_for_month

from .models import BaseSalary, EmployeePay

OPEN = ("imported", "unchecked", "pending", "modified")


def default_salary(member, month):
    """まだ入れていない月の給与：前に入れた額 → 基本給 の順に使う。"""
    last = EmployeePay.objects.filter(member=member, month__lt=month).order_by("-month").first()
    if last:
        return last.salary, "前回と同じ"
    bs = BaseSalary.for_month(member, month)
    if bs:
        return bs.amount, "基本給"
    return None, ""


def staff_rows(month):
    from apps.expenses.models import ExpenseRecord

    recs = (ExpenseRecord.objects.filter(Q(target_month=month) | Q(target_month__isnull=True, auto_target_month=month))
            .exclude(status="excluded").select_related("member", "auto_member", "category", "auto_category"))
    exp = {}
    for r in recs:
        m, c = r.effective_member, r.effective_category
        if not m or m.employment_type != EmploymentType.EMPLOYEE or (c and c.code == "gasoline"):
            continue  # ガソリン代は会社のカードで払っているので、正社員の分は足さない
        e = exp.setdefault(m.pk, {"transport": 0, "other": 0, "open": 0})
        if r.status in OPEN:
            e["open"] += 1
            continue  # 確認待ちは承認するまで入れない
        e["transport" if c and c.code == "transport" else "other"] += r.effective_amount or 0
    ids = set(members_for_month(month, EmploymentType.EMPLOYEE).values_list("pk", flat=True)) | set(exp)
    ids |= set(EmployeePay.objects.filter(month=month).values_list("member_id", flat=True))
    saved = {p.member_id: p for p in EmployeePay.objects.filter(month=month)}
    rows = []
    for m in Member.objects.filter(pk__in=ids).order_by("display_order", "id"):
        e = exp.get(m.pk, {"transport": 0, "other": 0, "open": 0})
        if m.pk in saved:
            salary, src = saved[m.pk].salary, ""
        else:
            salary, src = default_salary(m, month)
        total = (salary or 0) + e["transport"] + e["other"]
        rows.append({"member": m, "salary": salary, "src": src, "saved": m.pk in saved, **e, "total": total})
    return rows


def staff_total(month):
    rows = staff_rows(month)
    if not rows:
        return None
    return {"amount": sum(r["total"] for r in rows), "count": len(rows),
            "unsaved": sum(1 for r in rows if not r["saved"]), "open": sum(r["open"] for r in rows)}
