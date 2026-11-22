"""営業件数の取り込み（LINEの文・スクショ・表のコピペ・Excel/CSV）。

- 獲得件数: 営業ダッシュボード（LINEで届く【名前】ごとの実績の文）
- 入金確定件数: 入金確定シート（スプレッドシートの「営業別件数」表）
読み取った結果は必ず確認画面で見てから保存する（名前の照合・合計の一致を確認）。
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field

SPLIT_NAMES = re.compile(r"[＋+・、,／/＆&]")


@dataclass
class Entry:
    name: str
    count: int
    attendance: float | None = None
    line: str = ""
    note: str = ""


@dataclass
class Parsed:
    entries: list = field(default_factory=list)
    month: int | None = None
    total: int | None = None  # 表の「月合計」など
    warnings: list = field(default_factory=list)


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def clean_name(s: str) -> str:
    s = re.sub(r"[🔥🎉⭐️✨👏💪\s]", "", nfkc(s))
    return s.strip("【】[]「」:：")


def parse_dashboard(text: str) -> Parsed:
    """営業ダッシュボード（LINEの文）を読む。

    【堀田】の次の行「131/90（進捗…）」の 131 が獲得件数。
    【平川＋田所・谷口】のように複数人のブロックは、名前の行の次の「3日8件」や「じゅんき：67件」を個人の件数とし、
    書かれていない1人はブロックの合計から他の人を引いた件数にする。
    """
    p = Parsed()
    lines = [nfkc(l).strip() for l in (text or "").splitlines()]
    m = re.search(r"【\s*(\d{1,2})\s*月", text and nfkc(text) or "")
    if m:
        p.month = int(m.group(1))
    blocks, cur = [], None
    for ln in lines:
        hm = re.match(r"^【(.+?)】", ln)
        if hm and not re.search(r"\d+\s*月", hm.group(1)):
            cur = {"header": hm.group(1), "lines": [], "raw": ln}
            blocks.append(cur)
        elif cur is not None and ln and not set(ln) <= set("⸻-ー—=_ "):
            cur["lines"].append(ln)
    for b in blocks:
        names = [clean_name(n) for n in SPLIT_NAMES.split(b["header"]) if clean_name(n)]
        total = att = None
        explicit: dict[str, Entry] = {}
        pending_name = None
        for ln in b["lines"]:
            if total is None:
                mt = re.match(r"^(\d+)\s*/\s*\d+", ln)
                if mt:
                    total = int(mt.group(1))
                    continue
            ma = re.search(r"出勤\s*([\d.]+)\s*/", ln)
            if ma and att is None:
                att = float(ma.group(1))
                continue
            mc = re.match(r"^(\S+?)\s*[:：]\s*(\d+)\s*件", ln)
            if mc:
                explicit[clean_name(mc.group(1))] = Entry(clean_name(mc.group(1)), int(mc.group(2)), line=ln)
                pending_name = None
                continue
            md = re.match(r"^([\d.]+)\s*日\s*(\d+)\s*件", ln)
            if md and pending_name:
                explicit[pending_name] = Entry(pending_name, int(md.group(2)), float(md.group(1)), line=f"{pending_name} {ln}")
                pending_name = None
                continue
            if re.fullmatch(r"[^\d\s|｜:：/]{1,8}", clean_name(ln)) and clean_name(ln) in names:
                pending_name = clean_name(ln)
        if total is None:
            p.warnings.append(f"【{b['header']}】の件数（例: 131/90）が見つかりませんでした。")
            continue
        p.entries.extend(explicit.values())
        rest = [n for n in names if n not in explicit]
        if len(rest) == 1:
            count = total - sum(e.count for e in explicit.values())
            note = f"【{b['header']}】{total}件 − 他のメンバー" if explicit else ""
            p.entries.append(Entry(rest[0], count, att, line=b["raw"], note=note))
        elif len(rest) > 1:
            p.warnings.append(f"【{b['header']}】は人ごとの件数が分からない人がいます（{'・'.join(rest)}）。確認画面で入力してください。")
            for n in rest:
                p.entries.append(Entry(n, 0, None, line=b["raw"], note="件数が不明（手入力）"))
    if not p.entries:
        p.warnings.append("【名前】と「件数/目標」の形が見つかりませんでした。")
    return p


def parse_count_table(text: str) -> Parsed:
    """入金確定シートの「営業別件数」表をコピペ・OCRした文を読む。行の最後の数字＝その人の合計。

    「月合計」の行は全体の合計として、取り込んだ合計と一致するかの確認に使う。
    """
    p = Parsed()
    for raw in (text or "").splitlines():
        ln = nfkc(raw).strip()
        if not ln:
            continue
        tokens = re.split(r"[\t ,|｜]+", ln)
        name = tokens[0]
        nums = [t.replace(",", "") for t in tokens[1:] if re.fullmatch(r"[\d,]+", t)]
        if not nums or re.search(r"\d", name):
            continue
        if "合計" in name or name in ("計", "総計"):
            p.total = int(nums[-1])
            continue
        if any(k in name for k in ("営業", "■", "名前", "氏名")):
            continue
        p.entries.append(Entry(clean_name(name), int(nums[-1]), line=raw.strip()))
    if not p.entries:
        p.warnings.append("「名前 … 合計」の行が見つかりませんでした。")
    return p


def text_from_upload(f) -> str:
    """スクショ（画像）は文字を読み取り、Excel/CSV は表を文にする。"""
    name = f.name.lower()
    data = f.read()
    if name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        from PIL import Image

        from apps.expenses import ocr

        img = Image.open(io.BytesIO(data)).convert("L")
        if img.width < 1600:
            img = img.resize((img.width * 2, img.height * 2))
        words = ocr.ocr_words(img, lang="jpn+eng", psm=6)
        return "\n".join(line.text for line in ocr.group_lines(words, tolerance=18))
    if name.endswith((".xlsx", ".xls", ".csv")):
        from apps.expenses.parsers import read_table_rows

        fmt = "csv" if name.endswith(".csv") else ("xls" if name.endswith(".xls") else "xlsx")
        rows = read_table_rows(data, fmt)
        return "\n".join("\t".join("" if c is None else str(c) for c in r) for r in rows)
    if name.endswith(".pdf"):
        from apps.expenses import ocr

        return "\n".join(ocr.pdf_text_pages(data))
    return data.decode("utf-8-sig", "replace")
