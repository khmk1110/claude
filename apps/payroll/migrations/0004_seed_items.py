"""給与項目・商材の初期設定（既存の運用ルール）。個人への割り当ては管理画面で行う。"""

from django.db import migrations

C, I = "contractor", "intern"

PRODUCTS = [
    ("propane_bath", "プロパン（風呂あり）", 10),
    ("propane_nobath", "プロパン（風呂なし）", 20),
    ("toshigas", "都市ガス", 30),
    ("toshigas_denki", "都市ガス電気", 40),
    ("houmon_denki", "訪問電気", 50),
]

# code, 名称, 区分, 計算方法, params, 金額, 範囲, 雇用区分, 初回のみ, 表示順, 内訳文言
ITEMS = [
    ("sales_commission", "営業報酬", "earning", "sales_commission", {"always_show": True}, None, "employment_type", [C, I], False, 10, ""),
    ("product_propane_bath", "プロパン（風呂あり）", "earning", "product_commission", {"product": "propane_bath"}, 35000, "member", [], False, 11, ""),
    ("product_propane_nobath", "プロパン（風呂なし）", "earning", "product_commission", {"product": "propane_nobath"}, 25000, "member", [], False, 12, ""),
    # 都市ガスの単価はその月の電気の単価と同じ（電気＝単価表の自動単価）
    ("product_toshigas", "都市ガス", "earning", "product_commission", {"product": "toshigas", "unit_price": "auto"}, None, "member", [], False, 13, ""),
    ("product_toshigas_denki", "都市ガス電気", "earning", "product_commission", {"product": "toshigas_denki", "unit_price": "auto"}, None, "member", [], False, 14, ""),
    ("product_houmon_denki", "訪問電気", "earning", "product_commission", {"product": "houmon_denki", "unit_price": "auto"}, None, "member", [], False, 15, ""),
    ("team_bonus", "チームリーダーボーナス", "earning", "team_bonus",
     {"tiers": [[70, 30000], [80, 40000], [90, 50000]], "manual_from": 100, "include_leader_in_average": True}, None, "all", [], False, 20, ""),
    ("intern_leader", "インターンリーダー手当", "earning", "intern_leader",
     {"rate": 500, "min_attendance_days": 4, "attendance_month": "current", "include_leader": False}, None, "all", [], False, 21, ""),
    ("transport_contractor", "交通費精算", "earning", "expense_sum", {"category": "transport", "subtract": 35000, "always_show": True}, None, "employment_type", [C], False, 30, ""),
    ("transport_intern", "交通費精算", "earning", "expense_sum", {"category": "transport", "always_show": True}, None, "employment_type", [I], False, 30, ""),
    ("advance", "立替金", "earning", "expense_sum", {"category": "advance"}, None, "all", [], False, 31, ""),
    ("allowance", "手当", "earning", "manual", {"always_show": True}, 0, "employment_type", [C, I], False, 40, ""),
    ("special_allowance", "特別手当", "earning", "manual", {}, 0, "employment_type", [C, I], False, 41, ""),
    ("other_earning", "その他報酬", "earning", "manual", {}, 0, "employment_type", [C, I], False, 42, ""),
    ("gasoline", "ガソリン代", "deduction", "expense_sum", {"category": "gasoline", "always_show": True}, None, "employment_type", [C], False, 50, ""),
    ("app_fee", "アプリ手数料", "deduction", "fixed", {"always_show": True}, 4000, "employment_type", [C], False, 51, "個人事業主のみ"),
    ("log_device", "ログ機械代金", "deduction", "fixed", {}, 5000, "employment_type", [C], True, 52, "初回のみ"),
    ("other_deduction", "その他控除", "deduction", "manual", {}, 0, "employment_type", [C, I], False, 60, ""),
]


def seed(apps, schema_editor):
    Product = apps.get_model("payroll", "Product")
    Item = apps.get_model("payroll", "PayrollItemDefinition")
    for code, name, order in PRODUCTS:
        Product.objects.get_or_create(code=code, defaults={"name": name, "display_order": order})
    for code, name, kind, method, params, amount, scope, types, first, order, label in ITEMS:
        Item.objects.get_or_create(
            code=code,
            defaults=dict(name=name, kind=kind, method=method, params=params, default_amount=amount, scope=scope,
                          employment_types=types, first_payroll_only=first, display_order=order, slip_label=label),
        )


class Migration(migrations.Migration):
    dependencies = [("payroll", "0003_calc_fields")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
