from django.db import migrations

CATEGORIES = [
    ("gasoline", "ガソリン代", "gasoline", "gasoline", 10),
    ("transport", "交通費", "transport", "transport", 20),
    ("advance", "立替金", "advance", "advance", 30),
    ("other_expense", "その他経費", "other_expense", "", 40),
]


def seed(apps, schema_editor):
    Cat = apps.get_model("expenses", "ExpenseCategory")
    for code, name, kind, item, order in CATEGORIES:
        Cat.objects.get_or_create(code=code, defaults=dict(name=name, document_kind=kind, payroll_item_code=item,
                                                            display_order=order))


class Migration(migrations.Migration):
    dependencies = [("expenses", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
