"""会社（代表）の支払い管理。何に・いくら・いつまでに・どこへ振り込んだかを月ごとに管理する。

請求書（写真・PDF）を読み取って下書きを作り、確認して保存する。支払先は覚えておき、次回は分類・振込先を自動で入れる。
"""

from django.conf import settings
from django.db import models

from apps.core.months import validate_month_start

CATEGORY_CHOICES = ["外注・業務委託", "家賃", "給与", "税金", "返済", "経費", "広告", "その他"]


class Payee(models.Model):
    """支払先（取引先）。振込先と分類を覚えておく。"""

    name = models.CharField("支払先", max_length=200, unique=True)
    category = models.CharField("分類", max_length=50, blank=True)
    bank = models.TextField("振込先（銀行・支店・口座）", blank=True)
    monthly = models.BooleanField("毎月の支払い", default=False)
    monthly_amount = models.IntegerField("毎月の金額", null=True, blank=True)
    note = models.CharField("メモ", max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "支払先"
        verbose_name_plural = "支払先"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Payment(models.Model):
    class Status(models.TextChoices):
        PROCESSING = "processing", "読み取り中"
        DRAFT = "draft", "確認待ち"
        UNPAID = "unpaid", "未払い"
        PAID = "paid", "支払い済み"

    month = models.DateField("対象月", validators=[validate_month_start], db_index=True)
    payee = models.ForeignKey(Payee, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments")
    payee_name = models.CharField("支払先", max_length=200, blank=True)
    amount = models.IntegerField("金額", null=True, blank=True)
    category = models.CharField("分類", max_length=50, blank=True)
    due_date = models.DateField("支払期限", null=True, blank=True)
    bank = models.TextField("振込先", blank=True)
    memo = models.CharField("メモ", max_length=255, blank=True)
    status = models.CharField("状態", max_length=16, choices=Status.choices, default=Status.UNPAID, db_index=True)
    paid_at = models.DateTimeField("支払った日時", null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    file = models.FileField("請求書", upload_to="payments/%Y/%m/", blank=True)
    original_name = models.CharField("ファイル名", max_length=255, blank=True)
    ocr_text = models.TextField("読み取った文字", blank=True)
    error_message = models.CharField("エラー", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "支払い"
        verbose_name_plural = "支払い"
        ordering = ["status", "due_date", "id"]

    def __str__(self):
        return f"{self.month:%Y-%m} {self.payee_name} {self.amount}"


class Sale(models.Model):
    """売上（その月の入金予定・入金）。"""

    month = models.DateField("対象月", validators=[validate_month_start], db_index=True)
    class State(models.TextChoices):
        PROCESSING = "processing", "読み取り中"
        DRAFT = "draft", "確認待ち"
        OK = "ok", "登録済み"

    client = models.CharField("取引先・内容", max_length=200, blank=True)
    amount = models.IntegerField("金額", null=True, blank=True)
    state = models.CharField("状態", max_length=16, choices=State.choices, default=State.OK, db_index=True)
    due_date = models.DateField("入金予定日", null=True, blank=True)
    file = models.FileField("資料", upload_to="sales/%Y/%m/", blank=True)
    original_name = models.CharField("ファイル名", max_length=255, blank=True)
    ocr_text = models.TextField("読み取った文字", blank=True)
    error_message = models.CharField("エラー", max_length=255, blank=True)
    received = models.BooleanField("入金済み", default=False)
    received_at = models.DateTimeField("入金を確認した日時", null=True, blank=True)
    memo = models.CharField("メモ", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "売上"
        verbose_name_plural = "売上"
        ordering = ["received", "id"]

    def __str__(self):
        return f"{self.month:%Y-%m} {self.client} {self.amount}"
