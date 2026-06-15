"""
app/routes/push.py
====================
Blueprint: /api/push

Endpoints
---------
POST /api/push/<schedule_id>   -- unified push (push_type: 'dates' | 'status' | 'full')
POST /api/push/dates            -- write planned_start/end back to Jira subtask fields  (legacy)
POST /api/push/status           -- transition Jira subtask statuses                     (legacy)
GET  /api/push/log              -- list push audit log entries
"""

from __future__ import annotations

import logging
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.db import Database, get_db
from app.connectors import current_jira as _get_jira

logger = logging.getLogger(__name__)
bp = Blueprint("push", __name__, url_prefix="/api/push")


def _get_db() -> Database:
    return Database(get_db())


# ---------------------------------------------------------------------------
# POST /api/push/<schedule_id>  -- unified endpoint
# ---------------------------------------------------------------------------

@bp.post("/<int:schedule_id>")
def push_unified(schedule_id: int):
    """
    Unified push endpoint.

    Body (JSON)::

        {
            "push_type": "dates" | "status" | "full",
            "dry_run": false,
            "transition_map": {"In Progress": "11", "Done": "31"}
        }

    Behaviour by push_type
    ----------------------
    dates  -- update planned_start / planned_end on each child sub-task.
    status -- transition each child sub-task via the supplied transition_map.
    full   -- dates first, then status.
    """
    body = request.get_json(silent=True) or {}
    push_type = body.get("push_type", "dates")
    dry_run = bool(body.get("dry_run", False))
    transition_map: dict = body.get("transition_map", {})

    if push_type not in ("dates", "status", "full"):
        return jsonify({"error": f"Unknown push_type '{push_type}'. Use dates|status|full"}), 400

    db = _get_db()
    tasks = db.get_scheduled_tasks(schedule_id)
    if not tasks:
        return jsonify({"error": f"No tasks found for schedule {schedule_id}"}), 404

    results: dict = {
        "schedule_id": schedule_id,
        "push_type": push_type,
        "dry_run": dry_run,
        "dates": {"pushed": 0, "failed": 0, "skipped": 0, "errors": []},
        "status": {"pushed": 0, "failed": 0, "skipped": 0, "errors": []},
    }

    jira = None if dry_run else _get_jira()

    # -- dates --
    if push_type in ("dates", "full"):
        pushable = [t for t in tasks if t.jira_child_key]
        results["dates"]["skipped"] = len(tasks) - len(pushable)
        if not dry_run and jira:
            for task in pushable:
                try:
                    fields: dict = {}
                    if task.planned_start:
                        fields["customfield_10015"] = task.planned_start.isoformat()
                    if task.planned_end:
                        fields["duedate"] = task.planned_end.isoformat()
                    if fields:
                        jira.update_issue(task.jira_child_key, fields)
                    results["dates"]["pushed"] += 1
                except Exception as exc:
                    results["dates"]["failed"] += 1
                    results["dates"]["errors"].append(
                        {"key": task.jira_child_key, "error": str(exc)}
                    )
        else:
            results["dates"]["pushed"] = len(pushable)

    # -- status --
    if push_type in ("status", "full"):
        if not transition_map:
            results["status"]["skipped"] = len(tasks)
            results["status"]["note"] = "transition_map not provided"
        else:
            pushable_s = [t for t in tasks if t.jira_child_key and t.status in transition_map]
            results["status"]["skipped"] = len(tasks) - len(pushable_s)
            if not dry_run and jira:
                for task in pushable_s:
                    try:
                        jira.transition_issue(task.jira_child_key, transition_map[task.status])
                        results["status"]["pushed"] += 1
                    except Exception as exc:
                        results["status"]["failed"] += 1
                        results["status"]["errors"].append(
                            {"key": task.jira_child_key, "error": str(exc)}
                        )
            else:
                results["status"]["pushed"] = len(pushable_s)

    db.log_push(
        schedule_id=schedule_id,
        push_type=push_type,
        dry_run=dry_run,
        pushed=(results["dates"]["pushed"] + results["status"]["pushed"]),
        failed=(results["dates"]["failed"] + results["status"]["failed"]),
        timestamp=datetime.utcnow().isoformat(),
    )

    return jsonify(results)


# ---------------------------------------------------------------------------
# POST /api/push/dates  (legacy)
# ---------------------------------------------------------------------------

@bp.post("/dates")
def push_dates():
    """
    Body (JSON)::

        { "schedule_id": 1, "dry_run": false }

    Writes planned_start to customfield_10015 and planned_end to duedate
    for each scheduled task that has a jira_child_key.
    """
    body = request.get_json(silent=True) or {}
    schedule_id = body.get("schedule_id")
    if not schedule_id:
        return jsonify({"error": "schedule_id is required"}), 400
    dry_run = bool(body.get("dry_run", False))

    db = _get_db()
    tasks = db.get_scheduled_tasks(int(schedule_id))
    pushable = [t for t in tasks if t.jira_child_key]

    if not pushable:
        return jsonify({"pushed": 0, "skipped": len(tasks), "dry_run": dry_run})

    results = {"pushed": 0, "failed": 0, "skipped": len(tasks) - len(pushable), "errors": []}

    if not dry_run:
        jira = _get_jira()
        for task in pushable:
            try:
                fields: dict = {}
                if task.planned_start:
                    fields["customfield_10015"] = task.planned_start.isoformat()
                if task.planned_end:
                    fields["duedate"] = task.planned_end.isoformat()
                if fields:
                    jira.update_issue(task.jira_child_key, fields)
                results["pushed"] += 1
            except Exception as exc:
                results["failed"] += 1
                results["errors"].append({"key": task.jira_child_key, "error": str(exc)})

    db.log_push(
        schedule_id=int(schedule_id),
        push_type="dates",
        dry_run=dry_run,
        pushed=results["pushed"] if not dry_run else 0,
        failed=results.get("failed", 0),
        timestamp=datetime.utcnow().isoformat(),
    )

    results["dry_run"] = dry_run
    return jsonify(results)


# ---------------------------------------------------------------------------
# POST /api/push/status  (legacy)
# ---------------------------------------------------------------------------

@bp.post("/status")
def push_status():
    """
    Body (JSON)::

        { "schedule_id": 1, "transition_map": {"In Progress": "11", "Done": "31"}, "dry_run": false }

    Transitions each scheduled task's Jira subtask to the mapped transition id.
    """
    body = request.get_json(silent=True) or {}
    schedule_id = body.get("schedule_id")
    if not schedule_id:
        return jsonify({"error": "schedule_id is required"}), 400
    transition_map: dict = body.get("transition_map", {})
    dry_run = bool(body.get("dry_run", False))

    db = _get_db()
    tasks = db.get_scheduled_tasks(int(schedule_id))
    pushable = [t for t in tasks if t.jira_child_key and t.status in transition_map]

    results = {"pushed": 0, "failed": 0, "skipped": len(tasks) - len(pushable), "errors": []}

    if not dry_run:
        jira = _get_jira()
        for task in pushable:
            transition_id = transition_map[task.status]
            try:
                jira.transition_issue(task.jira_child_key, transition_id)
                results["pushed"] += 1
            except Exception as exc:
                results["failed"] += 1
                results["errors"].append({"key": task.jira_child_key, "error": str(exc)})

    db.log_push(
        schedule_id=int(schedule_id),
        push_type="status",
        dry_run=dry_run,
        pushed=results["pushed"] if not dry_run else 0,
        failed=results.get("failed", 0),
        timestamp=datetime.utcnow().isoformat(),
    )

    results["dry_run"] = dry_run
    return jsonify(results)


# ---------------------------------------------------------------------------
# GET /api/push/log
# ---------------------------------------------------------------------------

@bp.get("/log")
def push_log():
    db = _get_db()
    entries = db.get_push_log()
    return jsonify(entries)
