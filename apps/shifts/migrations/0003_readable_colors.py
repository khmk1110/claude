"""シフト表を見やすくする: 休みは空欄（文字なし・白）、出勤ははっきりした青、有給は緑。"""

from django.db import migrations

COLORS = {
    # code: (表示文字, 背景色, 文字色)
    "work": ("●", "#3b82f6", "#ffffff"),
    "half": ("半", "#bfdbfe", "#1e3a8a"),
    "off": ("", "#ffffff", "#9ca3af"),
    "paid": ("有", "#22c55e", "#ffffff"),
    "half_paid": ("半有", "#fde68a", "#92400e"),
}


def apply(apps, schema_editor):
    WorkType = apps.get_model("shifts", "WorkType")
    for code, (label, bg, fg) in COLORS.items():
        WorkType.objects.filter(code=code).update(short_label=label, color=bg, text_color=fg)


class Migration(migrations.Migration):
    dependencies = [("shifts", "0002_seed_work_types")]
    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
