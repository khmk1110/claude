"""給与のルールを運用に合わせる。

- チームリーダーボーナス：前月の獲得件数のチーム平均で自動計算（70件3万・80件4万・90件5万／メンバー1名あたり、100件以上は別途協議）
- インターン生リーダー手当：当月4日以上の出勤が条件
- サウナコンサル：毎月30,000円（手入力→固定額）。項目がなければ、個人別の商材（プロパン風呂あり）の対象者に作る
- アプリ手数料：2026年10月分までは「対象者」に選んだ人だけ、11月分からは個人事業主全員
"""

from django.db import migrations


def forward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    tb = Item.objects.filter(code="team_bonus").first()
    if tb:
        tb.params = {**(tb.params or {}), "tiers": [[70, 30000], [80, 40000], [90, 50000]], "manual_from": 100,
                     "include_leader_in_average": True, "count_basis": "prev_acquired"}
        tb.save()
    il = Item.objects.filter(code="intern_leader").first()
    if il:
        il.params = {**(il.params or {}), "min_attendance_days": 4, "attendance_month": "current"}
        il.save()

    sauna = Item.objects.filter(name__contains="サウナ").first()
    if sauna:
        sauna.method, sauna.default_amount, sauna.is_active = "fixed", 30000, True
        sauna.save()
    else:
        base = Item.objects.filter(code="product_propane_bath").first()
        if base and base.members.exists():
            sauna = Item.objects.create(code="sauna_consult", name="サウナコンサル", kind="earning", method="fixed",
                                        default_amount=30000, scope="member", display_order=16, params={})
            sauna.members.set(base.members.all())

    fee = Item.objects.filter(code="app_fee").first()
    if fee:
        fee.params = {**(fee.params or {}), "only_members_until": "2026-10"}
        fee.save()


def backward(apps, schema_editor):
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    fee = Item.objects.filter(code="app_fee").first()
    if fee:
        params = dict(fee.params or {})
        params.pop("only_members_until", None)
        fee.params = params
        fee.save()


class Migration(migrations.Migration):
    dependencies = [("payroll", "0012_merge_allowance_into_team_bonus")]
    operations = [migrations.RunPython(forward, backward)]
