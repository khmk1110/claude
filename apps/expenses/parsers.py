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
    warnings_ok: str = ""  # 読み取りが確実だった理由（合計一致など）


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
            # スクショの読み取りでは「交通 費 (8 月 分 ) 矢 木 淳之介」のように1文字ずつ空白が入るので詰める
            line = re.sub(r"(?<=[^\x00-\x7F]) +(?=[^\x00-\x7F])", "", line)
            line = re.sub(r"[(（]\s*(\d{1,2})\s*月\s*分\s*[)）]", r"(\1月分)", line)
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


def summary_page_order(pages: list[str]) -> list[int]:
    """請求明細表（合計版）らしいページから順に並べる（文字情報は崩れていることが多いので緩く判定）。"""
    def score(t):
        n = re.sub(r"\s", "", nfkc(t))
        return (all(ch in n for ch in "合計版") * 4 + ("R000" in n.upper()) * 2 + ("明細" in n), -len(n))
    return sorted(range(len(pages)), key=lambda i: score(pages[i]), reverse=True)


def parse_eneos_card_summary(data: bytes, pages: list[str] | None = None) -> ParsedDocument:
    """ENEOS の請求書PDF。請求明細表（合計版）らしいページを画像にして parse_eneos_image で読む。"""
    from . import ocr

    if not ocr.ocr_available():
        doc = ParsedDocument("eneos_card_summary", used_ocr=True)
        doc.warnings.append("OCR（Tesseract）が使えないため読み取れません。カード別の金額を手入力してください。")
        return doc
    if pages is None:
        pages = ocr.pdf_text_pages(data)
    best = None
    for idx in (summary_page_order(pages) or [0])[:2]:
        imgs = ocr.render_pages(data, dpi=200, first=idx + 1, last=idx + 1)
        if not imgs:
            continue
        doc = parse_eneos_image(imgs[0])
        if doc.rows and (best is None or len(doc.rows) > len(best.rows)):
            best = doc
        if doc.warnings_ok:
            return doc
    if best is None:
        best = ParsedDocument("eneos_card_summary", used_ocr=True)
        best.warnings.append("請求明細表（合計版）のページが見つかりませんでした。カード別の金額を手入力してください。")
    return best


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


# ---------------------------------------------------------------------------
# ENEOS FC 請求明細表（合計版）— 画像（スクショ・PDFのページ画像）からの読み取り
# ---------------------------------------------------------------------------
def _ocr_tsv(img, *, lang="eng", psm=6, digits=False):
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png") as t:
        img.save(t.name)
        args = ["tesseract", t.name, "stdout", "-l", lang, "--psm", str(psm)]
        if digits:
            args += ["-c", "tessedit_char_whitelist=0123456789"]
        out = subprocess.run(args + ["tsv"], capture_output=True, timeout=120).stdout.decode("utf-8", "replace")
    rows = []
    for line in out.splitlines()[1:]:
        c = line.split("\t")
        if len(c) >= 12 and c[11].strip():
            rows.append({"text": c[11].strip(), "left": int(c[6]), "top": int(c[7]), "w": int(c[8]), "h": int(c[9])})
    return rows


def card_totals_from_amounts(codes: list, amounts: list) -> tuple[dict, int | None]:
    """カード識別4桁の位置（y）と金額列の値（y, 金額）から、カードごとの合計を決める。

    表の線に頼らず、次のルールで決める（ご指示どおり「カード計」がその番号の人の合計）:
    - その番号から次の番号までにある金額のうち、それより上の金額の合計と一致する金額＝「カード計」
    - 見つからなければ、その番号の行の金額（1行だけのカード）
    お客様計（請求の合計）は最も大きい金額。
    """
    codes = sorted(codes)
    result = {}
    for i, (y, code) in enumerate(codes):
        y_next = codes[i + 1][0] if i + 1 < len(codes) else 10 ** 9
        band = [v for (ay, v) in sorted(amounts) if y - 6 <= ay < y_next - 6]
        if not band:
            continue
        total = band[0]
        for j in range(2, len(band)):
            if band[j] == sum(band[:j]):
                total = band[j]
                break
        result[code] = total
    grand = max((v for _, v in amounts), default=None)
    return result, grand


def parse_eneos_image(img) -> ParsedDocument:
    """スクショやPDFのページ画像から、カードごとの合計を読む。

    金額は誤読しやすいため、拡大率・白黒の境目・区切り線の除去の有無を変えて何度も読み、多数決で決める。
    カード計の合計が請求の合計（お客様計）と一致したときだけ「読み取り確実」とし、そうでなければ確認待ちにする。
    """
    import collections

    import numpy as np
    from PIL import Image, ImageFilter

    doc = ParsedDocument("eneos_card_summary", used_ocr=True)
    gray = img.convert("L")
    if gray.width > 2400:  # 300dpi のPDFページなどは縮小して処理を軽くする
        r = 2400 / gray.width
        gray = gray.resize((2400, int(gray.height * r)), Image.LANCZOS)
    W, H = gray.size

    # 1) カード識別4桁の位置。「0566-007-4 S [ R0007 ]」の真ん中の3桁と、［ ］内の R＋4桁の両方から読む
    from . import ocr as _ocr

    def norm(t):
        t = nfkc(t).upper()
        return t.translate(str.maketrans({"O": "0", "Q": "0", "D": "0", "!": "1", "|": "1", "I": "1", "L": "1"}))

    found = []
    left = gray.crop((0, 0, int(W * 0.5), H))  # カード番号は表の左側にある（処理を軽くする）
    for s_, thr in ((3, 180), (2, 170), (3, 160), (4, 190), (2, 200)):
        big = left.resize((left.width * s_, H * s_), Image.LANCZOS)
        bw = Image.fromarray(np.where(np.array(big) < thr, 0, 255).astype("uint8"))
        for psm in (4, 6):
            words = [_ocr.Word(w["text"], w["left"], w["top"], w["w"], w["h"], 0) for w in _ocr_tsv(bw, lang="jpn+eng", psm=psm)]
            for line in _ocr.group_lines(words, tolerance=10 * s_):
                t = norm(line.text).replace(" ", "")
                code = None
                m = re.search(r"0566-?(\d{3})-?\d", t)
                if m:
                    code = "0" + m.group(1)
                else:
                    m = re.search(r"R(\d{4})", t)
                    code = m.group(1) if m else None
                if code == "0000":  # 「会社コード 3-50-4005-0566-000-9」の誤検出
                    code = None
                if code:
                    y = sum(w.top + w.height / 2 for w in line.words) / len(line.words) / s_
                    found.append((code, y))
    codes = {}
    for code, y in found:
        codes.setdefault(code, []).append(y)
    # 1回しか見つからない番号は誤読の可能性が高いので捨てる（3回中2回以上）
    codes = {k: v for k, v in codes.items() if len(v) >= 2}
    text_all = " ".join(w["text"] for w in _ocr_tsv(Image.fromarray(np.where(np.array(gray) < 170, 0, 255).astype("uint8")), lang="jpn+eng", psm=6))
    m = re.search(r"(\d{4})\s*[年%]\s*(\d{1,2})\s*月", nfkc(text_all))
    if m:
        doc.document_month = date(int(m[1]), int(m[2]), 1)
    code_pos = [(sorted(v)[len(v) // 2], k) for k, v in codes.items()]
    if not code_pos:
        doc.warnings.append("カード番号（〔R0007〕など）を読み取れませんでした。金額を手入力してください。")
        return doc

    # 2) 金額の列を探す（数字が縦に並んでいる一番右の列）
    probe = gray.resize((W * 2, H * 2), Image.LANCZOS)
    nums = [w for w in _ocr_tsv(Image.fromarray(np.where(np.array(probe) < 170, 0, 255).astype("uint8")), psm=6, digits=True)
            if len(w["text"]) >= 3]
    xs = sorted((((w["left"] + w["w"] / 2) / 2, w) for w in nums), key=lambda t: t[0])
    columns = []
    for x, w in xs:
        if columns and x - columns[-1]["x"] < 40:
            columns[-1]["items"].append(w)
            columns[-1]["x"] = x
        else:
            columns.append({"x": x, "items": [w]})
    columns = [c for c in columns if len(c["items"]) >= 3]
    if columns:
        col = columns[-1]
        x0 = min(w["left"] for w in col["items"]) / 2 - 25
        x1 = max(w["left"] + w["w"] for w in col["items"]) / 2 + 25
    else:
        x0, x1 = W * 0.70, W * 0.82
    x0, x1 = max(0, int(x0)), min(W, int(x1))

    # 3) 金額を多数決で読む
    votes = collections.defaultdict(list)
    crop = gray.crop((x0, 0, x1, H))
    for s in (3, 4):
        base = crop.resize((crop.width * s, crop.height * s), Image.LANCZOS)
        for thr in (150, 170, 190):
            for blur in (False, True):
                im = base.filter(ImageFilter.MedianFilter(3)) if blur else base
                a = np.array(im)
                for remove in (False, True):
                    b = a < thr
                    if remove:  # 数字の間の縦の区切り線と、下線・罫線（横線）を消す
                        dens = b[int(H * s * 0.2):].mean(axis=0)
                        a2 = a.copy()
                        a2[:, dens > 0.3] = 255
                        b = a2 < thr
                        hdens = b.mean(axis=1)
                        a2[hdens > 0.55, :] = 255
                        b = a2 < thr
                    pic = Image.fromarray(np.where(b, 0, 255).astype("uint8"))
                    for psm in (6, 11):
                        for w in _ocr_tsv(pic, psm=psm, digits=True):
                            if re.fullmatch(r"\d{3,7}", w["text"]):
                                y = (w["top"] + w["h"] / 2) / s
                                votes[round(y * 10)].append(int(w["text"]))
    # 縦位置が近い票をまとめて（5px以内）、最も多い値を採用（票が少ないものは捨てる）
    flat = sorted((k / 10, v) for k, vs in votes.items() for v in vs)
    clusters = []
    for y, v in flat:
        if clusters and y - clusters[-1]["y"] <= 5:
            clusters[-1]["vals"].append(v)
            clusters[-1]["y"] = y
        else:
            clusters.append({"y": y, "y0": y, "vals": [v]})
    rows, alts = [], {}
    for c in clusters:
        common = collections.Counter(c["vals"]).most_common(3)
        val, n = common[0]
        if n >= 4:
            y = (c["y0"] + c["y"]) / 2
            rows.append((y, val))
            alts[y] = [v for v, k in common[1:] if k >= 2]
    totals, grand = card_totals_from_amounts(code_pos, rows)
    if grand is not None and sum(totals.values()) != grand:
        # 合計が合わないとき: 1枚だけ別の読み方に置き換えると合計が一致するなら、それを採用する
        diff = grand - sum(totals.values())
        fixes = []
        for y, val in rows:
            for alt in alts.get(y, []):
                if alt - val == diff:
                    trial = [(yy, alt if yy == y else vv) for yy, vv in rows]
                    t2, g2 = card_totals_from_amounts(code_pos, trial)
                    if g2 == grand and sum(t2.values()) == grand:
                        fixes.append(t2)
        if len(fixes) == 1:
            totals = fixes[0]
    verified = grand is not None and sum(totals.values()) == grand and len(totals) == len(code_pos)
    doc.source_total = grand
    for i, (y, code) in enumerate(sorted(code_pos)):
        doc.rows.append(ParsedRow(row_number=i + 1, raw={"OCR": f"〔R{code}〕 カード計 {totals.get(code, '?')}"}, card_code=code,
                                  description="ガソリン（カード別合計）", amount=totals.get(code),
                                  confidence=0.99 if verified else 0.6))
    if verified:
        doc.warnings_ok = f"カード計の合計 {grand:,}円 が請求の合計（お客様計）と一致しました。"
    else:
        doc.warnings.append("カード計の合計が請求の合計と一致しませんでした。画像と見比べて金額を確認してください。")
    return doc


def parse_gasoline_text(text: str) -> ParsedDocument:
    """「0007 82049」「〔R0007〕82,049円」のような文から、カードごとの金額を読む。"""
    doc = ParsedDocument("gasoline_text")
    n = 0
    for raw in text.splitlines():
        t = nfkc(raw).upper().replace("O", "0")
        m = re.search(r"R?\s*(\d{4})\D+?(-?[\d,]{3,})", t)
        if not m:
            continue
        n += 1
        doc.rows.append(ParsedRow(row_number=n, raw={"行": raw.strip()}, card_code=m.group(1),
                                  description="ガソリン（カード別合計）", amount=parse_amount(m.group(2))))
    if not doc.rows:
        doc.warnings.append("「カード番号の4桁 金額」の形で入力してください（例: 0007 82049）。")
    return doc
