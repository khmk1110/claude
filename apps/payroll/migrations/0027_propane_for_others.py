"""プロパン（LP）を個人専用の単価がない人にも払う：風呂あり 30,000円・風呂なし 20,000円（件数がある月だけ行が出る）。"""

from django.db import migrations

ITEMS = [("product_propane_bath_std", "プロパン（風呂あり）", "propane_bath", 30000, 11),
         ("product_propane_nobath_std", "プロパン（風呂なし）", "propane_nobath", 20000, 12)]


def forward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    for code, name, product, amount, order in ITEMS:
        Item.objects.get_or_create(code=code, defaults=dict(
            name=name, kind="earning", method="product_commission", params={"product": product, "for_others": True},
            default_amount=amount, scope="all", employment_types=[], first_payroll_only=False, display_order=order, slip_label=""))


class Migration(migrations.Migration):
    dependencies = [("payroll", "0026_recalc_after_update")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
