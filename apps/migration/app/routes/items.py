"""
app/routes/items.py
====================
Blueprint: /api/items

Endpoints
---------
GET  /api/items              - list items (optional ?stream=&module=&status=)
GET  /api/items/export       - download items as XLSX
POST /api/items/import/file  - upload CSV/XLSX and persist items
POST /api/items/import/jira  - fetch items from Jira and persist
GET  /api/items/<id>         - single item detail
DELETE /api/items/<id>       - remove item
"""

from __future__ import annotations

import io
import logging
import os
import tempfile

from flask import Blueprint, jsonify, request, send_file

from app.db import Database, get_db
from app.connectors.file_connector import FileConnector
from app.connectors import current_jira

logger = logging.getLogger(__name__)
bp = Blueprint("items", __name__, url_prefix="/api/items")


def _get_db() -> Database:
    return Database(get_db())


def _item_dict(item) -> dict:
    import json as _json
    extra = {}
    if getattr(item, "extra_fields", None):
        try:
            extra = _json.loads(item.extra_fields)
        except Exception:
            pass
    return {
        "id": item.id,
        "jira_key": item.jira_key,
        "summary": item.summary,
        "issue_type": item.issue_type,
        "module": item.module,
        "stream": item.stream,
        "priority": item.priority,
        "effort_days": item.effort_days,
        "status": item.status,
        "epic_key": item.epic_key,
        "imported_at": item.imported_at,
        "extra_fields": extra,
    }


# ---------------------------------------------------------------------------
# GET /api/items
# ---------------------------------------------------------------------------

@bp.get("/")
def list_items():
    db = _get_db()
    filters: dict = {}
    for key in ("stream", "module", "status", "issue_type"):
        val = request.args.get(key)
        if val:
            filters[key] = val

    items = db.get_items(**filters)
    return jsonify([_item_dict(i) for i in items])


# ---------------------------------------------------------------------------
# GET /api/items/<id>
# ---------------------------------------------------------------------------

@bp.get("/<int:item_id>")
def get_item(item_id: int):
    db = _get_db()
    item = db.get_item(item_id)
    if item is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_item_dict(item))


# ---------------------------------------------------------------------------
# PUT /api/items/<id>  — update editable fields
# ---------------------------------------------------------------------------

@bp.put("/<int:item_id>")
def update_item(item_id: int):
    db = _get_db()
    item = db.get_item(item_id)
    if item is None:
        return jsonify({"error": "Not found"}), 404
    body = request.get_json(silent=True) or {}
    mutable = ("summary", "effort_days", "priority", "status",
                "module", "stream", "issue_type", "epic_key")
    updates = {k: body[k] for k in mutable if k in body}
    if updates:
        db.update_item(item_id, **updates)
        item = db.get_item(item_id)  # re-fetch to return persisted state
    return jsonify(_item_dict(item))


# ---------------------------------------------------------------------------
# DELETE /api/items          — clear ALL items
# ---------------------------------------------------------------------------

@bp.delete("/")
def clear_all_items():
    db = _get_db()
    db.clear_items()
    return jsonify({"cleared": True})


# ---------------------------------------------------------------------------
# DELETE /api/items/<id>     — remove single item
# ---------------------------------------------------------------------------

@bp.delete("/<int:item_id>")
def delete_item(item_id: int):
    db = _get_db()
    item = db.get_item(item_id)
    if item is None:
        return jsonify({"error": "Not found"}), 404
    db.delete_item(item_id)
    return jsonify({"deleted": item_id})


# ---------------------------------------------------------------------------
# POST /api/items/import/file
# ---------------------------------------------------------------------------

@bp.post("/import/file")
def import_file():
    """
    Accepts multipart/form-data with field 'file' (CSV or XLSX/XLSM).
    Optional form fields:
      replace   (true/false)  - if true, clears existing items first
      sheet_name (str)        - Excel sheet to read (default: active sheet)
    """
    if "file" not in request.files:
        return jsonify({"error": "No file field in request"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    replace = request.form.get("replace", "false").lower() == "true"
    sheet_name = request.form.get("sheet_name") or None

    # Save to a temp file so FileConnector can detect extension
    suffix = os.path.splitext(f.filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    try:
        fc = FileConnector()
        items = fc.load(tmp_path, sheet_name=sheet_name)
    except Exception as exc:
        logger.exception("File import failed")
        return jsonify({"error": str(exc)}), 422
    finally:
        os.unlink(tmp_path)

    db = _get_db()
    if replace:
        db.clear_items()

    saved = db.upsert_items(items)
    return jsonify({"imported": len(saved), "replace": replace})


# ---------------------------------------------------------------------------
# GET /api/items/export
# ---------------------------------------------------------------------------

@bp.get("/export")
def export_items():
    """
    Returns all items as an XLSX file download.
    Optional query params: stream=, module=, status=, issue_type=
    """
    try:
        import openpyxl  # noqa: PLC0415
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return jsonify({"error": "openpyxl not installed"}), 500

    db = _get_db()
    filters: dict = {}
    for key in ("stream", "module", "status", "issue_type"):
        val = request.args.get(key)
        if val:
            filters[key] = val
    items = db.get_items(**filters)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Items"

    headers = [
        "ID", "Jira Key", "Summary", "Issue Type", "Module",
        "Stream", "Priority", "Effort Days", "Status", "Epic Key", "Imported At",
    ]
    ws.append(headers)

    # Style header row
    header_fill = PatternFill("solid", fgColor="003087")  # NTT dark blue
    header_font = Font(bold=True, color="FFFFFF")
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for item in items:
        ws.append([
            item.id,
            item.jira_key,
            item.summary,
            item.issue_type,
            item.module,
            item.stream,
            item.priority,
            item.effort_days,
            item.status,
            item.epic_key,
            item.imported_at,
        ])

    # Auto-fit column widths (approximate)
    for col in ws.columns:
        max_len = max((len(str(c.value)) if c.value else 0) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="items_export.xlsx",
    )


# ---------------------------------------------------------------------------
# POST /api/items/import/jira
# ---------------------------------------------------------------------------

@bp.post("/import/jira")
def import_jira():
    """
    Body (JSON) — two accepted shapes:

    Shape 1 (UI default):
        { "jql": "project = JUM AND issuetype = ...",
          "max_results": 200, "replace": false }

    Shape 2 (legacy / backward-compat):
        { "project_key": "JUM", "issue_types": ["WRICEF"],
          "max_results": 500, "replace": false }

    If both jql and project_key are provided, jql takes precedence.
    Field mappings are loaded automatically from the DB (jira_field_map table).
    """
    body = request.get_json(silent=True) or {}
    max_results = int(body.get("max_results", 500))
    replace = bool(body.get("replace", False))

    # ── Resolve JQL ────────────────────────────────────────────────
    jql = (body.get("jql") or "").strip()

    if not jql:
        # Fall back to building JQL from project_key
        project_key = body.get("project_key", "").strip()
        if not project_key:
            return jsonify({
                "error": "Provide either 'jql' or 'project_key' in the request body."
            }), 400
        issue_types = body.get("issue_types") or None
        jql_parts = [f"project = {project_key}"]
        if issue_types:
            types_str = ", ".join(f'"{t}"' for t in issue_types)
            jql_parts.append(f"issuetype in ({types_str})")
        jql = " AND ".join(jql_parts) + " ORDER BY created DESC"

    logger.info("Jira import — JQL: %s  max_results: %d", jql, max_results)

    # ── Load field map from DB ──────────────────────────────────────
    # get_jira_field_map_full() returns list[dict] with field_label/jira_field_id keys
    # which is the format expected by JiraConnector._build_reverse_map()
    db = _get_db()
    raw_fm = db.get_jira_field_map_full()   # returns [{field_label, jira_field_id, notes}, ...]
    field_map = raw_fm if raw_fm else None

    try:
        jira = current_jira()
        items = jira.fetch_items_by_jql(
            jql=jql,
            field_map=field_map,
            max_results=max_results,
        )
    except Exception as exc:
        logger.exception("Jira import failed")
        return jsonify({"error": str(exc)}), 502

    if replace:
        db.clear_items()

    saved = db.upsert_items(items)
    return jsonify({"imported": len(saved), "replace": replace})


# ---------------------------------------------------------------------------
# GET /api/items/fields
# ---------------------------------------------------------------------------

@bp.get("/fields")
def list_field_map():
    """Return all jira_field_map rows as JSON.

    Returns list of: { field_label, jira_field_id, notes }
    (field_key → field_label, mapping → jira_field_id, field_name → notes)
    """
    try:
        db = _get_db()
        rows = db.get_jira_field_map_full()  # list of dicts from DB
        result = [
            {
                "field_label": r.get("field_label", ""),
                "jira_field_id": r.get("jira_field_id", "") or "",
                "notes": r.get("notes", "") or "",
                "plane_target": r.get("plane_target") or "description",
            }
            for r in rows
        ]
        return jsonify(result)
    except Exception as exc:
        logger.exception("list_field_map failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/items/fields
# ---------------------------------------------------------------------------

@bp.post("/fields")
def save_field_map():
    """
    Bulk-upsert field map rows.

    Body (JSON) — frontend format:
        [
          { "field_label": "module", "jira_field_id": "customfield_10028", "notes": "..." },
          ...
        ]

    field_label  → stored as field_key  (the human lookup label, e.g. "module")
    jira_field_id → stored as mapping   (the actual Jira custom field ID)
    notes        → stored as field_name (display / annotation)
    """
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "Expected a JSON array of field map rows"}), 400

    rows = []
    for entry in body:
        field_label   = (entry.get("field_label") or "").strip()  # preserve case
        jira_field_id = (entry.get("jira_field_id") or "").strip()
        notes         = (entry.get("notes") or "").strip()
        if not field_label:
            continue
        rows.append({
            "field_label":   field_label,
            "jira_field_id": jira_field_id,
            "notes":         notes,
            "plane_target":  (entry.get("plane_target") or "description").strip(),
        })

    if not rows:
        return jsonify({"error": "No valid rows provided"}), 400

    try:
        db = _get_db()
        # Clear all existing rows then insert fresh — full replace semantics
        db._conn.execute("DELETE FROM jira_field_map")
        db._conn.commit()
        count = db.bulk_upsert_jira_field_map(rows)
        return jsonify({"saved": count})
    except Exception as exc:
        logger.exception("save_field_map failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/items/fields/discover
# ---------------------------------------------------------------------------

@bp.get("/fields/discover")
def discover_jira_fields():
    """
    Call Jira's /rest/api/3/field endpoint and return a filtered list of
    fields that could be useful for field mapping.

    Returns a list of:
        { "id": "customfield_10016", "name": "Story Points", "custom": true }

    The UI can present this list so the user can map labels -> jira_field_id.
    Query params:
        custom_only=true   (default true)  — filter to custom fields only
    """
    custom_only = request.args.get("custom_only", "true").lower() != "false"

    try:
        jira = current_jira()
        all_fields = jira.fetch_fields()
    except Exception as exc:
        logger.exception("Jira field discovery failed")
        return jsonify({"error": str(exc)}), 502

    result = []
    for f in all_fields:
        is_custom = f.get("custom", False)
        if custom_only and not is_custom:
            continue
        result.append({
            "id": f.get("id"),
            "name": f.get("name"),
            "custom": is_custom,
            "schema_type": (f.get("schema") or {}).get("type"),
        })

    result.sort(key=lambda x: (x["name"] or "").lower())
    return jsonify(result)
