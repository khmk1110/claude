import os
import sqlite3
from datetime import date, datetime, timedelta

from flask import Flask, g, redirect, render_template, request, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tasks.db")

STATUSES = ["todo", "in_progress", "done"]
STATUS_LABELS = {"todo": "未着手", "in_progress": "進行中", "done": "完了"}
PRIORITIES = ["high", "medium", "low"]
PRIORITY_LABELS = {"high": "高", "medium": "中", "low": "低"}
DUE_SOON_DAYS = 2

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            priority TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'todo',
            due_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.commit()
    db.close()


def task_urgency(task_row):
    """Classify a task's due date as overdue / due_soon / normal / none."""
    if task_row["status"] == "done" or not task_row["due_date"]:
        return "none"
    try:
        due = date.fromisoformat(task_row["due_date"])
    except ValueError:
        return "none"
    today = date.today()
    if due < today:
        return "overdue"
    if due <= today + timedelta(days=DUE_SOON_DAYS):
        return "due_soon"
    return "normal"


app.jinja_env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    PRIORITY_LABELS=PRIORITY_LABELS,
    STATUSES=STATUSES,
    PRIORITIES=PRIORITIES,
)


@app.route("/")
def index():
    db = get_db()
    status_filter = request.args.get("status", "")
    category_filter = request.args.get("category", "")
    sort = request.args.get("sort", "due_date")

    query = "SELECT * FROM tasks WHERE 1=1"
    params = []
    if status_filter in STATUSES:
        query += " AND status = ?"
        params.append(status_filter)
    if category_filter:
        query += " AND category = ?"
        params.append(category_filter)

    order_map = {
        "due_date": "CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date ASC",
        "priority": "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END",
        "created": "created_at DESC",
    }
    query += " ORDER BY " + order_map.get(sort, order_map["due_date"])

    rows = db.execute(query, params).fetchall()
    tasks = []
    overdue_count = 0
    due_soon_count = 0
    for row in rows:
        urgency = task_urgency(row)
        if urgency == "overdue":
            overdue_count += 1
        elif urgency == "due_soon":
            due_soon_count += 1
        tasks.append({**dict(row), "urgency": urgency})

    categories = [
        r["category"]
        for r in db.execute(
            "SELECT DISTINCT category FROM tasks WHERE category != '' ORDER BY category"
        ).fetchall()
    ]

    return render_template(
        "index.html",
        tasks=tasks,
        categories=categories,
        status_filter=status_filter,
        category_filter=category_filter,
        sort=sort,
        overdue_count=overdue_count,
        due_soon_count=due_soon_count,
        today=date.today().isoformat(),
    )


@app.route("/tasks", methods=["POST"])
def create_task():
    db = get_db()
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("index"))
    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        """
        INSERT INTO tasks (title, description, category, priority, status, due_date, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'todo', ?, ?, ?)
        """,
        (
            title,
            request.form.get("description", "").strip(),
            request.form.get("category", "").strip(),
            request.form.get("priority", "medium"),
            request.form.get("due_date") or None,
            now,
            now,
        ),
    )
    db.commit()
    return redirect(url_for("index"))


@app.route("/tasks/<int:task_id>/edit", methods=["GET"])
def edit_task_form(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        return redirect(url_for("index"))
    return render_template("edit.html", task=task)


@app.route("/tasks/<int:task_id>/update", methods=["POST"])
def update_task(task_id):
    db = get_db()
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("edit_task_form", task_id=task_id))
    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        """
        UPDATE tasks
        SET title = ?, description = ?, category = ?, priority = ?, status = ?, due_date = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            title,
            request.form.get("description", "").strip(),
            request.form.get("category", "").strip(),
            request.form.get("priority", "medium"),
            request.form.get("status", "todo"),
            request.form.get("due_date") or None,
            now,
            task_id,
        ),
    )
    db.commit()
    return redirect(url_for("index"))


def redirect_to_list():
    return redirect(
        url_for(
            "index",
            status=request.form.get("status_filter", ""),
            category=request.form.get("category_filter", ""),
            sort=request.form.get("sort", ""),
        )
    )


@app.route("/tasks/<int:task_id>/status", methods=["POST"])
def update_status(task_id):
    db = get_db()
    status = request.form.get("status", "todo")
    if status in STATUSES:
        now = datetime.now().isoformat(timespec="seconds")
        db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, task_id),
        )
        db.commit()
    return redirect_to_list()


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    return redirect_to_list()


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
