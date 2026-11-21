"""資料の読み取り。

出力は ParsedDocument（行の一覧＋資料の対象月・合計など）。氏名→メンバー、カード→メンバーの
振り分けや重複検出は importer.py で行う。

対応している読み取り方法:
- table           : Excel / CSV / Googleスプレッドシート（見出し行＋列の対応＝読み取りルール）
- transport_invoice: 請求書PDFの「交通費(8月分) 氏名 単価 数量 価格」の行
- eneos_card_summary: ENEOS FC 請求明細表（合計版）の画像PDF。カード識別4桁ごとの合計を OCR で読み取る
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .models import DocumentKind

FIELDS = {
    "name": "氏名",
    "member_no": "メンバー番号",
    "card": "カード番号",
    "used_on": "利用日",
    "description": "利用内容",
    "route": "利用区間",
    "amount": "金額",
    "txn_id": "取引番号",
}


@dataclass
class ParsedRow:
    row_number: int
    raw: dict
    name: str = ""
    member_no: str = ""
    card_code: str = ""
    card_last4: str = ""
    used_on: date | None = None
    description: str = ""
    route: str = ""
    amount: int | None = None
    txn_id: str = ""
    confidence: float = 1.0
    excluded_note: str = ""


@dataclass
class ParsedDocument:
    parser: str
    rows: list = field(default_factory=list)
    document_month: date | None = None  # 資料が対象とする月（利用月・〇月分）
    source_total: int | None = None
    used_ocr: bool = False
    headers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class NeedsMapping(Exception):
    """見出しの読み取りルールが未登録（または見出しが変わった）。"""

    def __init__(self, headers, signature):
        super().__init__("読み取りルールが必要です")
        self.headers, self.signature = headers, signature


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------
def nfkc(v) -> str:
    return unicodedata.normalize("NFKC", str(v or "")).strip()


def parse_amount(v) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float, Decimal)):
        return int(round(float(v)))
    t = nfkc(v).replace(",", "").replace("円", "").replace("¥", "").replace("\\", "").strip()
    if t.startswith("(") and t.endswith(")"):
        t = "-" + t[1:-1]
    try:
        return int(Decimal(t).to_integral_value())
    except InvalidOperation:
        return None


def parse_date(v, default_year: int | None = None) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    t = nfkc(v)
    m = re.search(r"(\d{4})[/\-年.](\d{1,2})[/\-月.](\d{1,2})", t)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"^(\d{1,2})[/\-月](\d{1,2})", t)
    if m and default_year:
        return date(default_year, int(m[1]), int(m[2]))
    return None


def card_code_from(text) -> tuple[str, str]:
    """カード番号の表記から（識別4桁, 下4桁）を取り出す。

    マニュアルどおり、末尾の数字ではなく右側に記載された〔0007〕などの4桁で判定する。
    """
    t = nfkc(text).upper().replace("O", "0")
    m = re.search(r"[\[〔(（「]\s*R?\s*(\d{4})\s*[\]〕)）」]", t) or re.search(r"\bR(\d{4})\b", t)
    code = m.group(1) if m else ""
    digits = re.sub(r"\D", "", re.sub(r"[\[〔(（「].*?[\]〕)）」]", "", t))
    return code, digits[-4:] if len(digits) >= 4 else ""


def header_signature(headers: list[str]) -> str:
    norm = "|".join(re.sub(r"\s+", "", nfkc(h)) for h in headers)
    return hashlib.sha256(norm.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 表形式（Excel / CSV）
# ---------------------------------------------------------------------------
def read_table_rows(data: bytes, file_format: str) -> list[list]:
    if file_format == "csv":
        for enc in ("utf-8-sig", "cp932", "utf-16"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("CSV の文字コードを判別できません。")
        return [row for row in csv.reader(io.StringIO(text))]
    if file_format == "xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if file_format == "xls":
        import xlrd

        book = xlrd.open_workbook(file_contents=data)
        sh = book.sheet_by_index(0)
        rows = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                else:
                    row.append(cell.value)
            rows.append(row)
        return rows
    raise ValueError(f"表として読めない形式です: {file_format}")


def find_header(rows: list[list]) -> tuple[int, list[str]]:
    for i, row in enumerate(rows[:30]):
        cells = [nfkc(c) for c in row]
        texts = [c for c in cells if c and not re.fullmatch(r"[\d,.\-]+", c)]
        if len(texts) >= 2:
            return i, cells
    raise ValueError("見出し行が見つかりません。")


def parse_table(rows: list[list], profile) -> ParsedDocument:
    hi, headers = find_header(rows)
    sig = header_signature(headers)
    if profile is None or profile.header_signature != sig:
        raise NeedsMapping(headers, sig)
    idx = {f: headers.index(h) for f, h in profile.column_map.items() if h in headers}
    if "amount" not in idx:
        raise NeedsMapping(headers, sig)
    doc = ParsedDocument("table", headers=headers)
    default_year = profile.options.get("default_year")
    for n, row in enumerate(rows[hi + 1:], start=hi + 2):
        get = lambda f: row[idx[f]] if f in idx and idx[f] < len(row) else None  # noqa: E731
        if all(c in (None, "") for c in row):
            continue
        name = nfkc(get("name"))
        if any(k in nfkc(c) for c in row for k in ("合計", "総計", "小計")):
            doc.source_total = parse_amount(get("amount"))
            continue
        code, last4 = card_code_from(get("card")) if get("card") else ("", "")
        doc.rows.append(ParsedRow(
            row_number=n,
            raw={headers[i] if i < len(headers) else f"列{i + 1}": (str(c) if c is not None else "") for i, c in enumerate(row)},
            name=name, member_no=nfkc(get("member_no")), card_code=code, card_last4=last4,
            used_on=parse_date(get("used_on"), default_year), description=nfkc(get("description")),
            route=nfkc(get("route")), amount=parse_amount(get("amount")), txn_id=nfkc(get("txn_id")),
        ))
    return doc


# ---------------------------------------------------------------------------
# 請求書PDF の交通費行
# ---------------------------------------------------------------------------
TRANSPORT_LINE = re.compile(
    r"交通費\s*[(（]\s*(\d{1,2})\s*月分\s*[)）]\s*(.+?)\s+(-?[\d,]+)\s+(\d+)\s+(?:\S+\s+)?(-?[\d,]+)\s*$"
)


def parse_transport_invoice(pages: list[str], today: date) -> ParsedDocument:
    doc = ParsedDocument("transport_invoice")
    months = set()
    issued = None
    for text in pages:
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", nfkc(text))
        if m and issued is None:
            issued = date(int(m[1]), int(m[2]), int(m[3]))
    n = 0
    for page_no, text in enumerate(pages, start=1):
        for line in text.splitlines():
            line = nfkc(line)
            m = TRANSPORT_LINE.search(line)
            if not m:
                continue
            n += 1
            month_no, name, unit, qty, total = int(m[1]), m[2].strip(), m[3], m[4], m[5]
            months.add(month_no)
            doc.rows.append(ParsedRow(row_number=n, raw={"ページ": page_no, "行": line}, name=name,
                                      description=f"交通費（{month_no}月分）", amount=parse_amount(total)))
    if len(months) == 1:
        mo = months.pop()
        base = issued or today
        year = base.year if mo <= base.month else base.year - 1  # 発行日（なければ今日）より前の直近の月
        doc.document_month = date(year, mo, 1)
    elif len(months) > 1:
        doc.warnings.append("複数の月の交通費が含まれています。対象月を確認してください。")
    if not doc.rows:
        doc.warnings.append("「交通費(〇月分) 氏名 単価 数量 価格」の行が見つかりませんでした。")
    return doc


# ---------------------------------------------------------------------------
# ENEOS FC 請求明細表（合計版）— 画像PDF
# ---------------------------------------------------------------------------
CARD_IN_LINE = re.compile(r"R\s*([0O]{1,3}\s*\d{1,3}|\d{4})")


def _norm_card(token: str) -> str:
    d = token.upper().replace("O", "0").replace(" ", "")
    d = re.sub(r"\D", "", d)
    return d.zfill(4)[-4:] if d else ""


def parse_eneos_card_summary(data: bytes) -> ParsedDocument:
    from . import ocr

    doc = ParsedDocument("eneos_card_summary", used_ocr=True)
    if not ocr.ocr_available():
        doc.warnings.append("OCR（Tesseract）が使えないため読み取れません。カード別の金額を手入力してください。")
        return doc
    images = ocr.render_pages(data, dpi=300)
    target = None
    for img in images:
        clean = ocr.remove_grid_lines(img)
        words = ocr.ocr_words(clean)
        text = " ".join(w.text for w in words)
        if "合計版" in text or "カード" in text and "計" in text and "R0" in text.replace("O", "0"):
            target = (img, clean, words, text)
            if "合計版" in text:
                break
    if target is None:
        doc.warnings.append("請求明細表（合計版）のページが見つかりませんでした。")
        return doc
    img, clean, words, text = target
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*1\s*[日F]", text)
    if m:
        doc.document_month = date(int(m[1]), int(m[2]), 1)
    # 金額列は数字の区切り線などで誤読しやすいので、前処理を変えて複数回読み、2回以上一致した値だけを候補にする
    import numpy as np
    from PIL import Image

    W, H = img.size
    gray = np.array(img.convert("L"))
    dark = gray < 160
    col = (int(0.50 * W), int(0.60 * W))
    variants = []
    for vmin in (40, 50, 60):
        mask = ocr._long_runs(dark, 120) | ocr._long_runs(dark.T, vmin).T
        g = gray.copy()
        g[mask] = 255
        crop = Image.fromarray(g).crop((col[0], 0, col[1], H))
        nums = []
        for w in ocr.ocr_words(crop, lang="eng", psm=6):
            d = re.sub(r"[^0-9]", "", w.text)
            if len(d) >= 3 and re.fullmatch(r"[\d,()]+", w.text.strip("|[]")):
                nums.append((w.top + w.height / 2, int(d)))
        variants.append(nums)

    lines = ocr.group_lines(words)
    marks = []  # (y, 種類, 行)
    for line in lines:
        t = line.text
        tn = t.upper().replace("O", "0")
        cm = CARD_IN_LINE.search(tn)
        if cm and ("0566" in tn or "[" in t):
            marks.append((line.top, "card", line, _norm_card(cm.group(1))))
        elif "お客様" in t:
            marks.append((line.top, "grand", line, None))
        elif re.search(r"(^|\s)計(\s|\||$)", t) and "カード" not in t:
            marks.append((line.top, "total", line, None))
    marks.sort(key=lambda m: m[0])

    def vote(y0, y1):
        picks = []
        for nums in variants:
            inside = [n for y, n in nums if y0 - 10 <= y < y1 - 10]
            if inside:
                picks.append(inside[-1])  # 複数行のカードは一番下（カード計）
        best = max(set(picks), key=picks.count) if picks else None
        agree = picks.count(best) if best is not None else 0
        return (best, 0.7) if agree >= 2 else (None, 0.3)

    n = 0
    for i, (y, kind, line, code) in enumerate(marks):
        y_next = marks[i + 1][0] if i + 1 < len(marks) else H
        if kind == "card":
            amount, conf = vote(y, y_next)
            n += 1
            doc.rows.append(ParsedRow(row_number=n, raw={"OCR": line.text}, card_code=code,
                                      description="ガソリン（カード別合計）", amount=amount, confidence=conf))
        elif kind == "grand":
            doc.source_total, _ = vote(y, y_next)
    if not doc.rows:
        doc.warnings.append("カード番号を読み取れませんでした。カード別の金額を手入力してください。")
    return doc


# ---------------------------------------------------------------------------
# 種類の自動判定
# ---------------------------------------------------------------------------
KIND_KEYWORDS = [
    (DocumentKind.GASOLINE, ("ガソリン", "ENEOS", "給油", "軽油", "レギュラー", "FC請求")),
    (DocumentKind.TRANSPORT, ("交通費", "運賃", "区間", "乗車")),
    (DocumentKind.ADVANCE, ("立替",)),
    (DocumentKind.SALES, ("獲得", "入金確定", "件数")),
    (DocumentKind.ATTENDANCE, ("出勤", "勤怠", "シフト")),
]


def guess_kind(text: str) -> str:
    t = nfkc(text).upper()
    scores = {k: sum(t.count(w.upper()) for w in words) for k, words in KIND_KEYWORDS}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else DocumentKind.UNKNOWN
