"""動作確認用の架空データを作成する（開発環境専用）。本番では実行しないこと。"""

import random
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.core.months import days_in_month, month_start, prev_month
from apps.members.models import Member
from apps.shifts import services
from apps.shifts.models import WorkType

DEMO = [
    ("D001", "青木 一郎", "employee"), ("D002", "井上 花子", "employee"),
    ("D101", "上田 健", "contractor"), ("D102", "江藤 翔", "contractor"), ("D103", "大野 誠", "contractor"),
    ("D201", "加藤 さくら", "intern"), ("D202", "木村 蓮", "intern"),
]


class Command(BaseCommand):
    help = "架空のメンバー・シフトを作成する（DEBUG=True のときのみ）"

    def handle(self, *args, **opts):
        if not settings.DEBUG:
            raise CommandError("DEBUG=True の開発環境でのみ実行できます。")
        if not User.objects.filter(username="demo").exists():
            User.objects.create_superuser("demo", password="demo-pass-123", role=User.Role.OWNER)
        types = {w.code: w for w in WorkType.objects.all()}
        rnd = random.Random(1)
        for i, (no, name, emp) in enumerate(DEMO, start=1):
            m, _ = Member.objects.get_or_create(
                member_no=no,
                defaults=dict(name=name, employment_type=emp, display_order=i * 10, joined_on=date(2024, 4, 1),
                              paid_leave_target=emp == "employee"),
            )
            this = month_start(date.today())
            for month in (prev_month(this), this):
                for d in days_in_month(month):
                    p = rnd.random()
                    code = "work" if p < 0.62 else "off" if p < 0.85 else "half" if p < 0.92 else "paid" if emp == "employee" else "off"
                    services.set_shifts([m], [d], types[code], reason="デモデータ")
        self._payroll_demo(rnd)
        self.stdout.write(self.style.SUCCESS("デモデータを作成しました（ユーザー demo / demo-pass-123）"))

    def _payroll_demo(self, rnd):
        from apps.accounts import stepup
        from apps.accounts.models import StepUpPassword
        from apps.payroll.models import RateTable, SalesCount

        for scope in ("payroll", "expenses"):
            if not StepUpPassword.objects.filter(scope=scope).exists():
                stepup.set_password(scope, "demo-pass", note="デモ")
        # 架空の単価（実際の単価表ではない）
        for emp, days, counts in (("contractor", [0, 11, 16, 19, 22, 25, 28], [0, 40, 50, 60, 80, 100, 120]),
                                  ("intern", [0, 5, 8, 11, 16, 19, 22], [0, 24, 30, 40, 50, 60])):
            RateTable.objects.get_or_create(
                employment_type=emp, effective_from=date(2026, 1, 1),
                defaults=dict(name=f"デモ単価（{emp}）", day_lower_bounds=days, count_lower_bounds=counts,
                              prices=[[3000 + 250 * r + 250 * c for c in range(len(counts))] for r in range(len(days))]))
        this = month_start(date.today())
        for m in Member.objects.filter(member_no__startswith="D"):
            for month, kind in ((prev_month(this), "acquired"), (this, "acquired"), (this, "confirmed")):
                SalesCount.objects.get_or_create(member=m, month=month, kind=kind, product=None,
                                                 defaults={"count": rnd.randint(5, 90)})
