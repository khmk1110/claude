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


def draw_statement(c: canvas.Canvas, snap: dict, *, draft: bool = False) -> None:
    f = font_name()
    W, H = A4
    left, right = 22 * mm, W - 22 * mm
    month = date.fromisoformat(snap["month"])
    pay = date.fromisoformat(snap["payment_date"])

    if draft:
        c.saveState()
        c.setFont(f, 60)
        c.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.12))
        c.translate(W / 2, H / 2)
        c.rotate(30)
        c.drawCentredString(0, 0, "未確定（確認用）")
        c.restoreState()

    y = H - 30 * mm
    c.setFont(f, 24)
    c.drawCentredString(W / 2, y, "明 細 書")
    c.setFont(f, 10)
    c.setFillColor(colors.HexColor("#6b7280"))
    c.drawCentredString(W / 2, y - 7 * mm, "STATEMENT")
    c.setFillColor(colors.black)

    y -= 22 * mm
    c.setFont(f, 15)
    c.drawString(left, y, f"{snap['member']['name']} 様")
    c.setLineWidth(0.8)
    c.line(left, y - 2.5 * mm, left + 85 * mm, y - 2.5 * mm)
    c.setFont(f, 10.5)
    c.drawRightString(right, y + 6 * mm, f"{month.year}年{month.month}月分")
    c.drawRightString(right, y, f"支払日　{pay.year}年{pay.month}月{pay.day}日")

    # 表
    rows = slip_rows(snap)
    total = sum(r[2] for r in rows)
    y -= 16 * mm
    col1, col2, col3 = left, left + 52 * mm, right
    row_h = 9.5 * mm
    c.setFillColor(colors.HexColor("#f3f4f6"))
    c.rect(left, y - row_h, right - left, row_h, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont(f, 10.5)
    c.drawString(col1 + 3 * mm, y - 6.3 * mm, "内容")
    c.drawString(col2 + 3 * mm, y - 6.3 * mm, "内訳")
    c.drawRightString(col3 - 3 * mm, y - 6.3 * mm, "金額（税込）")
    c.setStrokeColor(colors.HexColor("#9ca3af"))
    c.line(left, y, right, y)
    c.line(left, y - row_h, right, y - row_h)
    y -= row_h
    c.setStrokeColor(colors.HexColor("#d1d5db"))
    for name, detail, amount in rows:
        c.setFont(f, 10.5)
        c.drawString(col1 + 3 * mm, y - 6.3 * mm, name)
        c.setFont(f, 9.5)
        c.drawString(col2 + 3 * mm, y - 6.3 * mm, detail[:40])
        c.setFont(f, 11)
        c.drawRightString(col3 - 3 * mm, y - 6.3 * mm, _yen(amount))
        y -= row_h
        c.line(left, y, right, y)
    c.setStrokeColor(colors.HexColor("#9ca3af"))
    for label, value, size in (("報酬額合計", total, 11.5), ("差引支払額", snap["totals"]["net"], 13)):
        c.setFont(f, size)
        c.drawString(col1 + 3 * mm, y - 6.6 * mm, label)
        c.drawRightString(col3 - 3 * mm, y - 6.6 * mm, f"{_yen(value)} 円")
        y -= row_h + 1 * mm
        c.line(left, y, right, y)
    c.setLineWidth(1.2)
    c.line(left, y, right, y)

    # 発行者
    issuer = snap.get("issuer", {})
    y -= 18 * mm
    c.setFont(f, 10)
    c.drawString(left, y, f"発行者　{issuer.get('company', '')}")
    if issuer.get("representative"):
        c.drawString(left + 95 * mm, y, issuer["representative"])
    y -= 6 * mm
    if issuer.get("address"):
        c.drawString(left, y, f"所在地　{issuer['address']}")
    if issuer.get("tel"):
        c.drawString(left + 95 * mm, y, f"TEL　{issuer['tel']}")


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
