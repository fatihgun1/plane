"""routes/holidays.py — Public holidays CRUD."""
from __future__ import annotations
from datetime import date
from flask import Blueprint, jsonify, request
from app.db import get_db, Database

bp = Blueprint("holidays", __name__, url_prefix="/api/holidays")


def _db() -> Database:
    return Database(get_db())


@bp.get("")
def list_holidays():
    return jsonify(_db().get_holidays_full())


@bp.post("")
def add_holiday():
    data = request.get_json(force=True) or {}
    try:
        d = date.fromisoformat(data["date"])
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    name      = data.get("name", "").strip()
    country   = data.get("country") or None
    recurring = 1 if data.get("recurring") else 0
    new_id = _db().add_holiday(d, name, country=country, recurring=recurring)
    return jsonify({
        "id":        new_id,
        "date":      d.isoformat(),
        "name":      name,
        "country":   country,
        "recurring": bool(recurring),
    }), 201


@bp.get("/<int:hid>")
def get_holiday(hid: int):
    row = _db().get_holiday(hid)
    if row is None:
        return jsonify({"error": "not found"}), 404
    row["recurring"] = bool(row.get("recurring"))
    return jsonify(row)


@bp.put("/<int:hid>")
def update_holiday(hid: int):
    data = request.get_json(force=True) or {}
    db   = _db()
    if db.get_holiday(hid) is None:
        return jsonify({"error": "not found"}), 404

    fields: dict = {}
    if "date" in data:
        try:
            fields["date"] = date.fromisoformat(data["date"]).isoformat()
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    if "name"      in data: fields["name"]      = data["name"].strip()
    if "country"   in data: fields["country"]   = data["country"] or None
    if "recurring" in data: fields["recurring"] = 1 if data["recurring"] else 0

    db.update_holiday(hid, **fields)
    updated = db.get_holiday(hid)
    updated["recurring"] = bool(updated.get("recurring"))
    return jsonify(updated)


@bp.delete("/<int:hid>")
def delete_holiday(hid: int):
    ok = _db().delete_holiday(hid)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": hid})
