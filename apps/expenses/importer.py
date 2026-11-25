"""資料の取り込み・振り分け・確認・承認。

- 同じファイルは2回取り込まない（SHA-256）
- 氏名・カード識別4桁でメンバーに振り分け、迷うものは「確認待ち」にする（推測で確定しない）
- 日付と金額が同じというだけで削除しない（重複の疑いとして表示するだけ）
- 承認済みだけが給与計算に反映される。給与反映済みのデータは変更できない
- 給与確定後に経費が追加・修正された場合は、給与側に「要確認」を立てて通知する（金額は自動で変えない）
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from apps.core import audit
from apps.core.months import month_start
from apps.members.matching import match_member
from apps.members.models import CardCode, EmploymentType, Member, MemberAlias

from . import parsers
from .models import DocumentKind, ExpenseCategory, ExpenseIssue, ExpenseRecord, ImportProfile, SourceFile

R = ExpenseRecord.Status
Code = ExpenseIssue.Code
MAX_BYTES = 20 * 1024 * 1024
FORMATS = {".xlsx": "xlsx", ".xls": "xls", ".csv": "csv", ".pdf": "pdf",
           ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image"}
LOW_CONFIDENCE = 0.8


class DuplicateFile(Exception):
    def __init__(self, existing: SourceFile):
        super().__init__(f"同じファイルはすでに取り込み済みです（{existing.original_name}・{existing.uploaded_at:%Y-%m-%d}）。")
        self.existing = existing


class RecordLocked(Exception):
    pass


def add_months(m: date, n: int) -> date:
    y, mo = divmod(m.month - 1 + n, 12)
    return date(m.year + y, mo + 1, 1)


# ---------------------------------------------------------------------------
# 取り込み
# ---------------------------------------------------------------------------
@transaction.atomic
def import_file(data: bytes, filename: str, *, user=None, kind: str | None = None, target_month: date | None = None,
                gsheet_url: str = "") -> SourceFile:
    ext = os.path.splitext(filename.lower())[1]
    if gsheet_url:
        fmt = SourceFile.Format.GSHEET
    elif ext in FORMATS:
        fmt = FORMATS[ext]
    else:
        raise ValueError(f"{filename}: 対応していない形式です（Excel・CSV・PDF・画像（PNG/JPEG）のみ）。")
    if len(data) > MAX_BYTES:
        raise ValueError(f"{filename}: ファイルが大きすぎます（20MBまで）。")
    sha = hashlib.sha256(data).hexdigest()
    existing = SourceFile.objects.filter(sha256=sha).first()
    if existing:
        audit.log("expenses.duplicate_file", target=existing, after={"filename": filename}, user=user)
        wrong_kind = kind in DocumentKind.values and kind != DocumentKind.UNKNOWN and existing.document_kind != kind
        stuck = existing.status == SourceFile.Status.PROCESSING and (existing.processing_started_at or existing.uploaded_at) < timezone.now() - timedelta(minutes=3)
        failed = stuck or existing.status == SourceFile.Status.ERROR or (existing.status == SourceFile.Status.PARSED and not existing.row_count)
        locked = existing.records.exclude(status__in=[R.IMPORTED, R.UNCHECKED, R.PENDING]).exists()
        if (wrong_kind or failed) and not locked:
            # 同じファイルを別の種類（例: ガソリン代→交通費）で選び直した時や、前回読めなかった時は、読み直す
            before = {"kind": existing.document_kind, "target_month": existing.target_month}
            if wrong_kind:
                existing.document_kind = kind
                existing.document_kind_auto = kind
            if target_month:
                existing.target_month = target_month
            existing.save()
            audit.log("expenses.reimport", target=existing, before=before,
                      after={"kind": existing.document_kind, "target_month": existing.target_month}, user=user)
            if existing.file_format in ("image", "pdf") and getattr(settings, "OCR_IN_BACKGROUND", True):
                existing.status, existing.processing_started_at = SourceFile.Status.PROCESSING, timezone.now()
                existing.save(update_fields=["status", "processing_started_at"])
                transaction.on_commit(lambda: _start_background(existing.pk))
                return existing
            process(existing, data=data, user=user)
            return existing
        raise DuplicateFile(existing)
    sf = SourceFile(original_name=filename[:255], sha256=sha, size=len(data), file_format=fmt, gsheet_url=gsheet_url,
                    uploaded_by=user, target_month=target_month)
    sf.file.save(f"{sha[:16]}{ext or '.csv'}", ContentFile(data), save=False)
    if kind in DocumentKind.values and kind != DocumentKind.UNKNOWN:
        sf.document_kind = kind
    sf.save()
    audit.log("expenses.import", target=sf, after={"filename": filename, "format": fmt, "size": len(data)}, user=user)
    if fmt in ("image", "pdf") and getattr(settings, "OCR_IN_BACKGROUND", True):
        # 画像の読み取り（OCR）は時間がかかるため、保存後に裏で読み取る（画面は「読み取り中」と表示）
        sf.status, sf.processing_started_at = SourceFile.Status.PROCESSING, timezone.now()
        sf.save(update_fields=["status", "processing_started_at"])
        transaction.on_commit(lambda: _start_background(sf.pk))
        return sf
    process(sf, data=data, user=user)
    return sf


def _start_background(pk: int) -> None:
    """裏で読み取る。全部の読み取りを1本の順番待ちに入れる（同時に読むと遅くなって止まるため）。"""
    from apps.core import jobs

    jobs.submit(("expense", pk), _process_in_background, pk)


def _process_in_background(pk: int) -> None:
    try:
        sf = SourceFile.objects.get(pk=pk)
        if sf.status != SourceFile.Status.PROCESSING:
            return
        process(sf)
    except Exception as e:  # noqa: BLE001
        SourceFile.objects.filter(pk=pk).update(status=SourceFile.Status.ERROR, error_message=f"読み取れませんでした: {e}")


STUCK_AFTER = timedelta(minutes=8)


def recover_processing(files=None) -> None:
    """「読み取り中」のまま止まったファイルを直す。

    サーバーの再起動（更新・メモリ不足）で裏の読み取りが消えると、ずっと「読み取り中」のままになる。
    画面を開いた時に、順番待ちに無いものは入れ直す。長く終わらないものは「読み取れなかった」にして、
    もう一度読むか消すかを選べるようにする（グルグルが終わらない状態を残さない）。
    """
    from apps.core import jobs

    if not getattr(settings, "OCR_IN_BACKGROUND", True):
        return
    qs = files if files is not None else SourceFile.objects.filter(status=SourceFile.Status.PROCESSING)
    now = timezone.now()
    for sf in qs:
        if sf.status != SourceFile.Status.PROCESSING or jobs.is_queued(("expense", sf.pk)) or jobs.is_queued(("receipt", sf.pk)):
            continue
        started = sf.processing_started_at or sf.uploaded_at
        if now - started > STUCK_AFTER:
            SourceFile.objects.filter(pk=sf.pk, status=SourceFile.Status.PROCESSING).update(
                status=SourceFile.Status.ERROR, error_message="読み取りが途中で止まりました。「もう一度読み取る」を押してください。")
            sf.status = SourceFile.Status.ERROR
            sf.error_message = "読み取りが途中で止まりました。「もう一度読み取る」を押してください。"
        elif sf.document_kind == DocumentKind.OTHER_EXPENSE:
            from . import receipts

            receipts.enqueue(sf.pk)
        else:
            _start_background(sf.pk)


def import_google_sheet(url: str, *, user=None, kind=None, target_month=None) -> SourceFile:
    """リンク共有されたスプレッドシートを CSV として取り込む（Google Sheets API 未設定でも使える）。"""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_\-]+)", url)
    if not m:
        raise ValueError("GoogleスプレッドシートのURLではありません。")
    gid = urllib.parse.parse_qs(urllib.parse.urlparse(url).fragment).get("gid", ["0"])[0]
    export = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid}"
    try:
        with urllib.request.urlopen(export, timeout=20) as res:
            data = res.read(MAX_BYTES + 1)
    except Exception as e:  # noqa: BLE001
        raise ValueError("スプレッドシートを読み込めませんでした。共有設定を「リンクを知っている全員（閲覧者）」にするか、"
                         "CSV／Excel でダウンロードしてアップロードしてください。") from e
    return import_file(data, f"Googleスプレッドシート_{m.group(1)[:8]}.csv", user=user, kind=kind,
                       target_month=target_month, gsheet_url=url)


def _builtin_profile(parser: str) -> ImportProfile | None:
    return ImportProfile.objects.filter(header_signature=f"builtin:{parser}", is_active=True).first()


def _category_for(sf: SourceFile, profile: ImportProfile | None) -> ExpenseCategory | None:
    if profile and profile.default_category_id:
        return profile.default_category
    return ExpenseCategory.objects.filter(document_kind=sf.document_kind, is_active=True).order_by("display_order").first()


@transaction.atomic
def process(sf: SourceFile, *, data: bytes | None = None, user=None) -> SourceFile:
    """資料を読み取って明細行を作る（読み取りルールの登録後に再実行してもよい）。"""
    if sf.records.exclude(status__in=[R.IMPORTED, R.UNCHECKED, R.PENDING]).exists():
        raise RecordLocked("修正・承認済みの行があるため、読み取りをやり直せません。")
    sf.records.all().delete()
    sf.issues.all().delete()
    data = data if data is not None else sf.file.open("rb").read()
    sf.error_message = ""
    try:
        doc, profile = _parse(sf, data)
    except parsers.NeedsMapping as e:
        sf.headers = e.headers
        sf.status = SourceFile.Status.NEEDS_MAPPING
        sf.save()
        return sf
    except Exception as e:  # noqa: BLE001 — 読めない資料はエラーとして残し、手入力できるようにする
        import subprocess

        sf.status = SourceFile.Status.ERROR
        if isinstance(e, (subprocess.TimeoutExpired, subprocess.CalledProcessError)):
            sf.error_message = "画像の読み取りに時間がかかりすぎました（スクショで取り込むと確実です）。"
        else:
            sf.error_message = f"読み取れませんでした: {e}"
        sf.save()
        return sf

    sf.profile, sf.parser, sf.used_ocr = profile, doc.parser, doc.used_ocr
    sf.headers = doc.headers
    if doc.document_month is None:
        months = [month_start(r.used_on) for r in doc.rows if r.used_on]
        if months:
            doc.document_month = max(set(months), key=months.count)  # 利用日の最も多い月
    if doc.document_month:
        sf.document_month = doc.document_month
    offset = int((profile.options or {}).get("payroll_month_offset", 0)) if profile else 0
    if sf.target_month is None and sf.document_month:
        sf.target_month = add_months(sf.document_month, offset)
    if doc.source_total is not None:
        sf.source_total = doc.source_total
    sf.row_count = len(doc.rows)
    sf.status = SourceFile.Status.PARSED if sf.document_kind != DocumentKind.UNKNOWN else SourceFile.Status.NEEDS_KIND
    sf.save()
    for w in doc.warnings:
        ExpenseIssue.objects.create(source_file=sf, code=Code.LOW_CONFIDENCE, message=w[:255])
    if getattr(doc, "warnings_ok", ""):
        sf.error_message = ""
        sf.save(update_fields=["error_message"])
        audit.log("expenses.ocr_verified", target=sf, after={"note": doc.warnings_ok})
    if not doc.rows and getattr(doc, "extracted_text", None) is not None:
        # 何も読み取れなかった時は、読み取った文字の先頭を残して原因を確かめられるようにする
        snippet = " / ".join(l.strip() for l in doc.extracted_text.splitlines() if l.strip())[:400]
        sf.error_message = f"読み取った文字（先頭）: {snippet or '（文字が見つかりませんでした）'}"
        sf.save(update_fields=["error_message"])
    _create_records(sf, doc, _category_for(sf, profile))
    check_file(sf)
    return sf


def _create_records(sf: SourceFile, doc, category) -> None:
    for row in doc.rows:
        rec = ExpenseRecord(
            source_file=sf, row_number=row.row_number, raw=row.raw, auto_category=category,
            auto_used_on=row.used_on, auto_description=row.description[:255], auto_route=row.route[:255],
            auto_amount=row.amount, auto_target_month=sf.target_month, name_text=row.name[:100],
            member_no_text=row.member_no[:20], card_code=row.card_code, card_last4=row.card_last4,
            transaction_id=row.txn_id[:100], confidence=row.confidence,
        )
        rec.dedupe_key = dedupe_key(rec, sf)
        rec.save()
        evaluate(rec, initial=True)


LINE_ROW = re.compile(r"^(?P<name>[^\d\s,:：]+(?:[ \u3000][^\d\s,:：]+)?)[\s,:：]*(?P<rest>.*)$")


def parse_line_text(text: str) -> tuple[list, list]:
    """『青木 600』『井上 5,950円 交通費』のような文面を（氏名, 金額, 元の行）に分ける。"""
    rows, bad = [], []
    for raw in text.splitlines():
        line = parsers.nfkc(raw)
        if not line:
            continue
        m = LINE_ROW.match(line)
        nums = re.findall(r"-?\d[\d,]*", m.group("rest")) if m else []
        if not m or not nums:
            bad.append(raw.strip())
            continue
        amount = parsers.parse_amount(nums[0])
        rest = re.sub(r"-?\d[\d,]*\s*円?", "", m.group("rest"), count=1).strip()
        rows.append((m.group("name"), amount, raw.strip(), rest))
    return rows, bad


@transaction.atomic
def import_line_text(text: str, *, category: ExpenseCategory, target_month: date, user=None, evidence=None,
                     note: str = "") -> SourceFile:
    """LINE などで別途届いた経費の文面を取り込む（1行＝1人分）。"""
    rows, bad = parse_line_text(text)
    if not rows:
        raise ValueError("「氏名 金額」の形で入力してください（例: 青木 600）。")
    target_month = month_start(target_month)
    now = timezone.now()
    sha = hashlib.sha256(f"line|{text}|{now.isoformat()}".encode()).hexdigest()
    sf = SourceFile(original_name=f"LINE等の経費 {category.name} {target_month:%Y年%m月}（{now:%m/%d %H:%M}）",
                    sha256=sha, file_format=SourceFile.Format.MANUAL, document_kind=category.document_kind,
                    document_kind_auto=category.document_kind, target_month=target_month, parser="line_text",
                    status=SourceFile.Status.PARSED, uploaded_by=user, row_count=len(rows))
    if evidence is not None:
        sf.file.save(f"line_{sha[:16]}{os.path.splitext(evidence.name)[1].lower()}", evidence, save=False)
    sf.save()
    doc = parsers.ParsedDocument("line_text")
    for n, (name, amount, raw, rest) in enumerate(rows, start=1):
        doc.rows.append(parsers.ParsedRow(row_number=n, raw={"行": raw, "メモ": note}, name=name, amount=amount,
                                          description=(rest or note or category.name)[:255]))
    _create_records(sf, doc, category)
    for b in bad:
        ExpenseIssue.objects.create(source_file=sf, code=Code.LOW_CONFIDENCE, message=f"読み取れない行があります: {b}"[:255])
    audit.log("expenses.import_line", target=sf, after={"rows": len(rows), "category": category.name}, user=user)
    return sf


@transaction.atomic
def import_gasoline_text(text: str, *, target_month: date, user=None) -> SourceFile:
    """「0007 82049」のような文からガソリン代を取り込む（カード識別4桁で担当者を判定）。"""
    doc = parsers.parse_gasoline_text(text)
    if not doc.rows:
        raise ValueError(doc.warnings[0])
    target_month = month_start(target_month)
    now = timezone.now()
    sha = hashlib.sha256(f"gas|{text}|{now.isoformat()}".encode()).hexdigest()
    sf = SourceFile.objects.create(original_name=f"ガソリン代（文で入力）{target_month:%Y年%m月}（{now:%m/%d %H:%M}）", sha256=sha,
                                   file_format=SourceFile.Format.MANUAL, document_kind=DocumentKind.GASOLINE,
                                   document_kind_auto=DocumentKind.GASOLINE, target_month=target_month, parser="gasoline_text",
                                   status=SourceFile.Status.PARSED, uploaded_by=user, row_count=len(doc.rows))
    _create_records(sf, doc, ExpenseCategory.objects.get(code="gasoline"))
    audit.log("expenses.import_gasoline_text", target=sf, after={"rows": len(doc.rows)}, user=user)
    return sf


@transaction.atomic
def confirm_amounts(sf: SourceFile, amounts: dict, *, user=None, reason: str = "元資料と照合") -> int:
    """OCR などで読んだ行の金額を、元資料と見比べてまとめて確定する。"""
    n = 0
    for rec in sf.records.filter(pk__in=list(amounts)):
        if rec.status in (R.EXCLUDED, R.REFLECTED):
            continue
        update_record(rec, user=user, reason=reason or "元資料と照合", amount=amounts[rec.pk])
        n += 1
    return n


def _parse(sf: SourceFile, data: bytes):
    if sf.file_format in ("xlsx", "xls", "csv", "gsheet"):
        rows = parsers.read_table_rows(data, "csv" if sf.file_format == "gsheet" else sf.file_format)
        _, headers = parsers.find_header(rows)
        sig = parsers.header_signature(headers)
        profile = ImportProfile.objects.filter(header_signature=sig, is_active=True).first()
        if sf.document_kind == DocumentKind.UNKNOWN:
            sf.document_kind_auto = profile.document_kind if profile else parsers.guess_kind(sf.original_name + " ".join(headers))
            sf.document_kind = sf.document_kind_auto
        return parsers.parse_table(rows, profile), profile

    if sf.file_format == SourceFile.Format.IMAGE:
        from PIL import Image

        from . import ocr

        img = Image.open(io.BytesIO(data))
        img.load()
        if sf.document_kind in (DocumentKind.UNKNOWN, DocumentKind.GASOLINE):
            doc = parsers.parse_eneos_image(img)
            if doc.rows:
                sf.document_kind_auto = DocumentKind.GASOLINE
                sf.document_kind = DocumentKind.GASOLINE
                return doc, _builtin_profile("eneos_card_summary")
            # ガソリン代の明細として読めなかった時は、交通費の請求書として試す（枠を間違えて選んだ時のため）
            text = _transport_text_from_image(img)
            tdoc = parsers.parse_transport_invoice([text], timezone.localdate())
            if tdoc.rows:
                sf.document_kind_auto = sf.document_kind = DocumentKind.TRANSPORT
                tdoc.extracted_text, tdoc.used_ocr = text, True
                tdoc.warnings.append("ガソリン代として選ばれましたが、交通費の請求書だったので交通費として取り込みました。")
                for r in tdoc.rows:
                    r.confidence = 0.6
                return tdoc, _builtin_profile("transport_invoice")
            sf.document_kind_auto = sf.document_kind = DocumentKind.GASOLINE
            return doc, _builtin_profile("eneos_card_summary")
        # 交通費などの請求書のスクショ: 文字を読み取って「交通費(8月分) 氏名 … 金額」の行を探す（必ず確認待ち）
        text = _transport_text_from_image(img)
        doc = parsers.parse_transport_invoice([text], timezone.localdate())
        doc.extracted_text = text
        doc.used_ocr = True
        for r in doc.rows:
            r.confidence = 0.6
        return doc, _builtin_profile("transport_invoice")

    # PDF
    from . import ocr

    pages = ocr.pdf_text_pages(data)
    text = "\n".join(pages)
    flat = parsers.nfkc(text).replace(" ", "")
    if sf.document_kind == DocumentKind.TRANSPORT and not re.search(r"交通費[(（]?\d{1,2}月", flat):
        # 文字の入っていない（スキャンした）PDF：ページを画像にして読み取る
        pages = [_transport_text_from_image(im) for im in ocr.render_pages(data, dpi=200, first=1, last=5)]
        text = "\n".join(pages)
        flat = parsers.nfkc(text).replace(" ", "")
        flat = re.sub(r"\s+", "", flat)
    if "ENEOS" in flat.upper() or sf.document_kind == DocumentKind.GASOLINE:
        parser = "eneos_card_summary"
    elif re.search(r"交通費[(（]?\d{1,2}月", flat) or sf.document_kind == DocumentKind.TRANSPORT:
        parser = "transport_invoice"
    else:
        raise ValueError("この形式のPDFの読み取りルールがありません。行を手入力してください。")
    profile = _builtin_profile(parser)
    sf.document_kind_auto = profile.document_kind if profile else parsers.guess_kind(text)
    if sf.document_kind == DocumentKind.UNKNOWN:
        sf.document_kind = sf.document_kind_auto
    if parser == "transport_invoice":
        doc = parsers.parse_transport_invoice(pages, timezone.localdate())
        doc.extracted_text = text
        return doc, profile
    return parsers.parse_eneos_card_summary(data, pages), profile


def _transport_text_from_image(img) -> str:
    """請求書のスクショを文字にする。「交通費(8月分) 名前 金額…」の名前の部分は日本語だけでもう一度読む（名前の精度を上げる）。"""
    from PIL import ImageOps

    from . import ocr

    g = img.convert("L")
    if g.width < 1000:
        g = g.resize((g.width * 2, g.height * 2))
    elif g.width > 1800:
        g = g.resize((1600, int(g.height * 1600 / g.width)))
    out = []
    for line in ocr.group_lines(ocr.ocr_words(g, lang="jpn+eng", psm=6), tolerance=20):
        ws = sorted(line.words, key=lambda w: w.left)
        texts = [parsers.nfkc(w.text) for w in ws]
        joined = "".join(texts)
        end = next((i for i, t in enumerate(texts) if ")" in t or "）" in t), None)
        num = next((i for i, t in enumerate(texts) if end is not None and i > end and re.fullmatch(r"-?[\d,]+", t)), None)
        if "交通" in joined and end is not None and num is not None and num > end + 1:
            name_ws = ws[end + 1:num]
            top = max(0, min(w.top for w in name_ws) - 6)
            bot = max(w.top + w.height for w in name_ws) + 6
            crop = g.crop((max(0, name_ws[0].left - 6), top, name_ws[-1].left + name_ws[-1].width + 6, bot))
            crop = ImageOps.expand(crop.resize((crop.width * 3, crop.height * 3)), border=20, fill=255)
            name = "".join(w.text for w in ocr.ocr_words(crop, lang="jpn", psm=7)).replace(" ", "")
            if name:
                texts = texts[:end + 1] + [name] + texts[num:]
        out.append(" ".join(texts))
    return "\n".join(out)


def dedupe_key(rec: ExpenseRecord, sf: SourceFile) -> str:
    if rec.transaction_id:
        return "txn:" + rec.transaction_id
    who = rec.card_code or parsers.nfkc(rec.name_text).replace(" ", "") or rec.member_no_text
    base = "|".join(str(x) for x in (sf.document_kind, who, rec.auto_used_on, rec.auto_amount,
                                       sf.document_month, rec.auto_description))
    return hashlib.sha1(base.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 振り分け・確認事項の判定
# ---------------------------------------------------------------------------
def _issue(rec, code, message):
    if not rec.issues.filter(code=code, resolved=False).exists():
        ExpenseIssue.objects.create(record=rec, code=code, message=message[:255])


def _resolve(rec, code, note="自動解決", user=None):
    rec.issues.filter(code=code, resolved=False).update(resolved=True, resolved_at=timezone.now(), resolved_by=user,
                                                         resolution_note=note)


def evaluate(rec: ExpenseRecord, *, initial: bool = False) -> ExpenseRecord:
    """振り分けと確認事項の判定。取り込み時と修正時に呼ぶ。"""
    if initial:
        by_card = by_name = None
        if rec.card_code:
            card = CardCode.objects.filter(code=rec.card_code).select_related("member").first()
            if card is None:
                # 運用ルール: カード対応表にないカードは無視する（給与に反映しない）
                rec.status = R.EXCLUDED
                rec.note = f"カード〔{rec.card_code}〕はカード対応表にないため対象外です。"
                rec.save()
                return rec
            if card.ignored:
                rec.status = R.EXCLUDED
                rec.note = f"カード〔{rec.card_code}〕は給与に反映しない設定です（{card.label or card.member or '対象外'}）。"
                rec.save()
                return rec
            else:
                by_card = card.member
        if rec.name_text or rec.member_no_text:
            m = Member.objects.filter(member_no=rec.member_no_text).first() if rec.member_no_text else None
            if m is None and rec.name_text:
                res = match_member(rec.name_text)
                if res.ok:
                    m = res.member
                elif res.method == "ambiguous":
                    _issue(rec, Code.MULTIPLE_CANDIDATES,
                           f"「{rec.name_text}」に当てはまる人が複数います（{'、'.join(c.name for c in res.candidates)}）。")
                else:
                    _issue(rec, Code.UNKNOWN_MEMBER, f"「{rec.name_text}」はメンバーに登録されていません（別表記の登録が必要かもしれません）。")
            by_name = m
        if by_card and by_name and by_card.pk != by_name.pk:
            _issue(rec, Code.NAME_CARD_MISMATCH, f"氏名（{by_name}）とカードの担当者（{by_card}）が一致しません。")
        rec.auto_member = by_card or by_name
        cat = rec.effective_category
        if rec.auto_member and rec.auto_member.employment_type == EmploymentType.EMPLOYEE and cat and cat.code == "gasoline":
            # 正社員のガソリン代は会社のカードの支払いなので差し引かない（交通費・経費は「正社員の給与」の合計に入る）
            rec.status = R.EXCLUDED
            rec.note = "正社員のガソリン代は差し引きの対象外です。"
            rec.save()
            return rec
        if rec.confidence is not None and float(rec.confidence) < LOW_CONFIDENCE:
            _issue(rec, Code.LOW_CONFIDENCE, "OCR（画像からの読み取り）のため、金額を元資料と見比べて確認してください。"
                   if rec.source_file.used_ocr else "読み取り精度が低いため確認してください。")
        if rec.auto_amount is None:
            _issue(rec, Code.LOW_CONFIDENCE, "金額を読み取れませんでした。元資料を見て入力してください。")
        dup = ExpenseRecord.objects.filter(dedupe_key=rec.dedupe_key).exclude(source_file=rec.source_file).exclude(
            status=R.EXCLUDED).first()
        if dup:
            _issue(rec, Code.DUPLICATE_TXN, f"別の資料（{dup.source_file.original_name} {dup.row_number}行目）に同じ取引があります。")
    else:
        # 修正後: 対象者・金額が入っていれば関連する確認事項を自動で解決
        if rec.effective_member:
            for code in (Code.UNKNOWN_MEMBER, Code.UNKNOWN_CARD, Code.MULTIPLE_CANDIDATES, Code.NAME_CARD_MISMATCH):
                if rec.member_id:
                    _resolve(rec, code, "対象者を指定")
    # 対象月の整合性
    tm, fm = rec.effective_target_month, rec.source_file.target_month
    if tm and fm and tm != fm:
        _issue(rec, Code.MONTH_MISMATCH, f"反映月（{tm:%Y年%m月}）が資料の反映月（{fm:%Y年%m月}）と異なります。")
    else:
        _resolve(rec, Code.MONTH_MISMATCH)
    if rec.status in (R.IMPORTED, R.UNCHECKED, R.PENDING):
        rec.status = R.PENDING if rec.issues.filter(resolved=False).exists() else R.UNCHECKED
    rec.save()
    return rec


def check_file(sf: SourceFile) -> None:
    """資料の合計と読み取った金額の合計を比べる。"""
    sf.issues.filter(code=Code.TOTAL_MISMATCH, resolved=False).delete()
    if sf.source_total is None:
        return
    total = sum((r.effective_amount or 0) for r in sf.records.all())
    if total != sf.source_total:
        ExpenseIssue.objects.create(source_file=sf, code=Code.TOTAL_MISMATCH,
                                    message=f"資料の合計 {sf.source_total:,}円 と、読み取った金額の合計 {total:,}円 が一致しません。")


# ---------------------------------------------------------------------------
# 管理者の操作
# ---------------------------------------------------------------------------
def _snap(rec: ExpenseRecord) -> dict:
    return {
        "対象者": str(rec.effective_member or ""), "経費項目": str(rec.effective_category or ""),
        "利用日": rec.effective_used_on.isoformat() if rec.effective_used_on else None,
        "利用内容": rec.description or rec.auto_description, "金額": rec.effective_amount,
        "反映月": rec.effective_target_month.isoformat() if rec.effective_target_month else None,
        "状態": rec.get_status_display(),
    }


def _ensure_editable(rec: ExpenseRecord):
    if rec.status == R.REFLECTED:
        raise RecordLocked("給与反映済みのデータは変更できません。先に給与の確定を取り消してください。")


def _notify_payroll(rec: ExpenseRecord, note: str, user=None):
    """給与確定後の変更なら、給与側に要確認を立てる。"""
    from apps.payroll.models import PayrollStatement

    m, tm = rec.effective_member, rec.effective_target_month
    if not (m and tm):
        return
    st = PayrollStatement.objects.filter(member=m, month=tm, status=PayrollStatement.Status.CONFIRMED).first()
    if st:
        PayrollStatement.objects.filter(pk=st.pk).update(
            needs_review=True, review_note=(st.review_note + "\n" if st.review_note else "") + note)
        _issue(rec, Code.CHANGED_AFTER_PAYROLL, f"{tm:%Y年%m月}の給与は確定済みです。給与画面で再計算するか判断してください。")
        audit.log("payroll.flag_review", target=st, after={"note": note}, user=user)


@transaction.atomic
def update_record(rec: ExpenseRecord, *, user=None, reason: str, member=None, category=None, used_on=None,
                  description=None, amount=None, target_month=None, save_alias: bool = False) -> ExpenseRecord:
    rec = ExpenseRecord.objects.select_for_update().get(pk=rec.pk)
    _ensure_editable(rec)
    if not reason:
        raise ValueError("修正理由を入力してください。")
    before = _snap(rec)
    if member is not None:
        rec.member = member
    if category is not None:
        rec.category = category
    if used_on is not None:
        rec.used_on = used_on
    if description is not None:
        rec.description = description[:255]
    if amount is not None:
        rec.amount = amount
        _resolve(rec, Code.LOW_CONFIDENCE, "金額を確認・入力", user)
    if target_month is not None:
        rec.target_month = month_start(target_month)
    rec.override_reason = reason[:255]
    was_approved = rec.status == R.APPROVED
    if rec.status in (R.IMPORTED, R.UNCHECKED, R.PENDING, R.APPROVED, R.EXCLUDED):
        rec.status = R.MODIFIED
    rec.save()
    if save_alias and member is not None and rec.name_text:
        MemberAlias.objects.get_or_create(member=member, alias=rec.name_text, defaults={"note": "資料の取り込み時に登録"})
    evaluate(rec)
    audit.log("expenses.record_update", target=rec, before=before, after=_snap(rec), reason=reason, user=user)
    if was_approved:
        _notify_payroll(rec, f"承認済み経費（{rec.source_file.original_name} {rec.row_number}行目）が修正されました", user)
    check_file(rec.source_file)
    return rec


@transaction.atomic
def resolve_issue(issue: ExpenseIssue, *, user=None, note: str) -> None:
    if not note:
        raise ValueError("確認した内容を入力してください。")
    issue.resolved, issue.resolved_at, issue.resolved_by, issue.resolution_note = True, timezone.now(), user, note[:255]
    issue.save()
    if issue.record_id:
        evaluate(issue.record)
    audit.log("expenses.issue_resolve", target_type="expenses.ExpenseIssue", target_id=issue.pk,
              target_repr=issue.get_code_display(), after={"note": note}, user=user)


def approval_problems(rec: ExpenseRecord) -> list[str]:
    problems = []
    if rec.status in (R.APPROVED, R.REFLECTED, R.EXCLUDED):
        problems.append(f"状態が「{rec.get_status_display()}」です")
    if rec.issues.filter(resolved=False).exclude(code=Code.CHANGED_AFTER_PAYROLL).exists():
        problems.append("未解決の確認事項があります")
    if not rec.effective_member:
        problems.append("対象者が未設定です")
    if not rec.effective_category:
        problems.append("経費項目が未設定です")
    if rec.effective_amount is None:
        problems.append("金額が未設定です")
    if not rec.effective_target_month:
        problems.append("反映する給与の月が未設定です")
    return problems


# 給与の画面で人と金額を見て「内容OK」を押した時に、確認済みとして扱ってよい注意（二重払いの恐れがあるものは除く）
SOFT_ISSUES = {Code.LOW_CONFIDENCE, Code.AMOUNT_MISMATCH, Code.MONTH_MISMATCH, Code.TOTAL_MISMATCH,
               Code.NAME_CARD_MISMATCH, Code.UNKNOWN_MEMBER, Code.UNKNOWN_CARD, Code.MULTIPLE_CANDIDATES}


def record_label(rec: ExpenseRecord) -> str:
    m = rec.effective_member
    return m.name if m else (rec.name_text or (f"カード {rec.card_code}" if rec.card_code else f"{rec.row_number}行目"))


@transaction.atomic
def approve(records, *, user=None, confirm_soft_issues: bool = False) -> tuple[int, list[str]]:
    """承認する。confirm_soft_issues=True は「内容を見て OK を押した」時：読み取り精度などの注意を確認済みにしてから承認する。"""
    ok, skipped = 0, []
    for rec in records:
        rec = ExpenseRecord.objects.select_for_update().get(pk=rec.pk)  # 常に最新の内容で判定する（古い値で上書きしない）
        if confirm_soft_issues and rec.effective_member and rec.effective_amount is not None:
            for issue in rec.issues.filter(resolved=False, code__in=SOFT_ISSUES):
                issue.resolved, issue.resolved_at, issue.resolved_by = True, timezone.now(), user
                issue.resolution_note = "給与の画面で名前と金額を確認して承認"
                issue.save()
        problems = approval_problems(rec)
        if problems:
            left = [i.message for i in rec.issues.filter(resolved=False).exclude(code=Code.CHANGED_AFTER_PAYROLL)]
            detail = "・".join(p for p in problems if p != "未解決の確認事項があります")
            skipped.append(f"{record_label(rec)}: " + "・".join(x for x in [detail, *left[:2]] if x))
            continue
        rec.status, rec.approved_at, rec.approved_by = R.APPROVED, timezone.now(), user
        rec.save()
        audit.log("expenses.approve", target=rec, after=_snap(rec), user=user)
        _notify_payroll(rec, f"経費が承認されました（{rec.effective_category} {rec.effective_amount:,}円）", user)
        ok += 1
    return ok, skipped


@transaction.atomic
def unapprove(rec: ExpenseRecord, *, user=None, reason: str) -> None:
    rec = ExpenseRecord.objects.select_for_update().get(pk=rec.pk)
    _ensure_editable(rec)
    if rec.status != R.APPROVED:
        raise ValueError("承認済みのデータではありません。")
    if not reason:
        raise ValueError("承認を取り消す理由を入力してください。")
    rec.status, rec.approved_at, rec.approved_by = R.MODIFIED, None, None
    rec.save()
    audit.log("expenses.unapprove", target=rec, reason=reason, user=user)


@transaction.atomic
def exclude(records, *, user=None, reason: str) -> int:
    if not reason:
        raise ValueError("対象外にする理由を入力してください。")
    n = 0
    for rec in records:
        rec = ExpenseRecord.objects.select_for_update().get(pk=rec.pk)
        _ensure_editable(rec)
        was_approved = rec.status == R.APPROVED
        before = rec.get_status_display()
        rec.status, rec.note = R.EXCLUDED, reason[:255]
        rec.save()
        audit.log("expenses.exclude", target=rec, before={"状態": before}, reason=reason, user=user)
        if was_approved:
            _notify_payroll(rec, f"承認済み経費（{rec.row_number}行目）が対象外にされました", user)
        n += 1
    return n


@transaction.atomic
def add_manual_record(sf: SourceFile, *, user=None, member, category, amount: int, used_on=None, description="",
                      target_month=None, reason="") -> ExpenseRecord:
    n = (sf.records.order_by("-row_number").values_list("row_number", flat=True).first() or 0) + 1
    rec = ExpenseRecord(source_file=sf, row_number=n, raw={"手入力": reason or "手入力"}, member=member, category=category,
                        amount=amount, used_on=used_on, description=description[:255],
                        target_month=month_start(target_month) if target_month else None, override_reason=reason or "手入力",
                        status=R.MODIFIED)
    rec.dedupe_key = "manual:" + hashlib.sha1(f"{sf.pk}|{n}|{member.pk}|{amount}".encode()).hexdigest()
    rec.save()
    evaluate(rec)
    audit.log("expenses.record_add", target=rec, after=_snap(rec), reason=reason, user=user)
    check_file(sf)
    return rec


@transaction.atomic
def set_file_meta(sf: SourceFile, *, user=None, kind=None, target_month=None, source_total="__keep__") -> None:
    before = {"種類": sf.get_document_kind_display(), "反映月": sf.target_month, "資料の合計": sf.source_total}
    if kind:
        sf.document_kind = kind
        if sf.status == SourceFile.Status.NEEDS_KIND and kind != DocumentKind.UNKNOWN:
            sf.status = SourceFile.Status.PARSED
    if target_month:
        sf.target_month = month_start(target_month)
        for rec in sf.records.exclude(status__in=[R.REFLECTED]):
            rec.auto_target_month = sf.target_month
            rec.save(update_fields=["auto_target_month", "updated_at"])
            evaluate(rec)
    if source_total != "__keep__":
        sf.source_total = source_total
    sf.save()
    check_file(sf)
    audit.log("expenses.file_update", target=sf, before=before,
              after={"種類": sf.get_document_kind_display(), "反映月": sf.target_month, "資料の合計": sf.source_total}, user=user)


@transaction.atomic
def save_mapping(sf: SourceFile, *, user=None, name: str, kind: str, column_map: dict, category=None,
                 payroll_month_offset: int = 0) -> ImportProfile:
    if "amount" not in column_map:
        raise ValueError("「金額」の列を指定してください。")
    if not any(k in column_map for k in ("name", "member_no", "card")):
        raise ValueError("「氏名」「メンバー番号」「カード番号」のいずれかの列を指定してください。")
    sig = parsers.header_signature(sf.headers)
    profile, _ = ImportProfile.objects.update_or_create(
        header_signature=sig,
        defaults=dict(name=name or sf.original_name, document_kind=kind, headers=sf.headers, column_map=column_map,
                      default_category=category, options={"payroll_month_offset": payroll_month_offset},
                      created_by=user, is_active=True),
    )
    sf.document_kind = kind
    sf.save()
    audit.log("expenses.profile_save", target=profile, after={"columns": column_map, "kind": kind}, user=user)
    process(sf, user=user)
    return profile


AUTO_APPROVE_EXCLUDE = {"gasoline", "transport"}


def auto_approve_added(records, *, user=None) -> int:
    """自分で人を選んで追加した経費（レシート・文で入力・手入力の「その他の経費」）は、そのまま承認して給与に入れる。

    ガソリン代・交通費は資料から自動で読むため、確認してから承認する（ここでは承認しない）。
    誰の分か・金額が決まっていないものは承認せず「確認待ち」に残す。
    """
    targets = [r for r in records if r.status in (R.IMPORTED, R.UNCHECKED, R.PENDING, R.MODIFIED)
               and r.effective_member and r.effective_amount is not None
               and not (r.effective_category and r.effective_category.code in AUTO_APPROVE_EXCLUDE)]
    if not targets:
        return 0
    with transaction.atomic():
        ok, _ = approve(targets, user=user, confirm_soft_issues=True)
    return ok
