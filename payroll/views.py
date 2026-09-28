import csv
import io
from datetime import date

from flask import (Blueprint, Response, abort, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)

from . import calc, rates, services
from .db import get_db

bp = Blueprint("main", __name__)


def current_month():
    m = request.values.get("month", "")
    return m if services.valid_month(m) else date.today().strftime("%Y-%m")


def parse_int(value, label, errors, lo=0, hi=None):
    """数字のみ受け付ける。空欄は None。"""
    value = (value or "").strip().replace(",", "")
    if value == "":
        return None
    if not value.isdigit():
        errors.append(f"{label}は数字のみで入力してください")
        return None
    n = int(value)
    if n < lo or (hi is not None and n > hi):
        errors.append(f"{label}は{lo}～{hi}の範囲で入力してください" if hi is not None else f"{label}は{lo}以上で入力してください")
        return None
    return n


def month_context(conn, month):
    return {
        "month": month,
        "closed": services.is_closed(conn, month),
        "prev_month": services.prev_month(month),
        "next_month": services.next_month(month),
        "known_months": services.known_months(conn),
    }


# ---------- ダッシュボード ----------

@bp.route("/")
def dashboard():
    conn = get_db()
    month = current_month()
    rows = services.month_rows(conn, month)
    return render_template("dashboard.html", rows=rows, summary=services.month_summary(rows),
                           **month_context(conn, month))


# ---------- 月次入力 ----------

@bp.route("/monthly")
def monthly():
    conn = get_db()
    month = current_month()
    rows = services.month_rows(conn, month)
    members = services.list_members(conn)
    return render_template("monthly.html", rows=rows, members=members,
                           days_in_month=services.days_in_month(month),
                           summary=services.month_summary(rows), **month_context(conn, month))


@bp.post("/monthly/create")
def monthly_create():
    conn = get_db()
    month = current_month()
    try:
        n = services.create_month(conn, month)
        flash(f"{services.month_label(month)}の入力行を{n}名分作成しました" if n else "作成済みです（新しいメンバーはいません）", "ok")
    except services.ClosedMonthError as e:
        flash(str(e), "error")
    return redirect(url_for("main.monthly", month=month))


@bp.post("/monthly/save")
def monthly_save():
    conn = get_db()
    month = current_month()
    dim = services.days_in_month(month)
    errors, updates = [], []
    for rec in conn.execute("SELECT r.*, m.name FROM monthly_records r JOIN members m ON m.id=r.member_id WHERE r.month=?", (month,)):
        mid = rec["member_id"]
        if f"wd_{mid}" not in request.form:
            continue
        e = []
        wd = parse_int(request.form.get(f"wd_{mid}"), f"{rec['name']}の出勤日数", e, 0, dim)
        acq = parse_int(request.form.get(f"acq_{mid}"), f"{rec['name']}の当月獲得件数", e)
        pm = parse_int(request.form.get(f"prev_{mid}"), f"{rec['name']}の前月獲得件数", e)
        errors += e
        updates.append((mid, wd, acq, pm))
    if errors:
        flash("保存していません：" + " / ".join(errors), "error")
        return redirect(url_for("main.monthly", month=month))
    try:
        changed = sum(services.save_record(conn, month, *u) for u in updates)
        conn.commit()
        flash(f"保存しました（{changed}名分の変更）", "ok")
    except (services.ClosedMonthError, services.ValidationError) as ex:
        conn.rollback()
        flash(str(ex), "error")
    return redirect(url_for("main.monthly", month=month))


@bp.get("/api/preview")
def api_preview():
    """入力中の値で単価・営業報酬をプレビューする（保存はしない）。"""
    conn = get_db()
    month = current_month()
    mid = request.args.get("member_id", type=int)
    rec = services.get_record(conn, month, mid)
    if rec is None:
        abort(404)
    errs = []
    wd = parse_int(request.args.get("work_days"), "出勤日数", errs, 0, services.days_in_month(month))
    acq = parse_int(request.args.get("acquisitions"), "当月獲得件数", errs)
    pm = parse_int(request.args.get("prev_manual"), "前月獲得件数", errs)
    fake = dict(rec)
    fake.update(work_days=wd, acquisitions=acq, prev_count_manual=pm)
    row = services.compute_live(conn, month, fake, services.get_member(conn, mid))
    return jsonify({
        "prev_count": row["prev_count"], "rate": row["rate"], "sales_reward": row["sales_reward"],
        "payment_total": row["payment_total"],
        "issues": [i["message"] for i in row["issues"]] + errs,
    })


# ---------- 支払項目 ----------

@bp.post("/items/add")
def items_add():
    conn = get_db()
    month = current_month()
    errs = []
    amount = parse_int(request.form.get("amount"), "金額", errs, 1)
    member_id = request.form.get("member_id", type=int)
    back = request.form.get("next") or url_for("main.monthly", month=month)
    if errs:
        flash(" / ".join(errs), "error")
        return redirect(back)
    try:
        services.add_item(conn, month, member_id, request.form.get("item_type"),
                          request.form.get("target_month"), amount, request.form.get("description"))
        flash("支払項目を追加しました", "ok")
    except (services.ClosedMonthError, services.ValidationError) as ex:
        flash(str(ex), "error")
    return redirect(back)


@bp.post("/items/<int:item_id>/delete")
def items_delete(item_id):
    conn = get_db()
    try:
        it = services.delete_item(conn, item_id)
        if it:
            flash("支払項目を削除しました", "ok")
    except services.ClosedMonthError as ex:
        flash(str(ex), "error")
    return redirect(request.form.get("next") or url_for("main.items"))


@bp.route("/items")
def items():
    conn = get_db()
    month = request.args.get("month", "")
    month = month if services.valid_month(month) else ""
    member_id = request.args.get("member_id", type=int)
    item_type = request.args.get("item_type") or None
    rows = services.list_items(conn, month=month or None, member_id=member_id, item_type=item_type)
    return render_template("items.html", items=rows, members=services.list_members(conn),
                           f_month=month, f_member=member_id, f_type=item_type,
                           total=sum(r["amount"] for r in rows),
                           month=month or date.today().strftime("%Y-%m"),
                           known_months=services.known_months(conn))


# ---------- 支払一覧 ----------

@bp.route("/payments")
def payments():
    conn = get_db()
    month = current_month()
    rows = services.month_rows(conn, month)
    return render_template("payments.html", rows=rows, summary=services.month_summary(rows),
                           **month_context(conn, month))


@bp.post("/payments/check")
def payments_check():
    conn = get_db()
    month = current_month()
    try:
        services.set_checked(conn, month, request.form.get("member_id", type=int), request.form.get("checked") == "1")
    except services.ClosedMonthError as ex:
        flash(str(ex), "error")
    return redirect(url_for("main.payments", month=month))


@bp.post("/months/close")
def months_close():
    conn = get_db()
    month = current_month()
    try:
        services.close_month(conn, month)
        flash(f"{services.month_label(month)}を締めました。以後この月の金額は固定されます", "ok")
    except (services.ClosedMonthError, services.ValidationError) as ex:
        flash(str(ex), "error")
    return redirect(url_for("main.payments", month=month))


@bp.post("/months/reopen")
def months_reopen():
    conn = get_db()
    month = current_month()
    try:
        services.reopen_month(conn, month, request.form.get("reason"))
        flash(f"{services.month_label(month)}の締めを解除しました", "ok")
    except services.ValidationError as ex:
        flash(str(ex), "error")
    return redirect(url_for("main.payments", month=month))


# ---------- 個人別明細 ----------

@bp.route("/payslip")
def payslip():
    conn = get_db()
    month = current_month()
    rows = services.month_rows(conn, month)
    member_id = request.args.get("member_id", type=int)
    selected = [r for r in rows if r["member_id"] == member_id] if member_id else []
    return render_template("payslip.html", rows=rows, slips=selected, member_id=member_id,
                           print_all=False, **month_context(conn, month))


@bp.route("/payslip/all")
def payslip_all():
    conn = get_db()
    month = current_month()
    rows = services.month_rows(conn, month)
    return render_template("payslip.html", rows=rows, slips=rows, member_id=None,
                           print_all=True, **month_context(conn, month))


# ---------- 個人別履歴 ----------

@bp.route("/history")
def history():
    conn = get_db()
    member_id = request.args.get("member_id", type=int)
    member = services.get_member(conn, member_id) if member_id else None
    rows = services.member_history(conn, member_id) if member else []
    return render_template("history.html", members=services.list_members(conn), member=member, rows=rows,
                           month=current_month())


# ---------- メンバー管理 ----------

@bp.route("/members", methods=["GET", "POST"])
def members():
    conn = get_db()
    if request.method == "POST":
        errs = []
        cap = parse_int(request.form.get("expense_cap"), "立替・経費の目安", errs)
        order = parse_int(request.form.get("sort_order"), "表示順", errs) or 0
        if errs:
            flash(" / ".join(errs), "error")
        else:
            try:
                services.save_member(
                    conn, request.form.get("id", type=int), request.form.get("name"),
                    request.form.get("category"), request.form.get("uses_private_car") == "1",
                    cap, request.form.get("note"), request.form.get("active") == "1", order)
                flash("メンバーを保存しました", "ok")
            except services.ValidationError as ex:
                flash(str(ex), "error")
        return redirect(url_for("main.members"))
    edit = services.get_member(conn, request.args.get("edit", type=int)) if request.args.get("edit") else None
    return render_template("members.html", members=services.list_members(conn), edit=edit, month=current_month())


# ---------- 単価表 ----------

@bp.route("/rates")
def rate_table():
    return render_template("rates.html", rates=rates, month=current_month())


# ---------- 変更履歴 ----------

@bp.route("/audit")
def audit():
    return render_template("audit.html", entries=services.audit_entries(get_db()), month=current_month())


# ---------- 出力 ----------

PAYMENT_COLUMNS = ["氏名", "区分", "出勤日数", "前月獲得件数", "当月獲得件数", "単価", "営業報酬",
                   "交通費", "立替金", "立替・経費", "その他報酬", "契約報酬", "ガソリン代", "支払総額", "確認", "エラー・要確認"]


def payment_table(rows):
    out = []
    for r in rows:
        t = r["totals"]
        out.append([
            r["name"], r["category_label"], r["work_days"], r["prev_count"], r["acquisitions"], r["rate"],
            r["sales_reward"], t["transport"], t["advance"], t["expense"], t["other_reward"],
            t["contract_reward"], t["gasoline"], r["payment_total"], "済" if r["checked"] else "",
            " / ".join(i["message"] for i in r["issues"]),
        ])
    return out


@bp.route("/export/payments.csv")
def export_payments_csv():
    month = current_month()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(PAYMENT_COLUMNS)
    w.writerows(payment_table(services.month_rows(get_db(), month)))
    # Excelで文字化けしないよう BOM 付き UTF-8
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=payments_{month}.csv"})


@bp.route("/export/payments.xlsx")
def export_payments_xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import Font

    month = current_month()
    conn = get_db()
    rows = services.month_rows(conn, month)
    wb = Workbook()
    ws = wb.active
    ws.title = "支払一覧"
    ws.append([f"{services.month_label(month)} 支払一覧"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(PAYMENT_COLUMNS)
    for c in ws[2]:
        c.font = Font(bold=True)
    for r in payment_table(rows):
        ws.append(r)
    for row in ws.iter_rows(min_row=3, min_col=6, max_col=14):
        for c in row:
            c.number_format = "#,##0"
    ws2 = wb.create_sheet("内訳")
    ws2.append(["対象者", "項目", "対象月", "金額", "内容"])
    for it in services.list_items(conn, month=month):
        ws2.append([it["member_name"], calc.ITEM_TYPES[it["item_type"]]["label"],
                    services.month_label(it["target_month"]), it["amount"], it["description"]])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio, as_attachment=True, download_name=f"payments_{month}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/export/items.csv")
def export_items_csv():
    month = request.args.get("month", "")
    month = month if services.valid_month(month) else None
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["支払月", "対象者", "項目", "対象月", "金額", "内容"])
    for it in services.list_items(get_db(), month=month, member_id=request.args.get("member_id", type=int),
                                  item_type=request.args.get("item_type") or None):
        w.writerow([it["month"], it["member_name"], calc.ITEM_TYPES[it["item_type"]]["label"],
                    it["target_month"] or "", it["amount"], it["description"]])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=items_{month or 'all'}.csv"})
