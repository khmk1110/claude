from decimal import Decimal

from django.core.validators import RegexValidator
from django.db import models

from apps.core.months import validate_month_start
from apps.core.text import normalize_name


class EmploymentType(models.TextChoices):
    EMPLOYEE = "employee", "正社員"
    CONTRACTOR = "contractor", "個人事業主"  # 代理店も個人事業主として登録（is_agency）
    INTERN = "intern", "インターン"


# 月間出勤基準の初期値。個人別（Member.standard_days）・月別（MonthlyStandardOverride）で上書きできる。
DEFAULT_STANDARD_DAYS = {
    EmploymentType.EMPLOYEE: Decimal("22"),
    EmploymentType.CONTRACTOR: Decimal("20"),
    EmploymentType.INTERN: None,  # 保留: インターンの基準日数は未確定
}


class Member(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "在籍"
        RETIRED = "retired", "退職"

    member_no = models.CharField(
        "メンバー番号", max_length=20, unique=True, blank=True,
        help_text="空欄なら自動で採番（1から順。退職・削除しても詰めない）",
    )
    name = models.CharField("氏名", max_length=100)
    name_kana = models.CharField("フリガナ", max_length=100, blank=True)
    short_name = models.CharField(
        "表示名（シフト表）", max_length=50, blank=True, help_text="空欄なら氏名を表示"
    )
    name_normalized = models.CharField(max_length=100, editable=False, db_index=True)
    employment_type = models.CharField("雇用区分", max_length=16, choices=EmploymentType.choices)
    is_agency = models.BooleanField("代理店", default=False)
    joined_on = models.DateField("入社日・契約開始日", null=True, blank=True)
    standard_days = models.DecimalField(
        "月間出勤基準（個人別）",
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
        help_text="空欄なら雇用区分の初期値（正社員22日・個人事業主20日）",
    )
    paid_leave_target = models.BooleanField("有給管理の対象", default=False)
    status = models.CharField("在籍状況", max_length=16, choices=Status.choices, default=Status.ACTIVE)
    retired_on = models.DateField("退職日", null=True, blank=True)
    display_order = models.PositiveIntegerField("並び順", default=0, help_text="0 なら追加した順で一番最後に並ぶ")
    note = models.TextField("備考", blank=True)
    shift_input_mode = models.CharField(
        "シフトの送り方", max_length=8, choices=[("work", "出勤日を送ってくる"), ("off", "休みの日を送ってくる")], default="work",
        help_text="LINEで届く数字が出勤日か休みの日か（シフト入力画面で前回の選択を覚えます）",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "メンバー"
        verbose_name_plural = "メンバー"
        ordering = ["display_order", "id"]

    def __str__(self):
        return self.name

    @classmethod
    def next_member_no(cls) -> str:
        """これまでで最大の番号＋1（数字だけの番号を対象。欠番は詰めない）。"""
        nums = [int(n) for n in cls.objects.values_list("member_no", flat=True) if str(n).isdigit()]
        return str(max(nums, default=0) + 1)

    def save(self, *args, **kwargs):
        self.name_normalized = normalize_name(self.name)
        if self._state.adding:
            if not self.member_no:
                self.member_no = self.next_member_no()
            if not self.display_order:
                last = type(self).objects.order_by("-display_order").values_list("display_order", flat=True).first()
                self.display_order = (last or 0) + 10  # 追加した順に一番最後へ
        super().save(*args, **kwargs)

    def has_history(self) -> bool:
        """シフト・勤怠・有給・営業件数・給与・経費のいずれかの記録があるか（あれば削除せず退職扱いにする）。"""
        from apps.attendance.models import AttendanceDay, PaidLeaveGrant, PaidLeaveRequest
        from apps.expenses.models import ExpenseRecord
        from apps.payroll.models import BaseSalary, PayrollStatement, SalesCount, TeamMembership
        from apps.shifts.models import ShiftEntry

        checks = [
            ShiftEntry.objects.filter(member=self), AttendanceDay.objects.filter(member=self),
            PaidLeaveGrant.objects.filter(member=self), PaidLeaveRequest.objects.filter(member=self),
            SalesCount.objects.filter(member=self), PayrollStatement.objects.filter(member=self),
            BaseSalary.objects.filter(member=self), TeamMembership.objects.filter(member=self),
            ExpenseRecord.objects.filter(member=self) | ExpenseRecord.objects.filter(auto_member=self),
            self.led_teams.all(), self.cards.all(),
        ]
        return any(q.exists() for q in checks)

    @property
    def display_label(self) -> str:
        return self.short_name or self.name

    def standard_days_for(self, month) -> Decimal | None:
        override = self.monthly_standards.filter(month=month).first()
        if override:
            return override.days
        if self.standard_days is not None:
            return self.standard_days
        return DEFAULT_STANDARD_DAYS.get(self.employment_type)


class MemberAlias(models.Model):
    """氏名の表記揺れ（例: 漢字の違い・空白の有無、シフト表の姓のみの表記など）。取り込み時の照合に使う。"""

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="aliases")
    alias = models.CharField("別名・表記", max_length=100)
    alias_normalized = models.CharField(max_length=100, editable=False, db_index=True)
    note = models.CharField("メモ", max_length=255, blank=True)

    class Meta:
        verbose_name = "氏名の別表記"
        verbose_name_plural = "氏名の別表記"
        constraints = [
            models.UniqueConstraint(fields=["member", "alias_normalized"], name="uniq_member_alias")
        ]

    def __str__(self):
        return f"{self.alias} → {self.member}"

    def save(self, *args, **kwargs):
        self.alias_normalized = normalize_name(self.alias)
        super().save(*args, **kwargs)


class CardCode(models.Model):
    """ガソリンカードの対応表。カード番号の右側にある〔0007〕などの4桁で担当者を判定する。

    カード番号そのもの（16桁）は保存しない。
    """

    code = models.CharField(
        "カード識別4桁",
        max_length=4,
        unique=True,
        validators=[RegexValidator(r"^\d{4}$", "4桁の数字で入力してください。")],
    )
    member = models.ForeignKey(
        Member, verbose_name="担当者", null=True, blank=True, on_delete=models.PROTECT, related_name="cards"
    )
    ignored = models.BooleanField(
        "給与に反映しない", default=False, help_text="正社員のカードなど、集計対象外のもの"
    )
    label = models.CharField("呼び名（資料上の表記）", max_length=50, blank=True)
    is_active = models.BooleanField("使用中", default=True)
    note = models.CharField("メモ", max_length=255, blank=True)

    class Meta:
        verbose_name = "カード対応表"
        verbose_name_plural = "カード対応表"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.member or self.label}"


class MonthlyStandardOverride(models.Model):
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="monthly_standards")
    month = models.DateField("対象年月", validators=[validate_month_start])
    days = models.DecimalField("出勤基準日数", max_digits=4, decimal_places=1)
    note = models.CharField("メモ", max_length=255, blank=True)

    class Meta:
        verbose_name = "月別の出勤基準"
        verbose_name_plural = "月別の出勤基準"
        constraints = [models.UniqueConstraint(fields=["member", "month"], name="uniq_member_month_std")]
