from decimal import Decimal

from django.db import migrations

WORK_TYPES = [
    # code, 名称, 表示, 背景色, 文字色, 実出勤, 有給
    ("work", "出勤", "●", "#dbeafe", "#1e3a8a", Decimal("1.0"), Decimal("0")),
    ("half", "半日稼働", "半", "#e0f2fe", "#075985", Decimal("0.5"), Decimal("0")),
    ("off", "休み", "休", "#f3f4f6", "#6b7280", Decimal("0"), Decimal("0")),
    ("paid", "有給", "有", "#dcfce7", "#166534", Decimal("0"), Decimal("1.0")),
    ("half_paid", "半日稼働＋半日有給", "半有", "#fef9c3", "#854d0e", Decimal("0.5"), Decimal("0.5")),
]


def seed(apps, schema_editor):
    WorkType = apps.get_model("shifts", "WorkType")
    for order, (code, name, label, bg, fg, att, paid) in enumerate(WORK_TYPES, start=1):
        WorkType.objects.get_or_create(
            code=code,
            defaults=dict(
                name=name,
                short_label=label,
                color=bg,
                text_color=fg,
                attendance_days=att,
                paid_leave_days=paid,
                display_order=order,
            ),
        )


class Migration(migrations.Migration):
    dependencies = [("shifts", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
