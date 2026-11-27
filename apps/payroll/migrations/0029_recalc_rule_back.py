"""個人事業主は報酬がない月も35,000円・アプリ手数料を引く決まりに戻したので、確定前の明細を計算し直す。"""

from django.db import migrations


def forward(apps, schema_editor):
    apps.get_model("payroll", "PayrollStatement").objects.exclude(status="confirmed").update(stale=True)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0028_recalc_expense_only")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
