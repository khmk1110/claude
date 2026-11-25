"""プロパン電気をプロパン（風呂なし）のすぐ下に並べる（明細・入力欄の順番）。"""

from django.db import migrations

ORDER = [("product_propane_bath", 11), ("product_propane_nobath", 12), ("product_propane_denki", 13),
         ("product_toshigas", 14), ("product_toshigas_denki", 15), ("product_houmon_denki", 16)]


def forward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    Line = apps.get_model("payroll", "PayrollLine")
    Product = apps.get_model("payroll", "Product")
    for code, order in ORDER:
        Item.objects.filter(code=code).update(display_order=order)
        # まだ確定していない明細の行も並べ替える（確定済みの明細は変えない）
        Line.objects.filter(code=code).exclude(statement__status="confirmed").update(display_order=order)
    Product.objects.filter(code="propane_denki").update(display_order=25)  # 風呂なし(20)と都市ガス(30)の間


class Migration(migrations.Migration):
    dependencies = [("payroll", "0019_propane_denki")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
