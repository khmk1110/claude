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
        self.stdout.write(self.style.SUCCESS("デモデータを作成しました（ユーザー demo / demo-pass-123）"))
