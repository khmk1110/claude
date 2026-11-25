"""給与・報酬管理。

すべて追加パスワード（payroll）の内側でのみ扱う。

計算の考え方（既存の「月次計算」シートを再現）:
    単価     = 単価表[前月出勤日数（切り捨て）][前月獲得件数]
    営業報酬 = 当月入金確定件数 × 単価
    支払総額 = 支給合計 − 控除合計
給与項目は PayrollItemDefinition で管理し、項目の追加・停止にプログラム修正を要しない。
"""

from __future__ import annotations

import bisect
from decimal import ROUND_FLOOR, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import ImmutableAfterConfirmError
from apps.core.months import month_end, validate_month_start
from apps.members.models import EmploymentType, Member


# ---------------------------------------------------------------------------
# 単価表
# ---------------------------------------------------------------------------
class RateTable(models.Model):
    """出勤日数 × 獲得件数 の単価マトリクス（有効期間つき）。

    day_lower_bounds / count_lower_bounds は各区分の下限値（昇順・先頭0）。
    例: 個人事業主の日数区分 [0, 11, 16, 19, 22, 25, 28] は「〜10日, 11〜15日, …, 28〜31日」。
    確定済み給与はスナップショットに単価表を写し取るため、ここを変更しても過去分は変わらない。
    """

    name = models.CharField("名称", max_length=100)
    employment_type = models.CharField("雇用区分", max_length=16, choices=EmploymentType.choices)
    effective_from = models.DateField("適用開始月", validators=[validate_month_start])
    effective_to = models.DateField("適用終了月", null=True, blank=True, validators=[validate_month_start])
    day_lower_bounds = models.JSONField("出勤日数の区分（下限）")
    count_lower_bounds = models.JSONField("獲得件数の区分（下限）")
    prices = models.JSONField("単価（行=日数区分、列=件数区分）")
    note = models.TextField("メモ", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "単価表"
        verbose_name_plural = "単価表"
        ordering = ["employment_type", "-effective_from"]

    def __str__(self):
        return f"{self.name}（{self.effective_from:%Y-%m}〜）"

    @staticmethod
    def _check_bounds(bounds, label):
        if not isinstance(bounds, list) or not bounds or not all(isinstance(b, int) for b in bounds):
            raise ValidationError(f"{label}は整数のリストで指定してください。")
        if bounds[0] != 0 or any(a >= b for a, b in zip(bounds, bounds[1:])):
            raise ValidationError(f"{label}は 0 から始まる昇順にしてください。")

    def clean(self):
        self._check_bounds(self.day_lower_bounds, "出勤日数の区分")
        self._check_bounds(self.count_lower_bounds, "獲得件数の区分")
        rows, cols = len(self.day_lower_bounds), len(self.count_lower_bounds)
        if (
            not isinstance(self.prices, list)
            or len(self.prices) != rows
            or any(not isinstance(r, list) or len(r) != cols for r in self.prices)
        ):
            raise ValidationError(f"単価は {rows} 行 × {cols} 列で指定してください。")
        if any(not isinstance(p, int) or p < 0 for r in self.prices for p in r):
            raise ValidationError("単価は0以上の整数（円）で指定してください。")
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError("適用終了月は適用開始月以降にしてください。")

    @staticmethod
    def floor_days(days) -> int:
        """半日（0.5日）を含む出勤日数は切り捨てて区分を判定する。"""
        return int(Decimal(str(days)).to_integral_value(rounding=ROUND_FLOOR))

    def lookup(self, days, count) -> int:
        if days is None or count is None:
            raise ValueError("前月出勤日数と前月獲得件数の両方が必要です。")
        d = self.floor_days(days)
        c = int(count)
        if d < 0 or c < 0:
            raise ValueError("出勤日数・件数に負の値は使えません。")
        row = bisect.bisect_right(self.day_lower_bounds, d) - 1
        col = bisect.bisect_right(self.count_lower_bounds, c) - 1
        return self.prices[row][col]

    def as_snapshot(self) -> dict:
        return {
            "id": self.pk,
            "name": self.name,
            "employment_type": self.employment_type,
            "effective_from": self.effective_from.isoformat(),
            "day_lower_bounds": self.day_lower_bounds,
            "count_lower_bounds": self.count_lower_bounds,
            "prices": self.prices,
        }

    @classmethod
    def for_month(cls, employment_type: str, month):
        return (
            cls.objects.filter(employment_type=employment_type, effective_from__lte=month)
            .filter(models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=month))
            .order_by("-effective_from")
            .first()
        )


# ---------------------------------------------------------------------------
# 営業実績
# ---------------------------------------------------------------------------
class Product(models.Model):
    """商材（例: プロパン（風呂あり）、都市ガス、訪問電気）。個人別の営業報酬項目で使う。"""

    code = models.SlugField("コード", max_length=32, unique=True)
    name = models.CharField("名称", max_length=100)
    display_order = models.PositiveIntegerField("表示順", default=0)
    is_active = models.BooleanField("使用中", default=True)

    class Meta:
        verbose_name = "商材"
        verbose_name_plural = "商材"
        ordering = ["display_order", "id"]

    def __str__(self):
        return self.name


class SalesCount(models.Model):
    """営業件数。

    - kind=ACQUIRED（獲得件数）: 営業ダッシュボードの件数。month は獲得した月。
      → 給与月 M の単価判定には month=M-1 の値を使う。
    - kind=CONFIRMED（入金確定件数）: 入金確定シートの総合件数。month は給与月。
      → 営業報酬 = この件数 × 単価。
    product を指定すると商材別の内訳（個人別の営業報酬項目用）。
    """

    class Kind(models.TextChoices):
        ACQUIRED = "acquired", "獲得件数"
        CONFIRMED = "confirmed", "入金確定件数"

    class Source(models.TextChoices):
        MANUAL = "manual", "手入力"
        IMAGE = "image", "写真・文面から入力"
        IMPORT = "import", "資料取り込み"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="sales_counts")
    month = models.DateField("対象年月", validators=[validate_month_start])
    kind = models.CharField("種類", max_length=16, choices=Kind.choices)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.PROTECT, verbose_name="商材")
    count = models.PositiveIntegerField("件数")
    source = models.CharField("入力元", max_length=16, choices=Source.choices, default=Source.MANUAL)
    evidence = models.FileField("根拠資料（写真など）", upload_to="evidence/sales/%Y/%m/", blank=True)
    note = models.CharField("メモ", max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        verbose_name = "営業件数"
        verbose_name_plural = "営業件数"
        constraints = [
            models.UniqueConstraint(
                fields=["member", "month", "kind", "product"],
                name="uniq_sales_count",
                nulls_distinct=False,
            )
        ]


class SalesEvidence(models.Model):
    """営業件数の根拠（写真・文面）。代表から届いた写真や LINE の文面を保存する。"""

    month = models.DateField("対象年月", validators=[validate_month_start])
    kind = models.CharField("種類", max_length=16, choices=SalesCount.Kind.choices)
    file = models.FileField("写真・資料", upload_to="evidence/sales/%Y/%m/", blank=True)
    text = models.TextField("文面", blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # スクショの読み取りは時間がかかるので裏で行い、結果（確認画面の内容）をここに置く
    status = models.CharField("状態", max_length=16, default="done")  # processing / done / error
    plan = models.JSONField("読み取り結果", null=True, blank=True)
    error_message = models.TextField("エラー", blank=True)
    # スクショが2枚以上の時、2枚目以降は1枚目にぶら下げて一緒に読む
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="parts")

    class Meta:
        verbose_name = "営業件数の根拠"
        verbose_name_plural = "営業件数の根拠"
        ordering = ["-uploaded_at"]


class BaseSalary(models.Model):
    """正社員の基本給（改定履歴）。正社員は明細を作らず、基本給・前月出勤日数・獲得件数の参照のみ。"""

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="base_salaries")
    effective_from = models.DateField("適用開始月", validators=[validate_month_start])
    amount = models.PositiveIntegerField("基本給（円）")
    note = models.CharField("メモ", max_length=255, blank=True)

    class Meta:
        verbose_name = "基本給"
        verbose_name_plural = "基本給"
        ordering = ["member", "-effective_from"]
        constraints = [
            models.UniqueConstraint(fields=["member", "effective_from"], name="uniq_base_salary_month")
        ]

    @classmethod
    def for_month(cls, member, month):
        return cls.objects.filter(member=member, effective_from__lte=month).order_by("-effective_from").first()


class Team(models.Model):
    """リーダー手当・チーム達成ボーナスの計算に使うチーム。"""

    class Kind(models.TextChoices):
        SALES = "sales", "営業チーム（チーム達成ボーナス）"
        INTERN = "intern", "インターンチーム（インターンリーダー手当）"

    name = models.CharField("チーム名", max_length=100)
    kind = models.CharField("種類", max_length=16, choices=Kind.choices, default=Kind.SALES)
    leader = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="led_teams", verbose_name="リーダー")
    is_active = models.BooleanField("使用中", default=True)
    note = models.CharField("メモ", max_length=255, blank=True)

    class Meta:
        verbose_name = "チーム"
        verbose_name_plural = "チーム"

    def __str__(self):
        return self.name

    def members_for(self, month):
        return Member.objects.filter(
            team_memberships__team=self,
            team_memberships__start_month__lte=month,
        ).filter(
            models.Q(team_memberships__end_month__isnull=True) | models.Q(team_memberships__end_month__gte=month)
        ).distinct()


class TeamMembership(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="team_memberships")
    start_month = models.DateField("所属開始月", validators=[validate_month_start])
    end_month = models.DateField("所属終了月", null=True, blank=True, validators=[validate_month_start])

    class Meta:
        verbose_name = "チーム所属"
        verbose_name_plural = "チーム所属"


# ---------------------------------------------------------------------------
# 給与項目
# ---------------------------------------------------------------------------
class PayrollItemDefinition(models.Model):
    """支給・控除の項目定義。管理者が追加・編集・停止できる（削除は未使用時のみ）。

    params の例:
        SALES_COMMISSION   : {}（単価表を使う）
        PRODUCT_COMMISSION : {"product": "toshigas", "unit_price": "auto" | 35000}
        EXPENSE_SUM        : {"category": "transport", "subtract": 35000}
        INTERN_LEADER      : {"rate": 500, "min_attendance_days": 4, "count_kind": "confirmed"}
        TEAM_BONUS         : {"tiers": [[70, 30000], [80, 40000], [90, 50000]], "manual_from": 100}
    """

    class Kind(models.TextChoices):
        EARNING = "earning", "支給"
        DEDUCTION = "deduction", "控除"

    class Method(models.TextChoices):
        MANUAL = "manual", "金額を毎月入力"
        FIXED = "fixed", "固定額"
        UNIT_X_QTY = "unit_x_qty", "単価×数量"
        SALES_COMMISSION = "sales_commission", "入金確定件数×自動単価"
        PRODUCT_COMMISSION = "product_commission", "商材別件数×単価"
        EXPENSE_SUM = "expense_sum", "承認済み経費の合計"
        INTERN_LEADER = "intern_leader", "インターンリーダー手当"
        TEAM_BONUS = "team_bonus", "チーム達成ボーナス"

    class Scope(models.TextChoices):
        ALL = "all", "全員共通"
        EMPLOYMENT_TYPE = "employment_type", "雇用区分別"
        MEMBER = "member", "個人専用"

    code = models.SlugField("コード", max_length=50, unique=True)
    name = models.CharField("項目名", max_length=100)
    kind = models.CharField("支給・控除", max_length=16, choices=Kind.choices)
    method = models.CharField("計算方法", max_length=32, choices=Method.choices, default=Method.MANUAL)
    params = models.JSONField("計算パラメータ", default=dict, blank=True)
    default_amount = models.IntegerField("金額・単価（円）", null=True, blank=True)
    scope = models.CharField("適用範囲", max_length=16, choices=Scope.choices, default=Scope.ALL)
    employment_types = models.JSONField("対象の雇用区分", default=list, blank=True)
    members = models.ManyToManyField(Member, blank=True, related_name="+", verbose_name="対象者（個人専用）")
    excluded_members = models.ManyToManyField(
        Member, blank=True, related_name="+", verbose_name="対象外にする人",
        help_text="例: 商材別報酬で計算する人は通常の営業報酬の対象外にする",
    )
    first_payroll_only = models.BooleanField("初回の給与のみ", default=False)
    effective_from = models.DateField("適用開始月", null=True, blank=True, validators=[validate_month_start])
    effective_to = models.DateField("適用終了月", null=True, blank=True, validators=[validate_month_start])
    show_on_slip = models.BooleanField("明細に表示", default=True)
    slip_label = models.CharField("明細の内訳欄の文言", max_length=100, blank=True)
    display_order = models.PositiveIntegerField("表示順", default=0)
    is_active = models.BooleanField("使用中", default=True)
    note = models.TextField("メモ", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "給与項目"
        verbose_name_plural = "給与項目"
        ordering = ["display_order", "id"]

    def __str__(self):
        return f"{self.name}（{self.get_kind_display()}）"

    def applies_to(self, member: Member, month) -> bool:
        if not self.is_active:
            return False
        if self.excluded_members.filter(pk=member.pk).exists():
            return False
        if self.effective_from and month < self.effective_from:
            return False
        if self.effective_to and month > self.effective_to:
            return False
        until = (self.params or {}).get("only_members_until")
        if until and self.scope == self.Scope.EMPLOYMENT_TYPE and f"{month:%Y-%m}" <= until:
            # この月までは「対象者」に選んだ人だけ（例: アプリ手数料は2026年10月分までは一部の人だけ）
            return member.employment_type in (self.employment_types or []) and self.members.filter(pk=member.pk).exists()
        if self.scope == self.Scope.EMPLOYMENT_TYPE:
            return member.employment_type in (self.employment_types or [])
        if self.scope == self.Scope.MEMBER:
            return self.members.filter(pk=member.pk).exists()
        return True


# ---------------------------------------------------------------------------
# 月次の給与計算
# ---------------------------------------------------------------------------
class PayrollMonth(models.Model):
    month = models.DateField("対象年月", unique=True, validators=[validate_month_start])
    payment_date = models.DateField("支払日", help_text="初期値は月末")
    note = models.TextField("メモ", blank=True)

    class Meta:
        verbose_name = "給与月"
        verbose_name_plural = "給与月"
        ordering = ["-month"]

    def __str__(self):
        return f"{self.month:%Y年%m月分}"

    def save(self, *args, **kwargs):
        if not self.payment_date:
            self.payment_date = month_end(self.month)
        super().save(*args, **kwargs)


class PayrollStatement(models.Model):
    """1人×1か月の給与・報酬計算。確定するとスナップショットを保存し、以後は変更不可。

    再計算は管理者が明示的に実行した場合のみ。確定後に元データ（経費など）が変わったら
    needs_review を立てて通知するだけで、金額は自動で変えない。
    """

    class Status(models.TextChoices):
        NOT_CALCULATED = "not_calculated", "未計算"
        IN_PROGRESS = "in_progress", "入力途中"
        CALCULATED = "calculated", "計算済み"
        REVIEW = "review", "確認待ち"
        CONFIRMED = "confirmed", "確定済み"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="payroll_statements")
    month = models.DateField("対象年月", validators=[validate_month_start])
    employment_type = models.CharField("雇用区分（計算時点）", max_length=16, choices=EmploymentType.choices)
    status = models.CharField("計算状況", max_length=20, choices=Status.choices, default=Status.NOT_CALCULATED)
    earnings_total = models.IntegerField("支給合計", null=True, blank=True)
    deductions_total = models.IntegerField("控除合計", null=True, blank=True)
    net_amount = models.IntegerField("差引支給額", null=True, blank=True)
    calculated_at = models.DateTimeField("計算日時", null=True, blank=True)
    confirmed_at = models.DateTimeField("確定日時", null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    snapshot = models.JSONField("確定時のスナップショット", null=True, blank=True)
    calc_messages = models.JSONField("計算時のエラー・注意", default=list, blank=True)
    needs_review = models.BooleanField("確定後に元データの変更あり", default=False)
    review_note = models.TextField("変更内容", blank=True)
    note = models.TextField("メモ", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "給与・報酬計算"
        verbose_name_plural = "給与・報酬計算"
        ordering = ["-month", "member__display_order"]
        constraints = [models.UniqueConstraint(fields=["member", "month"], name="uniq_statement_member_month")]

    def __str__(self):
        return f"{self.month:%Y-%m} {self.member}"

    @property
    def is_confirmed(self) -> bool:
        return self.status == self.Status.CONFIRMED

    @property
    def blocking_errors(self) -> list:
        return [m for m in (self.calc_messages or []) if m.get("level") == "error"]

    # 確定済みの金額・スナップショットは変更不可。状態の変更（差し戻し）と needs_review の更新のみ許可。
    PROTECTED_FIELDS = ("earnings_total", "deductions_total", "net_amount", "snapshot", "member_id", "month")

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).values("status", *self.PROTECTED_FIELDS).first()
            if old and old["status"] == self.Status.CONFIRMED and self.status == self.Status.CONFIRMED:
                for f in self.PROTECTED_FIELDS:
                    if old[f] != getattr(self, f):
                        raise ImmutableAfterConfirmError("確定済みの給与は変更できません。")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_confirmed:
            raise ImmutableAfterConfirmError("確定済みの給与は削除できません。")
        return super().delete(*args, **kwargs)


class _StatementChild(models.Model):
    class Meta:
        abstract = True

    def _guard(self):
        if PayrollStatement.objects.filter(pk=self.statement_id, status=PayrollStatement.Status.CONFIRMED).exists():
            raise ImmutableAfterConfirmError("確定済みの給与は変更できません。")

    def save(self, *args, **kwargs):
        self._guard()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._guard()
        return super().delete(*args, **kwargs)


class PayrollInput(_StatementChild):
    """計算の元になる数値。自動取得値と手動修正値を別々に保持する。

    手動修正しても、元のシフト・勤怠・営業件数データは変更しない。
    """

    class Key(models.TextChoices):
        PREV_ATTENDANCE_DAYS = "prev_attendance_days", "前月出勤日数"
        PREV_ACQUIRED_COUNT = "prev_acquired_count", "前月獲得件数"
        CURRENT_ACQUIRED_COUNT = "current_acquired_count", "当月獲得件数"
        CONFIRMED_COUNT = "confirmed_count", "当月入金確定件数"
        CURRENT_ATTENDANCE_DAYS = "current_attendance_days", "当月出勤日数"
        UNIT_PRICE = "unit_price", "インセンティブ単価"

    statement = models.ForeignKey(PayrollStatement, on_delete=models.CASCADE, related_name="inputs")
    key = models.CharField("項目", max_length=32, choices=Key.choices)
    auto_value = models.DecimalField("自動取得値", max_digits=12, decimal_places=1, null=True, blank=True)
    auto_source = models.CharField("取得元", max_length=255, blank=True)
    override_value = models.DecimalField("修正後の値", max_digits=12, decimal_places=1, null=True, blank=True)
    override_reason = models.CharField("修正理由", max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        verbose_name = "計算の元データ"
        verbose_name_plural = "計算の元データ"
        constraints = [models.UniqueConstraint(fields=["statement", "key"], name="uniq_statement_input")]

    @property
    def value(self):
        return self.override_value if self.override_value is not None else self.auto_value

    @property
    def is_overridden(self) -> bool:
        return self.override_value is not None


class PayrollLine(_StatementChild):
    """明細の1行（支給または控除）。項目名・単価は計算時点の値を写して保持する。"""

    statement = models.ForeignKey(PayrollStatement, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(
        PayrollItemDefinition, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    code = models.CharField("項目コード", max_length=50, blank=True)
    sub_key = models.CharField("内訳キー", max_length=50, blank=True)
    name = models.CharField("項目名", max_length=100)
    kind = models.CharField("支給・控除", max_length=16, choices=PayrollItemDefinition.Kind.choices)
    source_refs = models.JSONField("元データ（経費明細ID など）", default=list, blank=True)
    always_show = models.BooleanField("0円でも明細に表示", default=False)
    quantity = models.DecimalField("数量", max_digits=12, decimal_places=1, null=True, blank=True)
    unit_price = models.IntegerField("単価", null=True, blank=True)
    auto_amount = models.IntegerField("自動計算額", null=True, blank=True)
    override_amount = models.IntegerField("修正後の金額", null=True, blank=True)
    override_reason = models.CharField("修正理由", max_length=255, blank=True)
    detail = models.CharField("内訳", max_length=255, blank=True)
    is_adhoc = models.BooleanField("当月のみの項目", default=False)
    show_on_slip = models.BooleanField("明細に表示", default=True)
    display_order = models.PositiveIntegerField("表示順", default=0)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        verbose_name = "明細行"
        verbose_name_plural = "明細行"
        ordering = ["display_order", "id"]

    @property
    def amount(self) -> int:
        if self.override_amount is not None:
            return self.override_amount
        return self.auto_amount or 0


class IgnoredName(models.Model):
    """資料に載っていても取り込まない名前（例: 代表など給与計算の対象外の人）。確認画面で選ぶと次回から自動で除外する。"""

    name_normalized = models.CharField("名前（正規化）", max_length=100, unique=True)
    name = models.CharField("名前", max_length=100)
    note = models.CharField("メモ", max_length=255, blank=True)

    class Meta:
        verbose_name = "取り込まない名前"
        verbose_name_plural = "取り込まない名前"

    def __str__(self):
        return self.name


class StatementDelivery(models.Model):
    """振込・明細送付のチェック（確定した明細そのものは変えずに、別に記録する）。"""

    statement = models.OneToOneField(PayrollStatement, on_delete=models.CASCADE, related_name="delivery")
    paid_at = models.DateTimeField("振込済み", null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    sent_at = models.DateTimeField("明細送付済み", null=True, blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "振込・送付の記録"
        verbose_name_plural = "振込・送付の記録"


class EmployeePay(models.Model):
    """正社員のその月の給与額（明細は作らない。支出の合計に入れるための金額だけ）。"""

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="employee_pays")
    month = models.DateField("月", validators=[validate_month_start])
    salary = models.PositiveIntegerField("給与（振込額）")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "正社員の給与"
        verbose_name_plural = "正社員の給与"
        ordering = ["month", "member"]
        constraints = [models.UniqueConstraint(fields=["member", "month"], name="uniq_employee_pay_month")]
