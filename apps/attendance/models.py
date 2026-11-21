from django.conf import settings
from django.db import models

from apps.core.months import validate_month_start
from apps.members.models import Member
from apps.shifts.models import WorkType


class AttendanceDay(models.Model):
    """1人×1日の勤怠。予定はシフトから自動作成、実績は管理者が確認・修正する。

    status=PLANNED（未確定）の間は実績＝予定として扱い、シフト変更に追従する。
    CONFIRMED / MODIFIED になった日は、シフトを変更しても実績を上書きしない。
    """

    class Status(models.TextChoices):
        PLANNED = "planned", "未確定"
        CONFIRMED = "confirmed", "確定"
        MODIFIED = "modified", "修正済み"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="attendance_days")
    date = models.DateField("日付", db_index=True)
    planned_work_type = models.ForeignKey(
        WorkType, null=True, blank=True, on_delete=models.PROTECT, related_name="+", verbose_name="予定"
    )
    actual_work_type = models.ForeignKey(
        WorkType, null=True, blank=True, on_delete=models.PROTECT, related_name="+", verbose_name="実績"
    )
    status = models.CharField("状態", max_length=16, choices=Status.choices, default=Status.PLANNED)
    is_absence = models.BooleanField("欠勤", default=False)
    is_late = models.BooleanField("遅刻", default=False)
    is_early_leave = models.BooleanField("早退", default=False)
    note = models.CharField("メモ", max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        verbose_name = "勤怠"
        verbose_name_plural = "勤怠"
        constraints = [models.UniqueConstraint(fields=["member", "date"], name="uniq_attendance_member_date")]

    def __str__(self):
        return f"{self.member} {self.date}"

    @property
    def effective_work_type(self):
        if self.status == self.Status.PLANNED:
            return self.planned_work_type
        return self.actual_work_type

    @property
    def differs_from_plan(self) -> bool:
        return self.status != self.Status.PLANNED and self.actual_work_type_id != self.planned_work_type_id


class AttendanceMonthLock(models.Model):
    """勤怠の月次確定。確定後はその月の勤怠を変更できない（解除は理由付きで記録）。"""

    month = models.DateField("対象年月", unique=True, validators=[validate_month_start])
    locked_at = models.DateTimeField("確定日時", auto_now_add=True)
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name = "勤怠の月次確定"
        verbose_name_plural = "勤怠の月次確定"


class PaidLeaveGrant(models.Model):
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="paid_leave_grants")
    granted_on = models.DateField("付与日")
    days = models.DecimalField("付与日数", max_digits=4, decimal_places=1)
    expires_on = models.DateField("取得期限", help_text="法定では付与日から2年")
    is_statutory = models.BooleanField("法定付与", default=True)
    note = models.CharField("メモ", max_length=255, blank=True)

    class Meta:
        verbose_name = "有給付与"
        verbose_name_plural = "有給付与"
        ordering = ["member", "-granted_on"]
        constraints = [models.UniqueConstraint(fields=["member", "granted_on"], name="uniq_leave_grant")]


class PaidLeaveRequest(models.Model):
    """有給の取得予定・取得実績。申請中（REQUESTED）は取得済みに数えない。"""

    class Status(models.TextChoices):
        REQUESTED = "requested", "申請中"
        APPROVED = "approved", "承認済み"
        REJECTED = "rejected", "却下"
        CANCELLED = "cancelled", "取消"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="paid_leave_requests")
    date = models.DateField("取得日")
    days = models.DecimalField("日数", max_digits=2, decimal_places=1, help_text="全日1.0 / 半日0.5")
    status = models.CharField("状態", max_length=16, choices=Status.choices, default=Status.REQUESTED)
    from_shift = models.BooleanField("シフトから作成", default=False)
    note = models.CharField("メモ", max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        verbose_name = "有給取得"
        verbose_name_plural = "有給取得"
        ordering = ["member", "date"]
        constraints = [
            models.CheckConstraint(condition=models.Q(days__in=[0.5, 1.0]), name="leave_days_half_or_full"),
            models.UniqueConstraint(
                fields=["member", "date"],
                condition=models.Q(status__in=["requested", "approved"]),
                name="uniq_active_leave_per_day",
            ),
        ]


class PaidLeaveAdjustment(models.Model):
    """管理者による残日数の手動修正（移行時の繰越分など）。"""

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="paid_leave_adjustments")
    date = models.DateField("基準日")
    days = models.DecimalField("増減日数", max_digits=4, decimal_places=1, help_text="減らす場合はマイナス")
    reason = models.CharField("理由", max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name = "有給の手動修正"
        verbose_name_plural = "有給の手動修正"
