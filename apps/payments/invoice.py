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
        # スマホのスクショ（A4を縮小したもの）は字が小さいので2倍にして読む
        if g.width < 1700:
            g = g.resize((g.width * 2, g.height * 2), Image.LANCZOS)
        elif g.width > 2600:
            g = g.resize((2400, int(g.height * 2400 / g.width)), Image.LANCZOS)
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
               "合計金額", "税込合計", "総合計", "総額", "請求合計", "合計")
DUE_KEYS = ("お支払期限", "お支払い期限", "支払期限", "振込期限", "お振込期限", "支払期日", "お支払期日", "入金期日", "入金日",
            "お支払日", "支払日", "期日", "期限")
BANK_WORDS = ("銀行", "信用金庫", "信金", "支店", "普通", "当座", "口座", "ゆうちょ", "名義")
COMPANY = re.compile(r"(株式会社|株式会|\(株\)|（株）|有限会社|\(有\)|合同会社|一般社団法人|合資会社)")
OWN_HINT = re.compile(r"(D|O|0)?REAM|EF+ECT|F+FECT|ドリーム", re.I)
LABEL_WORDS = ("小計", "消費税", "源泉所得税", "源泉徴収税", "請求金額", "ご請求金額", "合計", "値引", "税額")
_DATE = re.compile(r"(20\d{2})\s*[年/.\-]?\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})")


def _clean_numbers(line: str) -> str:
    """金額と間違えやすい数字（電話・郵便番号・日付・請求書番号・口座番号・登録番号）を消し、OCRの「.」「\」を直す。"""
    s = nfkc(line).replace("\\", "¥").replace("一", "-").replace("ー", "-")
    s = _DATE.sub(" ", s)
    s = re.sub(r"\d{2,4}\s*-\s*\d{2,4}\s*-\s*\d{3,4}", " ", s)                 # 電話番号
    s = re.sub(r"0\d0\s+\d{4}\s+\d{4}", " ", s)                                   # 080 5426 8557
    s = re.sub(r"〒?\s*\d{3}-\d{4}", " ", s)                                         # 郵便番号
    s = re.sub(r"(No\.?|NO\.?|番号|INV-?|T)\s*[:：]?\s*[\d\-]{6,}", " ", s)           # 請求書番号・登録番号
    s = re.sub(r"(普通|当座|口座|預金)[^\d]{0,6}\d{5,8}", " ", s)                      # 口座番号
    s = re.sub(r"(\d{4,})\.\d{3}(?!\d)", r"\1", s)                                  # 2000.000・2042.042 → 2000・2042
    s = re.sub(r"(?<!\d)(\d{1,3})\.(\d{3})(?!\d)", r"\1,\2", s)                      # 120.000 → 120,000
    return s


def _amounts(line: str, *, signed: bool = False) -> list[int]:
    line = _clean_numbers(line)
    line = re.sub(r"(\d)\s*[,，]\s+(\d{3})", r"\1,\2", line)
    line = re.sub(r"(?<![\d,])(\d{1,3})\s(\d{3})(?=\s*円|\s*$|\s*-)", r"\1,\2", line)  # 「108 251 円」→「108,251」
    vals = []
    for m in re.finditer(r"(-?)\s*[¥￥]?\s*(\d{1,3}(?:[,，]\d{3})+|\d{3,9})\s*(円|-)?", line):
        digits = re.sub(r"[,，]", "", m.group(2))
        has_mark = bool(m.group(3)) or "," in m.group(2) or "¥" in line[max(0, m.start() - 2):m.start(2)]
        v = int(digits)
        if not has_mark and v < 1000:
            continue
        vals.append(-v if (signed and m.group(1)) else v)
    return vals


def _date(line: str, today: date) -> date | None:
    s = nfkc(line)
    m = _DATE.search(s)
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


def _column_value(header: str, values_line: str, key: str) -> int | None:
    """「小計 消費税 源泉所得税 請求金額」の下の「20,000 2,000 -2,042 19,958」から、key の列の金額を取る。"""
    found = sorted((header.find(w), w) for w in LABEL_WORDS if w in header)
    labels = []
    for pos, w in found:
        if not any(pos >= p and pos < p + len(x) for p, x in labels):  # 「ご請求金額」と「請求金額」の重なりを除く
            labels.append((pos, w))
    vals = _amounts(values_line, signed=True)
    if len(labels) >= 2 and len(vals) == len(labels):
        for i, (_, w) in enumerate(labels):
            if key in w or w in key:
                return abs(vals[i])
    return None


def _payee_from_lines(lines: list[str], own: str) -> str:
    def is_own(l):
        return bool(OWN_HINT.search(l)) or (own and own in l) or "御中" in l or l.endswith("様") or "様" in l[-3:]

    def clean(l):
        l = re.sub(r"【[^】]*】", "", l)
        l = re.sub(r"^(発行者|請求元|会社名)[:：]?", "", l)
        l = re.sub(r"(TEL|電話|〒|住所|登録番号|E-?mail|代表取締役).*$", "", l, flags=re.I)
        l = re.sub(r"株式会(?!社)", "株式会社", l)
        return l.strip(" :：|-ー")[:60]

    for l in lines:
        if COMPANY.search(l) and not is_own(l):
            c = clean(l)
            # 「株式会社」以外に、文字（かな・漢字・英字）が2文字以上ある時だけ（画面の記号などを拾わない）
            if len(re.sub(r"[^\wぁ-んァ-ヶ一-龠]", "", re.sub(COMPANY, "", c))) >= 2:
                return c
    # 会社名の形でない時（〇〇コンサルティング・〇〇SHOPなど）：住所・郵便番号の直前の行
    addr = re.compile(r"^(〒|\d{3}-?\d{4}|住所|東京都|北海道|大阪府|京都府|.{2,3}県)")
    for i, l in enumerate(lines[:-1]):
        nxt = " ".join(lines[i + 1:i + 3])
        if addr.search(nxt.strip()) and not is_own(l) and not addr.search(l) and not re.search(r"\d{3,}", l) \
                and 2 <= len(l) <= 40 and not any(w in l for w in ("請求", "下記", "件名", "発行", "日付")):
            return clean(l)
    return ""


def _core(name: str) -> str:
    return re.sub(r"(株式会社|有限会社|合同会社|\(株\)|（株）|\s)", "", nfkc(name))


def guess(text: str, *, own_company: str = "", known_payees: list[str] | None = None, today: date | None = None) -> InvoiceGuess:
    today = today or date.today()
    lines = [nfkc(l).strip() for l in (text or "").splitlines() if l.strip()]
    flat = [l.replace(" ", "") for l in lines]
    g = InvoiceGuess(text=text)

    # 金額：「ご請求金額」などの行（その次・前の行も見る）。表の見出し（小計・消費税・請求金額…）なら列を合わせる
    for key in AMOUNT_KEYS:
        found = []
        for i, l in enumerate(flat):
            if key not in l or re.search(r"(うち|内税)", l) or (key == "合計" and re.search(r"(消費税|税額|小計)", l)):
                continue
            if i + 1 < len(flat):
                col = _column_value(l, lines[i + 1], key)
                if col:
                    found.append(col)
                    continue
            here = _amounts(l[l.find(key):]) or _amounts(l)
            found += here or (_amounts(flat[i + 1]) if i + 1 < len(flat) else []) or (_amounts(flat[i - 1]) if i else [])
        if found:
            g.amount = max(found)
            break
    if g.amount is None:
        allv = [v for l in flat for v in _amounts(l) if v < 100_000_000]
        g.amount = max(allv) if allv else None

    # 支払期限：見出しの後ろ（なければ次の行）の日付。見出しが読めない時は、書いてある日付のうち一番遅い日
    for i, l in enumerate(flat):
        for k in DUE_KEYS:
            if k in l:
                g.due_date = _date(l[l.find(k):], today) or (_date(flat[i + 1], today) if i + 1 < len(flat) else None)
                if g.due_date:
                    break
        if g.due_date:
            break
    if g.due_date is None:
        dates = [d for l in flat for d in [_date(l, today)] if d]
        g.due_date = max(dates) if dates else None

    # 振込先：銀行・支店・口座の行（見出しや日付は除く）
    bank_lines = []
    for i, l in enumerate(lines):
        if any(w in flat[i] for w in BANK_WORDS) and "御中" not in l:
            t = re.sub(r"【[^】]*】|お支払条件|銀行振込(?=【|$)|お支払期限|までにお支払いください。?", " ", l)
            t = _DATE.sub(" ", t)
            t = re.sub(r"\s+", " ", t).strip(" |:：")
            if t:
                bank_lines.append(t)
    g.bank = "\n".join(dict.fromkeys(bank_lines))[:300]

    # 請求元：前に保存した支払先の名前が書いてあればそれを優先（「株式会社」を除いた名前で探す）
    own = re.sub(r"\s", "", nfkc(own_company))
    joined = "".join(flat).upper()
    for name in sorted(known_payees or [], key=lambda n: len(_core(n)), reverse=True):
        core = _core(name).upper()
        if len(core) >= 2 and core in joined and not (own and core in own.upper()):
            g.payee = name
            break
    if not g.payee:
        g.payee = _payee_from_lines(flat, own)
        # 読み間違い（水岩→林岩など）があっても、前に保存した支払先とほぼ同じ名前ならその名前にする
        if g.payee and known_payees:
            from difflib import SequenceMatcher

            core = _core(g.payee).upper()
            best = max(((SequenceMatcher(None, core, _core(n).upper()).ratio(), n) for n in known_payees), default=(0, ""))
            if best[0] >= 0.7:
                g.payee = best[1]
    return g


def guess_sale(text: str, *, own_company: str = "", known_clients: list[str] | None = None, today: date | None = None) -> InvoiceGuess:
    """売上の資料（自社の請求書・取引先からの支払通知書など）を読む。

    取引先は、宛名（「〇〇 御中」「〇〇 様」）が自社でなければ宛名、自社宛てなら発行元の会社。
    """
    g = guess(text, own_company=own_company, today=today)  # 金額・日付は請求書と同じ読み方
    g.payee = ""
    lines = [nfkc(l).strip().replace(" ", "") for l in (text or "").splitlines() if l.strip()]
    own = re.sub(r"\s", "", nfkc(own_company))
    joined = "".join(lines)
    for name in sorted(known_clients or [], key=len, reverse=True):
        key = re.sub(r"\s", "", nfkc(name))
        if key and key in joined and (not own or key not in own):
            g.payee = name
            break
    if not g.payee:
        for l in lines:
            if ("御中" in l or l.endswith("様")) and (not own or own not in l):
                g.payee = re.sub(r"(御中|様).*$", "", l).strip()[:60]
                break
    if not g.payee:
        for l in lines:
            if COMPANY.search(l) and (not own or own not in l) and "御中" not in l:
                g.payee = re.sub(r"(TEL|電話|〒|住所|登録番号).*$", "", l)[:60].strip()
                break
    return g


@dataclass
class TextLine:
    name: str
    amount: int
    done: bool = False      # 支払い済み・入金済み
    category: str = ""
    line: str = ""


def _category_from_name(name: str) -> str:
    for word, cat in (("家賃", "家賃"), ("給与", "給与"), ("給料", "給与"), ("税", "税金"), ("返済", "返済"), ("広告", "広告"),
                      ("委託", "外注・業務委託"), ("外注", "外注・業務委託")):
        if word in name:
            return cat
    return ""


def parse_text_lines(text: str) -> list[TextLine]:
    """「まろくん宅家賃 105,500円 支払い済」「家賃 3万」「(株)adbuys：165000」のような1行1件の文を読む。"""
    out = []
    for raw in (text or "").splitlines():
        line = nfkc(raw).strip().strip("・-*●")
        if not line:
            continue
        # 「返済」の「済」は状況ではないので、「支払い済」「済」（単独）などの時だけ済みにする
        status = r"(支払い?済み?|払い?済み?|振込済み?|入金済み?|払った|入金あり|(?<=[\s　:：])済み?(?=\s|$)|^済み?(?=\s|$))"
        done = bool(re.search(status, line))
        body = re.sub(status + r"|未払い?|未入金", " ", line)
        m = None
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*万(\d{1,4})?|[¥￥]?(\d{1,3}(?:,\d{3})+|\d+)\s*円?", body):
            pass  # 行の最後の金額を使う
        if not m:
            continue
        if m.group(1):
            amount = int(round(float(m.group(1)) * 10000)) + (int(m.group(2)) if m.group(2) else 0)
        else:
            amount = int(m.group(3).replace(",", ""))
        name = body[:m.start()].strip(" :：、,=＝-")
        if not name:
            name = body[m.end():].strip(" :：、,=＝-")
        name = re.sub(r"\s+", " ", name)[:200]
        if not name or amount <= 0:
            continue
        out.append(TextLine(name=name, amount=amount, done=done, category=_category_from_name(name), line=raw.strip()))
    return out
