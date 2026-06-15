"""
app/routes/dashboard.py — Server-rendered dashboard pages + summary API.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template

from app.db import Database, get_db

bp = Blueprint("dashboard", __name__)


def _get_db() -> Database:
    return Database(get_db())


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@bp.get("/")
def index():
    workflows = list(current_app.config.get("WORKFLOWS", {}).keys())
    return render_template("dashboard.html", workflows=workflows)


@bp.get("/consultants")
def consultants_page():
    return render_template("consultants.html")


@bp.get("/holidays")
def holidays_page():
    return render_template("holidays.html")


@bp.get("/items")
def items_page():
    return render_template("items.html")


@bp.get("/schedules")
def schedules_page():
    return render_template("schedules.html")


# ---------------------------------------------------------------------------
# API: summary counts for the dashboard
# ---------------------------------------------------------------------------

@bp.get("/api/summary")
def api_summary():
    """
    Returns high-level counts used by the dashboard overview cards.

    Response shape:
        {
          "items":       <int>,
          "consultants": <int>,
          "schedules":   <int>,
          "holidays":    <int>
        }
    """
    try:
        db = _get_db()
        items_count       = db._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        # Use a safe count that works regardless of schema version
        try:
            consultants_count = db._conn.execute("SELECT COUNT(*) FROM consultants WHERE active = 1").fetchone()[0]
        except Exception:
            consultants_count = db._conn.execute("SELECT COUNT(*) FROM consultants").fetchone()[0]
        holidays_count    = db._conn.execute("SELECT COUNT(*) FROM holidays").fetchone()[0]
        schedules_count   = db._conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]

        return jsonify({
            "items":       items_count,
            "consultants": consultants_count,
            "schedules":   schedules_count,
            "holidays":    holidays_count,
        })
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("api_summary failed")
        return jsonify({"error": str(exc), "items": 0, "consultants": 0, "schedules": 0, "holidays": 0}), 500
