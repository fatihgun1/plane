"""
app/routes/schedule.py
========================
Blueprint: /api/schedule

Endpoints
---------
POST   /api/schedule/run          – run scheduler, persist results
GET    /api/schedule/             – list all schedules
GET    /api/schedule/<id>         – get schedule + tasks
GET    /api/schedule/<id>/gantt   – gantt-ready JSON
DELETE /api/schedule/<id>         – delete schedule + tasks
"""

from __future__ import annotations

import logging
from datetime import date

from flask import Blueprint, jsonify, request

from app.db import Database, get_db
from app.scheduler import Scheduler, SchedulerInput

logger = logging.getLogger(__name__)
bp = Blueprint("schedule", __name__, url_prefix="/api/schedule")


def _get_db() -> Database:
    return Database(get_db())


def _task_dict(t) -> dict:
    return {
        "id": t.id,
        "schedule_id": t.schedule_id,
        "item_id": t.item_id,
        "jira_child_key": t.jira_child_key,
        "phase_name": t.phase_name,
        "required_role": t.required_role,
        "effort_days": t.effort_days,
        "assigned_consultant_id": t.assigned_consultant_id,
        "planned_start": t.planned_start.isoformat() if t.planned_start else None,
        "planned_end": t.planned_end.isoformat() if t.planned_end else None,
        "is_locked": t.is_locked,
        "score": t.score,
        "status": t.status,
    }


def _schedule_dict(s) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "workflow_template": s.workflow_template,
        "project_start": s.project_start.isoformat() if s.project_start else None,
        "max_parallel": s.max_parallel,
        "created_at": s.created_at,
        "status": s.status,
    }


# ---------------------------------------------------------------------------
# POST /api/schedule/run
# ---------------------------------------------------------------------------

@bp.post("/run")
def run_schedule():
    """
    Body (JSON):
        {
          "name": "Sprint 1 Plan",
          "project_start": "2026-06-01",
          "max_parallel": 3,
          "item_ids": [1, 2, 3]   // optional; omit to schedule all items
        }
    """
    body = request.get_json(silent=True) or {}
    name = body.get("name", f"Schedule {date.today()}")
    project_start_str = body.get("project_start", date.today().isoformat())
    max_parallel = int(body.get("max_parallel", 3))
    item_ids = body.get("item_ids") or None

    try:
        project_start = date.fromisoformat(project_start_str)
    except ValueError:
        return jsonify({"error": "Invalid project_start date (expected YYYY-MM-DD)"}), 400

    try:
        db = _get_db()
        items = db.get_all_items()
        if item_ids:
            items = [i for i in items if i.id in item_ids]

        if not items:
            return jsonify({"error": "No items to schedule"}), 400

        consultants = db.get_all_consultants()
        if not consultants:
            return jsonify({"error": "No active consultants configured"}), 400

        holidays = db.get_holidays()
        templates = db.get_workflow_templates()

        effort_overrides = db.get_workflow_effort_overrides()
        # Parse type_map from settings: "IssueType1,IssueType2=template_name" lines
        type_map_raw = db.get_setting("workflow.type_map", "")
        type_map: dict = {}
        for line in type_map_raw.splitlines():
            line = line.strip()
            if "=" in line:
                types_part, tname = line.rsplit("=", 1)
                for t in types_part.split(","):
                    t = t.strip()
                    if t:
                        type_map[t] = tname.strip()

        # Load child relation settings for each workflow template
        all_settings = db.get_all_settings()
        child_relation_map: dict = {}  # {template_name: {child_relation, link_type}}
        for key, val in all_settings.items():
            if key.startswith("workflow.") and key.endswith(".child_relation"):
                tname = key[len("workflow."):-len(".child_relation")]
                if tname not in child_relation_map:
                    child_relation_map[tname] = {}
                child_relation_map[tname]["child_relation"] = val
            elif key.startswith("workflow.") and key.endswith(".link_type"):
                tname = key[len("workflow."):-len(".link_type")]
                if tname not in child_relation_map:
                    child_relation_map[tname] = {}
                child_relation_map[tname]["link_type"] = val

        scheduler_input = SchedulerInput(
            items=items,
            consultants=consultants,
            workflow_templates=templates,
            holidays=holidays,
            project_start=project_start,
            max_parallel=max_parallel,
            effort_overrides=effort_overrides or None,
            type_map=type_map or None,
            child_relation_map=child_relation_map or None,
        )

        scheduler = Scheduler(scheduler_input)
        result = scheduler.run()
    except Exception as exc:
        logger.exception("Scheduler failed")
        return jsonify({"error": str(exc)}), 500

    try:
        from app.models import Schedule
        from datetime import datetime
        sched = Schedule(
            id=None,
            name=name,
            workflow_template="mixed",
            project_start=project_start,
            max_parallel=max_parallel,
            created_at=datetime.utcnow().isoformat(),
            status="complete" if not result.failed_items else "partial",
        )
        schedule_id = db.create_schedule(sched)

        for task in result.tasks:
            task.schedule_id = schedule_id
        db.save_scheduled_tasks_bulk(result.tasks)

        db.log_scheduler_run(
            mode="auto",
            items_scheduled=len(result.tasks),
            items_failed=len(result.failed_items),
            notes=f"Schedule id={schedule_id}",
        )
    except Exception as exc:
        logger.exception("Schedule save failed")
        return jsonify({"error": f"Schedule save failed: {str(exc)}"}), 500

    return jsonify({
        "schedule_id": schedule_id,
        "tasks_created": len(result.tasks),
        "failed_items": result.failed_items,
    }), 201


# ---------------------------------------------------------------------------
# GET /api/schedule/
# ---------------------------------------------------------------------------

@bp.get("/")
def list_schedules():
    db = _get_db()
    schedules = db.get_all_schedules()
    return jsonify([_schedule_dict(s) for s in schedules])


# ---------------------------------------------------------------------------
# GET /api/schedule/<id>
# ---------------------------------------------------------------------------

@bp.get("/<int:schedule_id>")
def get_schedule(schedule_id: int):
    db = _get_db()
    sched = db.get_schedule(schedule_id)
    if sched is None:
        return jsonify({"error": "Not found"}), 404
    tasks = db.get_scheduled_tasks(schedule_id)
    result = _schedule_dict(sched)
    result["tasks"] = [_task_dict(t) for t in tasks]
    return jsonify(result)


# ---------------------------------------------------------------------------
# GET /api/schedule/<id>/gantt
# ---------------------------------------------------------------------------

@bp.get("/<int:schedule_id>/gantt")
def gantt(schedule_id: int):
    """
    Returns gantt-ready rows:
        [ { item_id, jira_key, phase_name, consultant_id,
            start, end, effort_days, status }, ... ]
    sorted by planned_start.
    """
    db = _get_db()
    sched = db.get_schedule(schedule_id)
    if sched is None:
        return jsonify({"error": "Not found"}), 404
    tasks = db.get_scheduled_tasks(schedule_id)
    rows = sorted(
        [
            {
                "task_id": t.id,
                "item_id": t.item_id,
                "jira_child_key": t.jira_child_key,
                "phase_name": t.phase_name,
                "consultant_id": t.assigned_consultant_id,
                "start": t.planned_start.isoformat() if t.planned_start else None,
                "end": t.planned_end.isoformat() if t.planned_end else None,
                "effort_days": t.effort_days,
                "status": t.status,
            }
            for t in tasks
        ],
        key=lambda r: r["start"] or "",
    )
    return jsonify(rows)


# ---------------------------------------------------------------------------
# GET /api/schedule/<id>/tasks
# ---------------------------------------------------------------------------

@bp.get("/<int:schedule_id>/tasks")
def list_tasks(schedule_id: int):
    """Return all scheduled tasks for a given schedule."""
    db = _get_db()
    if db.get_schedule(schedule_id) is None:
        return jsonify({"error": "Not found"}), 404
    tasks = db.get_scheduled_tasks(schedule_id)
    return jsonify([_task_dict(t) for t in tasks])


# ---------------------------------------------------------------------------
# PUT /api/schedule/<id>/tasks/<task_id>  (schedule-scoped)
# ---------------------------------------------------------------------------

@bp.put("/<int:schedule_id>/tasks/<int:task_id>")
def update_task_scoped(schedule_id: int, task_id: int):
    """
    Patch a scheduled task – validates the task belongs to this schedule.

    Body (JSON – all fields optional):
        {
          "planned_start": "2026-06-10",
          "planned_end":   "2026-06-15",
          "assigned_consultant_id": 3,
          "is_locked": true,
          "status": "in_progress"
        }
    """
    db = _get_db()
    if db.get_schedule(schedule_id) is None:
        return jsonify({"error": "Schedule not found"}), 404
    task = db.get_scheduled_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    if task.schedule_id != schedule_id:
        return jsonify({"error": "Task does not belong to this schedule"}), 409

    body = request.get_json(silent=True) or {}
    allowed = {
        "planned_start", "planned_end", "assigned_consultant_id",
        "is_locked", "status", "jira_child_key",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if "is_locked" in updates:
        updates["is_locked"] = 1 if updates["is_locked"] else 0
    if not updates:
        return jsonify({"error": "No updatable fields provided"}), 400

    db.update_scheduled_task(task_id, **updates)
    updated = db.get_scheduled_task(task_id)
    return jsonify(_task_dict(updated))


# ---------------------------------------------------------------------------
# PUT /api/schedule/tasks/<task_id>  (legacy – kept for backward compat)
# ---------------------------------------------------------------------------

@bp.put("/tasks/<int:task_id>")
def update_task(task_id: int):
    """
    Patch a single scheduled task (lock/unlock, reassign, adjust dates).

    Body (JSON – all fields optional):
        {
          "planned_start": "2026-06-10",
          "planned_end":   "2026-06-15",
          "assigned_consultant_id": 3,
          "is_locked": true,
          "status": "in_progress"
        }
    """
    db = _get_db()
    task = db.get_scheduled_task(task_id)
    if task is None:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    allowed = {
        "planned_start", "planned_end", "assigned_consultant_id",
        "is_locked", "status", "jira_child_key",
    }
    updates = {k: v for k, v in body.items() if k in allowed}

    # Normalise boolean is_locked -> int (SQLite)
    if "is_locked" in updates:
        updates["is_locked"] = 1 if updates["is_locked"] else 0

    if not updates:
        return jsonify({"error": "No updatable fields provided"}), 400

    db.update_scheduled_task(task_id, **updates)
    updated = db.get_scheduled_task(task_id)
    return jsonify(_task_dict(updated))


# ---------------------------------------------------------------------------
# POST /api/schedule/<id>/recalculate
# ---------------------------------------------------------------------------

@bp.post("/<int:schedule_id>/recalculate")
def recalculate(schedule_id: int):
    """
    Re-run the scheduler for an existing schedule, preserving locked tasks
    and replacing all unlocked tasks with fresh results.

    Body (JSON – all optional):
        { "max_parallel": 3, "item_ids": [1, 2] }
    """
    db = _get_db()
    sched = db.get_schedule(schedule_id)
    if sched is None:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    max_parallel = int(body.get("max_parallel", sched.max_parallel or 3))
    item_ids = body.get("item_ids") or None

    items = db.get_all_items()
    if item_ids:
        items = [i for i in items if i.id in item_ids]
    if not items:
        return jsonify({"error": "No items to schedule"}), 400

    consultants = db.get_all_consultants()
    if not consultants:
        return jsonify({"error": "No active consultants configured"}), 400

    holidays = db.get_holidays()
    templates = db.get_workflow_templates()

    effort_overrides = db.get_workflow_effort_overrides()
    type_map_raw = db.get_setting("workflow.type_map", "")
    type_map: dict = {}
    for line in type_map_raw.splitlines():
        line = line.strip()
        if "=" in line:
            types_part, tname = line.rsplit("=", 1)
            for t in types_part.split(","):
                t = t.strip()
                if t:
                    type_map[t] = tname.strip()

    scheduler_input = SchedulerInput(
        items=items,
        consultants=consultants,
        workflow_templates=templates,
        holidays=holidays,
        project_start=sched.project_start or date.today(),
        max_parallel=max_parallel,
        effort_overrides=effort_overrides or None,
        type_map=type_map or None,
    )

    try:
        scheduler = Scheduler(scheduler_input)
        result = scheduler.run()
    except Exception as exc:
        logger.exception("Recalculate failed")
        return jsonify({"error": str(exc)}), 500

    deleted = db.delete_unlocked_scheduled_tasks(schedule_id)

    for task in result.tasks:
        task.schedule_id = schedule_id
    db.save_scheduled_tasks_bulk(result.tasks)

    new_status = "complete" if not result.failed_items else "partial"
    db.update_schedule_status(schedule_id, new_status)

    db.log_scheduler_run(
        mode="recalculate",
        items_scheduled=len(result.tasks),
        items_failed=len(result.failed_items),
        notes=f"Recalculate schedule id={schedule_id}, deleted_unlocked={deleted}",
    )

    return jsonify({
        "schedule_id": schedule_id,
        "unlocked_deleted": deleted,
        "tasks_created": len(result.tasks),
        "failed_items": result.failed_items,
        "status": new_status,
    })


# ---------------------------------------------------------------------------
# DELETE /api/schedule/<id>
# ---------------------------------------------------------------------------

@bp.delete("/<int:schedule_id>")
def delete_schedule(schedule_id: int):
    db = _get_db()
    sched = db.get_schedule(schedule_id)
    if sched is None:
        return jsonify({"error": "Not found"}), 404
    db.delete_schedule(schedule_id)
    return jsonify({"deleted": schedule_id})
