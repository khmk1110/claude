import uuid
from datetime import date, datetime
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

import db
import parser

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = "local-only-secret-key"
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB


def due_status(row):
    if row["status"] == "paid":
        return "paid"
    if not row["due_date"]:
        return "unknown"
    try:
        d = date.fromisoformat(row["due_date"])
    except ValueError:
        return "unknown"
    diff = (d - date.today()).days
    if diff < 0:
        return "overdue"
    if diff <= 7:
        return "soon"
    return "ok"


app.jinja_env.filters["due_status"] = due_status


def format_money(value):
    if value is None:
        return "-"
    return f"{value:,}"


app.jinja_env.filters["money"] = format_money


@app.route("/")
def index():
    conn = db.get_connection()
    try:
        invoices = db.list_invoices(conn)
    finally:
        conn.close()

    unpaid = [r for r in invoices if r["status"] == "unpaid"]
    paid = [r for r in invoices if r["status"] == "paid"]
    unpaid_total = sum(r["amount"] or 0 for r in unpaid)
    overdue_count = sum(1 for r in unpaid if due_status(r) == "overdue")

    return render_template(
        "index.html",
        unpaid=unpaid,
        paid=paid,
        unpaid_total=unpaid_total,
        unpaid_count=len(unpaid),
        overdue_count=overdue_count,
    )


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("pdfs")
    if not files or all(f.filename == "" for f in files):
        flash("PDFファイルを選択してください。", "error")
        return redirect(url_for("index"))

    conn = db.get_connection()
    registered = 0
    try:
        for f in files:
            if not f.filename:
                continue
            if not f.filename.lower().endswith(".pdf"):
                flash(f"{f.filename} はPDFではないためスキップしました。", "error")
                continue

            original_name = secure_filename(f.filename)
            stored_name = f"{uuid.uuid4().hex}.pdf"
            stored_path = UPLOAD_DIR / stored_name
            f.save(stored_path)

            fields = parser.extract_fields(stored_path)

            db.insert_invoice(
                conn,
                {
                    "payee": fields["payee"],
                    "amount": fields["amount"],
                    "issue_date": fields["issue_date"],
                    "due_date": fields["due_date"],
                    "status": "unpaid",
                    "paid_date": None,
                    "memo": None,
                    "pdf_filename": stored_name,
                    "original_filename": original_name,
                    "raw_text": fields["raw_text"],
                    "uploaded_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
            registered += 1
    finally:
        conn.close()

    if registered:
        flash(
            f"{registered}件の請求書を取り込みました。抽出できなかった項目は編集画面で補完してください。",
            "success",
        )
    return redirect(url_for("index"))


@app.route("/invoice/<int:invoice_id>/edit", methods=["GET", "POST"])
def edit(invoice_id):
    conn = db.get_connection()
    try:
        invoice = db.get_invoice(conn, invoice_id)
        if invoice is None:
            flash("請求書が見つかりません。", "error")
            return redirect(url_for("index"))

        if request.method == "POST":
            amount_raw = request.form.get("amount", "").replace(",", "").strip()
            data = {
                "payee": request.form.get("payee", "").strip() or None,
                "amount": int(amount_raw) if amount_raw.isdigit() else None,
                "issue_date": request.form.get("issue_date") or None,
                "due_date": request.form.get("due_date") or None,
                "memo": request.form.get("memo", "").strip() or None,
            }
            db.update_invoice(conn, invoice_id, data)
            flash("更新しました。", "success")
            return redirect(url_for("index"))

        return render_template("edit.html", invoice=invoice)
    finally:
        conn.close()


@app.route("/invoice/<int:invoice_id>/pay", methods=["POST"])
def pay(invoice_id):
    paid_date = request.form.get("paid_date") or date.today().isoformat()
    conn = db.get_connection()
    try:
        db.mark_paid(conn, invoice_id, paid_date)
    finally:
        conn.close()
    flash("支払済みにしました。", "success")
    return redirect(url_for("index"))


@app.route("/invoice/<int:invoice_id>/unpay", methods=["POST"])
def unpay(invoice_id):
    conn = db.get_connection()
    try:
        db.mark_unpaid(conn, invoice_id)
    finally:
        conn.close()
    flash("未払いに戻しました。", "success")
    return redirect(url_for("index"))


@app.route("/invoice/<int:invoice_id>/delete", methods=["POST"])
def delete(invoice_id):
    conn = db.get_connection()
    try:
        invoice = db.get_invoice(conn, invoice_id)
        if invoice:
            pdf_path = UPLOAD_DIR / invoice["pdf_filename"]
            if pdf_path.exists():
                pdf_path.unlink()
            db.delete_invoice(conn, invoice_id)
    finally:
        conn.close()
    flash("削除しました。", "success")
    return redirect(url_for("index"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, port=5000)
