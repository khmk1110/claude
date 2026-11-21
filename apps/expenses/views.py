"""資料・経費管理の画面。すべてのビューに追加認証（expenses）を必須にする（ミドルウェアと二重）。"""

from __future__ import annotations

import io
import unicodedata
from collections import defaultdict
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.decorators import stepup_required
from apps.core import audit
from apps.core.monthnav import month_context, month_from_request
from apps.core.months import month_end, parse_month
from apps.members.models import Member

from . import importer, parsers
from .models import DocumentKind, ExpenseCategory, ExpenseIssue, ExpenseRecord, SourceFile

R = ExpenseRecord.Status


def expenses_view(view):
    return login_required(stepup_required("expenses")(view))


def _int(text):
    t = unicodedata.normalize("NFKC", str(text or "")).replace(",", "").replace("円", "").strip()
    if t == "":
        return None
    try:
        return int(t)
    except ValueError:
        raise ValueError(f"「{text}」は金額（整数）で入力してください。")


def _month(text):
    return parse_month(text) if text else None


@expenses_view
def index(request):
    month = month_from_request(request)
    files = SourceFile.objects.filter(
        Q(target_month=month) | Q(target_month__isnull=True, uploaded_at__date__gte=month, uploaded_at__date__lte=month_end(month))
    ).select_related("uploaded_by")
    records = [r for r in ExpenseRecord.objects.select_related("source_file", "member", "auto_member", "category", "auto_category")
               .filter(Q(target_month=month) | Q(target_month__isnull=True, auto_target_month=month)
                       | Q(target_month__isnull=True, auto_target_month__isnull=True, source_file__target_month=month))]
    counts = defaultdict(int)
    for r in records:
        counts[r.status] += 1
    per_member: dict = {}
    cats = list(ExpenseCategory.objects.filter(is_active=True))
    totals = defaultdict(int)
    unreflected = 0
    for r in records:
        if r.status not in (R.APPROVED, R.REFLECTED):
            continue
        m, c = r.effective_member, r.effective_category
        if not (m and c):
            continue
        row = per_member.setdefault(m.pk, {"member": m, "by_cat": defaultdict(int), "total": 0})
        row["by_cat"][c.pk] += r.effective_amount or 0
        row["total"] += r.effective_amount or 0
        totals[c.pk] += r.effective_amount or 0
        if r.status == R.APPROVED:
            unreflected += r.effective_amount or 0
    member_rows = sorted(per_member.values(), key=lambda x: (x["member"].display_order, x["member"].pk))
    for row in member_rows:
        row["cells"] = [row["by_cat"].get(c.pk, 0) for c in cats]
    open_issues = ExpenseIssue.objects.filter(resolved=False).filter(
        Q(record__in=[r.pk for r in records]) | Q(source_file__in=files)).count()
    return render(request, "expenses/index.html", {
        **month_context(month), "files": files, "counts": counts, "status_choices": R.choices,
        "member_rows": member_rows, "cats": cats, "cat_totals": [totals.get(c.pk, 0) for c in cats],
        "grand_total": sum(totals.values()), "unreflected": unreflected, "open_issues": open_issues,
        "unchecked_files": files.exclude(status=SourceFile.Status.PARSED).count(),
        "kinds": DocumentKind.choices,
    })


@expenses_view
@require_POST
def upload(request):
    month_txt = request.POST.get("target_month")
    target = _month(month_txt) if month_txt else None
    kind = request.POST.get("kind") or None
    done, last = 0, None
    if request.POST.get("gsheet_url"):
        try:
            last = importer.import_google_sheet(request.POST["gsheet_url"].strip(), user=request.user, kind=kind, target_month=target)
            done += 1
        except (ValueError, importer.DuplicateFile) as e:
            messages.error(request, str(e))
    for f in request.FILES.getlist("files"):
        try:
            last = importer.import_file(f.read(), f.name, user=request.user, kind=kind, target_month=target)
            done += 1
        except (ValueError, importer.DuplicateFile) as e:
            messages.error(request, str(e))
    if done:
        messages.success(request, f"{done}件の資料を取り込みました。内容を確認してください。")
    if done == 1 and last:
        return redirect("expenses:file", pk=last.pk)
    return redirect(f"/expenses/?month={(target or date.today()).strftime('%Y-%m')}")


@expenses_view
def file_detail(request, pk):
    sf = get_object_or_404(SourceFile.objects.select_related("profile", "uploaded_by"), pk=pk)
    records = list(sf.records.select_related("member", "auto_member", "category", "auto_category", "reflected_statement")
                   .prefetch_related("issues"))
    audit.log("expenses.file_view", target=sf, user=request.user)
    total = sum((r.effective_amount or 0) for r in records)
    pages = []
    if sf.file_format == "pdf":
        try:
            import pdfplumber

            with pdfplumber.open(sf.file.open("rb")) as pdf:
                pages = list(range(1, len(pdf.pages) + 1))
        except Exception:  # noqa: BLE001
            pages = []
    return render(request, "expenses/file.html", {
        "sf": sf, "records": records, "total": total, "pages": pages,
        "file_issues": sf.issues.filter(resolved=False), "members": Member.objects.exclude(status=Member.Status.RETIRED),
        "cats": ExpenseCategory.objects.filter(is_active=True), "kinds": DocumentKind.choices,
        "fields": parsers.FIELDS,
    })


@expenses_view
@require_POST
def file_action(request, pk):
    sf = get_object_or_404(SourceFile, pk=pk)
    p, user = request.POST, request.user
    action = p.get("action")
    ids = [int(x) for x in p.getlist("records") if x.isdigit()]
    selected = list(sf.records.filter(pk__in=ids))
    try:
        if action == "approve":
            ok, skipped = importer.approve(selected, user=user)
            messages.success(request, f"{ok}件を承認しました。")
            for s in skipped[:10]:
                messages.warning(request, f"承認できません：{s}")
        elif action == "exclude":
            n = importer.exclude(selected, user=user, reason=p.get("reason", "").strip())
            messages.success(request, f"{n}件を対象外にしました。")
        elif action == "meta":
            total = p.get("source_total", "").strip()
            importer.set_file_meta(sf, user=user, kind=p.get("kind") or None, target_month=_month(p.get("target_month")),
                                   source_total=_int(total) if total != "" else None)
            messages.success(request, "資料の情報を更新しました。")
        elif action == "reprocess":
            importer.process(sf, user=user)
            messages.success(request, "読み取りをやり直しました。")
        elif action == "add":
            member = get_object_or_404(Member, pk=p.get("member"))
            cat = get_object_or_404(ExpenseCategory, pk=p.get("category"))
            amount = _int(p.get("amount"))
            if amount is None:
                raise ValueError("金額を入力してください。")
            importer.add_manual_record(sf, user=user, member=member, category=cat, amount=amount,
                                       used_on=date.fromisoformat(p["used_on"]) if p.get("used_on") else None,
                                       description=p.get("description", ""), target_month=_month(p.get("target_month")),
                                       reason=p.get("reason", ""))
            messages.success(request, "行を追加しました。")
        else:
            raise ValueError("不明な操作です。")
    except (ValueError, importer.RecordLocked) as e:
        messages.error(request, str(e))
    return redirect("expenses:file", pk=sf.pk)


@expenses_view
@require_POST
def record_action(request, pk):
    rec = get_object_or_404(ExpenseRecord, pk=pk)
    p, user = request.POST, request.user
    action = p.get("action")
    try:
        if action == "update":
            # 画面には現在の値が入っているので、変わった項目だけを修正として渡す
            member = Member.objects.filter(pk=p.get("member")).first() if p.get("member") else None
            cat = ExpenseCategory.objects.filter(pk=p.get("category")).first() if p.get("category") else None
            used_on = date.fromisoformat(p["used_on"]) if p.get("used_on") else None
            desc = p.get("description", "")
            amount = _int(p.get("amount"))
            tm = _month(p.get("target_month"))
            changes = {
                "member": member if member and member != rec.effective_member else None,
                "category": cat if cat and cat != rec.effective_category else None,
                "used_on": used_on if used_on and used_on != rec.effective_used_on else None,
                "description": desc if desc and desc != (rec.description or rec.auto_description) else None,
                # 金額は変更したとき、または「元資料と一致を確認」にチェックしたときに確定する
                "amount": amount if amount is not None and (amount != rec.effective_amount or p.get("confirm_amount")) else None,
                "target_month": tm if tm and tm != rec.effective_target_month else None,
            }
            if all(v is None for v in changes.values()):
                raise ValueError("変更された項目がありません。")
            importer.update_record(rec, user=user, reason=p.get("reason", "").strip(),
                                   save_alias=bool(p.get("save_alias")), **changes)
            messages.success(request, f"{rec.row_number}行目を修正しました。")
        elif action == "resolve":
            issue = get_object_or_404(ExpenseIssue, pk=p.get("issue_id"), record=rec)
            importer.resolve_issue(issue, user=user, note=p.get("note", "").strip())
            messages.success(request, "確認済みにしました。")
        elif action == "unapprove":
            importer.unapprove(rec, user=user, reason=p.get("reason", "").strip())
            messages.success(request, "承認を取り消しました。")
        else:
            raise ValueError("不明な操作です。")
    except (ValueError, importer.RecordLocked) as e:
        messages.error(request, str(e))
    return redirect(f"/expenses/files/{rec.source_file_id}/#r{rec.pk}")


@expenses_view
@require_POST
def file_issue_resolve(request, pk):
    issue = get_object_or_404(ExpenseIssue, pk=pk, record__isnull=True)
    try:
        importer.resolve_issue(issue, user=request.user, note=request.POST.get("note", "").strip())
        messages.success(request, "確認済みにしました。")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("expenses:file", pk=issue.source_file_id)


@expenses_view
def mapping(request, pk):
    sf = get_object_or_404(SourceFile, pk=pk)
    if request.method == "POST":
        col_map = {f: request.POST.get(f"col_{f}") for f in parsers.FIELDS if request.POST.get(f"col_{f}")}
        try:
            importer.save_mapping(
                sf, user=request.user, name=request.POST.get("name", "").strip(), kind=request.POST.get("kind"),
                column_map=col_map, category=ExpenseCategory.objects.filter(pk=request.POST.get("category")).first(),
                payroll_month_offset=int(request.POST.get("offset") or 0),
            )
        except (ValueError, importer.RecordLocked) as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "読み取りルールを保存しました。次回から同じ形式の資料は自動で読み取ります。")
            return redirect("expenses:file", pk=sf.pk)
    return render(request, "expenses/mapping.html", {
        "sf": sf, "fields": parsers.FIELDS.items(), "kinds": DocumentKind.choices, "cats": ExpenseCategory.objects.filter(is_active=True),
    })


@expenses_view
def source_download(request, pk):
    sf = get_object_or_404(SourceFile, pk=pk)
    if not sf.file:
        raise Http404
    audit.log("expenses.file_download", target=sf, user=request.user)
    return FileResponse(sf.file.open("rb"), filename=sf.original_name)


@expenses_view
def page_image(request, pk, page):
    sf = get_object_or_404(SourceFile, pk=pk, file_format="pdf")
    from . import ocr

    try:
        imgs = ocr.render_pages(sf.file.open("rb").read(), dpi=110, first=page, last=page)
    except Exception:  # noqa: BLE001
        raise Http404
    if not imgs:
        raise Http404
    buf = io.BytesIO()
    imgs[0].save(buf, "PNG")
    return HttpResponse(buf.getvalue(), content_type="image/png")
