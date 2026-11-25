"""その他の経費（レシートなど）を給与に入れる項目を追加する。これまでは集計先の項目がなく、承認しても明細に出なかった。"""

from django.db import migrations


def forward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    Item.objects.get_or_create(
        code="other_expense",
        defaults=dict(name="その他経費", kind="earning", method="expense_sum", params={"category": "other_expense"},
                      default_amount=None, scope="all", employment_types=[], first_payroll_only=False, display_order=32,
                      slip_label=""),
    )
    try:
        Cat = apps.get_model("expenses", "ExpenseCategory")
        Cat.objects.filter(code="other_expense").update(payroll_item_code="other_expense")
        # 既に入っているその他の経費は、次に給与の画面を開いた時に計算し直されるよう印を付ける
        from django.db.models import Q
        from django.utils import timezone

        Rec = apps.get_model("expenses", "ExpenseRecord")
        Rec.objects.filter(Q(category__code="other_expense") | Q(category__isnull=True, auto_category__code="other_expense")).update(
            updated_at=timezone.now())
    except LookupError:
        pass


class Migration(migrations.Migration):
    dependencies = [("payroll", "0016_team_bonus_leaders_only"), ("expenses", "0007_processing_started_at")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
