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


BRACKETS = "【】[]［］「」『』()（）|｜Il1"


def _split_header(header: str, known: list[str]) -> list[str]:
    """【平川＋田所・谷口】を名前に分ける。OCRで「＋」が「十」になったり「・」が消えた時は、登録済みの名前・別名で切り分ける。"""
    parts = [clean_name(n) for n in SPLIT_NAMES.split(header) if clean_name(n)]
    if not known:
        return parts
    known = sorted({k for k in known if len(k) >= 2}, key=len, reverse=True)
    out = []
    for part in parts:
        if part in known:
            out.append(part)
            continue
        found, i, rest = [], 0, ""
        while i < len(part):
            hit = next((k for k in known if part.startswith(k, i)), None)
            if hit:
                if rest.strip("十+"):
                    found.append(rest.strip("十+"))
                rest = ""
                found.append(hit)
                i += len(hit)
            else:
                rest += part[i]
                i += 1
        if rest.strip("十+"):
            found.append(rest.strip("十+"))
        out.extend(found if any(f in known for f in found) else [part])
    return out


def parse_dashboard(text: str, known_names: list[str] | None = None) -> Parsed:
    """営業ダッシュボード（LINEの文）を読む。

    【堀田】の次の行「131/90（進捗…）」の 131 が獲得件数。
    【平川＋田所・谷口】のように複数人のブロックは、名前の行の次の「3日8件」や「じゅんき：67件」を個人の件数とし、
    書かれていない1人はブロックの合計から他の人を引いた件数にする。
    """
    p = Parsed()
    lines = [nfkc(l).strip() for l in (text or "").splitlines()]
    m = (re.search(r"【\s*(\d{1,2})\s*月", nfkc(text or ""))
         or re.search(r"(\d{1,2})\s*月\s*(?:最終)?\s*(?:実績|結果)", nfkc(text or "")))
    if m:
        p.month = int(m.group(1))
    known = [clean_name(nfkc(k)).replace(" ", "") for k in (known_names or [])]
    # 意味のない行（区切り線・OCRのごみ）を除く
    lines = [ln for ln in lines if ln and len(re.sub(r"[\W_xX]", "", ln)) >= 1 and not re.fullmatch(r"[xX\W_十・]{1,3}", ln)]
    blocks, cur = [], None
    for idx, ln in enumerate(lines):
        hm = re.match(r"^【(.+?)】", ln)
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        # 【】が読み取れなくても、次の行が「131/90」なら名前の行とみなす
        looks_header = not re.search(r"\d", ln) and re.match(r"^\d+\s*/\s*\d+", nxt)
        if hm and not re.search(r"\d+\s*月", hm.group(1)):
            header = hm.group(1)
        elif looks_header:
            header = ln.strip(BRACKETS + " ")
        else:
            header = None
        if header:
            cur = {"header": header, "lines": [], "raw": ln}
            blocks.append(cur)
        elif cur is not None and not set(ln) <= set("⸻-ー—=_ "):
            cur["lines"].append(ln)
    for b in blocks:
        names = _split_header(b["header"], known)
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


def _clean_table_name(raw: str) -> str:
    """OCRで混ざる行番号・記号を落とし、文字の間の空白を詰める。"""
    s = re.sub(r"\s+", "", nfkc(raw))
    s = re.sub(r"^[\d\W_]+", "", s)
    s = re.sub(r"[\W_]+$", "", s)
    return clean_name(s)


def parse_count_table(text: str) -> Parsed:
    """入金確定シートの「営業別件数」表をコピペ・OCRした文を読む。行の最後の数字＝その人の合計。

    名前は行の数字より前の部分（OCRで1文字ずつ空白が入っても詰める）。
    「月合計」の行は全体の合計として、取り込んだ合計と一致するかの確認に使う。
    """
    p = Parsed()
    for raw in (text or "").splitlines():
        ln = nfkc(raw).strip()
        if not ln:
            continue
        tokens = [t for t in re.split(r"[\t ,|｜]+", ln) if t]
        # 行末から続く数字の列（件数）と、その前の部分（名前）に分ける
        k = len(tokens)
        while k > 0 and re.fullmatch(r"\d[\d,]*", tokens[k - 1]):
            k -= 1
        nums = [t.replace(",", "") for t in tokens[k:]]
        name = _clean_table_name("".join(tokens[:k]))
        if not nums or not name:
            continue
        if "合計" in name or name in ("計", "総計"):
            p.total = int(nums[-1])
            continue
        if any(kw in name for kw in ("営業", "■", "名前", "氏名")) or re.search(r"\d+月", name):
            continue
        p.entries.append(Entry(name, int(nums[-1]), line=raw.strip()))
    if not p.entries:
        p.warnings.append("「名前 … 合計」の行が見つかりませんでした。")
    return p


def guess_member(name: str, candidates: list[tuple[int, str]], taken: set | None = None):
    """OCRで1〜2文字違った名前を、登録メンバーの名前・別名から推定する。

    candidates: (member_id, 名前または別名) の一覧。はっきり1人に決まる時だけ member_id を返す。
    """
    from difflib import SequenceMatcher

    from apps.core.text import normalize_name

    key = normalize_name(name)
    if len(key) < 2:
        return None
    best: dict[int, float] = {}
    for mid, cand in candidates:
        c = normalize_name(cand)
        if not c:
            continue
        r = SequenceMatcher(None, key, c).ratio()
        if c in key or key in c:
            r = max(r, 0.75)
        best[mid] = max(best.get(mid, 0), r)
    ranked = sorted(((r, mid) for mid, r in best.items() if not taken or mid not in taken), reverse=True)
    if not ranked:
        return None
    r1, mid = ranked[0]
    r2 = ranked[1][0] if len(ranked) > 1 else 0
    if r1 >= 0.6 and r1 - r2 >= 0.15:
        return mid
    if r1 >= 0.3 and r1 - r2 >= 0.25 and key[0] == normalize_name([c for m, c in candidates if m == mid][0])[:1]:
        return mid
    return None


def _join_cjk(line: str) -> str:
    """OCRが日本語の1文字ずつに入れた空白を詰める（英数字の間の空白は残す）。"""
    return re.sub(r"(?<=[^\x00-\x7F]) +(?=[^\x00-\x7F])", "", line)


def _open_image(data: bytes):
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
    # スマホのスクショ（幅1000px以上）はそのままで十分読める。拡大すると時間だけかかる
    if img.width < 1000:
        img = img.resize((img.width * 2, img.height * 2))
    elif img.width > 2000:
        img = img.resize((2000, int(img.height * 2000 / img.width)))
    return img


def table_text_from_image(data: bytes) -> str:
    """表のスクショを、行ごとに「名前<TAB>数字…」の文にする。

    まず全体を読んで行と数字の位置を見つけ、名前の部分だけを日本語モードでもう一度読む（名前の精度を上げる）。
    """
    from PIL import ImageOps

    from apps.expenses import ocr

    img = ocr.remove_grid_lines(_open_image(data))
    out = []
    for line in ocr.group_lines(ocr.ocr_words(img, lang="jpn+eng", psm=6), tolerance=18):
        ws = sorted(line.words, key=lambda w: w.left)
        first_num = next((i for i, w in enumerate(ws) if re.fullmatch(r"\d[\d,]*", nfkc(w.text))
                          and all(re.fullmatch(r"\d[\d,]*", nfkc(x.text)) for x in ws[i:])), None)
        if first_num is None:
            out.append(_join_cjk(" ".join(w.text for w in ws)))
            continue
        nums = [nfkc(w.text) for w in ws[first_num:]]
        name = ""
        if first_num > 0:
            top = max(0, min(w.top for w in ws) - 8)
            bot = max(w.top + w.height for w in ws) + 8
            crop = img.crop((0, top, max(1, ws[first_num].left - 5), bot))
            if crop.height < 80:  # 名前の部分だけ拡大して読む（小さい範囲なので速い）
                crop = crop.resize((crop.width * 2, crop.height * 2))
            crop = ImageOps.expand(crop, border=20, fill=255)
            name = "".join(w.text for w in ocr.ocr_words(crop, lang="jpn", psm=7)) or "".join(w.text for w in ws[:first_num])
        out.append("\t".join([name] + nums))
    return "\n".join(out)


def dashboard_text_from_image(data: bytes) -> str:
    """LINEのスクショ（吹き出しの背景・絵文字あり）を読む。読み方を何通りか試し、「131/90」の行が一番多く読めた結果を使う。"""
    from apps.expenses import ocr

    img = _open_image(data)
    variants = [(img, 6), (img.point(lambda v: 255 if v > 175 else 0), 6), (img, 4)]
    best, best_score = "", -1
    for im, psm in variants:
        words = ocr.ocr_words(im, lang="jpn+eng", psm=psm)
        text = "\n".join(_join_cjk(line.text) for line in ocr.group_lines(words, tolerance=18))
        blocks = len(re.findall(r"(?m)^\s*\d+\s*/\s*\d+", nfkc(text)))
        score = blocks * 2 + nfkc(text).count("件")
        if score > best_score:
            best, best_score = text, score
        if blocks >= 2:  # 十分読めたら他の読み方は試さない（時間短縮）
            break
    return best


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


def is_slow_upload(name: str) -> bool:
    """文字の読み取り（OCR）が要る＝時間がかかるファイル。"""
    return name.lower().endswith(IMAGE_EXT + (".pdf",))


def text_from_upload(f, kind: str = "confirmed") -> str:
    """スクショ（画像）は文字を読み取り、Excel/CSV は表を文にする。"""
    name = f.name.lower()
    data = f.read()
    if name.endswith(IMAGE_EXT):
        if kind == "confirmed":
            return table_text_from_image(data)
        return dashboard_text_from_image(data)
    if name.endswith((".xlsx", ".xls", ".csv")):
        from apps.expenses.parsers import read_table_rows

        fmt = "csv" if name.endswith(".csv") else ("xls" if name.endswith(".xls") else "xlsx")
        rows = read_table_rows(data, fmt)
        return "\n".join("\t".join("" if c is None else str(c) for c in r) for r in rows)
    if name.endswith(".pdf"):
        from apps.expenses import ocr

        return "\n".join(ocr.pdf_text_pages(data))
    return data.decode("utf-8-sig", "replace")
