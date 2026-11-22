"""「手当」と「チームリーダーボーナス」は同じもの（個人事業主はチームリーダーボーナスと表示）。

- 手当（手入力・0円で常に表示）を使わないようにする（確定済みの明細はそのまま）
- チームリーダーボーナスを常に明細に表示し、表示順をスプレッドシートの明細と同じにする
  （営業報酬 → 交通費精算 → チームリーダーボーナス → ガソリン代 → アプリ手数料）
"""

from django.db import migrations


def forward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    Item.objects.filter(code="allowance").update(is_active=False)
    tb = Item.objects.filter(code="team_bonus").first()
    if tb:
        tb.params = {**(tb.params or {}), "always_show": True}
        tb.display_order = 35
        tb.save()
    Item.objects.filter(code="intern_leader").update(display_order=36)


def backward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    Item.objects.filter(code="allowance").update(is_active=True)
    tb = Item.objects.filter(code="team_bonus").first()
    if tb:
        params = dict(tb.params or {})
        params.pop("always_show", None)
        tb.params, tb.display_order = params, 20
        tb.save()
    Item.objects.filter(code="intern_leader").update(display_order=21)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0011_sales_evidence_parent")]
    operations = [migrations.RunPython(forward, backward)]
