"""運用変更: ログ機械代金の項目を削除する（ログ機械は各自で購入）。

確定済みの給与に含まれていた場合でも、明細はスナップショットから出力されるため影響しない。
未確定の給与に残っている行は削除する。
"""

from django.db import migrations


def delete_item(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    Line = apps.get_model("payroll", "PayrollLine")
    Line.objects.filter(code="log_device").exclude(statement__status="confirmed").delete()
    Item.objects.filter(code="log_device").delete()


class Migration(migrations.Migration):
    dependencies = [("payroll", "0006_stop_log_device")]
    operations = [migrations.RunPython(delete_item, migrations.RunPython.noop)]
