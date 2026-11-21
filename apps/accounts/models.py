from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = "owner", "代表者"
        ADMIN = "admin", "管理者"
        STAFF = "staff", "一般"

    role = models.CharField("権限", max_length=16, choices=Role.choices, default=Role.STAFF)
    display_name = models.CharField("表示名", max_length=100, blank=True)

    class Meta:
        verbose_name = "ユーザー"
        verbose_name_plural = "ユーザー"

    def __str__(self):
        return self.display_name or self.username

    @property
    def can_manage_stepup_passwords(self) -> bool:
        """追加パスワードを変更できるのは代表者（またはシステム管理者）のみ。"""
        return self.is_superuser or self.role == self.Role.OWNER


class StepUpScope(models.TextChoices):
    PAYROLL = "payroll", "給与・報酬管理"
    EXPENSES = "expenses", "資料・経費管理"


class StepUpPassword(models.Model):
    """給与用・経費用の追加パスワード。平文は保存しない（Django のパスワードハッシュ）。"""

    scope = models.CharField("対象機能", max_length=16, choices=StepUpScope.choices, unique=True)
    password_hash = models.CharField("パスワード（ハッシュ）", max_length=256)
    updated_at = models.DateTimeField("更新日時", auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        verbose_name = "追加パスワード"
        verbose_name_plural = "追加パスワード"

    def __str__(self):
        return self.get_scope_display()


class StepUpPasswordHistory(models.Model):
    """追加パスワードの変更履歴（いつ・誰が変更したか）。"""

    scope = models.CharField("対象機能", max_length=16, choices=StepUpScope.choices)
    changed_at = models.DateTimeField("変更日時", auto_now_add=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    note = models.CharField("メモ", max_length=255, blank=True)

    class Meta:
        verbose_name = "追加パスワード変更履歴"
        verbose_name_plural = "追加パスワード変更履歴"
        ordering = ["-changed_at"]


class StepUpAttempt(models.Model):
    """追加パスワードの入力記録。連続失敗によるロック判定に使う。"""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    scope = models.CharField(max_length=16, choices=StepUpScope.choices)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    success = models.BooleanField()
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = "追加認証の試行"
        verbose_name_plural = "追加認証の試行"
        indexes = [models.Index(fields=["user", "scope", "-at"])]
