"""app/routes/gantt.py — Gantt page route."""
from flask import Blueprint, render_template

bp = Blueprint("gantt", __name__)


@bp.get("/gantt")
def gantt_page():
    return render_template("gantt.html")
