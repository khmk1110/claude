from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """重要操作の記録。

    PostgreSQL ではトリガーにより UPDATE / DELETE を禁止している（migrations/0002）。
    管理画面からも編集・削除できない。
    """

    at = models.DateTimeField("操作日時", auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作ユーザー",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    username = models.CharField("ユーザー名（記録時点）", max_length=150, blank=True)
    action = models.CharField("操作", max_length=64, db_index=True)
    target_type = models.CharField("対象の種類", max_length=100, blank=True, db_index=True)
    target_id = models.CharField("対象ID", max_length=100, blank=True, db_index=True)
    target_repr = models.CharField("対象", max_length=255, blank=True)
    before = models.JSONField("変更前", null=True, blank=True)
    after = models.JSONField("変更後", null=True, blank=True)
    reason = models.TextField("理由", blank=True)
    ip = models.GenericIPAddressField("IPアドレス", null=True, blank=True)

    class Meta:
        verbose_name = "操作履歴"
        verbose_name_plural = "操作履歴"
        ordering = ["-at", "-id"]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.username} {self.action} {self.target_repr}"


class ImmutableAfterConfirmError(Exception):
    """確定済みデータを変更しようとした。"""


class StoredFile(models.Model):
    """アップロードされた資料・写真の保存先（DB）。DB のバックアップだけで資料も一緒に保全・復元できる。"""

    name = models.CharField(max_length=255, unique=True)
    content = models.BinaryField()
    size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "保存ファイル"
        verbose_name_plural = "保存ファイル"
