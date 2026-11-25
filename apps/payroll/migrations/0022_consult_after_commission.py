"""報酬（営業報酬・商材別の件数×単価）をまとめて上に並べ、コンサル代など毎月の固定額はその下にする。"""

from django.db import migrations

AFTER = 25  # 商材別（〜16）の下、交通費（30）の上


def forward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    Line = apps.get_model("payroll", "PayrollLine")
    for item in Item.objects.filter(scope="member", method__in=["fixed", "manual"], kind="earning", display_order__lte=16):
        item.display_order = AFTER
        item.save(update_fields=["display_order"])
        Line.objects.filter(code=item.code).exclude(statement__status="confirmed").update(display_order=AFTER)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0021_statement_stale")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
