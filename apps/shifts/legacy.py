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
