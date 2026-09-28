import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL CHECK (category IN ('contractor','employee','intern')),
    uses_private_car INTEGER NOT NULL DEFAULT 0,
    expense_cap INTEGER,
    note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 月×対象者ごとの入力値。月ごとに別レコードなので他の月には影響しない。
-- category / uses_private_car は作成時点のメンバー情報を保存（後から区分が変わっても過去月は変わらない）。
CREATE TABLE IF NOT EXISTS monthly_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    member_id INTEGER NOT NULL REFERENCES members(id),
    category TEXT NOT NULL,
    uses_private_car INTEGER NOT NULL DEFAULT 0,
    work_days INTEGER CHECK (work_days IS NULL OR work_days BETWEEN 0 AND 31),
    acquisitions INTEGER CHECK (acquisitions IS NULL OR acquisitions >= 0),
    prev_count_manual INTEGER CHECK (prev_count_manual IS NULL OR prev_count_manual >= 0),
    checked INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (month, member_id)
);

-- 交通費・立替金・ガソリン代・その他報酬などの内訳。month は支払月、target_month は「何月分か」。
CREATE TABLE IF NOT EXISTS line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    member_id INTEGER NOT NULL REFERENCES members(id),
    item_type TEXT NOT NULL,
    target_month TEXT,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 締め済みの月。締め時点の計算結果を snapshots に保存し、以後はその値を表示する。
CREATE TABLE IF NOT EXISTS closed_months (
    month TEXT PRIMARY KEY,
    closed_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS snapshots (
    month TEXT NOT NULL,
    member_id INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (month, member_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    month TEXT,
    member_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()
