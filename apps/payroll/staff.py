"""正社員の給与の合計（明細は作らない）。給与の内訳（基本給・手当）＋交通費＋その他の経費を1人ずつ出し、支出の合計にも使う。"""

from __future__ import annotations

from django.db.models import Q

from apps.members.models import EmploymentType, Member
from apps.shifts.services import members_for_month

from .models import BaseSalary, EmployeePay

OPEN = ("imported", "unchecked", "pending", "modified")
PARTS = EmployeePay.PARTS

# 基本給の決まり：入社1・2か月目は25万円、3か月目からは28万円
BASE_FIRST, BASE_FROM_THIRD = 250_000, 280_000


def month_no(member, month):
    """入社して何か月目か（入社した月＝1か月目）。入社日がなければ None。"""
    j = member.joined_on
    if not j:
        return None
    return (month.year - j.year) * 12 + (month.month - j.month) + 1


def expected_base(member, month):
    n = month_no(member, month)
    if n is None or n < 1:
        return None
    return BASE_FIRST if n < 3 else BASE_FROM_THIRD


def default_parts(member, month):
    """まだ保存していない月の内訳：前の月の内訳を引き継ぎ、基本給は決まり（25万→28万）を優先する。"""
    last = EmployeePay.objects.filter(member=member, month__lt=month).order_by("-month").first()
    parts = {k: (getattr(last, k) if last else 0) for k, _ in PARTS}
    exp = expected_base(member, month)
    if exp is not None:
        parts["base"] = exp
    elif not last:
        bs = BaseSalary.for_month(member, month)
        parts["base"] = bs.amount if bs else 0
    return parts, ("前回と同じ" if last else "基本給")


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
        sp = saved.get(m.pk)
        if sp:
            parts, src, checks = {k: getattr(sp, k) for k, _ in PARTS}, "", sp.checks or {}
        else:
            parts, src = default_parts(m, month)
            checks = {}
        salary = sum(parts.values())
        n, exp_base = month_no(m, month), expected_base(m, month)
        items = [{"key": k, "label": label, "value": parts[k], "checked": bool(checks.get(k))} for k, label in PARTS]
        rows.append({"member": m, "parts": parts, "items": items, "salary": salary, "src": src, "saved": bool(sp),
                     "month_no": n, "expected_base": exp_base, "base_ng": exp_base is not None and parts["base"] != exp_base,
                     "checked": sum(1 for x in items if x["checked"]), **e, "total": salary + e["transport"] + e["other"]})
    return rows


def staff_total(month):
    rows = staff_rows(month)
    if not rows:
        return None
    return {"amount": sum(r["total"] for r in rows), "count": len(rows),
            "unsaved": sum(1 for r in rows if not r["saved"]), "open": sum(r["open"] for r in rows)}
