from payroll import services as s


def seed(conn):
    mid = s.save_member(conn, None, "天野", "contractor", False, None, "", True, 1)
    hid = s.save_member(conn, None, "堀田", "contractor", False, 35000, "", True, 0)
    s.create_month(conn, "2026-08")
    s.save_record(conn, "2026-08", mid, None, 61, None)
    s.save_record(conn, "2026-08", hid, None, 40, None)
    s.create_month(conn, "2026-09")
    conn.commit()
    return mid, hid


def test_pages_render(client, conn):
    seed(conn)
    for url in ["/", "/?month=2026-09", "/monthly?month=2026-09", "/payments?month=2026-09",
                "/payslip?month=2026-09", "/payslip/all?month=2026-09", "/items", "/history?member_id=1",
                "/members", "/rates", "/audit", "/monthly?month=2030-01"]:
        assert client.get(url).status_code == 200, url


def test_full_flow(client, conn):
    mid, hid = seed(conn)
    r = client.post("/monthly/save?month=2026-09", data={
        f"wd_{mid}": "20", f"acq_{mid}": "50", f"prev_{mid}": "",
        f"wd_{hid}": "20", f"acq_{hid}": "10", f"prev_{hid}": ""}, follow_redirects=True)
    assert "保存しました" in r.get_data(as_text=True)
    for data in [
        {"member_id": mid, "item_type": "transport", "target_month": "2026-09", "amount": "45000", "description": "ワースリビング分調整後"},
        {"member_id": mid, "item_type": "advance", "target_month": "2026-09", "amount": "10000", "description": ""},
        {"member_id": mid, "item_type": "gasoline", "target_month": "2026-09", "amount": "15000", "description": "ガソリン"},
        {"member_id": hid, "item_type": "contract_reward", "target_month": "2026-09", "amount": "30000", "description": "サウナコンサル"},
        {"member_id": hid, "item_type": "expense", "target_month": "2026-09", "amount": "35000", "description": "食事代"},
    ]:
        client.post("/items/add?month=2026-09", data=data)

    page = client.get(f"/payslip?month=2026-09&member_id={mid}").get_data(as_text=True)
    assert "50件 × 6,750円" in page and "337,500" in page and "377,500" in page
    assert "35,000" not in page and "ワースリビング" not in page  # 控除・社内メモは明細に出さない

    hotta = client.get(f"/payslip?month=2026-09&member_id={hid}").get_data(as_text=True)
    assert "サウナコンサル" in hotta
    assert "立替・経費" in hotta
    # 前月40件・出勤20日 → 6,000円 × 10件 = 60,000 + 30,000 + 35,000
    assert "125,000" in hotta

    csv = client.get("/export/payments.csv?month=2026-09")
    assert csv.data.startswith("﻿".encode()) and "天野" in csv.get_data(as_text=True)
    assert client.get("/export/payments.xlsx?month=2026-09").status_code == 200
    assert client.get("/export/items.csv?month=2026-09").status_code == 200

    for m in (mid, hid):
        client.post("/payments/check?month=2026-09", data={"member_id": m, "checked": "1"})
    r = client.post("/months/close?month=2026-09", follow_redirects=True)
    assert "締めました" in r.get_data(as_text=True)
    assert s.is_closed(conn, "2026-09")


def test_rejects_non_numeric_and_out_of_range(client, conn):
    mid, _ = seed(conn)
    r = client.post("/monthly/save?month=2026-09", data={f"wd_{mid}": "abc", f"acq_{mid}": "5", f"prev_{mid}": ""},
                    follow_redirects=True)
    assert "数字のみ" in r.get_data(as_text=True)
    r = client.post("/monthly/save?month=2026-09", data={f"wd_{mid}": "31", f"acq_{mid}": "5", f"prev_{mid}": ""},
                    follow_redirects=True)
    assert "0～30" in r.get_data(as_text=True)  # 9月は30日まで
    assert s.get_record(conn, "2026-09", mid)["work_days"] is None


def test_preview_api(client, conn):
    mid, _ = seed(conn)
    d = client.get(f"/api/preview?month=2026-09&member_id={mid}&work_days=20&acquisitions=50").get_json()
    assert d["prev_count"] == 61 and d["rate"] == 6750 and d["sales_reward"] == 337500
