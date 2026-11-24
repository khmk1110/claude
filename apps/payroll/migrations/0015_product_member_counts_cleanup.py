"""件数の整理（代表者の依頼）。

- 商品別に報酬を計算する人（営業報酬の対象外にしている人）の「入金確定件数（商品なし）」を「訪問電気」の件数に移す
  （入金確定シートの件数＝訪問電気の件数。訪問電気の件数がすでにある月はそのまま）
- 都市ガスの件数は、商品別に報酬を計算する人以外は消す（必要な人にだけ、あとから追加する）
- 確定済みの月は変えない
"""

from django.db import migrations


def forward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    Product = apps.get_model("payroll", "Product")
    SalesCount = apps.get_model("payroll", "SalesCount")
    Statement = apps.get_model("payroll", "PayrollStatement")
    sc = Item.objects.filter(code="sales_commission").first()
    product_members = set(sc.excluded_members.values_list("pk", flat=True)) if sc else set()
    confirmed = set(Statement.objects.filter(status="confirmed").values_list("member_id", "month"))

    houmon = Product.objects.filter(code="houmon_denki").first()
    if houmon:
        for row in SalesCount.objects.filter(member_id__in=product_members, kind="confirmed", product__isnull=True):
            if (row.member_id, row.month) in confirmed:
                continue
            if not SalesCount.objects.filter(member_id=row.member_id, month=row.month, kind="confirmed", product=houmon).exists():
                SalesCount.objects.create(member_id=row.member_id, month=row.month, kind="confirmed", product=houmon,
                                          count=row.count, source=row.source)
            row.delete()

    toshigas = Product.objects.filter(code="toshigas").first()
    if toshigas:
        for row in SalesCount.objects.filter(product=toshigas, kind="confirmed").exclude(member_id__in=product_members):
            if (row.member_id, row.month) not in confirmed:
                row.delete()


class Migration(migrations.Migration):
    dependencies = [("payroll", "0014_statement_delivery")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
