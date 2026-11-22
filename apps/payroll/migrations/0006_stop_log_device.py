"""運用変更: ログ機械は各自で購入することになったため、初回給与からのログ機械代金（5,000円）の差し引きを停止する。

項目は削除せず「使用中」をオフにする（過去の確定済み給与はスナップショットのまま変わらない）。
"""

from django.db import migrations


def stop(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    Item.objects.filter(code="log_device").update(
        is_active=False, note="2026年11月〜 停止：ログ機械は各自で購入するため差し引かない"
    )


def restart(apps, schema_editor):
    apps.get_model("payroll", "PayrollItemDefinition").objects.filter(code="log_device").update(is_active=True)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0005_restore_bypass")]
    operations = [migrations.RunPython(stop, restart)]
