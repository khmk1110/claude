from payroll import calc


def items(*pairs):
    return [{"item_type": t, "amount": a, "target_month": "2026-09"} for t, a in pairs]


def test_spec_example_uses_current_month_count():
    r = calc.compute("contractor", work_days=20, acquisitions=50, prev_count=61, prev_count_source="auto", items=[])
    assert r.rate == 6750
    assert r.sales_reward == 337500          # 当月50件 × 6,750
    assert r.sales_reward != 61 * 6750       # 前月件数×単価 ではない
    assert r.payment_total == 337500


def test_payslip_example_total():
    # 13. 個人別報酬明細の例：337,500 + 45,000 + 10,000 - 15,000 = 377,500
    r = calc.compute("contractor", 20, 50, 61, "auto",
                     items(("transport", 45000), ("advance", 10000), ("gasoline", 15000)))
    assert r.payment_total == 377500
    assert r.car_allowance == 0              # 個人事業主は車手当なし


def test_all_additions_and_multiple_gasoline_months():
    its = items(("transport", 45000), ("advance", 1000), ("expense", 2000),
                ("other_reward", 3000), ("contract_reward", 30000))
    its += [{"item_type": "gasoline", "amount": 33936, "target_month": "2026-05"},
            {"item_type": "gasoline", "amount": 55542, "target_month": "2026-07"}]
    r = calc.compute("contractor", 20, 50, 61, "auto", its)
    assert r.totals["gasoline"] == 89478
    assert r.other_total == 33000
    assert r.payment_total == 337500 + 45000 + 1000 + 2000 + 33000 - 89478


def test_missing_inputs_block_total():
    r = calc.compute("contractor", None, 50, 61, "auto", [])
    assert r.payment_total is None and r.rate is None
    assert any("出勤日数" in e.message for e in r.errors)
    r = calc.compute("contractor", 20, None, 61, "auto", [])
    assert r.payment_total is None
    r = calc.compute("contractor", 20, 50, None, "", [], prev_missing_message="前月なし")
    assert r.payment_total is None and r.errors[0].message == "前月なし"


def test_work_days_over_month_length():
    r = calc.compute("contractor", 30, 10, 10, "auto", [], days_in_month=28)
    assert r.errors and r.payment_total is None


def test_gasoline_without_target_month_is_error_but_rate_still_shown():
    r = calc.compute("contractor", 20, 50, 61, "auto", [{"item_type": "gasoline", "amount": 100, "target_month": None}])
    assert r.rate == 6750 and r.sales_reward == 337500
    assert r.payment_total is None
    assert any("ガソリン" in e.message for e in r.errors)


def test_expense_cap_warning():
    r = calc.compute("contractor", 20, 50, 61, "auto", items(("expense", 36000)), expense_cap=35000)
    assert r.payment_total is not None
    assert any("目安" in w.message for w in r.warnings)
    r = calc.compute("contractor", 20, 50, 61, "auto", items(("expense", 35000)), expense_cap=35000)
    assert not r.warnings


def test_employee_not_auto_computed():
    r = calc.compute("employee", 20, 50, 61, "auto", [], uses_private_car=True)
    assert r.payment_total is None and r.sales_reward is None
    assert r.car_allowance == 20000
    assert any("仕様確認" in w.message for w in r.warnings)
    assert calc.compute("employee", 20, 50, 61, "auto", []).car_allowance == 0
    assert calc.compute("contractor", 20, 50, 61, "auto", [], uses_private_car=True).car_allowance == 0
