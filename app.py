from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, date
from functools import wraps
from typing import Any, Dict, Optional

from flask import Flask, render_template, request, redirect, url_for, flash, session

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "todo.db")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

# Simple demo login: set this in your terminal before running:
#   Windows PowerShell:  $env:TODO_APP_PASSWORD="4321"
#   Mac/Linux:           export TODO_APP_PASSWORD="4321"
APP_PASSWORD = os.environ.get("TODO_APP_PASSWORD", "1234")


# -------------------------
# Auth helper
# -------------------------
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


# -------------------------
# DB helpers
# -------------------------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def init_db() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                due_date TEXT,
                done INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                task_id INTEGER,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # Safety migration: if someone ran the older version without due_date
        cols = [r["name"] for r in db.execute("PRAGMA table_info(tasks);").fetchall()]
        if "due_date" not in cols:
            db.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT;")


def log_event(db: sqlite3.Connection, action: str, task_id: Optional[int], payload: Dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO events (action, task_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (action, task_id, json.dumps(payload), now_iso()),
    )


def fetch_task(db: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("Task not found")
    return row


# -------------------------
# Auth routes
# -------------------------
@app.get("/login")
def login():
    if session.get("authed"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/login")
def login_post():
    pw = (request.form.get("password") or "").strip()
    if pw == APP_PASSWORD:
        session["authed"] = True
        return redirect(url_for("index"))
    flash("Wrong password.", "error")
    return redirect(url_for("login"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -------------------------
# App routes
# -------------------------
@app.get("/")
@login_required
def index():
    show = request.args.get("show", "active")  # active | all | completed | deleted
    sort = request.args.get("sort", "recent")  # recent | due

    today = date.today().isoformat()

    with get_db() as db:
        where = ""
        if show == "all":
            where = "WHERE deleted=0"
        elif show == "completed":
            where = "WHERE deleted=0 AND done=1"
        elif show == "deleted":
            where = "WHERE deleted=1"
        else:
            where = "WHERE deleted=0 AND done=0"

        if sort == "due":
            # due date first; nulls last; then updated_at
            order = """
            ORDER BY
              CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,
              due_date ASC,
              updated_at DESC
            """
        else:
            order = "ORDER BY updated_at DESC"

        tasks_rows = db.execute(f"SELECT * FROM tasks {where} {order}").fetchall()
        last_event = db.execute("SELECT id, action, created_at FROM events ORDER BY id DESC LIMIT 1").fetchone()

    # Decorate tasks with badges
    tasks = []
    for r in tasks_rows:
        t = dict(r)
        badge = None
        if t["deleted"] == 0 and t["done"] == 0 and t.get("due_date"):
            if t["due_date"] < today:
                badge = ("Overdue", "danger")
            elif t["due_date"] == today:
                badge = ("Due today", "warning")
            else:
                badge = ("Scheduled", "secondary")
        t["badge"] = badge
        tasks.append(t)

    return render_template("index.html", tasks=tasks, show=show, sort=sort, last_event=last_event)


@app.post("/add")
@login_required
def add_task():
    title = (request.form.get("title") or "").strip()
    due_date = (request.form.get("due_date") or "").strip() or None

    if not title:
        flash("Task title can’t be empty.", "error")
        return redirect(url_for("index"))

    with get_db() as db:
        ts = now_iso()
        cur = db.execute(
            "INSERT INTO tasks (title, due_date, done, deleted, created_at, updated_at) VALUES (?, ?, 0, 0, ?, ?)",
            (title, due_date, ts, ts),
        )
        task_id = cur.lastrowid
        log_event(db, "create", task_id, {"title": title, "due_date": due_date})

    return redirect(url_for("index"))


@app.post("/toggle/<int:task_id>")
@login_required
def toggle_task(task_id: int):
    with get_db() as db:
        task = fetch_task(db, task_id)
        if task["deleted"] == 1:
            flash("Can’t toggle a deleted task.", "error")
            return redirect(url_for("index", show="deleted"))

        before_done = int(task["done"])
        after_done = 0 if before_done == 1 else 1
        ts = now_iso()
        db.execute("UPDATE tasks SET done=?, updated_at=? WHERE id=?", (after_done, ts, task_id))
        log_event(db, "toggle", task_id, {"before_done": before_done, "after_done": after_done})

    return redirect(url_for("index"))


@app.get("/edit/<int:task_id>")
@login_required
def edit_page(task_id: int):
    with get_db() as db:
        task = fetch_task(db, task_id)
    return render_template("edit.html", task=task)


@app.post("/edit/<int:task_id>")
@login_required
def edit_task(task_id: int):
    new_title = (request.form.get("title") or "").strip()
    new_due = (request.form.get("due_date") or "").strip() or None

    if not new_title:
        flash("Task title can’t be empty.", "error")
        return redirect(url_for("edit_page", task_id=task_id))

    with get_db() as db:
        task = fetch_task(db, task_id)
        old_title = task["title"]
        old_due = task["due_date"]

        ts = now_iso()
        db.execute(
            "UPDATE tasks SET title=?, due_date=?, updated_at=? WHERE id=?",
            (new_title, new_due, ts, task_id),
        )
        log_event(
            db,
            "edit",
            task_id,
            {"before_title": old_title, "after_title": new_title, "before_due": old_due, "after_due": new_due},
        )

    return redirect(url_for("index"))


@app.post("/delete/<int:task_id>")
@login_required
def delete_task(task_id: int):
    with get_db() as db:
        task = fetch_task(db, task_id)
        if task["deleted"] == 1:
            return redirect(url_for("index", show="deleted"))

        ts = now_iso()
        db.execute("UPDATE tasks SET deleted=1, updated_at=? WHERE id=?", (ts, task_id))
        log_event(
            db,
            "delete",
            task_id,
            {"title": task["title"], "was_done": int(task["done"]), "due_date": task["due_date"]},
        )

    return redirect(url_for("index"))


@app.post("/restore/<int:task_id>")
@login_required
def restore_task(task_id: int):
    with get_db() as db:
        task = fetch_task(db, task_id)
        if task["deleted"] == 0:
            return redirect(url_for("index"))

        ts = now_iso()
        db.execute("UPDATE tasks SET deleted=0, updated_at=? WHERE id=?", (ts, task_id))
        log_event(db, "restore", task_id, {"title": task["title"], "due_date": task["due_date"]})

    return redirect(url_for("index", show="deleted"))


@app.post("/undo")
@login_required
def undo_last():
    """
    Undo the most recent event.
    - create -> delete created task row
    - toggle -> revert done
    - edit -> revert title + due_date
    - delete -> un-delete
    - restore -> re-delete
    Then removes the event row.
    """
    with get_db() as db:
        ev = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
        if ev is None:
            flash("Nothing to undo yet.", "info")
            return redirect(url_for("index"))

        action = ev["action"]
        task_id = ev["task_id"]
        payload = json.loads(ev["payload_json"])

        try:
            if action == "create":
                db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            elif action == "toggle":
                before_done = int(payload["before_done"])
                ts = now_iso()
                db.execute("UPDATE tasks SET done=?, updated_at=? WHERE id=?", (before_done, ts, task_id))
            elif action == "edit":
                before_title = payload.get("before_title")
                before_due = payload.get("before_due")
                ts = now_iso()
                db.execute(
                    "UPDATE tasks SET title=?, due_date=?, updated_at=? WHERE id=?",
                    (before_title, before_due, ts, task_id),
                )
            elif action == "delete":
                ts = now_iso()
                db.execute("UPDATE tasks SET deleted=0, updated_at=? WHERE id=?", (ts, task_id))
            elif action == "restore":
                ts = now_iso()
                db.execute("UPDATE tasks SET deleted=1, updated_at=? WHERE id=?", (ts, task_id))
            else:
                flash("Undo not supported for that action yet.", "error")
                return redirect(url_for("index"))
        except Exception:
            flash("Undo failed (task may have been removed).", "error")
            return redirect(url_for("index"))

        db.execute("DELETE FROM events WHERE id=?", (ev["id"],))
        flash("Undid last action.", "success")

    return redirect(url_for("index"))


@app.get("/activity")
@login_required
def activity():
    with get_db() as db:
        events = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("activity.html", events=events)


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
