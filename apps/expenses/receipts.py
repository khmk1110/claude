"""レシート（写真）からその他の経費を取り込む。名前は書いていないので、先に選んだ人の経費にする。

金額（合計）と日付を読み取り、「確認待ち」にする（承認するまで給与には入らない）。読み取りは1枚ずつ順番に行う。
"""

from __future__ import annotations

import hashlib
import queue
import re
import threading

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections
from django.utils import timezone

from apps.core import audit

from .models import DocumentKind, ExpenseIssue, ExpenseRecord, SourceFile

RECEIPT_KEYS = ("合計", "お会計", "御会計", "お買上計", "お買上げ計", "領収金額", "ご請求額", "お支払い", "お支払金額", "現計", "計")

_jobs: "queue.Queue" = queue.Queue()
_lock = threading.Lock()
_worker = None


def _read(sf_id: int) -> None:
    from apps.payments import invoice

    sf = SourceFile.objects.get(pk=sf_id)
    rec = sf.records.first()
    try:
        with sf.file.open("rb") as fh:
            data = fh.read()
        text = invoice.text_from_file(data, sf.original_name)
        lines = [invoice.nfkc(l).replace(" ", "") for l in text.splitlines() if l.strip()]
        amount = None
        for key in RECEIPT_KEYS:
            vals = []
            for i, l in enumerate(lines):
                if key in l and not re.search(r"(小計|対象|内税|消費税|税率|お預り|お預かり|釣|ポイント)", l):
                    vals += invoice._amounts(l[l.find(key):]) or (invoice._amounts(lines[i + 1]) if i + 1 < len(lines) else [])
            if vals:
                amount = max(vals)
                break
        if amount is None:
            allv = [v for l in lines for v in invoice._amounts(l) if v < 10_000_000]
            amount = max(allv) if allv else None
        used = next((d for l in lines for d in [invoice._date(l, timezone.localdate())] if d), None)
        store = next((l for l in lines[:6] if len(re.sub(r"[^\wぁ-んァ-ヶ一-龠]", "", l)) >= 2 and not re.search(r"\d{3,}", l)), "")
        rec.auto_amount, rec.auto_used_on, rec.auto_description = amount, used, store[:255]
        rec.raw = {"読み取った文字": text[:3000]}
        rec.save()
        sf.status, sf.error_message = SourceFile.Status.PARSED, ""
        sf.save(update_fields=["status", "error_message"])
        msg = "レシートから読み取りました。金額を確認してください。" if amount else "金額を読み取れませんでした。金額を入れてください。"
        ExpenseIssue.objects.create(record=rec, source_file=sf, code=ExpenseIssue.Code.LOW_CONFIDENCE, message=msg)
    except Exception as e:  # noqa: BLE001
        sf.status, sf.error_message = SourceFile.Status.PARSED, f"読み取れませんでした（{type(e).__name__}）。金額を入れてください。"
        sf.save(update_fields=["status", "error_message"])
        ExpenseIssue.objects.create(record=rec, source_file=sf, code=ExpenseIssue.Code.LOW_CONFIDENCE, message="金額を入れてください。")


def _loop():
    while True:
        sf_id = _jobs.get()
        close_old_connections()
        try:
            _read(sf_id)
        except Exception:  # noqa: BLE001
            pass
        finally:
            close_old_connections()
            _jobs.task_done()


def enqueue(sf_id: int) -> None:
    global _worker
    if not getattr(settings, "OCR_IN_BACKGROUND", True):
        _read(sf_id)
        return
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, daemon=True, name="receipt-reader")
            _worker.start()
    _jobs.put(sf_id)


def add_receipts(files, *, member, category, month, user) -> tuple[int, list[str]]:
    """選んだ人・項目のレシートを取り込む（1枚＝1件）。同じ写真は二重に取り込まない。"""
    added, skipped = 0, []
    for f in files[:20]:
        data = f.read()
        sha = hashlib.sha256(data).hexdigest()
        if SourceFile.objects.filter(sha256=sha).exists():
            skipped.append(f"{f.name}（取り込み済み）")
            continue
        ext = (f.name.rsplit(".", 1)[-1] if "." in f.name else "jpg").lower()
        fmt = SourceFile.Format.PDF if ext == "pdf" else SourceFile.Format.IMAGE
        sf = SourceFile(original_name=f.name[:255], sha256=sha, size=len(data), file_format=fmt, uploaded_by=user,
                        target_month=month, document_kind=DocumentKind.OTHER_EXPENSE, document_kind_auto=DocumentKind.OTHER_EXPENSE,
                        status=SourceFile.Status.PROCESSING, row_count=1)
        sf.file.save(f"{sha[:16]}.{ext}", ContentFile(data), save=False)
        sf.save()
        rec = ExpenseRecord(source_file=sf, row_number=1, member=member, category=category, auto_category=category,
                            auto_target_month=month, name_text=member.name, status=ExpenseRecord.Status.PENDING,
                            dedupe_key=f"receipt:{sha[:40]}", override_reason="レシートの取り込み（人を選んで取り込み）")
        rec.save()
        audit.log("expenses.receipt", target=sf, after={"対象者": member.name, "項目": category.name, "file": f.name}, user=user)
        enqueue(sf.pk)
        added += 1
    return added, skipped
