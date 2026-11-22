"""既存シフト表（「出勤日」または「休み」＋日付リスト）の読み取り。"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation

_SEP = re.compile(r"[,，、.．\s・/]+")


class DayListError(ValueError):
    pass


def parse_day_list(value, last_day: int) -> list[int]:
    """'1,2,3' / '1、2' / '1.4.8' / 21.22（数値）などを日付の一覧にする。

    範囲外の日付や数字以外が含まれる場合はエラーにする（推測で補正しない）。
    数値として保存されて末尾の0が落ちた可能性がある値（例: 21.2）もエラーにして確認を促す。
    """
    if value is None or value == "":
        return []
    if isinstance(value, (int, float, Decimal)):
        text = str(value)
        if isinstance(value, float):
            try:
                text = format(Decimal(repr(value)), "f")
            except InvalidOperation:
                pass
            if text.endswith(".0"):
                text = text[:-2]
        parts = text.split(".")
        if len(parts) == 2 and len(parts[1]) == 1:
            raise DayListError(f"「{text}」は数値として保存され、日付が欠けた可能性があります（例: 21.20 → 21.2）。")
    else:
        text = unicodedata.normalize("NFKC", str(value))
    days = []
    for token in _SEP.split(text.strip()):
        if token == "":
            continue
        if not token.isdigit():
            raise DayListError(f"日付として読めない値があります: 「{token}」")
        d = int(token)
        if not 1 <= d <= last_day:
            raise DayListError(f"{d}日はこの月にありません。")
        if d not in days:
            days.append(d)
    return sorted(days)


def read_shift_sheet(data: bytes, sheet: str = "シフト入力") -> tuple:
    """既存シフト表の「シフト入力」シートを読み、(対象月, 行の一覧) を返す。書き込みはしない。

    行: {"row", "name", "mode", "value", "work_days"(list), "error", "member_id", "candidates", "method"}
    「出勤日」: 入力した日＝出勤、それ以外＝休み / 「休み」: 入力した日＝休み、それ以外＝出勤
    """
    import calendar
    import io
    from datetime import date

    import openpyxl

    from apps.members.matching import match_member

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    if sheet not in wb.sheetnames:
        raise DayListError(f"「{sheet}」シートが見つかりません。")
    ws = wb[sheet]
    try:
        month = date(int(ws["B1"].value), int(ws["E1"].value), 1)
    except (TypeError, ValueError) as e:
        raise DayListError("年（B1）・月（E1）が読めません。") from e
    last = calendar.monthrange(month.year, month.month)[1]
    rows = []
    for r in range(4, ws.max_row + 1):
        name, mode, value = (ws.cell(r, c).value for c in (1, 2, 3))
        if not name:
            continue
        name = str(name).strip()
        item = {"row": r, "name": name, "mode": mode, "value": "" if value is None else str(value),
                "work_days": [], "error": "", "member_id": None, "candidates": [], "method": ""}
        try:
            listed = set(parse_day_list(value, last))
            if mode not in ("出勤日", "休み"):
                raise DayListError(f"入力方式「{mode}」は「出勤日」か「休み」にしてください。")
            work = listed if mode == "出勤日" else set(range(1, last + 1)) - listed
            item["work_days"] = sorted(work)
        except DayListError as e:
            item["error"] = str(e)
        m = match_member(name, include_retired=False)
        item["method"] = m.method
        item["candidates"] = [c.pk for c in m.candidates]
        if m.ok:
            item["member_id"] = m.member.pk
        rows.append(item)
    return month, rows


def apply_shift_rows(month, rows: list, *, user=None, save_alias: bool = True) -> int:
    """確認画面で対象者を決めた行を取り込む（member_id が空の行は取り込まない）。"""
    from apps.core.months import days_in_month
    from apps.members.models import Member, MemberAlias

    from . import services
    from .models import WorkType

    work, off = WorkType.objects.get(code="work"), WorkType.objects.get(code="off")
    dates = days_in_month(month)
    n = 0
    for item in rows:
        if not item.get("member_id") or item.get("error"):
            continue
        member = Member.objects.get(pk=item["member_id"])
        if save_alias and item.get("chosen"):
            MemberAlias.objects.get_or_create(member=member, alias=item["name"], defaults={"note": "シフト表の表記"})
        days = set(item["work_days"])
        n += services.set_shifts([member], [d for d in dates if d.day in days], work, user=user, reason="既存シフト表から取り込み")
        n += services.set_shifts([member], [d for d in dates if d.day not in days], off, user=user, reason="既存シフト表から取り込み")
    return n
