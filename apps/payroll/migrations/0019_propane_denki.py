"""商材「プロパン電気」を追加する。単価は訪問電気と同じ（単価表の自動単価）。

対象者は訪問電気を個人別で計算している人と同じにする（商材別で報酬を計算する人）。
"""

from django.db import migrations


def forward(apps, schema_editor):
    Product = apps.get_model("payroll", "Product")
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    Product.objects.get_or_create(code="propane_denki", defaults={"name": "プロパン電気", "display_order": 55})
    item, created = Item.objects.get_or_create(
        code="product_propane_denki",
        defaults=dict(name="プロパン電気", kind="earning", method="product_commission",
                      params={"product": "propane_denki", "unit_price": "auto"}, default_amount=None, scope="member",
                      employment_types=[], first_payroll_only=False, display_order=16, slip_label=""),
    )
    if created:
        houmon = Item.objects.filter(code="product_houmon_denki").first()
        if houmon:
            item.members.set(houmon.members.all())


class Migration(migrations.Migration):
    dependencies = [("payroll", "0018_employee_pay")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
