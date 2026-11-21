import getpass
import os

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.accounts import stepup
from apps.accounts.models import StepUpScope


class Command(BaseCommand):
    help = "給与用・経費用の追加パスワードを設定する（ハッシュ化して DB に保存）。"

    def add_arguments(self, parser):
        parser.add_argument("--scope", required=True, choices=StepUpScope.values)
        parser.add_argument(
            "--from-env",
            metavar="VAR",
            help="パスワードを環境変数から読む（自動デプロイ用）。指定しなければ対話入力。",
        )

    def handle(self, *args, scope, from_env=None, **opts):
        if from_env:
            raw = os.environ.get(from_env)
            if not raw:
                raise CommandError(f"環境変数 {from_env} が空です。")
        else:
            raw = getpass.getpass(f"{StepUpScope(scope).label} の新しいパスワード: ")
            if raw != getpass.getpass("もう一度入力: "):
                raise CommandError("一致しません。")
        try:
            stepup.set_password(scope, raw, note="管理コマンドで設定")
        except ValidationError as e:
            raise CommandError("; ".join(e.messages)) from e
        self.stdout.write(self.style.SUCCESS(f"{StepUpScope(scope).label} のパスワードを設定しました。"))
