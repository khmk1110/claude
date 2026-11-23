"""請求書（写真・PDF）から、請求元・金額・支払期限・振込先を読み取る。

読み取りは「下書き」まで。必ず画面で確認してから保存する（推測で確定しない）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date


@dataclass
class InvoiceGuess:
    payee: str = ""
    amount: int | None = None
    due_date: date | None = None
    bank: str = ""
    text: str = ""


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def _join_cjk(line: str) -> str:
    return re.sub(r"(?<=[^\x00-\x7F¥￥]) +(?=[^\x00-\x7F¥￥])", "", line)


def text_from_file(data: bytes, name: str) -> str:
    """写真は文字を読み取り、PDFは中の文字（なければ画像として読み取り）を使う。"""
    from PIL import Image, ImageOps

    from apps.expenses import ocr

    lower = name.lower()
    if lower.endswith(".pdf"):
        pages = ocr.pdf_text_pages(data)
        if "".join(pages).strip():
            return "\n".join(pages)
        images = ocr.render_pages(data, dpi=200, first=1, last=2)
    else:
        import io

        images = [ImageOps.exif_transpose(Image.open(io.BytesIO(data)))]
    out = []
    for img in images:
        g = img.convert("L")
        if g.width < 1000:
            g = g.resize((g.width * 2, g.height * 2))
        elif g.width > 2000:
            g = g.resize((1800, int(g.height * 1800 / g.width)))
        words = ocr.ocr_words(g, lang="jpn+eng", psm=6)
        for line in ocr.group_lines(words, tolerance=18):
            ws = sorted(line.words, key=lambda w: w.left)
            joined = nfkc("".join(w.text for w in ws))
            if any(k in joined for k in AMOUNT_KEYS + DUE_KEYS) or "円" in joined or "¥" in joined:
                # 金額・日付の行は、数字だけを読む方法でもう一度読む（0と9などの読み間違いを減らす）
                texts = []
                for w in ws:
                    t = nfkc(w.text)
                    if re.search(r"\d", t):
                        crop = g.crop((max(0, w.left - 4), max(0, w.top - 4), w.left + w.width + 4, w.top + w.height + 4))
                        crop = ImageOps.expand(crop.resize((crop.width * 2, crop.height * 2)), border=12, fill=255)
                        digits = ocr.ocr_digits(crop).strip()
                        digits_only = re.sub(r"\D", "", digits)
                        if digits_only and len(digits_only) == len(re.sub(r"\D", "", t)):
                            t = re.sub(r"[\d,，]+", digits, t, count=1) if re.search(r"[\d,，]+", t) else t
                    texts.append(t)
                out.append(_join_cjk(" ".join(texts)))
            else:
                out.append(_join_cjk(line.text))
    return "\n".join(out)


AMOUNT_KEYS = ("ご請求金額", "御請求金額", "請求金額", "ご請求額", "請求額", "お支払金額", "お支払い金額", "支払金額",
               "合計金額", "税込合計", "総合計", "総額", "合計")
DUE_KEYS = ("お支払期限", "お支払い期限", "支払期限", "振込期限", "お振込期限", "支払期日", "お支払期日", "期日", "期限")
BANK_WORDS = ("銀行", "信用金庫", "信金", "支店", "普通", "当座", "口座", "ゆうちょ", "名義")
COMPANY = re.compile(r"(株式会社|\(株\)|（株）|有限会社|\(有\)|合同会社|一般社団法人|合資会社)")


def _amounts(line: str) -> list[int]:
    vals = []
    line = re.sub(r"(\d)\s*[,，]\s+(\d{3})", r"\1,\2", line)
    line = re.sub(r"(?<![\d,])(\d{1,3})\s(\d{3})(?=\s*円|\s*$|\s*-)", r"\1,\2", line)  # 「108 251 円」→「108,251」
    for m in re.finditer(r"[¥￥]?\s*(\d{1,3}(?:[,，]\d{3})+|\d{4,9})\s*円?", line):
        try:
            vals.append(int(re.sub(r"[,，]", "", m.group(1))))
        except ValueError:
            pass
    return vals


def _date(line: str, today: date) -> date | None:
    s = nfkc(line)
    m = re.search(r"(20\d{2})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
    if m:
        try:
            d = date(today.year, int(m[1]), int(m[2]))
            return d if d >= today.replace(day=1) else date(today.year + 1, d.month, d.day)
        except ValueError:
            return None
    return None


def guess(text: str, *, own_company: str = "", known_payees: list[str] | None = None, today: date | None = None) -> InvoiceGuess:
    today = today or date.today()
    lines = [nfkc(l).strip() for l in (text or "").splitlines() if l.strip()]
    flat = [l.replace(" ", "") for l in lines]
    g = InvoiceGuess(text=text)

    # 金額：「ご請求金額」などの行の金額（なければ一番大きい金額）
    for key in AMOUNT_KEYS:
        found = []
        for i, l in enumerate(flat):
            if key in l and not re.search(r"(消費税|税額|小計|内税|うち)", l):
                found += _amounts(l) or (_amounts(flat[i + 1]) if i + 1 < len(flat) else [])
        if found:
            g.amount = max(found)
            break
    if g.amount is None:
        allv = [v for l in flat for v in _amounts(l) if v < 100_000_000]
        g.amount = max(allv) if allv else None

    # 支払期限
    for l in flat:
        if any(k in l for k in DUE_KEYS):
            g.due_date = _date(l, today)
            if g.due_date:
                break

    # 振込先：銀行・支店・口座の行
    bank_lines = [lines[i] for i, l in enumerate(flat) if any(w in l for w in BANK_WORDS) and "御中" not in l]
    g.bank = "\n".join(dict.fromkeys(bank_lines))[:300]

    # 請求元：登録済みの支払先の名前が書いてあればそれ。なければ「株式会社〜」の行（宛名の御中・様・自社は除く）
    own = re.sub(r"\s", "", nfkc(own_company))
    joined = "".join(flat)
    for name in sorted(known_payees or [], key=len, reverse=True):
        if re.sub(r"\s", "", nfkc(name)) and re.sub(r"\s", "", nfkc(name)) in joined:
            g.payee = name
            break
    if not g.payee:
        for l in flat:
            if COMPANY.search(l) and "御中" not in l and "様" not in l and (not own or own not in l):
                g.payee = re.sub(r"(TEL|電話|〒|住所|登録番号).*$", "", l)[:60].strip()
                break
    return g
