"""計算のしかた（手入力のチームボーナス・直した行の注意）を変えたので、確定前の明細を次に開いた時に計算し直す。"""

from django.db import migrations


def forward(apps, schema_editor):
    apps.get_model("payroll", "PayrollStatement").objects.exclude(status="confirmed").update(stale=True)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0025_statement_auto_created")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
