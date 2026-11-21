"""既存の給与計算ブックから、メンバーマスターと単価表を取り込む。

    python manage.py import_legacy_payroll_setup 給与計算.xlsx --effective-from 2026-01          # 確認のみ
    python manage.py import_legacy_payroll_setup 給与計算.xlsx --effective-from 2026-01 --apply  # 取り込む

実データはリポジトリに置かず、本番環境でこのコマンドを使って登録する。
- メンバー: 未登録の氏名だけ追加する（既存のメンバーは変更しない）
- 単価表: 同じ雇用区分・同じ適用開始月の単価表があれば中止する（上書きしない）
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

import openpyxl

from apps.core.months import parse_month
from apps.core.text import normalize_name
from apps.members.models import EmploymentType, Member
from apps.payroll.models import RateTable

KIND = {"個人事業主": EmploymentType.CONTRACTOR, "インターン": EmploymentType.INTERN, "正社員": EmploymentType.EMPLOYEE}
LAYOUT = {
    # 既存「月次計算」シートの数式（MATCH の区分）と単価表の位置
    EmploymentType.CONTRACTOR: ([0, 11, 16, 19, 22, 25, 28], [0, 40, 50, 60, 80, 100, 120], (3, 9), (2, 8)),
    EmploymentType.INTERN: ([0, 5, 8, 11, 16, 19, 22], [0, 24, 30, 40, 50, 60], (14, 20), (2, 7)),
}


class Command(BaseCommand):
    help = "既存の給与計算ブックからメンバーと単価表を取り込む"

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--effective-from", required=True, help="単価表の適用開始月（例: 2026-01）")
        parser.add_argument("--apply", action="store_true")

    def handle(self, path, effective_from, apply, **opts):
        wb = openpyxl.load_workbook(path, data_only=True)
        eff = parse_month(effective_from)
        # メンバー
        new_members = []
        existing = set(Member.objects.values_list("name_normalized", flat=True))
        ws = wb["メンバーマスター"]
        for r in range(2, ws.max_row + 1):
            name, kind = ws.cell(r, 1).value, ws.cell(r, 2).value
            if not name:
                continue
            if kind not in KIND:
                raise CommandError(f"メンバーマスター {r}行目: 区分「{kind}」が不明です。")
            if normalize_name(name) in existing:
                self.stdout.write(f"登録済み: {name}")
                continue
            new_members.append((name.strip(), KIND[kind]))
            self.stdout.write(f"追加予定: {name}（{kind}）")
        # 単価表
        t = wb["単価表"]
        tables = []
        for emp, (days, counts, (r0, r1), (c0, c1)) in LAYOUT.items():
            if RateTable.objects.filter(employment_type=emp, effective_from=eff).exists():
                raise CommandError(f"{EmploymentType(emp).label}の単価表（{eff:%Y-%m}〜）はすでに登録されています。")
            prices = [[int(t.cell(r, c).value) for c in range(c0, c1 + 1)] for r in range(r0, r1 + 1)]
            table = RateTable(name=f"{EmploymentType(emp).label}（既存ブックより）", employment_type=emp, effective_from=eff,
                              day_lower_bounds=days, count_lower_bounds=counts, prices=prices)
            table.full_clean()
            tables.append(table)
            self.stdout.write(f"単価表: {table.name} {len(days)}×{len(counts)}")
        if not apply:
            self.stdout.write(self.style.WARNING("確認のみ（--apply を付けると取り込みます）"))
            return
        with transaction.atomic():
            base = Member.objects.count()
            for i, (name, emp) in enumerate(new_members, start=1):
                Member.objects.create(member_no=f"M{base + i:04d}", name=name, employment_type=emp,
                                      display_order=(base + i) * 10, paid_leave_target=emp == EmploymentType.EMPLOYEE)
            for table in tables:
                table.save()
        self.stdout.write(self.style.SUCCESS(f"メンバー{len(new_members)}人・単価表{len(tables)}件を登録しました。"))
