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
from .models import CATEGORY_CHOICES, Payee, Payment, Sale

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


def payroll_row(month):
    """給与（個人事業主・インターン）の確定額。確定した明細から自動で出す（手で入れない）。"""
    from apps.payroll.models import PayrollStatement, StatementDelivery

    sts = list(PayrollStatement.objects.filter(month=month, status="confirmed"))
    if not sts:
        return None
    if Payment.objects.filter(month=month, payee_name__regex=r"(個人事業主|インターン).*給与").exclude(status=P.DRAFT).exists():
        return None  # シートから取り込んだ・手で入れた給与の行がある月は、二重に数えない
    paid = StatementDelivery.objects.filter(statement__in=sts, paid_at__isnull=False).count()
    return {"amount": sum(st.net_amount or 0 for st in sts), "count": len(sts), "paid_count": paid,
            "paid": paid == len(sts), "unconfirmed": PayrollStatement.objects.filter(month=month).exclude(status="confirmed").count()}


def month_totals(month):
    """その月の売上・支払い・差引（支払いには給与の確定額を含む）。"""
    pays = Payment.objects.filter(month=month, status__in=[P.UNPAID, P.PAID])
    pr = payroll_row(month)
    pay_total = sum(p.amount or 0 for p in pays) + (pr["amount"] if pr else 0)
    sales = Sale.objects.filter(month=month, state=Sale.State.OK)
    sale_total = sum(x.amount or 0 for x in sales)
    return {"sales": sale_total, "payments": pay_total, "diff": sale_total - pay_total,
            "unpaid": sum(p.amount or 0 for p in pays if p.status == P.UNPAID) + (pr["amount"] if pr and not pr["paid"] else 0),
            "unreceived": sum(x.amount or 0 for x in sales if not x.received)}


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
    pr = payroll_row(month)
    totals = month_totals(month)
    total = totals["payments"]
    by_cat: dict[str, int] = {}
    for p in unpaid + paid:
        by_cat[p.category or "未分類"] = by_cat.get(p.category or "未分類", 0) + (p.amount or 0)
    monthly = Payee.objects.filter(monthly=True).exclude(payments__month=month)
    if pr:
        by_cat["給与"] = by_cat.get("給与", 0) + pr["amount"]
    return render(request, "payments/index.html", {
        **month_context(month), "drafts": drafts, "unpaid": unpaid, "paid": paid, "total": total, "pr": pr, "totals": totals,
        "tab": "pay", "unpaid_total": totals["unpaid"], "by_cat": sorted(by_cat.items(), key=lambda x: -x[1]),
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
            for row in ws.iter_rows(min_row=9, min_col=1, max_col=3, values_only=True):
                client, amount, status = (list(row) + [None] * 3)[:3]
                amount = _int(amount)
                if not client or amount is None or Sale.objects.filter(month=sheet_month, client=str(client).strip(), amount=amount).exists():
                    continue
                done = "済" in str(status or "")
                Sale.objects.create(month=sheet_month, client=str(client).strip()[:200], amount=amount, received=done,
                                    received_at=timezone.now() if done else None, created_by=request.user)
                added += 1
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



def _read_sale(pk: int) -> None:
    x = Sale.objects.get(pk=pk)
    try:
        with x.file.open("rb") as fh:
            data = fh.read()
        text = invoice.text_from_file(data, x.original_name or x.file.name)
        clients = sorted(set(Sale.objects.exclude(client="").values_list("client", flat=True)))
        g = invoice.guess_sale(text, own_company=settings.COMPANY_NAME, known_clients=clients, today=timezone.localdate())
        x.ocr_text, x.client, x.amount, x.due_date = text[:8000], g.payee[:200], g.amount, g.due_date
        x.error_message = "" if (g.amount and g.payee) else "読み取れなかった項目があります。確認して入力してください。"
    except Exception as e:  # noqa: BLE001
        x.error_message = f"読み取れませんでした（{type(e).__name__}）。手で入力してください。"
    x.state = Sale.State.DRAFT
    x.save()


def _start_sale_read(pk: int) -> None:
    def run():
        close_old_connections()
        try:
            _read_sale(pk)
        finally:
            close_old_connections()

    threading.Thread(target=run, daemon=True).start()


@payments_view
@require_POST
def sale_upload(request):
    month = month_from_request(request)
    n = 0
    for f in request.FILES.getlist("files")[:10]:
        if not f.name.lower().endswith(IMAGE_EXT):
            messages.error(request, f"{f.name}: 写真（PNG/JPEG）かPDFを選んでください。")
            continue
        x = Sale.objects.create(month=month, state=Sale.State.PROCESSING, file=f, original_name=f.name[:255], created_by=request.user)
        audit.log("payments.sale_upload", target=x, after={"file": f.name}, user=request.user)
        if getattr(settings, "OCR_IN_BACKGROUND", True):
            transaction.on_commit(lambda pk=x.pk: _start_sale_read(pk))
        else:
            _read_sale(x.pk)
        n += 1
    if n:
        messages.success(request, f"{n}件を読み取っています。終わったら「確認待ち」に出ます。")
    return redirect(f"/payments/sales/?month={month:%Y-%m}")


@payments_view
def sale_file(request, pk):
    x = get_object_or_404(Sale, pk=pk)
    if not x.file:
        raise Http404
    return FileResponse(x.file.open("rb"), filename=x.original_name or "file", as_attachment=False)


@payments_view
def sales(request):
    month = month_from_request(request)
    stale = timezone.now() - timedelta(minutes=5)
    Sale.objects.filter(state=Sale.State.PROCESSING, created_at__lt=stale).update(
        state=Sale.State.DRAFT, error_message="読み取りが終わりませんでした。手で入力してください。")
    items = list(Sale.objects.filter(month=month))
    ok = [x for x in items if x.state == Sale.State.OK]
    drafts = [x for x in items if x.state != Sale.State.OK]
    return render(request, "payments/sales.html", {
        **month_context(month), "tab": "sales", "totals": month_totals(month), "drafts": drafts,
        "processing": any(x.state == Sale.State.PROCESSING for x in drafts),
        "waiting": [x for x in ok if not x.received], "received": [x for x in ok if x.received],
    })


@payments_view
def sale_edit(request, pk=None):
    x = get_object_or_404(Sale, pk=pk) if pk else Sale(month=month_from_request(request))
    if request.method == "POST":
        if request.POST.get("action") == "delete" and x.pk:
            audit.log("payments.sale_delete", target=x, before={"取引先": x.client, "金額": x.amount}, user=request.user)
            month = x.month
            x.delete()
            messages.success(request, "削除しました。")
            return redirect(f"/payments/sales/?month={month:%Y-%m}")
        client = request.POST.get("client", "").strip()
        amount = _int(request.POST.get("amount"))
        if not client or amount is None:
            messages.error(request, "取引先と金額を入れてください。")
        else:
            before = {"取引先": x.client, "金額": x.amount} if x.pk else None
            x.client, x.amount, x.memo = client[:200], amount, request.POST.get("memo", "").strip()[:255]
            due = request.POST.get("due_date") or None
            x.due_date = date.fromisoformat(due) if due else None
            x.state, x.error_message = Sale.State.OK, ""
            m = request.POST.get("month")
            if m:
                x.month = month_start(date.fromisoformat(m + "-01"))
            received = bool(request.POST.get("received"))
            if received and not x.received:
                x.received_at = timezone.now()
            if not received:
                x.received_at = None
            x.received = received
            if not x.created_by_id:
                x.created_by = request.user
            x.save()
            audit.log("payments.sale_save", target=x, before=before, after={"取引先": x.client, "金額": x.amount,
                                                                            "入金": "済" if x.received else "未"}, user=request.user)
            messages.success(request, f"{x.client} {x.amount:,}円 を保存しました。")
            return redirect(f"/payments/sales/?month={x.month:%Y-%m}")
    return render(request, "payments/sale_edit.html", {"x": x, "month": x.month, "month_param": f"{x.month:%Y-%m}",
                                                       "clients": sorted(set(Sale.objects.values_list("client", flat=True)))})


@payments_view
@require_POST
def sale_toggle(request, pk):
    x = get_object_or_404(Sale, pk=pk)
    on = request.POST.get("on") == "1"
    x.received, x.received_at = on, (timezone.now() if on else None)
    x.save()
    audit.log("payments.sale_received" if on else "payments.sale_unreceived", target=x,
              after={"取引先": x.client, "金額": x.amount}, user=request.user)
    return JsonResponse({"ok": True, "on": on})


@payments_view
def yearly(request):
    """年間収支：月ごとの売上・支払い・差引（直近12か月）。"""
    month = month_from_request(request)
    months = [month]
    for _ in range(11):
        months.append(prev_month(months[-1]))
    months.reverse()
    rows = [{"month": m, **month_totals(m)} for m in months]
    rows = [r for r in rows if r["sales"] or r["payments"]] or rows[-1:]
    return render(request, "payments/yearly.html", {
        **month_context(month), "tab": "year", "rows": rows,
        "sum": {k: sum(r[k] for r in rows) for k in ("sales", "payments", "diff")},
    })
