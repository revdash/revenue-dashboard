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
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response

DB_PATH = os.environ.get("DB_PATH", "/data/income.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# Optional password protection. If both are left unset, the dashboard
# is unprotected (fine for a LAN-only display, not fine if you port-
# forward this to the internet). Set both to require a login.
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

app = Flask(__name__)


def check_auth(username, password):
    # Timing-safe comparison so response time can't leak how many
    # characters of the password were guessed correctly.
    return (
        secrets.compare_digest(username, DASHBOARD_USER)
        and secrets.compare_digest(password, DASHBOARD_PASSWORD)
    )


@app.before_request
def require_auth_if_configured():
    if not DASHBOARD_USER or not DASHBOARD_PASSWORD:
        return  # auth not configured -- dashboard is open
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return Response(
            "Login required", 401,
            {"WWW-Authenticate": 'Basic realm="Revdash"'},
        )


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
            platform TEXT NOT NULL DEFAULT 'manual',
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
    # Migration: add platform column to a pre-existing DB that predates
    # this feature, and backfill it for rows already posted by the sync
    # integrations (identifiable by their consistent note text).
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(income_entries)")]
    if "platform" not in existing_cols:
        conn.execute("ALTER TABLE income_entries ADD COLUMN platform TEXT NOT NULL DEFAULT 'manual'")
    conn.execute("""
        UPDATE income_entries SET platform = 'appstore'
        WHERE note LIKE '%App Store Connect%' AND (platform IS NULL OR platform = 'manual')
    """)
    conn.execute("""
        UPDATE income_entries SET platform = 'stripe'
        WHERE note LIKE '%Stripe%' AND (platform IS NULL OR platform = 'manual')
    """)
    conn.execute("""
        UPDATE income_entries SET platform = 'admob'
        WHERE note LIKE '%AdMob%' AND (platform IS NULL OR platform = 'manual')
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

    by_platform = conn.execute("""
        SELECT platform, COALESCE(SUM(amount), 0) as total,
               COUNT(*) as entries
        FROM income_entries
        GROUP BY platform
        ORDER BY (platform = 'manual') ASC, total DESC
    """).fetchall()

    by_source_type = conn.execute("""
        SELECT source_type, COALESCE(SUM(amount), 0) as total,
               COUNT(*) as entries
        FROM income_entries
        GROUP BY source_type
        ORDER BY total DESC
    """).fetchall()

    window_start = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
    daily = conn.execute("""
        SELECT entry_date, COALESCE(SUM(amount), 0) as total
        FROM income_entries
        WHERE entry_date >= ?
        GROUP BY entry_date
        ORDER BY entry_date
    """, (window_start,)).fetchall()

    monthly = conn.execute("""
        SELECT strftime('%Y-%m', entry_date) as month, COALESCE(SUM(amount), 0) as total,
               COUNT(*) as entries
        FROM income_entries
        GROUP BY month
        ORDER BY month DESC
    """).fetchall()

    # Month-over-month delta: current calendar month vs the previous
    # calendar month, treating a month with zero entries as $0 rather
    # than skipping it -- so this reflects real elapsed time, not just
    # "the previous month that happened to have data".
    monthly_by_key = {r["month"]: r["total"] for r in monthly}
    today = datetime.now(timezone.utc).date()
    current_month_key = today.strftime("%Y-%m")
    prev_month_date = (today.replace(day=1) - timedelta(days=1))
    prev_month_key = prev_month_date.strftime("%Y-%m")

    current_month_total = monthly_by_key.get(current_month_key, 0.0)
    prev_month_total = monthly_by_key.get(prev_month_key, 0.0)

    mom_delta_pct = None
    if prev_month_total > 0:
        mom_delta_pct = round(
            ((current_month_total - prev_month_total) / prev_month_total) * 100, 1
        )

    yearly = conn.execute("""
        SELECT strftime('%Y', entry_date) as year, COALESCE(SUM(amount), 0) as total,
               COUNT(*) as entries
        FROM income_entries
        GROUP BY year
        ORDER BY year DESC
    """).fetchall()

    sources = conn.execute(
        "SELECT name, source_type FROM sources ORDER BY name"
    ).fetchall()

    manual_entries = conn.execute("""
        SELECT id, source_name, amount, entry_date
        FROM income_entries
        WHERE platform = 'manual'
        ORDER BY entry_date DESC, id DESC
    """).fetchall()

    conn.close()

    return jsonify({
        "total": round(total, 2),
        "by_source": [dict(r) for r in by_source],
        "by_platform": [dict(r) for r in by_platform],
        "by_source_type": [dict(r) for r in by_source_type],
        "daily_12mo": [dict(r) for r in daily],
        "monthly": [dict(r) for r in monthly],
        "yearly": [dict(r) for r in yearly],
        "mom_delta_pct": mom_delta_pct,
        "mom_current": round(current_month_total, 2),
        "mom_previous": round(prev_month_total, 2),
        "sources": [dict(r) for r in sources],
        "manual_entries": [dict(r) for r in manual_entries],
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
    platform = data.get("platform", "manual")

    ensure_source(data["source_name"], data["source_type"])

    conn = get_db()
    conn.execute("""
        INSERT INTO income_entries
            (source_name, source_type, amount, currency, entry_date, note, platform, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["source_name"], data["source_type"], amount, currency,
        entry_date, note, platform, datetime.now(timezone.utc).isoformat(),
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


@app.route("/api/platform/manual", methods=["DELETE"])
def api_delete_manual_entries():
    """Deletes every income entry tagged platform='manual'. Deliberately
    scoped to only this platform -- not a generic delete-by-platform
    route -- so this can never be used to wipe real synced revenue
    data from appstore/stripe/admob, even by accident."""
    conn = get_db()
    cur = conn.execute("DELETE FROM income_entries WHERE platform = 'manual'")
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return jsonify({"status": "ok", "deleted": deleted})


@app.route("/api/income/<int:entry_id>", methods=["DELETE"])
def api_delete_one_entry(entry_id):
    """Deletes a single entry by ID -- but only if it's a manual entry.
    Same safety scoping as the bulk-delete route: synced revenue data
    from appstore/stripe/admob can never be deleted through this UI,
    even if someone crafts the request by hand."""
    conn = get_db()
    row = conn.execute(
        "SELECT platform FROM income_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    if row["platform"] != "manual":
        conn.close()
        return jsonify({"error": "only manual entries can be deleted"}), 403
    conn.execute("DELETE FROM income_entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8420)
