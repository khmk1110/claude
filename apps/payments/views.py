"""会社の支払い管理の画面。給与のパスワードで守る（/payments/）。"""

from __future__ import annotations

import csv
import io
import re
import threading
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.db import close_old_connections, transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts import stepup
from apps.accounts.decorators import stepup_denied_response
from apps.core import audit
from apps.core.monthnav import month_context, month_from_request
from apps.core.months import month_start, prev_month

from . import invoice
from .models import CATEGORY_CHOICES, Payee, Payment

P = Payment.Status
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".pdf")


def payments_view(view):
    """会社の支払いは給与のパスワードで守る（ミドルウェアと二重）。"""
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not stepup.is_verified(request, "payroll"):
            return stepup_denied_response(request, "payroll")
        return view(request, *args, **kwargs)

    wrapped.stepup_scope = "payroll"
    return wrapped


def _back(month, anchor=""):
    return redirect(f"/payments/?month={month:%Y-%m}{anchor}")


def _int(v):
    try:
        return int(re.sub(r"[,，円¥￥\s]", "", str(v)))
    except (TypeError, ValueError):
        return None


def _apply_payee(p: Payment) -> None:
    """登録済みの支払先なら、分類と振込先を入れる（読み取った振込先がある時はそちらを優先）。"""
    payee = Payee.objects.filter(name=p.payee_name).first() if p.payee_name else None
    if payee:
        p.payee = payee
        p.category = p.category or payee.category
        p.bank = p.bank or payee.bank


def _read_invoice(pk: int) -> None:
    p = Payment.objects.get(pk=pk)
    try:
        with p.file.open("rb") as fh:
            data = fh.read()
        text = invoice.text_from_file(data, p.original_name or p.file.name)
        g = invoice.guess(text, own_company=settings.COMPANY_NAME, known_payees=list(Payee.objects.values_list("name", flat=True)),
                          today=timezone.localdate())
        p.ocr_text = text[:8000]
        p.payee_name = g.payee[:200]
        p.amount = g.amount
        p.due_date = g.due_date
        p.bank = g.bank
        _apply_payee(p)
        p.status = P.DRAFT
        p.error_message = "" if (g.amount and g.payee) else "読み取れなかった項目があります。確認して入力してください。"
    except Exception as e:  # noqa: BLE001
        p.status, p.error_message = P.DRAFT, f"読み取れませんでした（{type(e).__name__}）。手で入力してください。"
    p.save()


def _start_read(pk: int) -> None:
    def run():
        close_old_connections()
        try:
            _read_invoice(pk)
        finally:
            close_old_connections()

    threading.Thread(target=run, daemon=True).start()


@payments_view
def index(request):
    month = month_from_request(request)
    # 止まったままの読み取り（サーバーの再起動など）は、確認待ちにして手で入力できるようにする
    stale = timezone.now() - timedelta(minutes=5)
    Payment.objects.filter(status=P.PROCESSING, created_at__lt=stale).update(
        status=P.DRAFT, error_message="読み取りが終わりませんでした。手で入力してください。")
    items = list(Payment.objects.filter(month=month).select_related("payee"))
    if request.GET.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["支払先", "金額", "分類", "支払期限", "状態", "支払った日", "振込先", "メモ"])
        for p in items:
            w.writerow([p.payee_name, p.amount or 0, p.category, p.due_date or "", p.get_status_display(),
                        timezone.localtime(p.paid_at).strftime("%Y-%m-%d") if p.paid_at else "", p.bank.replace("\n", " "), p.memo])
        from django.utils.http import content_disposition_header

        resp = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = content_disposition_header(True, f"{month.month}月_会社の支払い.csv")
        return resp
    drafts = [p for p in items if p.status in (P.PROCESSING, P.DRAFT)]
    unpaid = [p for p in items if p.status == P.UNPAID]
    paid = [p for p in items if p.status == P.PAID]
    total = sum(p.amount or 0 for p in unpaid + paid)
    by_cat: dict[str, int] = {}
    for p in unpaid + paid:
        by_cat[p.category or "未分類"] = by_cat.get(p.category or "未分類", 0) + (p.amount or 0)
    monthly = Payee.objects.filter(monthly=True).exclude(payments__month=month)
    return render(request, "payments/index.html", {
        **month_context(month), "drafts": drafts, "unpaid": unpaid, "paid": paid, "total": total,
        "unpaid_total": sum(p.amount or 0 for p in unpaid), "by_cat": sorted(by_cat.items(), key=lambda x: -x[1]),
        "monthly_missing": list(monthly), "processing": any(p.status == P.PROCESSING for p in drafts),
        "categories": CATEGORY_CHOICES, "payees": Payee.objects.all(),
    })


@payments_view
@require_POST
def upload(request):
    month = month_from_request(request)
    files = request.FILES.getlist("files")
    n = 0
    for f in files[:10]:
        if not f.name.lower().endswith(IMAGE_EXT):
            messages.error(request, f"{f.name}: 写真（PNG/JPEG）かPDFを選んでください。")
            continue
        p = Payment.objects.create(month=month, status=P.PROCESSING, file=f, original_name=f.name[:255], created_by=request.user)
        audit.log("payments.upload", target=p, after={"file": f.name}, user=request.user)
        if getattr(settings, "OCR_IN_BACKGROUND", True):
            transaction.on_commit(lambda pk=p.pk: _start_read(pk))
        else:
            _read_invoice(p.pk)
        n += 1
    if n:
        messages.success(request, f"{n}件の請求書を読み取っています。終わったら「確認待ち」に出ます。")
    return _back(month)


@payments_view
def edit(request, pk=None):
    p = get_object_or_404(Payment, pk=pk) if pk else Payment(month=month_from_request(request), status=P.UNPAID)
    if request.method == "POST":
        before = {"支払先": p.payee_name, "金額": p.amount, "状態": p.get_status_display()} if p.pk else None
        if request.POST.get("action") == "delete" and p.pk:
            if p.status == P.PAID:
                messages.error(request, "支払い済みの支払いは消せません（先に「未払いに戻す」）。")
                return redirect("payments:edit", pk=p.pk)
            audit.log("payments.delete", target=p, before=before, user=request.user)
            month = p.month
            p.delete()
            messages.success(request, "削除しました。")
            return _back(month)
        name = request.POST.get("payee_name", "").strip()
        amount = _int(request.POST.get("amount"))
        if not name or amount is None:
            messages.error(request, "支払先と金額を入れてください。")
        else:
            p.payee_name = name[:200]
            p.amount = amount
            p.category = request.POST.get("category", "").strip()[:50]
            p.bank = request.POST.get("bank", "").strip()[:500]
            p.memo = request.POST.get("memo", "").strip()[:255]
            due = request.POST.get("due_date") or None
            p.due_date = date.fromisoformat(due) if due else None
            m = request.POST.get("month")
            if m:
                p.month = month_start(date.fromisoformat(m + "-01"))
            if p.status in (P.DRAFT, P.PROCESSING):
                p.status = P.UNPAID
            p.error_message = ""
            if not p.created_by_id:
                p.created_by = request.user
            # 支払先を覚える（次回から分類・振込先を自動で入れる）
            payee, _ = Payee.objects.get_or_create(name=p.payee_name)
            payee.category = p.category or payee.category
            payee.bank = p.bank or payee.bank
            if request.POST.get("monthly"):
                payee.monthly, payee.monthly_amount = True, p.amount
            elif request.POST.get("monthly_shown"):
                payee.monthly = False
            payee.save()
            p.payee = payee
            p.save()
            audit.log("payments.save", target=p, before=before,
                      after={"支払先": p.payee_name, "金額": p.amount, "分類": p.category, "状態": p.get_status_display()}, user=request.user)
            messages.success(request, f"{p.payee_name} {p.amount:,}円 を保存しました。")
            return _back(p.month)
    return render(request, "payments/edit.html", {
        "p": p, "categories": CATEGORY_CHOICES, "payees": Payee.objects.all(),
        "month": p.month, "month_param": f"{p.month:%Y-%m}",
        "is_monthly": bool(p.payee and p.payee.monthly),
    })


@payments_view
@require_POST
def toggle_paid(request, pk):
    p = get_object_or_404(Payment, pk=pk)
    if p.status not in (P.UNPAID, P.PAID):
        return JsonResponse({"detail": "先に内容を確認して保存してください。"}, status=400)
    on = request.POST.get("on") == "1"
    p.status = P.PAID if on else P.UNPAID
    p.paid_at, p.paid_by = (timezone.now(), request.user) if on else (None, None)
    p.save()
    audit.log("payments.paid" if on else "payments.unpaid", target=p, after={"支払先": p.payee_name, "金額": p.amount}, user=request.user)
    return JsonResponse({"ok": True, "on": on, "at": timezone.localtime(p.paid_at).strftime("%-m/%-d") if on else ""})


@payments_view
@require_POST
def add_monthly(request):
    """毎月の支払い（家賃など）を、この月の分としてまとめて入れる。"""
    month = month_from_request(request)
    n = 0
    for payee in Payee.objects.filter(monthly=True).exclude(payments__month=month):
        Payment.objects.create(month=month, payee=payee, payee_name=payee.name, amount=payee.monthly_amount,
                               category=payee.category, bank=payee.bank, status=P.UNPAID, created_by=request.user)
        n += 1
    audit.log("payments.add_monthly", target_type="payments_month", target_id=month.isoformat(), after={"件数": n}, user=request.user)
    messages.success(request, f"毎月の支払いを {n}件 入れました。金額が違う月は押して直してください。")
    return _back(month)


@payments_view
def invoice_file(request, pk):
    p = get_object_or_404(Payment, pk=pk)
    if not p.file:
        raise Http404
    return FileResponse(p.file.open("rb"), filename=p.original_name or "invoice", as_attachment=False)


@payments_view
@require_POST
def import_sheet(request):
    """今まで使っていた収支表（Excel）の「支払い入力」を取り込む。同じ月・同じ支払先・同じ金額は二重に入れない。"""
    import openpyxl

    f = request.FILES.get("file")
    month = month_from_request(request)
    if not f or not f.name.lower().endswith(".xlsx"):
        messages.error(request, "Excel（.xlsx）を選んでください。")
        return _back(month)
    wb = openpyxl.load_workbook(f, data_only=True, read_only=True)
    added = 0
    with transaction.atomic():
        for ws in wb.worksheets:
            title = str(ws.cell(1, 1).value or "")
            m = re.search(r"(20\d{2})年\s*(\d{1,2})月", title)
            if not m or "収支" not in title:
                continue
            sheet_month = date(int(m[1]), int(m[2]), 1)
            for row in ws.iter_rows(min_row=9, min_col=4, max_col=8, values_only=True):
                name, amount, status, cat, note = (list(row) + [None] * 5)[:5]
                amount = _int(amount)
                if not name or amount is None:
                    continue
                name = str(name).strip()
                if Payment.objects.filter(month=sheet_month, payee_name=name, amount=amount).exists():
                    continue
                payee, _ = Payee.objects.get_or_create(name=name, defaults={"category": str(cat or "")[:50]})
                paid = "済" in str(status or "")
                Payment.objects.create(month=sheet_month, payee=payee, payee_name=name, amount=amount,
                                       category=str(cat or payee.category or "")[:50], memo=str(note or "")[:255],
                                       status=P.PAID if paid else P.UNPAID, paid_at=timezone.now() if paid else None,
                                       created_by=request.user)
                added += 1
    audit.log("payments.import_sheet", target_type="payments", target_id="sheet", after={"file": f.name, "件数": added}, user=request.user)
    messages.success(request, f"シートから {added}件 取り込みました。")
    return _back(month)
