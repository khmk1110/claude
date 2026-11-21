"""起動時の初期設定（Render の無料プランはコマンドを実行できないため、環境変数から設定する）。

一度設定された値は上書きしない（何度起動しても安全）。設定が終わったら環境変数は削除してよい。
    INITIAL_ADMIN_USERNAME / INITIAL_ADMIN_PASSWORD : 最初の代表者ユーザー（ユーザーが1人もいないときだけ作成）
    INITIAL_PAYROLL_PASSWORD / INITIAL_EXPENSES_PASSWORD : 追加パスワード（未設定のときだけ設定）
"""

import os

from django.core.management.base import BaseCommand

from apps.accounts import stepup
from apps.accounts.models import StepUpPassword, User


class Command(BaseCommand):
    help = "環境変数から初期ユーザーと追加パスワードを設定する（未設定の場合のみ）"

    def handle(self, *args, **opts):
        name, pw = os.environ.get("INITIAL_ADMIN_USERNAME"), os.environ.get("INITIAL_ADMIN_PASSWORD")
        if name and pw and not User.objects.exists():
            User.objects.create_superuser(name, password=pw, role=User.Role.OWNER)
            self.stdout.write(f"代表者ユーザー「{name}」を作成しました。")
        for scope, var in (("payroll", "INITIAL_PAYROLL_PASSWORD"), ("expenses", "INITIAL_EXPENSES_PASSWORD")):
            value = os.environ.get(var)
            if value and not StepUpPassword.objects.filter(scope=scope).exists():
                stepup.set_password(scope, value, note="初期設定（環境変数）")
                self.stdout.write(f"{scope} の追加パスワードを設定しました。")
