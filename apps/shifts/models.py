from django.conf import settings
from django.db import models

from apps.core.months import validate_month_start
from apps.members.models import Member


class WorkType(models.Model):
    """勤務区分。出勤換算・有給換算は区分ごとに設定（例: 半日稼働＋半日有給 = 0.5 / 0.5）。"""

    code = models.SlugField("コード", max_length=32, unique=True)
    name = models.CharField("名称", max_length=50)
    short_label = models.CharField("表示文字", max_length=4, help_text="シフト表のセルに表示する文字")
    color = models.CharField("背景色", max_length=7, default="#ffffff")
    text_color = models.CharField("文字色", max_length=7, default="#111827")
    attendance_days = models.DecimalField("実出勤日数", max_digits=3, decimal_places=1, default=0)
    paid_leave_days = models.DecimalField("有給取得日数", max_digits=3, decimal_places=1, default=0)
    is_active = models.BooleanField("使用中", default=True)
    display_order = models.PositiveIntegerField("表示順", default=0)

    class Meta:
        verbose_name = "勤務区分"
        verbose_name_plural = "勤務区分"
        ordering = ["display_order", "id"]

    def __str__(self):
        return self.name


class ShiftMonth(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "作成中"
        PUBLISHED = "published", "公開済み"

    month = models.DateField("対象年月", unique=True, validators=[validate_month_start])
    status = models.CharField("状態", max_length=16, choices=Status.choices, default=Status.DRAFT)
    note = models.TextField("メモ", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "月別シフト"
        verbose_name_plural = "月別シフト"
        ordering = ["-month"]

    def __str__(self):
        return f"{self.month:%Y年%m月}"


class ShiftEntry(models.Model):
    """1人×1日の勤務予定。未入力の日は「未定」（休み扱いにしない）。

    土日祝日を自動で休みにすることはしない。変更履歴は監査ログ（action=shift.*）に残す。
    """

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="shift_entries")
    date = models.DateField("日付", db_index=True)
    work_type = models.ForeignKey(WorkType, on_delete=models.PROTECT, verbose_name="勤務区分")
    note = models.CharField("メモ", max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        verbose_name = "シフト"
        verbose_name_plural = "シフト"
        constraints = [models.UniqueConstraint(fields=["member", "date"], name="uniq_shift_member_date")]

    def __str__(self):
        return f"{self.member} {self.date} {self.work_type}"
