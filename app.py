#!/usr/bin/env python3
"""
mm-dashboard: live income dashboard for the money machine.

Tracks revenue per source (app, website, etc), stores it in SQLite,
and serves a dashboard showing totals, per-source breakdown, and a
30-day trend. Designed to be shown on a wall display / kiosk browser.

Data comes in via:
  - the manual entry form on the dashboard itself
  - POST /api/income (for future automation -- Stripe webhooks,
    App Store Connect sales report imports, RevenueCat webhooks, etc)

Nothing is wired to a real payment provider yet. This is the
plumbing; sources get connected as they go live.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

DB_PATH = os.environ.get("DB_PATH", "/data/income.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS income_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            entry_date TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            name TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def ensure_source(name, source_type):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO sources (name, source_type, created_at) VALUES (?, ?, ?)",
        (name, source_type, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


@app.route("/")
def dashboard():
    return render_template("index.html")


@app.route("/api/summary")
def api_summary():
    conn = get_db()

    total_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM income_entries"
    ).fetchone()
    total = total_row["total"]

    by_source = conn.execute("""
        SELECT source_name, source_type, COALESCE(SUM(amount), 0) as total,
               COUNT(*) as entries
        FROM income_entries
        GROUP BY source_name, source_type
        ORDER BY total DESC
    """).fetchall()

    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    daily = conn.execute("""
        SELECT entry_date, COALESCE(SUM(amount), 0) as total
        FROM income_entries
        WHERE entry_date >= ?
        GROUP BY entry_date
        ORDER BY entry_date
    """, (thirty_days_ago,)).fetchall()

    sources = conn.execute(
        "SELECT name, source_type FROM sources ORDER BY name"
    ).fetchall()

    conn.close()

    return jsonify({
        "total": round(total, 2),
        "by_source": [dict(r) for r in by_source],
        "daily_last_30": [dict(r) for r in daily],
        "sources": [dict(r) for r in sources],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/income", methods=["POST"])
def api_add_income():
    data = request.get_json(force=True)

    required = ["source_name", "source_type", "amount"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    try:
        amount = float(data["amount"])
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400

    entry_date = data.get("entry_date") or datetime.now(timezone.utc).date().isoformat()
    currency = data.get("currency", "USD")
    note = data.get("note", "")

    ensure_source(data["source_name"], data["source_type"])

    conn = get_db()
    conn.execute("""
        INSERT INTO income_entries
            (source_name, source_type, amount, currency, entry_date, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data["source_name"], data["source_type"], amount, currency,
        entry_date, note, datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"}), 201


@app.route("/api/sources", methods=["POST"])
def api_add_source():
    data = request.get_json(force=True)
    if "name" not in data or "source_type" not in data:
        return jsonify({"error": "name and source_type required"}), 400
    ensure_source(data["name"], data["source_type"])
    return jsonify({"status": "ok"}), 201


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8420)
