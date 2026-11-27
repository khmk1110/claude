"""交通費・経費だけの月の計算（35,000円・アプリ手数料を引かない）に合わせて、確定前の明細を計算し直す。"""

from django.db import migrations


def forward(apps, schema_editor):
    apps.get_model("payroll", "PayrollStatement").objects.exclude(status="confirmed").update(stale=True)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0027_propane_for_others")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
