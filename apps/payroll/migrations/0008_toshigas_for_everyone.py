"""都市ガスは誰でも取る可能性があるため、全員共通の項目にする（その月に件数を入れた人にだけ行が付く）。"""

from django.db import migrations


def apply(apps, schema_editor):
    apps.get_model("payroll", "PayrollItemDefinition").objects.filter(code="product_toshigas").update(scope="all")


def revert(apps, schema_editor):
    apps.get_model("payroll", "PayrollItemDefinition").objects.filter(code="product_toshigas").update(scope="member")


class Migration(migrations.Migration):
    dependencies = [("payroll", "0007_delete_log_device")]
    operations = [migrations.RunPython(apply, revert)]
