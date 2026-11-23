"""メンバー番号1の正社員を有給管理の対象にする（代表者の依頼。名前はコードに書かない）。"""

from django.db import migrations


def forward(apps, schema_editor):
    Member = apps.get_model("members", "Member")
    Member.objects.filter(member_no="1", employment_type="employee", status="active").update(paid_leave_target=True)


class Migration(migrations.Migration):
    dependencies = [("members", "0003_shift_input_mode")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
