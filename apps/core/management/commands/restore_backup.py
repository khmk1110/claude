"""バックアップ（kanri-backup-*.json.gz または .json）から復元する。

空のデータベース（migrate 直後）にだけ復元できる。既存データとの混在を防ぐため、メンバーや給与が
1件でもある場合は中止する。
"""

import gzip
import os
import tempfile

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.members.models import Member
from apps.payroll.models import PayrollStatement


def restore(path: str, stdout=None) -> None:
    if Member.objects.exists() or PayrollStatement.objects.exists():
        raise CommandError("データベースが空ではないため復元できません（新しいデータベースに復元してください）。")
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp.write(raw)
        name = tmp.name
    try:
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as c:
                    c.execute("SET LOCAL app.restoring = 'on'")  # このトランザクション内だけ保護トリガーを通す
            call_command("loaddata", name, stdout=stdout)
            if connection.vendor == "postgresql":
                # 連番（ID）を復元したデータの続きに合わせる
                from django.apps import apps
                from django.core.management.color import no_style

                models = [m for m in apps.get_models()]
                for sql in connection.ops.sequence_reset_sql(no_style(), models):
                    with connection.cursor() as c:
                        c.execute(sql)
    finally:
        os.unlink(name)


class Command(BaseCommand):
    help = "バックアップファイルから空のデータベースへ復元する"

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, path, **opts):
        restore(path, stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS("復元しました。"))
