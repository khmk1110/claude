"""資料・経費管理。すべて追加パスワード（expenses）の内側でのみ扱う。

流れ: 資料アップロード → 種類判定・読み取りルール適用 → 明細行（ExpenseRecord）作成
      → メンバー自動振り分け → エラー・重複検出 → 管理者が修正・承認 → 給与へ反映（1回だけ）
"""

from django.conf import settings
from django.db import models

from apps.core.months import validate_month_start
from apps.members.models import Member


class DocumentKind(models.TextChoices):
    GASOLINE = "gasoline", "ガソリン代"
    TRANSPORT = "transport", "交通費"
    ADVANCE = "advance", "立替金"
    OTHER_EXPENSE = "other_expense", "その他経費"
    SALES = "sales", "営業実績"
    ATTENDANCE = "attendance", "勤怠関連"
    OTHER_PAYROLL = "other_payroll", "その他支給・控除"
    UNKNOWN = "unknown", "未判定"


class ExpenseCategory(models.Model):
    """経費項目。どの給与項目に集計されるかを payroll_item_code で指定する（管理者が追加・変更可）。"""

    code = models.SlugField("コード", max_length=32, unique=True)
    name = models.CharField("名称", max_length=100)
    document_kind = models.CharField("資料の種類", max_length=20, choices=DocumentKind.choices)
    payroll_item_code = models.CharField(
        "反映先の給与項目コード", max_length=50, blank=True, help_text="空欄なら給与に反映しない"
    )
    display_order = models.PositiveIntegerField("表示順", default=0)
    is_active = models.BooleanField("使用中", default=True)

    class Meta:
        verbose_name = "経費項目"
        verbose_name_plural = "経費項目"
        ordering = ["display_order", "id"]

    def __str__(self):
        return self.name


class ImportProfile(models.Model):
    """資料の読み取りルール。見出し行の並びで同じ形式の資料を判定し、次回以降も自動で使う。

    見出しが変わった資料には自動適用せず、管理者に確認する（NEEDS_MAPPING）。
    """

    name = models.CharField("ルール名", max_length=100)
    document_kind = models.CharField("資料の種類", max_length=20, choices=DocumentKind.choices)
    header_signature = models.CharField("見出しの指紋", max_length=80, db_index=True)
    headers = models.JSONField("見出し一覧", default=list)
    column_map = models.JSONField(
        "列の対応", default=dict, help_text='例: {"amount": "利用金額", "used_on": "利用日", "card": "カード番号"}'
    )
    options = models.JSONField("オプション", default=dict, blank=True)
    default_category = models.ForeignKey(
        ExpenseCategory, null=True, blank=True, on_delete=models.PROTECT, verbose_name="既定の経費項目"
    )
    is_active = models.BooleanField("使用中", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        verbose_name = "読み取りルール"
        verbose_name_plural = "読み取りルール"

    def __str__(self):
        return self.name


class SourceFile(models.Model):
    """取り込んだ元資料。ファイルは保存し、どの金額がどの資料から来たか辿れるようにする。"""

    class Format(models.TextChoices):
        XLSX = "xlsx", "Excel（xlsx）"
        XLS = "xls", "Excel（xls）"
        CSV = "csv", "CSV"
        GSHEET = "gsheet", "Googleスプレッドシート"
        PDF = "pdf", "PDF"
        MANUAL = "manual", "手入力（LINEなど）"

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "取り込み済み"
        NEEDS_KIND = "needs_kind", "種類の選択待ち"
        NEEDS_MAPPING = "needs_mapping", "読み取りルールの確認待ち"
        PARSED = "parsed", "読み取り完了"
        ERROR = "error", "エラー"

    file = models.FileField("ファイル", upload_to="sources/%Y/%m/", blank=True)
    gsheet_url = models.URLField("GoogleスプレッドシートURL", blank=True)
    original_name = models.CharField("元のファイル名", max_length=255)
    sha256 = models.CharField("ファイルの指紋", max_length=64, unique=True)
    size = models.PositiveBigIntegerField("サイズ", default=0)
    file_format = models.CharField("形式", max_length=10, choices=Format.choices)
    document_kind = models.CharField(
        "資料の種類", max_length=20, choices=DocumentKind.choices, default=DocumentKind.UNKNOWN
    )
    document_kind_auto = models.CharField(
        "資料の種類（自動判定）", max_length=20, choices=DocumentKind.choices, default=DocumentKind.UNKNOWN
    )
    profile = models.ForeignKey(ImportProfile, null=True, blank=True, on_delete=models.PROTECT)
    parser = models.CharField("読み取り方法", max_length=32, blank=True)
    headers = models.JSONField("見出し", default=list, blank=True)
    document_month = models.DateField("資料の月（利用月・〇月分）", null=True, blank=True, validators=[validate_month_start])
    target_month = models.DateField("反映する給与の月", null=True, blank=True, validators=[validate_month_start])
    status = models.CharField("状態", max_length=20, choices=Status.choices, default=Status.UPLOADED)
    used_ocr = models.BooleanField("OCRで読み取り", default=False)
    row_count = models.PositiveIntegerField("行数", default=0)
    source_total = models.IntegerField("資料上の合計金額", null=True, blank=True)
    error_message = models.TextField("エラー内容", blank=True)
    uploaded_at = models.DateTimeField("取り込み日時", auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        verbose_name = "取り込み資料"
        verbose_name_plural = "取り込み資料"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.original_name


class ExpenseRecord(models.Model):
    """資料から読み取った1件。自動読み取り値（auto_*）と修正後の値を別々に保持する。"""

    class Status(models.TextChoices):
        IMPORTED = "imported", "取り込み済み"
        UNCHECKED = "unchecked", "未確認"
        PENDING = "pending", "確認待ち"
        MODIFIED = "modified", "修正済み"
        APPROVED = "approved", "承認済み"
        REFLECTED = "reflected", "給与反映済み"
        EXCLUDED = "excluded", "対象外"

    source_file = models.ForeignKey(SourceFile, on_delete=models.PROTECT, related_name="records")
    row_number = models.PositiveIntegerField("資料の行番号")
    raw = models.JSONField("元の行データ", default=dict)

    # 自動読み取り値（取り込み後は変更しない）
    auto_member = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.PROTECT, related_name="+", verbose_name="対象者（自動）"
    )
    auto_category = models.ForeignKey(
        ExpenseCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    auto_used_on = models.DateField("利用日（自動）", null=True, blank=True)
    auto_description = models.CharField("利用内容（自動）", max_length=255, blank=True)
    auto_route = models.CharField("利用区間（自動）", max_length=255, blank=True)
    auto_amount = models.IntegerField("金額（自動）", null=True, blank=True)
    auto_target_month = models.DateField(null=True, blank=True, validators=[validate_month_start])
    name_text = models.CharField("資料上の氏名", max_length=100, blank=True)
    member_no_text = models.CharField("資料上のメンバー番号", max_length=20, blank=True)
    card_code = models.CharField("カード識別4桁", max_length=4, blank=True, db_index=True)
    card_last4 = models.CharField("カード番号の下4桁", max_length=4, blank=True)
    transaction_id = models.CharField("取引番号", max_length=100, blank=True, db_index=True)
    confidence = models.DecimalField("読み取り精度", max_digits=4, decimal_places=3, default=1)

    # 管理者による修正値（空欄なら自動値を使う）
    member = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.PROTECT, related_name="expense_records", verbose_name="対象者"
    )
    category = models.ForeignKey(ExpenseCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    used_on = models.DateField("利用日", null=True, blank=True)
    description = models.CharField("利用内容", max_length=255, blank=True)
    amount = models.IntegerField("金額", null=True, blank=True)
    target_month = models.DateField("対象年月", null=True, blank=True, validators=[validate_month_start])
    override_reason = models.CharField("修正理由", max_length=255, blank=True)
    note = models.CharField("メモ", max_length=255, blank=True)

    status = models.CharField("状態", max_length=16, choices=Status.choices, default=Status.IMPORTED, db_index=True)
    dedupe_key = models.CharField("重複判定キー", max_length=128, db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reflected_statement = models.ForeignKey(
        "payroll.PayrollStatement",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expense_records",
        verbose_name="反映先の給与",
    )
    reflected_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "経費明細"
        verbose_name_plural = "経費明細"
        ordering = ["source_file", "row_number"]
        constraints = [
            models.UniqueConstraint(fields=["source_file", "row_number"], name="uniq_record_row"),
            # 給与反映済みなら反映先が必須（二重反映・反映漏れの防止）
            models.CheckConstraint(
                condition=~models.Q(status="reflected") | models.Q(reflected_statement__isnull=False),
                name="reflected_requires_statement",
            ),
        ]

    @property
    def raw_display(self) -> str:
        """資料上の元の行（OCR結果・PDFの行・表の各列）を1行で表示する。"""
        raw = self.raw or {}
        for key in ("OCR", "行", "手入力"):
            if raw.get(key):
                return str(raw[key])
        return " / ".join(str(v) for v in raw.values() if v not in (None, ""))

    # 実際に使う値（修正値 → 自動値の順）
    @property
    def effective_member(self):
        return self.member or self.auto_member

    @property
    def effective_category(self):
        return self.category or self.auto_category

    @property
    def effective_amount(self):
        return self.amount if self.amount is not None else self.auto_amount

    @property
    def effective_used_on(self):
        return self.used_on or self.auto_used_on

    @property
    def effective_target_month(self):
        return self.target_month or self.auto_target_month or self.source_file.target_month


class ExpenseIssue(models.Model):
    """自動検出したエラー・確認事項。未解決のものがある明細は承認できない。"""

    class Code(models.TextChoices):
        DUPLICATE_FILE = "duplicate_file", "同一ファイルの重複取り込み"
        DUPLICATE_TXN = "duplicate_txn", "同一取引の重複の疑い"
        UNKNOWN_MEMBER = "unknown_member", "未登録の氏名"
        UNKNOWN_CARD = "unknown_card", "未登録のカード"
        MULTIPLE_CANDIDATES = "multiple_candidates", "対象者の候補が複数"
        NAME_CARD_MISMATCH = "name_card_mismatch", "氏名とカードの不一致"
        AMOUNT_MISMATCH = "amount_mismatch", "金額の不一致"
        MONTH_MISMATCH = "month_mismatch", "日付・対象月の不一致"
        TOTAL_MISMATCH = "total_mismatch", "資料の合計と集計結果の不一致"
        LOW_CONFIDENCE = "low_confidence", "読み取り精度が低い"
        CHANGED_AFTER_PAYROLL = "changed_after_payroll", "給与確定後の追加・修正"

    record = models.ForeignKey(ExpenseRecord, null=True, blank=True, on_delete=models.CASCADE, related_name="issues")
    source_file = models.ForeignKey(SourceFile, null=True, blank=True, on_delete=models.CASCADE, related_name="issues")
    code = models.CharField("種類", max_length=32, choices=Code.choices)
    message = models.CharField("内容", max_length=255)
    resolved = models.BooleanField("解決済み", default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resolution_note = models.CharField("対応内容", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "確認事項"
        verbose_name_plural = "確認事項"
        ordering = ["resolved", "-created_at"]
