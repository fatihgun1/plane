"""
app/routes/pages.py
===================
Blueprint for the Confluence → Plane Pages migration (the "Pages" tab).

Mirrors routes/migration.py: a config endpoint (Plane projects + Confluence
spaces), a Confluence connection save, a preview (sample page tree), and the
job lifecycle. The actual work runs in page_migration_worker; because Plane
Pages use the session-authenticated app API, the job stores the user's session
cookie for the worker (cleared when the job finishes).
"""

from __future__ import annotations

import json
import logging

from flask import Blueprint, current_app, jsonify, request

from app.auth import PLANE_API_URL, SESSION_COOKIE, current_user
from app.connectors import (
    JiraNotConfigured,
    PlaneNotConfigured,
    current_confluence,
    current_plane,
    ensure_plane_access,
    get_user_confluence_config,
    save_user_confluence_config,
)
from app.db import Database, get_db
from app.services import page_migration_worker

logger = logging.getLogger(__name__)
bp = Blueprint("pages", __name__)


def _get_db() -> Database:
    return Database(get_db())


@bp.get("/api/pages/config")
def pages_config():
    db = _get_db()
    try:
        access = ensure_plane_access(db, request.cookies.get(SESSION_COOKIE, ""))
        projects = current_plane().get_projects(access["workspace_slug"])
    except PlaneNotConfigured as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        logger.exception("pages config failed")
        return jsonify({"error": str(exc)}), 502
    cfg = get_user_confluence_config(db)
    return jsonify({
        "workspace_slug": access["workspace_slug"],
        "default_project": db.get_setting("migration.default_project", ""),
        "confluence_base_url": cfg["base_url"],
        "confluence_space": cfg["space"],
        "projects": [{"id": str(p["id"]), "name": p.get("name", ""),
                      "identifier": p.get("identifier", "")} for p in projects],
    })


@bp.post("/api/pages/confluence-config")
def save_confluence_config():
    body = request.get_json(silent=True) or {}
    base_url = (body.get("base_url") or "").strip()
    space = (body.get("space") or "").strip()
    save_user_confluence_config(_get_db(), base_url, space)
    return jsonify({"saved": True})


@bp.get("/api/pages/spaces")
def list_spaces():
    try:
        spaces = current_confluence().list_spaces()
    except JiraNotConfigured as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("list spaces failed")
        return jsonify({"error": str(exc)}), 502
    return jsonify([{"key": s["key"], "name": s["name"]} for s in spaces])


@bp.post("/api/pages/preview")
def preview():
    """Sample the space's pages (first page of results) to show the tree."""
    body = request.get_json(silent=True) or {}
    space = (body.get("space") or get_user_confluence_config(_get_db())["space"]).strip()
    if not space:
        return jsonify({"error": "Confluence space key is required"}), 400
    try:
        conf = current_confluence()
        space_id = conf.get_space_id(space)
        if not space_id:
            return jsonify({"error": f"Space {space!r} not found"}), 404
        pages = next(conf.iter_pages(space_id), [])  # first batch only
    except JiraNotConfigured as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("pages preview failed")
        return jsonify({"error": str(exc)}), 502
    return jsonify({
        "sample": len(pages),
        "items": [{"id": p["id"], "title": p["title"], "parent_id": p["parent_id"]}
                  for p in pages],
    })


@bp.post("/api/pages/jobs")
def create_job():
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id") or ""
    db = _get_db()
    space = (body.get("space") or get_user_confluence_config(db)["space"]).strip()
    if not project_id or not space:
        return jsonify({"error": "project_id and Confluence space are required"}), 400
    batch_size = max(1, min(int(body.get("batch_size") or 20), 100))
    session = request.cookies.get(SESSION_COOKIE, "")

    user = current_user()
    active = db.get_active_page_job(user["id"])
    if active:
        return jsonify({"error": f"Page job #{active['id']} is still {active['status']} — "
                                 "cancel or wait."}), 409
    try:
        access = ensure_plane_access(db, session)
        current_confluence()  # validate Confluence connection
    except (JiraNotConfigured, PlaneNotConfigured) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("page job preflight failed")
        return jsonify({"error": str(exc)}), 502

    job_id = db.create_page_job(
        created_by=user["id"], space_key=space, workspace_slug=access["workspace_slug"],
        project_id=project_id, batch_size=batch_size, session=session, options="{}",
    )
    db.set_setting("migration.default_project", project_id)
    page_migration_worker.start_worker(current_app.config["DB_PATH"], job_id, PLANE_API_URL)
    return jsonify({"job_id": job_id})


@bp.get("/api/pages/jobs")
def list_jobs():
    jobs = _get_db().list_page_jobs(created_by=current_user()["id"])
    for j in jobs:
        j.pop("session", None)  # never expose the stored session cookie
    return jsonify(jobs)


@bp.get("/api/pages/jobs/<int:job_id>")
def get_job(job_id: int):
    db = _get_db()
    job = db.get_page_job(job_id)
    if not job or job["created_by"] != current_user()["id"]:
        return jsonify({"error": "Job not found"}), 404
    job["stalled"] = page_migration_worker.is_job_stalled(job)
    job.pop("session", None)
    result = {"job": job, "counts": db.count_page_items_by_status(job_id)}
    if request.args.get("items"):
        result["items"] = [
            {k: v for k, v in row.items() if k != "payload"}
            for row in db.get_page_items(
                job_id, status=request.args.get("status") or None,
                offset=int(request.args.get("offset") or 0),
                limit=min(int(request.args.get("limit") or 200), 500))
        ]
    return jsonify(result)


@bp.post("/api/pages/jobs/<int:job_id>/cancel")
def cancel_job(job_id: int):
    db = _get_db()
    job = db.get_page_job(job_id)
    if not job or job["created_by"] != current_user()["id"]:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] not in ("pending", "ingesting", "running"):
        return jsonify({"error": f"Job is {job['status']} — nothing to cancel"}), 400
    db.update_page_job(job_id, status="cancelling")
    return jsonify({"cancelling": True})


@bp.post("/api/pages/jobs/<int:job_id>/resume")
def resume_job(job_id: int):
    db = _get_db()
    job = db.get_page_job(job_id)
    if not job or job["created_by"] != current_user()["id"]:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] == "running" and not page_migration_worker.is_job_stalled(job):
        return jsonify({"error": "Job is already running"}), 400
    if not db.get_page_items(job_id, status="pending", limit=1):
        return jsonify({"error": "Nothing left to process"}), 400
    # refresh the session cookie so the worker can keep talking to Plane
    db.update_page_job(job_id, status="pending", error=None,
                       session=request.cookies.get(SESSION_COOKIE, ""))
    started = page_migration_worker.start_worker(current_app.config["DB_PATH"], job_id, PLANE_API_URL)
    return jsonify({"resumed": started})
