"""明細書 PDF（A4）。既存の「明細書 / STATEMENT」のレイアウトを再現する。

確定済みは確定時のスナップショットから作るため、後から設定を変えても同じ明細を再出力できる。
フォントは TrueType の日本語フォントを埋め込む（文字化け防止）。
"""

from __future__ import annotations

import io
import os
from datetime import date
from functools import lru_cache

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_CANDIDATES = [
    os.environ.get("PDF_FONT_PATH", ""),
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/ipafont/ipag.ttf",
]


@lru_cache(maxsize=1)
def font_name() -> str:
    for path in FONT_CANDIDATES:
        if path and os.path.exists(path):
            pdfmetrics.registerFont(TTFont("JP", path))
            return "JP"
    raise RuntimeError("PDF用の日本語フォント（TrueType）が見つかりません。PDF_FONT_PATH を設定してください。")


def _yen(n) -> str:
    n = int(n or 0)
    return f"-{abs(n):,}" if n < 0 else f"{n:,}"


def slip_rows(snapshot: dict) -> list[tuple[str, str, int]]:
    """明細の行（内容・内訳・符号付き金額）。0円の行は『常に表示』の項目だけ出す。"""
    rows = []
    for l in snapshot["lines"]:
        if not l.get("show_on_slip", True):
            continue
        amount = int(l["amount"] or 0)
        if amount == 0 and not l.get("always_show"):
            continue
        signed = amount if l["kind"] == "earning" else -amount
        rows.append((l["name"], l.get("detail") or "", signed))
    return rows


NAVY = colors.HexColor("#1e293b")
LABEL = colors.HexColor("#6b7280")
RULE = colors.HexColor("#c8c2a8")


def draw_statement(c: canvas.Canvas, snap: dict, *, draft: bool = False) -> None:
    """スプレッドシートの「個人別明細」と同じ構成：明細書/STATEMENT・宛名・支払日・内容/内訳/金額（税込）・合計・差引支払額・発行者。"""
    f = font_name()
    W, H = A4
    left, right = 20 * mm, W - 20 * mm
    pay = date.fromisoformat(snap["payment_date"])

    if draft:
        c.saveState()
        c.setFont(f, 60)
        c.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.12))
        c.translate(W / 2, H / 2)
        c.rotate(30)
        c.drawCentredString(0, 0, "未確定（確認用）")
        c.restoreState()

    # 見出し
    y = H - 30 * mm
    c.setFillColor(colors.black)
    c.setFont(f, 22)
    c.drawString(left, y, "明 細 書")
    c.setFont(f, 10)
    c.setFillColor(LABEL)
    c.drawRightString(right, y + 1 * mm, "STATEMENT")
    c.setStrokeColor(RULE)
    c.setLineWidth(0.8)
    c.line(left, y - 5 * mm, right, y - 5 * mm)

    # 宛名・支払日
    y -= 18 * mm
    c.setFont(f, 9)
    c.setFillColor(LABEL)
    c.drawString(left, y, "宛名")
    c.drawString(left + 100 * mm, y, "支払日")
    c.setFillColor(colors.black)
    c.setFont(f, 12)
    c.drawString(left + 22 * mm, y, f"{snap['member']['name']} 様")
    c.drawRightString(right, y, f"{pay.year}年{pay.month}月{pay.day}日")
    c.setStrokeColor(colors.HexColor("#e5e7eb"))
    c.setLineWidth(0.5)
    c.line(left, y - 3 * mm, right, y - 3 * mm)

    # 表
    rows = slip_rows(snap)
    total = sum(r[2] for r in rows)
    y -= 14 * mm
    col1, col2, col3 = left, left + 38 * mm, right
    head_h, row_h = 10 * mm, 11 * mm
    c.setFillColor(NAVY)
    c.rect(left, y - head_h, right - left, head_h, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont(f, 10)
    c.drawString(col1 + 3 * mm, y - 6.6 * mm, "内容")
    c.drawString(col2 + 3 * mm, y - 6.6 * mm, "内訳")
    c.drawRightString(col3 - 3 * mm, y - 6.6 * mm, "金額（税込）")
    y -= head_h
    c.setStrokeColor(colors.HexColor("#e5e7eb"))
    for name, detail, amount in rows:
        c.setFillColor(colors.black)
        c.setFont(f, 10.5)
        c.drawString(col1 + 3 * mm, y - 7 * mm, name)
        c.setFont(f, 9.5)
        c.drawString(col2 + 3 * mm, y - 7 * mm, detail[:42])
        c.setFont(f, 10.5)
        c.drawRightString(col3 - 3 * mm, y - 7 * mm, f"{_yen(amount)}円")
        y -= row_h
        c.line(left, y, right, y)
    # 報酬額合計
    c.setFont(f, 10.5)
    c.drawString(col1 + 3 * mm, y - 7 * mm, "報酬額合計")
    c.drawRightString(col3 - 3 * mm, y - 7 * mm, f"{_yen(total)}円")
    y -= row_h
    c.line(left, y, right, y)
    # 差引支払額（黒帯）
    y -= 4 * mm
    bar_h = 9 * mm
    c.setFillColor(colors.black)
    c.rect(left, y - bar_h, right - left, bar_h, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont(f, 12)
    c.drawString(col1 + 3 * mm, y - 6.2 * mm, "差引支払額")
    c.setFont(f, 14)
    c.drawRightString(col3 - 3 * mm, y - 6.4 * mm, f"{_yen(snap['totals']['net'])}円")
    y -= bar_h

    # 発行者
    issuer = snap.get("issuer", {})
    y -= 16 * mm
    c.setStrokeColor(RULE)
    c.setLineWidth(0.8)
    c.line(left, y + 5 * mm, right, y + 5 * mm)
    for label1, value1, label2, value2 in (
        ("発行者", issuer.get("company", ""), "代表取締役" if issuer.get("representative") else "", issuer.get("representative", "")),
        ("所在地", issuer.get("address", ""), "TEL" if issuer.get("tel") else "", issuer.get("tel", "")),
    ):
        if not value1 and not value2:
            continue
        c.setFont(f, 8.5)
        c.setFillColor(LABEL)
        c.drawString(left, y, label1)
        if label2:
            c.drawString(left + 100 * mm, y, label2)
        c.setFillColor(colors.black)
        c.setFont(f, 9.5)
        c.drawString(left + 22 * mm, y, value1)
        if label2:
            c.drawString(left + 125 * mm, y, value2)
        y -= 6 * mm


def render_pdf(items: list[tuple[dict, bool]]) -> bytes:
    """[(snapshot, draft), ...] を1つの PDF にする（1人1ページ）。"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("明細書")
    for snap, draft in items:
        draw_statement(c, snap, draft=draft)
        c.showPage()
    c.save()
    return buf.getvalue()
