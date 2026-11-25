"""チームリーダーボーナスはリーダーの人だけに出す（リーダーでない人の明細に0円の行を出さない）。"""

from django.db import migrations


def forward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    tb = Item.objects.filter(code="team_bonus").first()
    if tb:
        params = dict(tb.params or {})
        params.pop("always_show", None)
        tb.params = params
        tb.save()


class Migration(migrations.Migration):
    dependencies = [("payroll", "0015_product_member_counts_cleanup")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
