"""既存シフト表（Excel の「シフト入力」シート）を取り込む。

    python manage.py import_legacy_shift シフト表.xlsx            # 確認のみ（書き込まない）
    python manage.py import_legacy_shift シフト表.xlsx --apply    # 取り込む

「出勤日」: 入力した日＝出勤、それ以外＝休み / 「休み」: 入力した日＝休み、それ以外＝出勤
（既存シートの●の付き方を再現）。氏名が特定できない行・日付が読めない行があれば、何も書き込まない。
"""

from datetime import date

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.months import days_in_month, month_end
from apps.members.matching import match_member
from apps.shifts import services
from apps.shifts.legacy import DayListError, parse_day_list
from apps.shifts.models import ShiftEntry, WorkType


class Command(BaseCommand):
    help = "既存シフト表（シフト入力シート）を取り込む"

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--sheet", default="シフト入力")
        parser.add_argument("--apply", action="store_true", help="実際に書き込む")
        parser.add_argument("--overwrite", action="store_true", help="入力済みの月でも上書きする")

    def handle(self, path, sheet, apply, overwrite, **opts):
        ws = openpyxl.load_workbook(path, data_only=True)[sheet]
        try:
            month = date(int(ws["B1"].value), int(ws["E1"].value), 1)
        except (TypeError, ValueError) as e:
            raise CommandError("B1（年）・E1（月）が読めません。") from e
        last = month_end(month).day
        work, off = WorkType.objects.get(code="work"), WorkType.objects.get(code="off")
        plan, errors = [], []
        for r in range(4, ws.max_row + 1):
            name, mode, value = (ws.cell(r, c).value for c in (1, 2, 3))
            if not name:
                continue
            m = match_member(str(name))
            if not m.ok:
                cands = "、".join(c.name for c in m.candidates) or "該当なし"
                errors.append(f"{r}行目「{name}」: メンバーを特定できません（候補: {cands}）。別表記を登録してください。")
                continue
            try:
                listed = set(parse_day_list(value, last))
            except DayListError as e:
                errors.append(f"{r}行目「{name}」: {e}")
                continue
            if mode not in ("出勤日", "休み"):
                errors.append(f"{r}行目「{name}」: 入力方式「{mode}」は「出勤日」か「休み」にしてください。")
                continue
            work_days = listed if mode == "出勤日" else set(range(1, last + 1)) - listed
            plan.append((m.member, work_days))
            self.stdout.write(f"{m.member.name}（{name}）: 出勤 {len(work_days)}日 / {mode} {sorted(listed)}")
        if errors:
            for e in errors:
                self.stderr.write(self.style.ERROR(e))
            raise CommandError("エラーがあるため取り込みませんでした。")
        existing = ShiftEntry.objects.filter(date__gte=month, date__lte=month_end(month)).count()
        if existing and not overwrite:
            raise CommandError(f"{month:%Y年%m月}にはすでに{existing}件のシフトがあります。上書きする場合は --overwrite を付けてください。")
        if not apply:
            self.stdout.write(self.style.WARNING("確認のみ（--apply を付けると取り込みます）"))
            return
        with transaction.atomic():
            n = 0
            for member, work_days in plan:
                dates = days_in_month(month)
                n += services.set_shifts([member], [d for d in dates if d.day in work_days], work,
                                         reason="既存シフト表から取り込み")
                n += services.set_shifts([member], [d for d in dates if d.day not in work_days], off,
                                         reason="既存シフト表から取り込み")
        self.stdout.write(self.style.SUCCESS(f"{month:%Y年%m月}のシフトを取り込みました（{n}件）。"))
