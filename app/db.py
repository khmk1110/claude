import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "payments.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payee TEXT,
    amount INTEGER,
    issue_date TEXT,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'unpaid',
    paid_date TEXT,
    memo TEXT,
    pdf_filename TEXT,
    original_filename TEXT,
    raw_text TEXT,
    uploaded_at TEXT NOT NULL
);
"""


def get_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def insert_invoice(conn, data):
    cur = conn.execute(
        """
        INSERT INTO invoices
            (payee, amount, issue_date, due_date, status, paid_date, memo,
             pdf_filename, original_filename, raw_text, uploaded_at)
        VALUES (:payee, :amount, :issue_date, :due_date, :status, :paid_date, :memo,
                :pdf_filename, :original_filename, :raw_text, :uploaded_at)
        """,
        data,
    )
    conn.commit()
    return cur.lastrowid


def update_invoice(conn, invoice_id, data):
    conn.execute(
        """
        UPDATE invoices
        SET payee = :payee,
            amount = :amount,
            issue_date = :issue_date,
            due_date = :due_date,
            memo = :memo
        WHERE id = :id
        """,
        {**data, "id": invoice_id},
    )
    conn.commit()


def mark_paid(conn, invoice_id, paid_date):
    conn.execute(
        "UPDATE invoices SET status = 'paid', paid_date = ? WHERE id = ?",
        (paid_date, invoice_id),
    )
    conn.commit()


def mark_unpaid(conn, invoice_id):
    conn.execute(
        "UPDATE invoices SET status = 'unpaid', paid_date = NULL WHERE id = ?",
        (invoice_id,),
    )
    conn.commit()


def delete_invoice(conn, invoice_id):
    conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    conn.commit()


def get_invoice(conn, invoice_id):
    return conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()


def list_invoices(conn, status=None):
    if status:
        rows = conn.execute(
            "SELECT * FROM invoices WHERE status = ? ORDER BY due_date IS NULL, due_date ASC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM invoices ORDER BY due_date IS NULL, due_date ASC"
        ).fetchall()
    return rows
