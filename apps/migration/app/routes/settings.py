"""
app/routes/settings.py
======================
Blueprint: /settings  (page) + /api/settings  (JSON CRUD)

Sections
--------
GET  /settings                      - Settings page (HTML)
GET  /api/settings                  - Return all settings as { key: value }
POST /api/settings                  - Bulk-save { key: value } dict
GET  /api/settings/fields           - Proxy to items field map (same as /api/items/fields)
POST /api/settings/fields           - Proxy to items field map save
GET  /api/settings/fields/discover  - Proxy to Jira field discovery
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, redirect, request, url_for

from app.db import Database, get_db
from app.connectors import current_jira, get_user_jira_credentials, save_user_jira_credentials
from app.services.field_mapping import suggest_field_target

logger = logging.getLogger(__name__)
bp = Blueprint("settings", __name__)


def _get_db() -> Database:
    return Database(get_db())


# ---------------------------------------------------------------------------
# Page route — settings now live as tabs on the Migration page
# ---------------------------------------------------------------------------

@bp.get("/settings")
def settings_page():
    return redirect(url_for("migration.migration_page"))


# ---------------------------------------------------------------------------
# GET /api/settings
# ---------------------------------------------------------------------------

@bp.get("/api/settings")
def get_settings():
    """Return all persisted app_settings rows as {key: value}.
    Per-user credential rows (jira.user.*, plane.user.*) are never exposed here."""
    try:
        db = _get_db()
        rows = db.get_all_settings()
        rows = {k: v for k, v in rows.items()
                if not k.startswith(("jira.user.", "plane.user.", "confluence.user."))}
        return jsonify(rows)
    except Exception as exc:
        logger.exception("get_settings failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/settings
# ---------------------------------------------------------------------------

@bp.post("/api/settings")
def save_settings():
    """Bulk-save settings. Body: { key: value, ... }"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Expected a JSON object"}), 400
    body = {k: v for k, v in body.items()
            if not k.startswith(("jira.user.", "plane.user.", "confluence.user."))}
    try:
        db = _get_db()
        count = db.bulk_upsert_settings(body)
        return jsonify({"saved": count})
    except Exception as exc:
        logger.exception("save_settings failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Per-user Jira credentials (the token is write-only — never echoed back)
# ---------------------------------------------------------------------------

@bp.get("/api/settings/jira-credentials")
def get_jira_credentials():
    creds = get_user_jira_credentials(_get_db())
    return jsonify({
        "email": creds["email"],
        "has_token": bool(creds["token"]),
        "base_url": creds["base_url"],
        "project_key": creds["project_key"],
    })


@bp.post("/api/settings/jira-credentials")
def save_jira_credentials():
    """Save the full per-user Jira connection (base URL, project key, email,
    token) — every user keeps their own configuration."""
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    token = (body.get("token") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    project_key = (body.get("project_key") or "").strip()
    if not base_url:
        return jsonify({"error": "Jira base URL is required"}), 400
    if not email:
        return jsonify({"error": "Jira email is required"}), 400
    db = _get_db()
    # Empty token keeps the previously stored one (lets users update other fields only)
    if not token:
        token = get_user_jira_credentials(db)["token"]
    if not token:
        return jsonify({"error": "Jira API token is required"}), 400
    save_user_jira_credentials(db, email, token, base_url, project_key)
    return jsonify({"saved": True})


# ---------------------------------------------------------------------------
# Field map proxies (data actually lives in jira_field_map table)
# ---------------------------------------------------------------------------

@bp.get("/api/settings/fields")
def list_field_map():
    try:
        db = _get_db()
        rows = db.get_jira_field_map_full()
        result = [
            {
                "field_label":   r.get("field_label", ""),
                "jira_field_id": r.get("jira_field_id", "") or "",
                "notes":         r.get("notes", "") or "",
                "plane_target":  r.get("plane_target") or "description",
            }
            for r in rows
        ]
        return jsonify(result)
    except Exception as exc:
        logger.exception("list_field_map failed")
        return jsonify({"error": str(exc)}), 500


@bp.post("/api/settings/fields")
def save_field_map():
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "Expected a JSON array"}), 400

    rows = []
    for entry in body:
        field_label   = (entry.get("field_label") or "").strip()  # preserve case
        jira_field_id = (entry.get("jira_field_id") or "").strip()
        notes         = (entry.get("notes") or "").strip()
        plane_target  = (entry.get("plane_target") or "description").strip()
        if not field_label:
            continue
        rows.append({"field_label": field_label, "jira_field_id": jira_field_id,
                     "notes": notes, "plane_target": plane_target})

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
# Workflow: issue type mapping + effort overrides
# ---------------------------------------------------------------------------

@bp.get("/api/settings/workflows")
def get_workflows():
    """Return loaded workflow templates with their phases and current effort overrides."""
    from flask import current_app
    import yaml as _yaml
    workflows_raw = current_app.config.get("WORKFLOWS", {})
    db = _get_db()
    overrides = db.get_workflow_effort_overrides()  # {template_name: {phase_name: factor}}
    # Also load from YAML files directly to get phase metadata
    import os
    templates_dir = current_app.config.get("WORKFLOW_TEMPLATES_DIR", "")
    result = []
    for fname in sorted(os.listdir(templates_dir)) if os.path.isdir(templates_dir) else []:
        if not fname.endswith(".yaml"):
            continue
        try:
            with open(os.path.join(templates_dir, fname), encoding="utf-8") as fh:
                data = _yaml.safe_load(fh)
            tname = data.get("template_name", fname.replace(".yaml", ""))
            phases = []
            for p in sorted(data.get("phases", []), key=lambda x: x.get("order", 0)):
                pname = p["name"]
                default_factor = float(p.get("effort_factor", 0.1))
                default_blocking = bool(p.get("is_blocking", True))
                phase_override = overrides.get(tname, {}).get(pname) or {}
                # phase_override is now a dict {effort_factor, jira_issue_type, is_blocking}
                if isinstance(phase_override, dict):
                    eff = phase_override.get("effort_factor")
                    jit = phase_override.get("jira_issue_type") or ""
                    ib  = phase_override.get("is_blocking")
                else:
                    # legacy scalar
                    eff = phase_override if phase_override else None
                    jit = ""
                    ib  = None
                phases.append({
                    "order": p.get("order"),
                    "name": pname,
                    "effort_factor": eff if eff is not None else default_factor,
                    "default_effort_factor": default_factor,
                    "jira_issue_type": jit,
                    "is_blocking": ib if ib is not None else default_blocking,
                    "default_is_blocking": default_blocking,
                    "effort_source": phase_override.get("effort_source") if isinstance(phase_override, dict) else None,
                    "blocks_phases": phase_override.get("blocks_phases") if isinstance(phase_override, dict) else None,
                })
            result.append({
                "template_name": tname,
                "applies_to": data.get("applies_to", []),
                "phases": phases,
            })
        except Exception as exc:
            logger.warning("Failed to load workflow %s: %s", fname, exc)
    return jsonify(result)


@bp.post("/api/settings/workflows/effort")
def save_workflow_effort():
    """Save effort factor overrides. Body: [{template_name, phase_name, effort_factor, jira_issue_type?, is_blocking?}, ...]"""
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "Expected a JSON array"}), 400
    try:
        db = _get_db()
        count = db.bulk_upsert_workflow_effort_overrides(body)
        return jsonify({"saved": count})
    except Exception as exc:
        logger.exception("save_workflow_effort failed")
        return jsonify({"error": str(exc)}), 500


@bp.get("/api/settings/fields/discover")
def discover_fields():
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
        schema = f.get("schema") or {}
        result.append({
            "id":               f.get("id"),
            "name":             f.get("name"),
            "custom":           is_custom,
            "schema_type":      schema.get("type"),
            "suggested_target": suggest_field_target(f.get("name"), schema),
        })
    result.sort(key=lambda x: (x["name"] or "").lower())
    return jsonify(result)
