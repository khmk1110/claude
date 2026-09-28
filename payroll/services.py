"""DBアクセスと月次集計。計算そのものは calc.py が担当する。"""
import calendar
import json
import re

from . import calc

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ClosedMonthError(Exception):
    pass


class ValidationError(Exception):
    pass


# ---------- 月の扱い ----------

def valid_month(month):
    return bool(month and MONTH_RE.match(month))


def prev_month(month):
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def next_month(month):
    y, m = map(int, month.split("-"))
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def days_in_month(month):
    y, m = map(int, month.split("-"))
    return calendar.monthrange(y, m)[1]


def month_label(month):
    if not valid_month(month or ""):
        return month or ""
    y, m = month.split("-")
    return f"{y}年{int(m)}月"


def short_month_label(month):
    if not valid_month(month or ""):
        return month or ""
    return f"{int(month.split('-')[1])}月"


def known_months(conn):
    rows = conn.execute(
        "SELECT month FROM monthly_records UNION SELECT month FROM line_items ORDER BY month DESC"
    ).fetchall()
    return [r["month"] for r in rows]


def is_closed(conn, month):
    return conn.execute("SELECT 1 FROM closed_months WHERE month=?", (month,)).fetchone() is not None


def _require_open(conn, month):
    if is_closed(conn, month):
        raise ClosedMonthError(f"{month_label(month)}は締め済みのため変更できません（締め解除が必要です）")


def log(conn, action, month=None, member_id=None, detail=""):
    conn.execute(
        "INSERT INTO audit_log (month, member_id, action, detail) VALUES (?,?,?,?)",
        (month, member_id, action, detail),
    )


# ---------- メンバー ----------

def list_members(conn, active_only=False):
    sql = "SELECT * FROM members"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY sort_order, id"
    return conn.execute(sql).fetchall()


def get_member(conn, member_id):
    return conn.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()


def save_member(conn, member_id, name, category, uses_private_car, expense_cap, note, active, sort_order):
    name = (name or "").strip()
    if not name:
        raise ValidationError("氏名を入力してください")
    if category not in calc.CATEGORIES:
        raise ValidationError("区分を選択してください")
    if category == "contractor" and uses_private_car:
        raise ValidationError("個人事業主は車手当の対象外です（自家用車使用フラグは正社員のみ）")
    dup = conn.execute("SELECT id FROM members WHERE name=? AND id IS NOT ?", (name, member_id)).fetchone()
    if dup:
        raise ValidationError(f"「{name}」は既に登録されています")
    vals = (name, category, int(bool(uses_private_car)), expense_cap, note or "", int(bool(active)), sort_order)
    if member_id:
        conn.execute(
            "UPDATE members SET name=?, category=?, uses_private_car=?, expense_cap=?, note=?, active=?, sort_order=? WHERE id=?",
            vals + (member_id,),
        )
        log(conn, "メンバー更新", member_id=member_id, detail=f"{name} / {calc.CATEGORIES[category]}")
    else:
        cur = conn.execute(
            "INSERT INTO members (name, category, uses_private_car, expense_cap, note, active, sort_order) VALUES (?,?,?,?,?,?,?)",
            vals,
        )
        member_id = cur.lastrowid
        log(conn, "メンバー追加", member_id=member_id, detail=f"{name} / {calc.CATEGORIES[category]}")
    conn.commit()
    return member_id


# ---------- 月次レコード ----------

def create_month(conn, month):
    """対象月の入力行を、在籍中メンバー分だけ作成する（既存行はそのまま）。"""
    _require_open(conn, month)
    created = 0
    for m in list_members(conn, active_only=True):
        cur = conn.execute(
            "INSERT OR IGNORE INTO monthly_records (month, member_id, category, uses_private_car) VALUES (?,?,?,?)",
            (month, m["id"], m["category"], m["uses_private_car"]),
        )
        created += cur.rowcount
    if created:
        log(conn, "月次作成", month=month, detail=f"{created}名分の入力行を作成")
    conn.commit()
    return created


def get_record(conn, month, member_id):
    return conn.execute(
        "SELECT * FROM monthly_records WHERE month=? AND member_id=?", (month, member_id)
    ).fetchone()


def save_record(conn, month, member_id, work_days, acquisitions, prev_count_manual):
    _require_open(conn, month)
    rec = get_record(conn, month, member_id)
    if rec is None:
        raise ValidationError("対象月の入力行がありません（先に「今月分を作成」してください）")
    if work_days is not None and not 0 <= work_days <= days_in_month(month):
        raise ValidationError(f"出勤日数は0～{days_in_month(month)}日で入力してください")
    if acquisitions is not None and acquisitions < 0:
        raise ValidationError("獲得件数は0以上で入力してください")
    # 前月データがある場合は自動取得が優先。手入力値は保持しない。
    if get_record(conn, prev_month(month), member_id) is not None:
        prev_count_manual = None
    if prev_count_manual is not None and prev_count_manual < 0:
        raise ValidationError("前月獲得件数は0以上で入力してください")
    new = (work_days, acquisitions, prev_count_manual)
    old = (rec["work_days"], rec["acquisitions"], rec["prev_count_manual"])
    if new == old:
        return False
    conn.execute(
        "UPDATE monthly_records SET work_days=?, acquisitions=?, prev_count_manual=?, checked=0,"
        " updated_at=datetime('now','localtime') WHERE id=?",
        new + (rec["id"],),
    )
    log(conn, "入力変更", month=month, member_id=member_id,
        detail=f"出勤日数 {old[0]}→{new[0]} / 当月件数 {old[1]}→{new[1]} / 前月件数(手入力) {old[2]}→{new[2]}")
    return True


def set_checked(conn, month, member_id, checked):
    _require_open(conn, month)
    conn.execute(
        "UPDATE monthly_records SET checked=? WHERE month=? AND member_id=?",
        (int(bool(checked)), month, member_id),
    )
    conn.commit()


# ---------- 支払項目（内訳） ----------

def list_items(conn, month=None, member_id=None, item_type=None):
    sql = ("SELECT li.*, m.name AS member_name FROM line_items li"
           " JOIN members m ON m.id = li.member_id WHERE 1=1")
    args = []
    if month:
        sql += " AND li.month=?"
        args.append(month)
    if member_id:
        sql += " AND li.member_id=?"
        args.append(member_id)
    if item_type:
        sql += " AND li.item_type=?"
        args.append(item_type)
    sql += " ORDER BY li.month DESC, m.sort_order, m.id, li.item_type, li.target_month, li.id"
    return conn.execute(sql, args).fetchall()


def add_item(conn, month, member_id, item_type, target_month, amount, description):
    _require_open(conn, month)
    if item_type not in calc.ITEM_TYPES:
        raise ValidationError("項目を選択してください")
    if get_member(conn, member_id) is None:
        raise ValidationError("対象者を選択してください")
    if amount is None or amount <= 0:
        raise ValidationError("金額は1円以上の数字で入力してください")
    if target_month and not valid_month(target_month):
        raise ValidationError("対象月が不正です")
    if item_type == "gasoline" and not target_month:
        raise ValidationError("ガソリン代は「何月分か（対象月）」の入力が必須です")
    if item_type == "contract_reward" and not (description or "").strip():
        raise ValidationError("契約報酬は内容（例：サウナコンサル、プロパン）の入力が必須です")
    if get_record(conn, month, member_id) is None:
        raise ValidationError("対象月にこの対象者の入力行がありません（先に「今月分を作成」してください）")
    conn.execute(
        "INSERT INTO line_items (month, member_id, item_type, target_month, amount, description) VALUES (?,?,?,?,?,?)",
        (month, member_id, item_type, target_month or None, amount, (description or "").strip()),
    )
    conn.execute("UPDATE monthly_records SET checked=0 WHERE month=? AND member_id=?", (month, member_id))
    log(conn, "項目追加", month=month, member_id=member_id,
        detail=f"{calc.ITEM_TYPES[item_type]['label']} {amount:,}円 ({target_month or '-'}) {description or ''}")
    conn.commit()


def delete_item(conn, item_id):
    it = conn.execute("SELECT * FROM line_items WHERE id=?", (item_id,)).fetchone()
    if it is None:
        return None
    _require_open(conn, it["month"])
    conn.execute("DELETE FROM line_items WHERE id=?", (item_id,))
    conn.execute("UPDATE monthly_records SET checked=0 WHERE month=? AND member_id=?", (it["month"], it["member_id"]))
    log(conn, "項目削除", month=it["month"], member_id=it["member_id"],
        detail=f"{calc.ITEM_TYPES[it['item_type']]['label']} {it['amount']:,}円 ({it['target_month'] or '-'}) {it['description']}")
    conn.commit()
    return it


# ---------- 計算 ----------

def _prev_count(conn, month, member_id, rec):
    prev = get_record(conn, prev_month(month), member_id)
    label = month_label(prev_month(month))
    if prev is not None:
        if prev["acquisitions"] is None:
            return None, "", f"前月（{label}）の獲得件数が未入力のため単価を判定できません"
        return prev["acquisitions"], "auto", ""
    if rec["prev_count_manual"] is not None:
        return rec["prev_count_manual"], "manual", ""
    return None, "", f"前月（{label}）のデータがないため単価を判定できません（前月分を入力するか、前月件数を手入力してください）"


def compute_live(conn, month, rec, member):
    prev_count, source, missing_msg = _prev_count(conn, month, member["id"], rec)
    order = list(calc.ITEM_TYPES)
    items = sorted((dict(i) for i in list_items(conn, month=month, member_id=member["id"])),
                   key=lambda i: (order.index(i["item_type"]), i["target_month"] or "", i["id"]))
    result = calc.compute(
        category=rec["category"],
        work_days=rec["work_days"],
        acquisitions=rec["acquisitions"],
        prev_count=prev_count,
        prev_count_source=source,
        items=items,
        days_in_month=days_in_month(month),
        expense_cap=member["expense_cap"],
        uses_private_car=bool(rec["uses_private_car"]),
        prev_missing_message=missing_msg or "前月獲得件数を取得できません",
    )
    row = result.to_dict()
    row.update(
        member_id=member["id"],
        name=member["name"],
        category_label=calc.CATEGORIES[rec["category"]],
        prev_count_manual=rec["prev_count_manual"],
        has_prev_record=get_record(conn, prev_month(month), member["id"]) is not None,
        items=[{k: i[k] for k in ("id", "item_type", "target_month", "amount", "description")} for i in items],
        rate_table_version=calc.rates.RATE_TABLE_VERSION,
    )
    return row


def month_rows(conn, month):
    """対象月の全員分の計算結果。締め済みの月は締め時点のスナップショットを返す。"""
    closed = is_closed(conn, month)
    snaps = {}
    if closed:
        for s in conn.execute("SELECT * FROM snapshots WHERE month=?", (month,)):
            snaps[s["member_id"]] = json.loads(s["data"])
    recs = conn.execute(
        "SELECT r.*, m.sort_order FROM monthly_records r JOIN members m ON m.id=r.member_id"
        " WHERE r.month=? ORDER BY m.sort_order, m.id",
        (month,),
    ).fetchall()
    rows = []
    for rec in recs:
        member = get_member(conn, rec["member_id"])
        live = compute_live(conn, month, rec, member)
        if closed and rec["member_id"] in snaps:
            row = snaps[rec["member_id"]]
            if live["payment_total"] != row["payment_total"] or live["rate"] != row["rate"]:
                row["issues"] = row["issues"] + [{
                    "level": "warning",
                    "message": "締め後に元データ（前月件数など）が変更されています。締め時点の金額を表示しています",
                }]
            row["closed"] = True
        else:
            row = live
            row["closed"] = False
        row["checked"] = bool(rec["checked"])
        row["errors"] = [i for i in row["issues"] if i["level"] == "error"]
        row["warnings"] = [i for i in row["issues"] if i["level"] == "warning"]
        rows.append(row)
    return rows


def month_summary(rows):
    def s(key):
        return sum(r[key] or 0 for r in rows)

    def st(t):
        return sum(r["totals"].get(t, 0) for r in rows)

    return {
        "people": len(rows),
        "sales_reward": s("sales_reward"),
        "transport": st("transport"),
        "advance": st("advance"),
        "expense": st("expense"),
        "other": s("other_total"),
        "gasoline": st("gasoline"),
        "payment_total": s("payment_total"),
        "uncomputed": sum(1 for r in rows if r["payment_total"] is None),
        "errors": sum(len(r["errors"]) for r in rows),
        "warnings": sum(len(r["warnings"]) for r in rows),
        "unchecked": sum(1 for r in rows if not r["checked"]),
    }


def close_month(conn, month):
    _require_open(conn, month)
    rows = month_rows(conn, month)
    if not rows:
        raise ValidationError("締める対象のデータがありません")
    errs = [f"{r['name']}：{e['message']}" for r in rows for e in r["errors"]]
    if errs:
        raise ValidationError("エラーが残っているため締められません。 " + " / ".join(errs))
    unchecked = [r["name"] for r in rows if not r["checked"]]
    if unchecked:
        raise ValidationError("未確認の人がいるため締められません：" + "、".join(unchecked))
    for r in rows:
        data = {k: v for k, v in r.items() if k not in ("errors", "warnings", "checked", "closed")}
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (month, member_id, data) VALUES (?,?,?)",
            (month, r["member_id"], json.dumps(data, ensure_ascii=False)),
        )
    conn.execute("INSERT INTO closed_months (month) VALUES (?)", (month,))
    log(conn, "月次締め", month=month, detail=f"支払総額 {month_summary(rows)['payment_total']:,}円")
    conn.commit()


def reopen_month(conn, month, reason):
    if not is_closed(conn, month):
        return
    if not (reason or "").strip():
        raise ValidationError("締め解除の理由を入力してください")
    conn.execute("DELETE FROM snapshots WHERE month=?", (month,))
    conn.execute("DELETE FROM closed_months WHERE month=?", (month,))
    log(conn, "締め解除", month=month, detail=reason.strip())
    conn.commit()


def member_history(conn, member_id):
    months = [r["month"] for r in conn.execute(
        "SELECT month FROM monthly_records WHERE member_id=? ORDER BY month DESC", (member_id,))]
    out = []
    for month in months:
        for row in month_rows(conn, month):
            if row["member_id"] == member_id:
                row["month"] = month
                out.append(row)
    return out


def audit_entries(conn, limit=300):
    return conn.execute(
        "SELECT a.*, m.name AS member_name FROM audit_log a LEFT JOIN members m ON m.id=a.member_id"
        " ORDER BY a.id DESC LIMIT ?", (limit,)
    ).fetchall()
