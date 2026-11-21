"""PDF 用の読み取りルール（既存資料の形式）。反映する給与の月は資料の月からのずれで指定する。

- ガソリン（ENEOS FC 請求明細表）: 9月分の請求 → 10月の給与（+1か月）
- 交通費（請求書の「交通費(8月分)」行）: 8月分 → 10月の給与（+2か月）
（2026年10月の給与と照合して確認済み）
"""

from django.db import migrations

PROFILES = [
    ("ENEOS FC 請求明細表（合計版）", "gasoline", "builtin:eneos_card_summary",
     {"parser": "eneos_card_summary", "payroll_month_offset": 1}, "gasoline"),
    ("請求書PDF（交通費の行）", "transport", "builtin:transport_invoice",
     {"parser": "transport_invoice", "payroll_month_offset": 2}, "transport"),
]


def seed(apps, schema_editor):
    Profile = apps.get_model("expenses", "ImportProfile")
    Cat = apps.get_model("expenses", "ExpenseCategory")
    for name, kind, sig, options, cat in PROFILES:
        Profile.objects.get_or_create(
            header_signature=sig,
            defaults=dict(name=name, document_kind=kind, options=options, default_category=Cat.objects.get(code=cat)),
        )


class Migration(migrations.Migration):
    dependencies = [("expenses", "0003_import_fields")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
