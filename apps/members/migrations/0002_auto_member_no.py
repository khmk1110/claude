"""メンバー番号を「1から順の数字」にそろえる（取り込み時の M0001 形式 → 1）。欠番は詰めない。"""

import re

from django.db import migrations, models


def to_numbers(apps, schema_editor):
    Member = apps.get_model("members", "Member")
    used = set(Member.objects.values_list("member_no", flat=True))
    for m in Member.objects.all():
        hit = re.fullmatch(r"M0*(\d+)", m.member_no or "")
        if hit and hit.group(1) not in used:
            used.discard(m.member_no)
            m.member_no = hit.group(1)
            used.add(m.member_no)
            m.save(update_fields=["member_no"])


class Migration(migrations.Migration):
    dependencies = [("members", "0001_initial")]
    operations = [
        migrations.AlterField(
            model_name="member", name="member_no",
            field=models.CharField(blank=True, help_text="空欄なら自動で採番（1から順。退職・削除しても詰めない）",
                                   max_length=20, unique=True, verbose_name="メンバー番号"),
        ),
        migrations.AlterField(
            model_name="member", name="display_order",
            field=models.PositiveIntegerField(default=0, help_text="0 なら追加した順で一番最後に並ぶ", verbose_name="並び順"),
        ),
        migrations.RunPython(to_numbers, migrations.RunPython.noop),
    ]
