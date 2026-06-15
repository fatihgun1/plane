"""routes/consultants.py — Consultants CRUD + availability windows."""
from __future__ import annotations
from datetime import date
from flask import Blueprint, jsonify, request
from app.db import get_db, Database
from app.models import Consultant, ConsultantAvailability

bp = Blueprint("consultants", __name__, url_prefix="/api/consultants")


def _db() -> Database:
    return Database(get_db())


@bp.get("")
def list_consultants():
    items = _db().get_all_consultants()
    return jsonify([_c(c) for c in items])


@bp.post("")
def create_consultant():
    data = request.get_json(force=True)
    if not data or not data.get("name"):
        return jsonify({"error": "name required"}), 400
    c = Consultant(
        id=None,
        name=data["name"],
        role=data.get("role"),
        stream=data.get("stream"),
        capacity_pct=float(data.get("capacity_pct", 100.0)),
        active=bool(data.get("active", True)),
        level=data.get("level", "Consultant") or "Consultant",
    )
    new_id = _db().create_consultant(c)
    c.id = new_id
    return jsonify(_c(c)), 201


@bp.get("/<int:cid>")
def get_consultant(cid: int):
    c = _db().get_consultant(cid)
    if not c:
        return jsonify({"error": "not found"}), 404
    return jsonify(_c(c))


@bp.put("/<int:cid>")
def update_consultant(cid: int):
    data = request.get_json(force=True) or {}
    db = _db()
    ok = db.update_consultant(cid, **{k: data[k] for k in data if k in {"name","role","stream","capacity_pct","active","level"}})
    if not ok:
        return jsonify({"error": "not found or no valid fields"}), 404
    return jsonify(_c(db.get_consultant(cid)))


@bp.delete("/<int:cid>")
def delete_consultant(cid: int):
    ok = _db().delete_consultant(cid)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": cid})


# --- Availability windows ---

@bp.get("/<int:cid>/availability")
def list_availability(cid: int):
    rows = _db().get_availability(cid)
    return jsonify([_a(r) for r in rows])


@bp.post("/<int:cid>/availability")
def add_availability(cid: int):
    data = request.get_json(force=True) or {}
    try:
        avail = ConsultantAvailability(
            id=None,
            consultant_id=cid,
            start_date=date.fromisoformat(data["start_date"]),
            end_date=date.fromisoformat(data["end_date"]),
            capacity_pct=float(data.get("capacity_pct", 100.0)),
            note=data.get("note"),
        )
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    new_id = _db().add_availability(avail)
    avail.id = new_id
    return jsonify(_a(avail)), 201


@bp.put("/<int:cid>/availability/<int:aid>")
def update_availability(cid: int, aid: int):
    data = request.get_json(force=True) or {}
    fields = {}
    for key in ("capacity_pct", "note"):
        if key in data:
            fields[key] = data[key]
    for key in ("start_date", "end_date"):
        if key in data:
            try:
                fields[key] = date.fromisoformat(data[key]).isoformat()
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
    if not fields:
        return jsonify({"error": "no valid fields"}), 400
    db = _db()
    ok = db.update_availability(aid, **fields)
    if not ok:
        return jsonify({"error": "not found"}), 404
    rows = db.get_availability(cid)
    updated = next((r for r in rows if r.id == aid), None)
    return jsonify(_a(updated) if updated else {"updated": aid})


@bp.delete("/<int:cid>/availability/<int:aid>")
def delete_availability(cid: int, aid: int):
    ok = _db().delete_availability(aid)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": aid})


# --- Serialisers ---

def _c(c: Consultant) -> dict:
    return {"id": c.id, "name": c.name, "role": c.role, "stream": c.stream,
            "capacity_pct": c.capacity_pct, "active": c.active,
            "level": getattr(c, "level", "Consultant") or "Consultant"}


def _a(a: ConsultantAvailability) -> dict:
    return {"id": a.id, "consultant_id": a.consultant_id,
            "start_date": a.start_date.isoformat(), "end_date": a.end_date.isoformat(),
            "capacity_pct": a.capacity_pct, "note": a.note}
