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


OPEN_EXPENSE = ["imported", "unchecked", "pending", "modified"]
TRANSPORT_DEDUCTION = 35000


def _month_expense_records(month, codes=None, exclude_codes=None):
    """その月の給与に入る経費（修正後の値で判定）。"""
    from django.db.models import Q

    from apps.expenses.models import ExpenseRecord

    qs = (ExpenseRecord.objects.filter(Q(target_month=month) | Q(target_month__isnull=True, auto_target_month=month))
          .select_related("member", "auto_member", "category", "auto_category", "source_file").prefetch_related("issues"))
    recs = list(qs.order_by("source_file_id", "row_number"))

    def code(r):
        c = r.effective_category
        return c.code if c else ""

    if codes is not None:
        recs = [r for r in recs if code(r) in codes]
    if exclude_codes:
        recs = [r for r in recs if code(r) not in exclude_codes]
    return recs


def _person_rows(recs):
    """経費を人ごとにまとめる（対象外は除く）。"""
    by = {}
    for r in recs:
        if r.status == "excluded":
            continue
        m = r.effective_member
        key = m.pk if m else f"?{r.card_code or r.name_text or r.pk}"
        row = by.setdefault(key, {"member": m, "label": m.name if m else (r.name_text or (f"カード {r.card_code}" if r.card_code else "（不明）")),
                                  "cards": set(), "amount": 0, "open": 0, "approved": 0, "issues": [], "records": []})
        if r.card_code:
            row["cards"].add(r.card_code)
        row["amount"] += r.effective_amount or 0
        row["records"].append(r)
        if r.status in OPEN_EXPENSE:
            row["open"] += 1
        else:
            row["approved"] += 1
        row["issues"] += [i.message for i in r.issues.all() if not i.resolved]
    rows = sorted(by.values(), key=lambda x: (x["member"] is None, getattr(x["member"], "display_order", 0), x["label"]))
    for row in rows:
        row["cards"] = "・".join(sorted(row["cards"]))
        row["state"] = "attn" if (row["member"] is None or row["issues"]) else ("open" if row["open"] else "ok")
    return rows


def _expense_panels(month, members):
    """給与の画面の「経費」ステップ用：ガソリン代・交通費・その他を分けてまとめる。"""
    from apps.expenses.models import SourceFile

    files = list(SourceFile.objects.filter(target_month=month).order_by("-uploaded_at"))
    processing = [f for f in files if f.status == "processing"]
    errors = [f for f in files if f.status == "error"]

    gas_recs = _month_expense_records(month, codes={"gasoline"})
    gas_rows = _person_rows(gas_recs)
    gas_files = [f for f in files if f.document_kind == "gasoline"]
    checks = []
    for f in gas_files:
        if f.status != "parsed" or f.source_total is None:
            continue
        got = sum(r.effective_amount or 0 for r in gas_recs if r.source_file_id == f.pk)
        checks.append({"file": f, "ok": got == f.source_total, "got": got, "total": f.source_total})
    unknown_cards = [r for r in gas_recs if r.status == "excluded" and r.card_code and not r.effective_member]

    tr_recs = _month_expense_records(month, codes={"transport"})
    tr_by = {row["member"].pk: row for row in _person_rows(tr_recs) if row["member"]}
    contractors, interns = [], []
    for m in members:
        row = tr_by.pop(m.pk, None)
        amount = row["amount"] if row else 0
        item = {"member": m, "amount": amount, "row": row}
        if m.employment_type == EmploymentType.CONTRACTOR:
            item["deduct"] = TRANSPORT_DEDUCTION
            item["net"] = max(amount - TRANSPORT_DEDUCTION, 0)
            item["short"] = 0 < amount < TRANSPORT_DEDUCTION
            contractors.append(item)
        else:
            item["net"] = amount
            if amount:
                interns.append(item)
    tr_unknown = [row for row in _person_rows(tr_recs) if not row["member"]]

    other_recs = _month_expense_records(month, exclude_codes={"gasoline", "transport"})
    other_rows = _person_rows(other_recs)

    def status(rows, has_any):
        if not has_any:
            return "empty"
        if any(r["state"] == "attn" for r in rows):
            return "attn"
        if any(r["open"] for r in rows):
            return "open"
        return "ok"

    gas = {"rows": gas_rows, "total": sum(r["amount"] for r in gas_rows), "checks": checks, "unknown_cards": unknown_cards,
           "files": gas_files, "open": sum(r["open"] for r in gas_rows),
           "empty": [f for f in gas_files if f.status == "parsed" and not f.row_count],
           "processing": [f for f in processing if f.document_kind in ("gasoline", "unknown", "")],
           "errors": [f for f in errors if f.document_kind in ("gasoline", "unknown", "")]}
    gas["status"] = "processing" if gas["processing"] else status(gas_rows, bool(gas_rows))
    tr = {"contractors": contractors, "interns": interns, "unknown": tr_unknown, "files": [f for f in files if f.document_kind == "transport"],
          "open": sum(r["row"]["open"] for r in contractors + interns if r["row"]) + sum(r["open"] for r in tr_unknown),
          "total": sum(r["amount"] for r in contractors + interns), "net_total": sum(r["net"] for r in contractors + interns),
          "processing": [f for f in processing if f.document_kind == "transport"],
          "needs": [f for f in files if f.status in ("needs_mapping", "needs_kind") and f.document_kind in ("transport", "unknown", "")],
          "empty": [f for f in files if f.document_kind == "transport" and f.status == "parsed" and not f.row_count],
          "errors": [f for f in errors if f.document_kind == "transport"]}
    tr_rows = [r["row"] for r in contractors + interns if r["row"]] + tr_unknown
    tr["status"] = "processing" if tr["processing"] else status(tr_rows, bool(tr_rows))
    other = {"rows": other_rows, "total": sum(r["amount"] for r in other_rows), "open": sum(r["open"] for r in other_rows)}
    other["status"] = status(other_rows, bool(other_rows))
    return {"gas": gas, "tr": tr, "other": other, "open_total": gas["open"] + tr["open"] + other["open"]}


@payroll_view
@require_POST
def expenses_approve_all(request):
    """給与の画面から、ガソリン代・交通費・その他の確認待ちをまとめて承認する。"""
    from apps.expenses import importer

    month = month_from_request(request)
    group = request.POST.get("group")
    if group == "gasoline":
        recs = _month_expense_records(month, codes={"gasoline"})
    elif group == "transport":
        recs = _month_expense_records(month, codes={"transport"})
    else:
        recs = _month_expense_records(month, exclude_codes={"gasoline", "transport"})
    recs = [r for r in recs if r.status in OPEN_EXPENSE]
    with transaction.atomic():
        ok, skipped = importer.approve(recs, user=request.user)
    if ok:
        messages.success(request, f"{ok}件を承認しました。給与に入ります。")
    for msg in skipped[:5]:
        messages.warning(request, f"承認できませんでした：{msg}（「明細を直す」から直してください）")
    return redirect(f"/payroll/?month={month:%Y-%m}#step2")



GROUP_LABELS = {"transport": "交通費", "gasoline": "ガソリン代", "other": "その他の経費"}


def _group_records(month, group):
    if group in ("transport", "gasoline"):
        return _month_expense_records(month, codes={group})
    return _month_expense_records(month, exclude_codes={"gasoline", "transport"})


@payroll_view
def expenses_fix(request):
    """名前と金額を手で直すための画面（給与の画面の「金額・名前を直す」）。"""
    from django.utils import timezone

    from apps.expenses import importer
    from apps.expenses.models import ExpenseCategory, SourceFile

    month = month_from_request(request)
    group = request.GET.get("group") or request.POST.get("group") or "transport"
    if group not in GROUP_LABELS:
        group = "transport"
    back = f"/payroll/?month={month:%Y-%m}#{'gas' if group == 'gasoline' else group}"
    recs = _group_records(month, group)
    members = list(Member.objects.filter(status=Member.Status.ACTIVE).order_by("display_order", "id"))
    by_id = {m.pk: m for m in members}

    if request.method == "POST":
        changed, errors = 0, []
        with transaction.atomic():
            for r in recs:
                if f"amount_{r.pk}" not in request.POST:
                    continue
                label = r.name_text or (f"カード {r.card_code}" if r.card_code else f"{r.row_number}行目")
                want_excluded = bool(request.POST.get(f"exclude_{r.pk}"))
                try:
                    if want_excluded and r.status != "excluded":
                        importer.exclude([r], user=request.user, reason="給与の画面で対象外にした")
                        changed += 1
                        continue
                    if want_excluded:
                        continue
                    mid = request.POST.get(f"member_{r.pk}")
                    member = by_id.get(int(mid)) if mid and mid.isdigit() else None
                    amount = _int_or_none(request.POST.get(f"amount_{r.pk}", "").replace(",", "").replace("円", ""))
                    if amount is None:
                        errors.append(f"{label}: 金額を数字で入れてください")
                        continue
                    new_member = member if member and member != r.effective_member else None
                    new_amount = amount if amount != r.effective_amount else None
                    if new_member is None and new_amount is None and r.status != "excluded":
                        continue
                    if r.status == "excluded" and new_member is None and new_amount is None:
                        new_amount = amount  # 対象外を戻す
                    importer.update_record(r, user=request.user, reason="給与の画面で修正", member=new_member, amount=new_amount,
                                           save_alias=bool(new_member) and bool(request.POST.get(f"alias_{r.pk}")))
                    changed += 1
                except (ValueError, importer.RecordLocked) as e:
                    errors.append(f"{label}: {e}")
            # 資料にない人を追加
            add_mid, add_amt = request.POST.get("add_member"), _int_or_none(request.POST.get("add_amount", "").replace(",", ""))
            if add_mid and add_mid.isdigit() and add_amt:
                cat = ExpenseCategory.objects.filter(code=group).first() if group != "other" else None
                sf = next((x.source_file for x in recs), None)
                if sf is None:
                    sf = SourceFile.objects.create(original_name=f"{GROUP_LABELS[group]}（手入力）", sha256=__import__("hashlib").sha256(f"manual-{group}-{month}-{timezone.now().isoformat()}".encode()).hexdigest(),
                                                   file_format=SourceFile.Format.MANUAL, document_kind=group if group != "other" else "unknown",
                                                   target_month=month, status=SourceFile.Status.PARSED, uploaded_by=request.user)
                if cat is None:
                    cat = ExpenseCategory.objects.filter(is_active=True).exclude(code__in=["gasoline", "transport"]).first()
                try:
                    importer.add_manual_record(sf, user=request.user, member=by_id[int(add_mid)], category=cat, amount=add_amt,
                                               description=f"{GROUP_LABELS[group]}（手入力）", target_month=month,
                                               reason="給与の画面で追加")
                    changed += 1
                except (ValueError, KeyError, importer.RecordLocked) as e:
                    errors.append(f"追加: {e}")
            if request.POST.get("then") == "approve" and not errors:
                fresh = [r for r in _group_records(month, group) if r.status in OPEN_EXPENSE]
                ok, skipped = importer.approve(fresh, user=request.user)
                if ok:
                    messages.success(request, f"{ok}件を承認しました。")
                for msg in skipped[:5]:
                    errors.append(f"承認できませんでした：{msg}")
        if changed:
            messages.success(request, f"{changed}件を保存しました。")
        for e in errors[:10]:
            messages.error(request, e)
        if errors:
            return redirect(f"/payroll/expenses/fix/?month={month:%Y-%m}&group={group}")
        return redirect(back)

    rows = []
    for r in recs:
        issues = [i.message for i in r.issues.all() if not i.resolved]
        m = r.effective_member
        rows.append({"r": r, "member": m, "issues": issues, "excluded": r.status == "excluded",
                     "approved": r.status in ("approved", "reflected"),
                     "attn": (m is None and r.status != "excluded") or bool(issues)})
    rows.sort(key=lambda x: (not x["attn"], x["excluded"]))
    return render(request, "payroll/expenses_fix.html", {
        **month_context(month), "group": group, "label": GROUP_LABELS[group], "rows": rows, "members": members,
        "back": back, "attn_count": sum(1 for x in rows if x["attn"]),
    })


@payroll_view
def month_home(request):
    """今月の給与（4ステップ）: ①件数 ②経費 ③計算結果の確認 ④確定・明細。"""
    from apps.expenses.models import ExpenseCategory, ExpenseRecord, SourceFile

    month = month_from_request(request)
    pm = prev_month(month)
    members = list(members_for_month(month).filter(employment_type__in=calc.PAYROLL_TYPES))
    acq = set(SalesCount.objects.filter(month=pm, kind="acquired", member__in=members).values_list("member", flat=True))
    conf = set(SalesCount.objects.filter(month=month, kind="confirmed", member__in=members).values_list("member", flat=True))
    sts = list(PayrollStatement.objects.filter(month=month).select_related("member").prefetch_related("lines", "inputs")
               .order_by("member__display_order", "member_id"))
    prev_net = dict(PayrollStatement.objects.filter(month=pm, status=S.CONFIRMED).values_list("member_id", "net_amount"))
    rows = []
    for st in sts:
        lines = {l.code: l.amount for l in st.lines.all()}
        inputs = {i.key: i.value for i in st.inputs.all()}
        notes = [m["text"] for m in (st.calc_messages or []) if m.get("level") == "error"]
        warns = [m["text"] for m in (st.calc_messages or []) if m.get("level") == "warning"]
        pn = prev_net.get(st.member_id)
        if pn and st.net_amount is not None and abs(st.net_amount - pn) > max(abs(pn) * 0.3, 30000):
            warns.append(f"先月（{pn:,}円）から大きく変わっています")
        commission = lines.get("sales_commission", 0) + sum(v for k, v in lines.items() if k.startswith("product_"))
        rows.append({"st": st, "unit": inputs.get("unit_price"), "conf": inputs.get("confirmed_count"),
                     "commission": commission, "transport": lines.get("transport_contractor", lines.get("transport_intern", 0)),
                     "gas": lines.get("gasoline", 0), "errors": notes, "warns": warns})
    # 経費は給与の画面から扱う（給与のパスワードで守られている）
    exp = _expense_panels(month, members)
    open_recs = exp["open_total"]
    errors = sum(1 for r in rows if r["errors"])
    confirmable = [st for st in sts if st.status in (S.CALCULATED, S.REVIEW) and not st.blocking_errors]
    contractors = [st for st in sts if st.employment_type == "contractor"]
    checklist = [
        ("全員分の給与データがある", bool(sts) and len(sts) >= len(members)),
        ("全員の計算が終わり、入力不足がない", bool(sts) and errors == 0 and all(st.status != S.NOT_CALCULATED for st in sts)),
        ("交通費から35,000円を引いた（個人事業主）", all("transport_contractor" in {l.code for l in st.lines.all()} for st in contractors)),
        ("アプリ手数料4,000円を引いた（個人事業主）", all("app_fee" in {l.code for l in st.lines.all()} for st in contractors)),
        ("ガソリン代・交通費の確認待ちがない", open_recs == 0),
    ]
    return render(request, "payroll/month_home.html", {
        **month_context(month), "prev": pm, "members": members, "acq": len(acq), "conf": len(conf), "rows": rows,
        "exp": exp, "open_recs": open_recs, "errors": errors,
        "confirmable": len(confirmable), "confirmed": sum(1 for st in sts if st.is_confirmed), "sts": sts,
        "checklist": checklist, "step1_done": members and len(acq) >= len(members) and len(conf) >= len(members),
        "categories": ExpenseCategory.objects.filter(is_active=True).exclude(code__in=["gasoline", "transport"]),
        "transport_category": ExpenseCategory.objects.filter(code="transport").first(),
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
    elif action == "confirm_all":
        ok, skipped = 0, []
        for st in PayrollStatement.objects.filter(month=month, status__in=[S.CALCULATED, S.REVIEW]):
            if st.blocking_errors:
                skipped.append(st.member.name)
                continue
            calc.confirm(st, user=request.user)
            ok += 1
        messages.success(request, f"{ok}人分を確定しました。" + (f"（入力不足のため確定しなかった人: {'、'.join(skipped)}）" if skipped else ""))
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
    nxt = request.POST.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
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
    _, common_products = _product_members(st.month)
    common_counts = [(p, SalesCount.objects.filter(member=st.member, month=st.month, kind=SalesCount.Kind.CONFIRMED,
                                                     product=p).values_list("count", flat=True).first()) for p in common_products]
    return render(request, "payroll/detail.html", {
        "st": st, "input_rows": input_rows, "lines": lines, "history": history, "common_counts": common_counts,
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
            elif action == "product_count":
                # 都市ガスなど全員共通の商材の件数を、この月だけ追加・変更・削除する
                if st.is_confirmed:
                    raise ImmutableAfterConfirmError("確定済みの給与は変更できません。")
                product = get_object_or_404(Product, code=p.get("product"))
                value = _int_or_none(p.get("count"))
                row = SalesCount.objects.filter(member=st.member, month=st.month, kind=SalesCount.Kind.CONFIRMED, product=product).first()
                before = row.count if row else None
                if value is None:
                    if row:
                        row.delete()
                elif row:
                    row.count, row.updated_by = value, user
                    row.save()
                else:
                    SalesCount.objects.create(member=st.member, month=st.month, kind=SalesCount.Kind.CONFIRMED, product=product,
                                              count=value, updated_by=user)
                audit.log("payroll.sales_count", target=st.member, before={"count": before},
                          after={"month": st.month, "kind": f"入金確定件数（{product.name}）", "count": value}, user=user)
                calc.calculate(st, user=user, log=False)
                messages.success(request, f"{product.name}を{'削除' if value is None else f'{value}件にして再計算'}しました。")
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
    """(個人専用の商材 {member_id: [Product]}, 全員共通の商材 [Product]) を返す。

    全員共通の商材（例: 都市ガス）は、その月に件数を入れた人にだけ給与の行が付く。
    """
    items = list(PayrollItemDefinition.objects.filter(is_active=True, method=PayrollItemDefinition.Method.PRODUCT_COMMISSION)
                 .prefetch_related("members"))
    products = {p.code: p for p in Product.objects.filter(code__in={i.params.get("product") for i in items})}
    personal: dict[int, list[Product]] = {}
    common: list[Product] = []
    for item in items:
        p = products.get(item.params.get("product"))
        if not p:
            continue
        if item.scope == PayrollItemDefinition.Scope.ALL:
            if p not in common:
                common.append(p)
            continue
        for m in item.members.all():
            if p not in personal.setdefault(m.pk, []):
                personal[m.pk].append(p)
    for v in personal.values():
        v.sort(key=lambda p: p.display_order)
    common.sort(key=lambda p: p.display_order)
    return personal, common


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
    prod_map, common_products = _product_members(month)
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
                    own = prod_map.get(m.pk, [])
                    fields += [(f"prod_{p.code}", SalesCount.Kind.CONFIRMED, p) for p in own + [c for c in common_products if c not in own]]
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
        own = prod_map.get(m.pk, [])
        rows.append({
            "member": m, "acq": val(SalesCount.Kind.ACQUIRED, prefix="acq"), "conf": val(SalesCount.Kind.CONFIRMED, prefix="conf"),
            "product_based": bool(own),
            "products": [(p, val(SalesCount.Kind.CONFIRMED, p, f"prod_{p.code}")) for p in own],
            # 全員共通の商材（都市ガスなど）。商材別の人は右の「商材別」欄で入力する
            "common": [(p, val(SalesCount.Kind.CONFIRMED, p, f"prod_{p.code}"), p not in own) for p in common_products],
        })
    return render(request, "payroll/sales.html", {
        **month_context(month), "rows": rows, "parsed": parsed, "common_products": common_products,
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


# ---------------------------------------------------------------------------
# 営業件数の取り込み（LINEの文・スクショ・表のコピペ）→ 確認 → 保存
# ---------------------------------------------------------------------------
def _resolve_year(month_no: int, base: date) -> date:
    y = base.year if month_no <= base.month else base.year - 1
    return date(y, month_no, 1)



def _known_names():
    """在籍メンバーと別名（資料の表記）。名前の読み取り・照合に使う。"""
    from apps.members.models import MemberAlias

    active = list(Member.objects.filter(status=Member.Status.ACTIVE))
    aliases = list(MemberAlias.objects.filter(member__in=active))
    known = [m.name for m in active] + [m.name.split()[0] for m in active if " " in m.name] + [a.alias for a in aliases]
    return active, aliases, known


def build_counts_plan(kind: str, text: str, payroll_month: date) -> dict:
    """読み取った文から、確認画面の内容（誰に何件か）を作る。名前は自動で照合し、OCRの1〜2文字違いも推定する。"""
    from apps.core.text import normalize_name
    from apps.members.models import MemberAlias

    from . import ingest
    from .models import IgnoredName

    active, aliases, known = _known_names()
    is_ranking = kind == "acquired" and ingest.RANKING_MARK in text
    if is_ranking:
        parsed = ingest.parse_ranking_text(text)  # 月別ランキングの画面には月が書かれていないので前月として扱う
    elif kind == "acquired":
        parsed = ingest.parse_dashboard(text, known)
    else:
        parsed = ingest.parse_count_table(text)
    if kind == "acquired":
        target = _resolve_year(parsed.month, payroll_month) if parsed.month else prev_month(payroll_month)
    else:
        target = payroll_month
    ignored = set(IgnoredName.objects.values_list("name_normalized", flat=True))
    candidates = [(m.pk, m.name) for m in active]
    candidates += [(a.member_id, a.alias) for a in aliases]
    # スクショが2枚で同じ行が重なって写っている時は1つにまとめる（件数が違えば両方残して確認してもらう）
    entries, seen = [], {}
    for e in parsed.entries:
        k = normalize_name(e.name)
        if k in seen and seen[k] == e.count:
            continue
        if k in seen:
            e.note = (e.note + "・" if e.note else "") + "同じ名前が別の件数でもう1行あります（どちらか選ぶ）"
        seen[k] = e.count
        entries.append(e)
    rows, taken = [], set()
    for i, e in enumerate(entries):
        res = match_member(e.name, include_retired=False)
        member_id, method, note = (res.member.pk if res.ok else None), res.method, e.note
        is_ignored = normalize_name(e.name) in ignored
        if member_id is None and not is_ignored and res.method != "ambiguous":
            member_id = ingest.guess_member(e.readings or e.name, candidates, None if is_ranking else taken)
            if member_id:
                method = "guess"
                note = (note + "・" if note else "") + "名前を推定しました（合っているか確認）"
        if member_id and member_id in taken:
            prev = next(r for r in rows if r["member_id"] == member_id)
            if prev["count"] == e.count:
                continue  # 2枚のスクショに同じ人が写っている
            note = (note + "・" if note else "") + "同じ人がもう1行あります（どちらか選ぶ）"
        if member_id:
            taken.add(member_id)
        rows.append({"i": i, "name": e.name, "count": e.count, "att": e.attendance, "line": e.line, "note": note,
                     "member_id": member_id, "method": method, "ignored": is_ignored})
    return {"kind": kind, "month": target.isoformat(), "payroll_month": payroll_month.isoformat(),
            "rows": rows, "total": parsed.total, "warnings": parsed.warnings, "text": text[:8000]}


def _start_counts_ocr(pk: int, payroll_month: date) -> None:
    import threading

    def run():
        from django.db import close_old_connections

        close_old_connections()
        try:
            _counts_ocr(pk, payroll_month)
        finally:
            close_old_connections()

    threading.Thread(target=run, daemon=True).start()


def _counts_ocr(pk: int, payroll_month: date) -> None:
    from . import ingest

    try:
        ev = SalesEvidence.objects.get(pk=pk)

        class _F:
            def __init__(self, field):
                self.field, self.name = field, field.name

            def read(self):
                with self.field.open("rb") as fh:
                    return fh.read()

        texts = [ev.text] if ev.text else []
        for e in [ev, *ev.parts.order_by("pk")]:
            texts.append(ingest.text_from_upload(_F(e.file), ev.kind, _known_names()[2]))
        text = "\n".join(texts)
        plan = build_counts_plan(ev.kind, text, payroll_month)
        SalesEvidence.objects.filter(pk=pk).update(status="done", plan=plan, text=text[:5000],
                                                   month=date.fromisoformat(plan["month"]))
    except Exception as e:  # noqa: BLE001
        SalesEvidence.objects.filter(pk=pk).update(status="error", error_message=f"読み取れませんでした: {e}")


@payroll_view
def counts_import(request):
    from django.utils import timezone

    from apps.core.months import month_start
    from apps.core.text import normalize_name

    from . import ingest
    from .models import IgnoredName

    from django.conf import settings as dj_settings

    payroll_month = month_from_request(request)
    back = f"/payroll/counts/?month={payroll_month:%Y-%m}"
    ev_id = request.GET.get("ev")
    if ev_id and request.method == "GET":
        ev = SalesEvidence.objects.filter(pk=ev_id).first()
        if ev is None:
            return redirect(back)
        if ev.status == "processing":
            elapsed = int((timezone.now() - ev.uploaded_at).total_seconds())
            if elapsed > 240:
                # サーバーの再起動などで読み取りが止まった時に、ずっと待ち画面にならないようにする
                SalesEvidence.objects.filter(pk=ev.pk, status="processing").update(
                    status="error", error_message="読み取りに時間がかかりすぎたため中止しました。もう一度選ぶか、LINEの文をコピーして貼り付けてください。")
                ev.refresh_from_db()
            else:
                return render(request, "payroll/counts_wait.html", {**month_context(payroll_month), "ev": ev, "elapsed": elapsed})
        if ev.status == "error":
            messages.error(request, ev.error_message or "読み取れませんでした。")
            return redirect(back)
        if ev.plan:
            request.session["counts_import"] = ev.plan
            SalesEvidence.objects.filter(pk=ev.pk).update(plan=None)
        return redirect(back)
    plan = request.session.get("counts_import")
    if request.method == "POST" and request.POST.get("step") == "parse":
        kind = request.POST.get("kind", "acquired")
        text = request.POST.get("text", "")
        files = request.FILES.getlist("file")
        if not files and not text.strip():
            messages.error(request, "文を貼り付けるか、ファイルを選んでください。")
            return redirect(back)
        if len(files) > 10:
            messages.error(request, "ファイルは一度に10個までです。")
            return redirect(back)
        if any(ingest.is_slow_upload(x.name) for x in files):
            # スクショ・PDFは裏で読み取り、待ち画面を出す（画面が固まらないように）。2枚目以降は1枚目にぶら下げる
            ev = SalesEvidence.objects.create(month=payroll_month, kind=kind, text=text[:5000], uploaded_by=request.user,
                                              file=files[0], status="processing")
            for x in files[1:]:
                SalesEvidence.objects.create(month=payroll_month, kind=kind, uploaded_by=request.user, file=x,
                                             status="part", parent=ev)
            if dj_settings.OCR_IN_BACKGROUND:
                transaction.on_commit(lambda: _start_counts_ocr(ev.pk, payroll_month))
            else:
                _counts_ocr(ev.pk, payroll_month)
            return redirect(f"{back}&ev={ev.pk}")
        try:
            for x in files:
                text = (text + "\n" if text else "") + ingest.text_from_upload(x, kind, _known_names()[2])
            new_plan = build_counts_plan(kind, text, payroll_month)
        except Exception as e:  # noqa: BLE001
            messages.error(request, f"読み取れませんでした: {e}")
            return redirect(back)
        SalesEvidence.objects.create(month=date.fromisoformat(new_plan["month"]), kind=kind, text=text[:5000], uploaded_by=request.user)
        request.session["counts_import"] = new_plan
        return redirect(back)
    if request.method == "POST" and request.POST.get("step") == "cancel":
        request.session.pop("counts_import", None)
        return redirect(f"/payroll/counts/?month={payroll_month:%Y-%m}")
    if request.method == "POST" and request.POST.get("step") == "apply" and plan:
        target = date.fromisoformat(plan["month"])
        kind = plan["kind"]
        saved = 0
        with transaction.atomic():
            for r in plan["rows"]:
                choice = request.POST.get(f"member_{r['i']}", "skip")
                if choice == "skip":
                    if request.POST.get(f"ignore_{r['i']}"):
                        IgnoredName.objects.get_or_create(name_normalized=normalize_name(r["name"]), defaults={"name": r["name"]})
                    continue
                m = get_object_or_404(Member, pk=int(choice))
                count = _int_or_none(request.POST.get(f"count_{r['i']}"))
                if count is None or count < 0:
                    continue
                if r["method"] != "exact" or r["member_id"] != m.pk:
                    from apps.members.models import MemberAlias

                    MemberAlias.objects.get_or_create(member=m, alias=r["name"], defaults={"note": "営業件数の資料の表記"})
                row = SalesCount.objects.filter(member=m, month=target, kind=kind, product__isnull=True).first()
                before = row.count if row else None
                if row:
                    row.count, row.source, row.updated_by = count, SalesCount.Source.IMAGE, request.user
                    row.save()
                else:
                    SalesCount.objects.create(member=m, month=target, kind=kind, count=count, source=SalesCount.Source.IMAGE,
                                              updated_by=request.user)
                if before != count:
                    label = SalesCount.Kind(kind).label
                    audit.log("payroll.sales_count", target=m, before={"count": before}, after={"month": target, "kind": label, "count": count},
                              user=request.user, reason="資料から取り込み")
                    for am in ([target] if kind == SalesCount.Kind.CONFIRMED else [target, next_month(target)]):
                        _flag_confirmed(m, am, f"{target:%Y年%m月}の{label}が {before} → {count} に変更", request.user)
                saved += 1
        request.session.pop("counts_import", None)
        messages.success(request, f"{target:%Y年%m月}の{SalesCount.Kind(kind).label}を {saved}人分 保存しました。")
        return redirect(f"/payroll/?month={payroll_month:%Y-%m}")

    ctx = {**month_context(payroll_month), "prev": prev_month(payroll_month)}
    if plan:
        target = date.fromisoformat(plan["month"])
        from apps.attendance import services as attendance_services

        members = Member.objects.filter(status=Member.Status.ACTIVE)
        existing = {s.member_id: s.count for s in SalesCount.objects.filter(month=target, kind=plan["kind"], product__isnull=True)}
        rows = []
        for r in plan["rows"]:
            sys_att = None
            if plan["kind"] == "acquired" and r["member_id"]:
                m = Member.objects.filter(pk=r["member_id"]).first()
                if m:
                    d, _ = attendance_services.actual_attendance_days(m, target)
                    sys_att = float(d) if d is not None else None
            rows.append({**r, "existing": existing.get(r["member_id"]), "sys_att": sys_att,
                         "att_mismatch": sys_att is not None and r["att"] is not None and abs(sys_att - r["att"]) >= 0.5})
        listed = {r["member_id"] for r in plan["rows"] if r["member_id"]}
        missing = [m for m in members.filter(employment_type__in=calc.PAYROLL_TYPES) if m.pk not in listed]
        total_sum = sum(r["count"] for r in plan["rows"])
        ctx.update({"plan": plan, "rows": rows, "members": members, "target": target, "missing": missing,
                    "total_sum": total_sum, "kind_label": SalesCount.Kind(plan["kind"]).label})
    return render(request, "payroll/counts_import.html", ctx)
