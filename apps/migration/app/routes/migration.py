"""
app/routes/migration.py
=======================
Blueprint: /migration (page) + /api/migration/* (JSON)

Jira → Plane migration: preview Jira issues with the configured connection,
map Jira statuses to Plane states, then push the selection into a Plane
project in background batches (see services/migration_worker.py).
"""

from __future__ import annotations

import json
import logging

from flask import Blueprint, current_app, jsonify, render_template, request

from app.auth import PLANE_API_URL, SESSION_COOKIE, current_user
from app.connectors import (
    JiraNotConfigured,
    PlaneNotConfigured,
    clear_user_plane_token,
    current_jira,
    current_plane,
    ensure_plane_access,
    get_user_jira_credentials,
    get_user_plane_token,
    get_workspace_member_emails,
    invite_workspace_members,
)
from app.db import Database, get_db
from app.services.field_mapping import suggest_state_group
from app.services import migration_worker
from app.utils.jql_presets import build_jql_presets

logger = logging.getLogger(__name__)
bp = Blueprint("migration", __name__)


def _get_db() -> Database:
    return Database(get_db())


@bp.get("/migration")
def migration_page():
    db = _get_db()
    return render_template(
        "migration.html",
        jql_presets=build_jql_presets(
            get_user_jira_credentials(db)["project_key"],
            db.get_setting("import.issue_type_filter", ""),
        ),
    )


# ---------------------------------------------------------------------------
# Config: Plane access (token auto-mint) + target projects
# ---------------------------------------------------------------------------

@bp.get("/api/migration/config")
def get_config():
    db = _get_db()
    try:
        access = ensure_plane_access(db, request.cookies.get(SESSION_COOKIE, ""))
        plane = current_plane()
        projects = plane.get_projects(access["workspace_slug"])
    except PlaneNotConfigured as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        logger.exception("migration config failed")
        return jsonify({"error": str(exc)}), 502
    user_project_key = get_user_jira_credentials(db)["project_key"]
    return jsonify({
        "workspace_slug": access["workspace_slug"],
        "has_plane_token": True,
        "default_jql": db.get_setting("import.default_jql", ""),
        "default_project": db.get_setting("migration.default_project", ""),
        "sprint_label_prefix": db.get_setting("import.sprint_label_prefix", ""),
        "jira_project_key": user_project_key,
        "jql_presets": build_jql_presets(
            user_project_key,
            db.get_setting("import.issue_type_filter", ""),
        ),
        "projects": [
            {"id": str(p["id"]), "name": p.get("name", ""), "identifier": p.get("identifier", "")}
            for p in projects
        ],
    })


@bp.post("/api/migration/ensure-project")
def ensure_project():
    """Find or create a Plane project mirroring the Jira project (same name
    and key). Returns the resolved project so the UI can select it."""
    body = request.get_json(silent=True) or {}
    db = _get_db()
    jira_key = (body.get("jira_project_key") or get_user_jira_credentials(db)["project_key"]).strip()
    if not jira_key:
        return jsonify({"error": "Set a Jira project key in the Jira Connection tab first."}), 400
    try:
        access = ensure_plane_access(db, request.cookies.get(SESSION_COOKIE, ""))
        slug = access["workspace_slug"]
        jproj = current_jira().fetch_project(jira_key)
        project_id, created = current_plane().ensure_project(slug, jproj["name"], jproj["key"])
    except (JiraNotConfigured, PlaneNotConfigured) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("ensure_project failed")
        return jsonify({"error": str(exc)}), 502
    db.set_setting("migration.default_project", project_id)
    return jsonify({
        "project_id": project_id,
        "name": jproj["name"],
        "identifier": jproj["key"].upper()[:12],
        "created": created,
    })


@bp.post("/api/migration/create-project")
def create_project():
    """Manually create a new Plane project from a name + key. Lets users
    target several distinct projects, not just the Jira-mirrored one."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    identifier = (body.get("identifier") or "").strip().upper()[:12]
    if not name or not identifier:
        return jsonify({"error": "Project name and key are required."}), 400
    db = _get_db()
    try:
        access = ensure_plane_access(db, request.cookies.get(SESSION_COOKIE, ""))
        project_id, created = current_plane().ensure_project(access["workspace_slug"], name, identifier)
    except PlaneNotConfigured as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("create_project failed")
        return jsonify({"error": str(exc)}), 502
    db.set_setting("migration.default_project", project_id)
    return jsonify({"project_id": project_id, "name": name, "identifier": identifier, "created": created})


@bp.post("/api/migration/token/reset")
def reset_token():
    db = _get_db()
    clear_user_plane_token(db)
    try:
        ensure_plane_access(db, request.cookies.get(SESSION_COOKIE, ""))
    except PlaneNotConfigured as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"reset": True})


# ---------------------------------------------------------------------------
# Preview (nothing persisted)
# ---------------------------------------------------------------------------

@bp.post("/api/migration/preview")
def preview():
    """Fetch a small SAMPLE of matching issues — just to sanity-check the JQL
    and surface the statuses for mapping. The actual job ingests ALL matches
    (no cap); this preview is bounded so the browser stays responsive."""
    body = request.get_json(silent=True) or {}
    jql = (body.get("jql") or "").strip()
    if not jql:
        return jsonify({"error": "JQL is required"}), 400
    sample = min(int(body.get("sample") or 200), 500)
    db = _get_db()
    try:
        issues = current_jira().fetch_issues_for_migration(
            jql, field_map=db.get_jira_field_map_full(), max_results=sample
        )
    except JiraNotConfigured as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("migration preview failed")
        return jsonify({"error": str(exc)}), 502

    statuses: dict[str, str] = {}
    assignees: dict[str, str] = {}  # email (lower) -> display name
    for issue in issues:
        if issue["status_name"]:
            statuses.setdefault(issue["status_name"], issue["status_category"])
        email = (issue.get("assignee_email") or "").strip()
        if email:
            assignees.setdefault(email.lower(), issue.get("assignee_name") or email)
    return jsonify({
        "sample": len(issues),
        "items": [{
            "key": i["key"], "summary": i["summary"], "issuetype": i["issuetype_name"],
            "status_name": i["status_name"], "parent_key": i["parent_key"],
            "is_subtask": i["is_subtask"], "assignee_email": i["assignee_email"],
            "labels": i["labels"], "duedate": i["duedate"],
        } for i in issues],
        "statuses": [
            {"name": name, "category": cat} for name, cat in statuses.items()
        ],
        "assignees": [{"email": e, "name": n} for e, n in sorted(assignees.items())],
    })


@bp.post("/api/migration/import-users")
def import_users():
    """Bring Jira assignees into Plane using Plane's own workspace invitation
    flow. Already-members are skipped; the rest get invited (role: Member).
    They become assignable once they accept the invite."""
    body = request.get_json(silent=True) or {}
    emails = sorted({(e or "").strip().lower() for e in (body.get("emails") or []) if (e or "").strip()})
    if not emails:
        return jsonify({"error": "No user emails provided"}), 400
    db = _get_db()
    cookie = request.cookies.get(SESSION_COOKIE, "")
    try:
        access = ensure_plane_access(db, cookie)
        slug = access["workspace_slug"]
        existing = get_workspace_member_emails(cookie, slug)
    except PlaneNotConfigured as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        logger.exception("import_users member lookup failed")
        return jsonify({"error": str(exc)}), 502

    already = [e for e in emails if e in existing]
    to_invite = [e for e in emails if e not in existing]
    if to_invite:
        try:
            invite_workspace_members(cookie, slug, to_invite)
        except Exception as exc:
            logger.exception("workspace invite failed")
            return jsonify({"error": f"Invite failed: {exc}",
                            "already_members": already}), 502
    return jsonify({"invited": to_invite, "already_members": already})


# ---------------------------------------------------------------------------
# State mapping
# ---------------------------------------------------------------------------

@bp.post("/api/migration/state-suggestions")
def state_suggestions():
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id") or ""
    statuses = body.get("statuses") or []
    if not project_id:
        return jsonify({"error": "project_id is required"}), 400
    db = _get_db()
    try:
        access = get_user_plane_token(db)
        states = current_plane().get_states(access["workspace_slug"], project_id)
    except PlaneNotConfigured as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        logger.exception("state suggestions failed")
        return jsonify({"error": str(exc)}), 502

    saved = json.loads(db.get_setting(f"migration.state_map.{project_id}", "") or "{}")
    by_name = {s["name"].lower(): s for s in states}
    suggestions = []
    for st in statuses:
        name, category = st.get("name", ""), st.get("category", "")
        entry = {"jira_status": name, "category": category}
        saved_entry = saved.get(name.lower())
        if saved_entry and any(str(s["id"]) == saved_entry.get("state_id") for s in states):
            entry.update(action="map", state_id=saved_entry["state_id"])
        elif name.lower() in by_name:
            entry.update(action="map", state_id=str(by_name[name.lower()]["id"]))
        else:
            entry.update(action="create", suggested_group=suggest_state_group(category))
        suggestions.append(entry)
    return jsonify({
        "states": [{"id": str(s["id"]), "name": s["name"], "group": s["group"]} for s in states],
        "suggestions": suggestions,
    })


@bp.post("/api/migration/state-map")
def save_state_map():
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id") or ""
    mapping = body.get("mapping") or {}
    if not project_id or not isinstance(mapping, dict):
        return jsonify({"error": "project_id and mapping are required"}), 400
    db = _get_db()
    db.set_setting(f"migration.state_map.{project_id}", json.dumps(mapping))
    return jsonify({"saved": True})


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@bp.post("/api/migration/jobs")
def create_job():
    """Create a migration job. The job is lightweight — it just records the
    JQL + target; the background worker streams ALL matching Jira issues into
    SQLite (ingest), topo-sorts, resolves states/labels, then pushes in
    batches. No upfront fetch here, so there is no result cap."""
    body = request.get_json(silent=True) or {}
    jql = (body.get("jql") or "").strip()
    project_id = body.get("project_id") or ""
    if not jql or not project_id:
        return jsonify({"error": "jql and project_id are required"}), 400
    batch_size = max(1, min(int(body.get("batch_size") or 20), 100))
    options_in = body.get("options") or {}

    db = _get_db()
    user = current_user()
    active = db.get_active_migration_job(user["id"])
    if active:
        return jsonify({"error": f"Job #{active['id']} is still {active['status']} — "
                                 "cancel or wait before starting a new one."}), 409

    # Verify both connections are usable before queueing (fail fast with a
    # clear message rather than failing inside the worker).
    try:
        access = ensure_plane_access(db, request.cookies.get(SESSION_COOKIE, ""))
        current_jira()  # raises JiraNotConfigured if creds are missing
    except (JiraNotConfigured, PlaneNotConfigured) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("job creation preflight failed")
        return jsonify({"error": str(exc)}), 502

    sprint_prefix = (options_in.get("sprint_label_prefix")
                     if options_in.get("sprint_label_prefix") is not None
                     else db.get_setting("import.sprint_label_prefix", "")).strip()
    db.set_setting("import.sprint_label_prefix", sprint_prefix)
    options = {
        "push_assignees": bool(options_in.get("push_assignees", True)),
        "push_labels": bool(options_in.get("push_labels", True)),
        "push_links": bool(options_in.get("push_links", True)),
        "push_cycles": bool(options_in.get("push_cycles", True)),
        "sprint_label_prefix": sprint_prefix,
    }
    job_id = db.create_migration_job(
        created_by=user["id"], jql=jql, workspace_slug=access["workspace_slug"],
        project_id=project_id, batch_size=batch_size, total_items=0,
        state_map="", options=json.dumps(options),
    )
    db.set_setting("migration.default_project", project_id)

    token = get_user_plane_token(db)["token"]
    migration_worker.start_worker(current_app.config["DB_PATH"], job_id, PLANE_API_URL, token)
    return jsonify({"job_id": job_id})


@bp.get("/api/migration/jobs")
def list_jobs():
    db = _get_db()
    return jsonify(db.list_migration_jobs(created_by=current_user()["id"]))


@bp.get("/api/migration/jobs/<int:job_id>")
def get_job(job_id: int):
    db = _get_db()
    job = db.get_migration_job(job_id)
    if not job or job["created_by"] != current_user()["id"]:
        return jsonify({"error": "Job not found"}), 404
    job["stalled"] = migration_worker.is_job_stalled(job)
    job.pop("options", None)  # contains member emails/ids — not needed by UI
    result = {"job": job, "counts": db.count_job_items_by_status(job_id)}
    if request.args.get("items"):
        result["items"] = [
            {k: v for k, v in row.items() if k != "payload"}
            for row in db.get_job_items(
                job_id,
                status=request.args.get("status") or None,
                offset=int(request.args.get("offset") or 0),
                limit=min(int(request.args.get("limit") or 200), 500),
            )
        ]
    return jsonify(result)


@bp.post("/api/migration/jobs/<int:job_id>/cancel")
def cancel_job(job_id: int):
    db = _get_db()
    job = db.get_migration_job(job_id)
    if not job or job["created_by"] != current_user()["id"]:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] not in ("pending", "running"):
        return jsonify({"error": f"Job is {job['status']} — nothing to cancel"}), 400
    db.update_migration_job(job_id, status="cancelling")
    return jsonify({"cancelling": True})


@bp.post("/api/migration/jobs/<int:job_id>/resume")
def resume_job(job_id: int):
    db = _get_db()
    job = db.get_migration_job(job_id)
    if not job or job["created_by"] != current_user()["id"]:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] == "running" and not migration_worker.is_job_stalled(job):
        return jsonify({"error": "Job is already running"}), 400
    if not db.get_job_items(job_id, status="pending", limit=1):
        return jsonify({"error": "Nothing left to process"}), 400
    db.update_migration_job(job_id, status="pending", error=None)
    token = get_user_plane_token(db).get("token")
    if not token:
        return jsonify({"error": "Plane API token missing — reload the page"}), 502
    started = migration_worker.start_worker(
        current_app.config["DB_PATH"], job_id, PLANE_API_URL, token
    )
    return jsonify({"resumed": started})
