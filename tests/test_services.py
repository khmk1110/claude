import pytest

from payroll import services as s


def add(conn, name, cap=None, category="contractor"):
    return s.save_member(conn, None, name, category, False, cap, "", True, 0)


def setup_amano(conn):
    mid = add(conn, "天野")
    s.create_month(conn, "2026-08")
    s.save_record(conn, "2026-08", mid, 22, 61, None)
    s.create_month(conn, "2026-09")
    s.save_record(conn, "2026-09", mid, 20, 50, None)
    conn.commit()
    return mid


def row(conn, month, mid):
    return next(r for r in s.month_rows(conn, month) if r["member_id"] == mid)


def test_month_helpers():
    assert s.prev_month("2026-01") == "2025-12"
    assert s.next_month("2026-12") == "2027-01"
    assert s.days_in_month("2028-02") == 29
    assert s.month_label("2026-09") == "2026年9月"


def test_prev_count_auto_fetched(conn):
    mid = setup_amano(conn)
    r = row(conn, "2026-09", mid)
    assert r["prev_count"] == 61 and r["prev_count_source"] == "auto"
    assert r["rate"] == 6750 and r["sales_reward"] == 337500


def test_months_are_independent(conn):
    mid = setup_amano(conn)
    s.save_record(conn, "2026-09", mid, 25, 70, None)
    conn.commit()
    aug = row(conn, "2026-08", mid)
    assert aug["acquisitions"] == 61 and aug["work_days"] == 22


def test_manual_prev_count_only_without_prev_record(conn):
    mid = add(conn, "島")
    s.create_month(conn, "2026-09")
    s.save_record(conn, "2026-09", mid, 20, 50, 61)
    conn.commit()
    r = row(conn, "2026-09", mid)
    assert r["prev_count"] == 61 and r["prev_count_source"] == "manual" and r["sales_reward"] == 337500
    # 前月データがあれば手入力値は使わず自動取得が優先
    s.create_month(conn, "2026-10")
    s.save_record(conn, "2026-10", mid, 20, 10, 999)
    conn.commit()
    r = row(conn, "2026-10", mid)
    assert r["prev_count"] == 50 and r["prev_count_source"] == "auto" and r["prev_count_manual"] is None


def test_missing_prev_month_is_error(conn):
    mid = add(conn, "平川")
    s.create_month(conn, "2026-09")
    s.save_record(conn, "2026-09", mid, 20, 50, None)
    r = row(conn, "2026-09", mid)
    assert r["payment_total"] is None and "前月" in r["errors"][0]["message"]


def test_items_and_validation(conn):
    mid = setup_amano(conn)
    s.add_item(conn, "2026-09", mid, "transport", "2026-09", 45000, "ワースリビング分調整後")
    s.add_item(conn, "2026-09", mid, "gasoline", "2026-05", 33936, "ガソリン")
    with pytest.raises(s.ValidationError):
        s.add_item(conn, "2026-09", mid, "gasoline", None, 100, "")
    with pytest.raises(s.ValidationError):
        s.add_item(conn, "2026-09", mid, "contract_reward", "2026-09", 30000, "")
    with pytest.raises(s.ValidationError):
        s.add_item(conn, "2026-09", mid, "transport", "2026-09", 0, "")
    r = row(conn, "2026-09", mid)
    assert r["payment_total"] == 337500 + 45000 - 33936


def test_close_freezes_values(conn):
    mid = setup_amano(conn)
    with pytest.raises(s.ValidationError):  # 未確認
        s.close_month(conn, "2026-09")
    s.set_checked(conn, "2026-09", mid, True)
    s.close_month(conn, "2026-09")
    with pytest.raises(s.ClosedMonthError):
        s.save_record(conn, "2026-09", mid, 1, 1, None)
    with pytest.raises(s.ClosedMonthError):
        s.add_item(conn, "2026-09", mid, "advance", "2026-09", 100, "")
    # 前月（8月）を後から修正しても、締め済みの9月の金額は変わらない
    s.save_record(conn, "2026-08", mid, 22, 130, None)
    conn.commit()
    r = row(conn, "2026-09", mid)
    assert r["rate"] == 6750 and r["payment_total"] == 337500 and r["closed"]
    assert any("締め後" in w["message"] for w in r["warnings"])
    with pytest.raises(s.ValidationError):
        s.reopen_month(conn, "2026-09", "")
    s.reopen_month(conn, "2026-09", "8月件数訂正のため")
    assert row(conn, "2026-09", mid)["rate"] == 7750  # 前月130件・出勤20日


def test_close_blocked_by_errors(conn):
    mid = add(conn, "矢木")
    s.create_month(conn, "2026-09")
    s.set_checked(conn, "2026-09", mid, True)
    with pytest.raises(s.ValidationError):
        s.close_month(conn, "2026-09")


def test_category_snapshot_on_record(conn):
    mid = setup_amano(conn)
    s.save_member(conn, mid, "天野", "employee", False, None, "", True, 0)
    assert row(conn, "2026-09", mid)["category"] == "contractor"


def test_contractor_cannot_have_car(conn):
    with pytest.raises(s.ValidationError):
        s.save_member(conn, None, "X", "contractor", True, None, "", True, 0)


def test_edit_resets_check(conn):
    mid = setup_amano(conn)
    s.set_checked(conn, "2026-09", mid, True)
    s.save_record(conn, "2026-09", mid, 21, 50, None)
    conn.commit()
    assert not row(conn, "2026-09", mid)["checked"]
