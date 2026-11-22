"""シフト表の PNG 画像（LINE 共有用）。全員分を横長・高解像度で1枚にまとめる。"""

from __future__ import annotations

import io
import os
from datetime import date
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from apps.core.calendar_jp import holiday_name, weekday_label
from apps.core.months import days_in_month

from .models import WorkType
from .services import month_grid

FONT_CANDIDATES = [
    os.environ.get("SHIFT_FONT_PATH", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
]


@lru_cache(maxsize=8)
def font(size: int):
    for path in FONT_CANDIDATES:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, size)
    raise RuntimeError("日本語フォントが見つかりません。SHIFT_FONT_PATH を設定してください。")


def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))


def _center(draw, box, text, fnt, fill):
    x0, y0, x1, y1 = box
    l, t, r, b = draw.textbbox((0, 0), text, font=fnt)
    draw.text((x0 + (x1 - x0 - (r - l)) / 2 - l, y0 + (y1 - y0 - (b - t)) / 2 - t), text, font=fnt, fill=fill)


def render_png(month: date, company: str, employment_type: str | None = None, scale: int = 2) -> bytes:
    rows = month_grid(month, employment_type)
    days = days_in_month(month)
    s = scale
    cell_w, cell_h, head_h = 46 * s, 44 * s, 58 * s
    name_w, total_w, pad, title_h = 170 * s, 96 * s, 28 * s, 76 * s
    legend_h = 56 * s
    width = pad * 2 + name_w + cell_w * len(days) + total_w
    height = pad * 2 + title_h + head_h + cell_h * max(len(rows), 1) + legend_h
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    line, fg, muted = (209, 213, 219), (17, 24, 39), (107, 114, 128)
    red, blue = (185, 28, 28), (29, 78, 216)

    d.text((pad, pad), f"{company}　{month.year}年{month.month}月 シフト表", font=font(30 * s), fill=fg)
    top = pad + title_h
    x_days = pad + name_w
    x_total = x_days + cell_w * len(days)

    # 見出し
    d.rectangle([pad, top, x_total + total_w, top + head_h], fill=(243, 244, 246))
    _center(d, (pad, top, x_days, top + head_h), "名前", font(18 * s), fg)
    _center(d, (x_total, top, x_total + total_w, top + head_h), "出勤日数", font(17 * s), fg)
    for i, day in enumerate(days):
        x = x_days + i * cell_w
        color = red if (holiday_name(day) or day.weekday() == 6) else blue if day.weekday() == 5 else fg
        _center(d, (x, top + 2 * s, x + cell_w, top + head_h / 2 + 4 * s), str(day.day), font(19 * s), color)
        _center(d, (x, top + head_h / 2, x + cell_w, top + head_h - 2 * s), weekday_label(day), font(15 * s), color)

    # 土日祝の列をうっすら色分け（休みは空欄なので、曜日の区切りが分かるように）
    body_top, body_bot = top + head_h, top + head_h + cell_h * max(len(rows), 1)
    for i, day in enumerate(days):
        x = x_days + i * cell_w
        if holiday_name(day) or day.weekday() == 6:
            d.rectangle([x, body_top, x + cell_w, body_bot], fill=(255, 244, 244))
        elif day.weekday() == 5:
            d.rectangle([x, body_top, x + cell_w, body_bot], fill=(243, 247, 255))

    # 本体
    y = top + head_h
    for row in rows:
        _center(d, (pad + 6 * s, y, x_days - 6 * s, y + cell_h), row.member.display_label, font(19 * s), fg)
        for i, (_, wt) in enumerate(row.cells):
            x = x_days + i * cell_w
            if wt and wt.short_label:  # 休みは何も描かない（空欄）
                d.rectangle([x, y, x + cell_w, y + cell_h], fill=_hex(wt.color))
                _center(d, (x, y, x + cell_w, y + cell_h), wt.short_label, font(18 * s), _hex(wt.text_color))
        days_txt = f"{row.planned_days.normalize():f}"
        _center(d, (x_total, y, x_total + total_w, y + cell_h), days_txt, font(22 * s), fg)
        y += cell_h

    # 罫線
    for i in range(len(rows) + 1):
        yy = top + head_h + i * cell_h
        d.line([pad, yy, x_total + total_w, yy], fill=line, width=s)
    d.line([pad, top, x_total + total_w, top], fill=line, width=s)
    for x in [pad, x_days] + [x_days + i * cell_w for i in range(1, len(days) + 1)] + [x_total + total_w]:
        d.line([x, top, x, y], fill=line, width=s)

    # 凡例
    lx, ly = pad, y + 16 * s
    for wt in WorkType.objects.filter(is_active=True).exclude(short_label=""):
        d.rectangle([lx, ly, lx + 34 * s, ly + 28 * s], fill=_hex(wt.color), outline=line)
        _center(d, (lx, ly, lx + 34 * s, ly + 28 * s), wt.short_label, font(14 * s), _hex(wt.text_color))
        d.text((lx + 42 * s, ly + 3 * s), wt.name, font=font(16 * s), fill=muted)
        lx += 42 * s + d.textlength(wt.name, font=font(16 * s)) + 28 * s

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
