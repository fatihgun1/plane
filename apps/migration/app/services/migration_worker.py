"""
app/services/migration_worker.py
================================
Background worker that pushes a prepared migration job's items into Plane,
in batches, on a daemon thread.

Design constraints
------------------
- Runs inside gunicorn workers: the thread opens its OWN sqlite connection
  (never Flask's g) and all job state lives in SQLite (WAL mode), so the UI
  can poll from any worker process.
- Cross-process double-start is prevented by an atomic claim
  (UPDATE ... WHERE status='pending').
- Plane writes are throttled (~1 req/s, 60/min API key limit) and idempotent
  (external_id upsert), so a crashed job can simply be resumed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime

from app.connectors import build_jira
from app.connectors.plane_connector import PlaneConnector, PlaneApiError
from app.db import Database
from app.services.field_mapping import (
    build_work_item_payload,
    collect_label_names,
    extract_cycle_name,
    map_relation,
    suggest_state_group,
    topo_sort,
)


class _Cancelled(Exception):
    """Internal signal: the job was cancelled mid-phase."""

logger = logging.getLogger(__name__)

_threads: dict[int, threading.Thread] = {}
_threads_lock = threading.Lock()

HEARTBEAT_STALE_SECONDS = 120


def start_worker(db_path: str, job_id: int, plane_base_url: str, plane_token: str) -> bool:
    """Claim the job and launch its thread. False if it was already claimed."""
    with _threads_lock:
        existing = _threads.get(job_id)
        if existing and existing.is_alive():
            return False
        conn = _open_conn(db_path)
        try:
            if not Database(conn).claim_migration_job(job_id):
                return False
        finally:
            conn.close()
        thread = threading.Thread(
            target=_run_job,
            args=(db_path, job_id, plane_base_url, plane_token),
            name=f"migration-job-{job_id}",
            daemon=True,
        )
        _threads[job_id] = thread
        thread.start()
        return True


def is_job_stalled(job: dict) -> bool:
    """True when an active job's heartbeat is too old (process died)."""
    if job.get("status") not in ("ingesting", "running"):
        return False
    with _threads_lock:
        thread = _threads.get(job["id"])
        if thread and thread.is_alive():
            return False
    heartbeat = job.get("heartbeat") or job.get("updated_at") or ""
    try:
        age = (datetime.now() - datetime.fromisoformat(heartbeat)).total_seconds()
    except ValueError:
        return True
    return age > HEARTBEAT_STALE_SECONDS


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _run_job(db_path: str, job_id: int, plane_base_url: str, plane_token: str) -> None:
    conn = _open_conn(db_path)
    db = Database(conn)
    plane = PlaneConnector(plane_base_url, plane_token)
    try:
        job = db.get_migration_job(job_id)
        slug, project_id = job["workspace_slug"], job["project_id"]
        options = json.loads(job["options"] or "{}")

        # ── Phase 1: ingest — stream every Jira page straight into SQLite ──
        if not job["ingested"]:
            _ingest(db, job_id, job)
            _order(db, job_id)
            job = db.get_migration_job(job_id)

        # ── Phase 2: preflight — resolve states / members / labels once the
        #    full set is known (idempotent; skipped if already done) ──
        if not (job["state_map"] and json.loads(job["state_map"])):
            _preflight(db, plane, job_id, slug, project_id, options)
            job = db.get_migration_job(job_id)

        db.update_migration_job(job_id, status="running")
        options = json.loads(job["options"] or "{}")
        ctx_base = {
            "state_map": json.loads(job["state_map"] or "{}"),
            "member_map": options.get("member_map", {}),
            "label_ids": options.get("label_map", {}),
            "push_assignees": options.get("push_assignees", True),
            "push_labels": options.get("push_labels", True),
            "sprint_label_prefix": options.get("sprint_label_prefix", ""),
        }
        # jira_key -> plane id for parents pushed in THIS job (and earlier runs)
        key_to_plane = {
            row["jira_key"]: row["plane_issue_id"]
            for row in db.get_job_items(job_id, status="done", limit=1000000)
            if row["plane_issue_id"]
        }

        logger.info("Migration job %s push starting (%s items)", job_id, job["total_items"])
        while True:
            current = db.get_migration_job(job_id)
            if current["status"] == "cancelling":
                _cancel_remaining(db, job_id)
                return
            batch = db.get_job_items(job_id, status="pending", limit=job["batch_size"])
            if not batch:
                break
            for row in batch:
                if db.get_migration_job(job_id)["status"] == "cancelling":
                    _cancel_remaining(db, job_id)
                    return
                _push_item(db, plane, job_id, row, slug, project_id, ctx_base, key_to_plane)
                db.update_migration_job(job_id, heartbeat=datetime.now().isoformat(timespec="seconds"))

        # Second pass: issue links (blocks / relates / duplicate) — needs every
        # work item to exist in Plane first, so it runs after the main loop.
        if options.get("push_links", True):
            linked = _link_pass(db, plane, job_id, slug, project_id, key_to_plane)
            logger.info("Migration job %s linked %s relation(s)", job_id, linked)

        # Third pass: sprints → cycles. Create a cycle per sprint and assign
        # the work items to it. Also needs every item to exist in Plane.
        if options.get("push_cycles", True):
            cycled = _cycle_pass(db, plane, job_id, slug, project_id, key_to_plane,
                                 options.get("sprint_label_prefix", ""))
            logger.info("Migration job %s assigned %s item(s) to cycles", job_id, cycled)

        counts = db.count_job_items_by_status(job_id)
        final = "completed" if not counts.get("error") else "completed_with_errors"
        db.update_migration_job(job_id, status=final)
        logger.info("Migration job %s finished: %s (%s)", job_id, final, counts)
    except _Cancelled:
        _cancel_remaining(db, job_id)
    except Exception as exc:
        logger.exception("Migration job %s crashed", job_id)
        try:
            db.update_migration_job(job_id, status="failed", error=str(exc)[:1000])
        except Exception:
            pass
    finally:
        conn.close()
        with _threads_lock:
            _threads.pop(job_id, None)


def _cancelling(db: Database, job_id: int) -> bool:
    return db.get_migration_job(job_id)["status"] == "cancelling"


def _ingest(db: Database, job_id: int, job: dict) -> None:
    """Phase 1 — stream every Jira page straight into SQLite (low memory,
    no result cap). Idempotent: re-ingest after a crash skips dupes."""
    db.update_migration_job(job_id, status="ingesting")
    jira = build_jira(db, job["created_by"])
    field_map = db.get_jira_field_map_full()
    total = 0
    for page in jira.iter_issue_pages(job["jql"], field_map):
        if _cancelling(db, job_id):
            raise _Cancelled()
        db.bulk_insert_job_items(job_id, [{
            "jira_key": i["key"], "parent_jira_key": i.get("parent_key") or None,
            "summary": i["summary"], "payload": json.dumps(i),
        } for i in page])
        total += len(page)
        db.update_migration_job(
            job_id, total_items=total,
            heartbeat=datetime.now().isoformat(timespec="seconds"),
        )
    logger.info("Migration job %s ingested %s issues", job_id, total)


def _order(db: Database, job_id: int) -> None:
    """Phase 1b — topo-sort so parents are pushed before their sub-tasks,
    then mark ingest complete."""
    hierarchy = db.get_job_item_hierarchy(job_id)
    ordered, warns = topo_sort([{"key": k, "parent_key": p or ""} for k, p in hierarchy])
    db.reorder_job_items(job_id, [i["key"] for i in ordered])
    counts = db.count_job_items_by_status(job_id)
    db.update_migration_job(job_id, ingested=1, total_items=sum(counts.values()))
    if warns:
        logger.warning("Migration job %s ordering: %s", job_id, "; ".join(warns))


def _preflight(db: Database, plane: PlaneConnector, job_id: int, slug: str,
               project_id: str, options: dict) -> None:
    """Phase 2 — resolve states (creating missing ones), members and labels
    over the full ingested set. Streams items so memory stays bounded."""
    statuses: dict[str, dict] = {}
    label_names: set[str] = set()
    sprint_prefix = options.get("sprint_label_prefix", "")
    offset = 0
    while True:
        rows = db.get_job_items(job_id, offset=offset, limit=2000)
        if not rows:
            break
        for r in rows:
            issue = json.loads(r["payload"])
            sn = issue.get("status_name", "")
            if sn:
                statuses.setdefault(sn.lower(), {"name": sn, "category": issue.get("status_category", "")})
            label_names |= collect_label_names([issue], sprint_prefix)
        offset += len(rows)

    saved = json.loads(db.get_setting(f"migration.state_map.{project_id}", "") or "{}")
    states = plane.get_states(slug, project_id)
    by_name = {s["name"].lower(): str(s["id"]) for s in states}
    valid = {str(s["id"]) for s in states}
    state_map: dict[str, str] = {}
    for nl, info in statuses.items():
        entry = saved.get(nl) or {}
        if entry.get("state_id") in valid:
            state_map[nl] = entry["state_id"]
        elif nl in by_name:
            state_map[nl] = by_name[nl]
        else:
            group = entry.get("group") or suggest_state_group(info["category"])
            state_map[nl] = plane.ensure_state(slug, project_id, info["name"], group, external_id=info["name"])

    member_map = {
        (m.get("email") or "").lower(): str(m["id"])
        for m in plane.get_members(slug, project_id) if m.get("email")
    }
    label_map: dict[str, str] = {}
    if options.get("push_labels", True):
        existing = {l["name"].lower(): str(l["id"]) for l in plane.get_labels(slug, project_id)}
        for name in label_names:
            label_map[name.lower()] = existing.get(name.lower()) or plane.ensure_label(slug, project_id, name)

    options["member_map"] = member_map
    options["label_map"] = label_map
    db.update_migration_job(job_id, state_map=json.dumps(state_map), options=json.dumps(options))
    logger.info("Migration job %s preflight: %s states, %s members, %s labels",
                job_id, len(state_map), len(member_map), len(label_map))


def _push_item(db: Database, plane: PlaneConnector, job_id: int, row: dict,
               slug: str, project_id: str, ctx_base: dict,
               key_to_plane: dict[str, str]) -> None:
    item_id = row["id"]
    db.update_job_item(item_id, status="in_progress", attempts=row["attempts"] + 1)
    try:
        issue = json.loads(row["payload"])
        ctx = dict(ctx_base)
        ctx["parent_plane_id"] = _resolve_parent(
            plane, slug, project_id, issue.get("parent_key"), key_to_plane
        )
        payload, warnings = build_work_item_payload(issue, ctx)
        plane_id, op = plane.create_or_update_work_item(slug, project_id, payload)
        key_to_plane[row["jira_key"]] = plane_id
        db.update_job_item(
            item_id, status="done", plane_issue_id=plane_id, op=op,
            error=None, warnings=json.dumps(warnings),
        )
        _bump(db, job_id, succeeded=1)
    except PlaneApiError as exc:
        db.update_job_item(item_id, status="error", error=str(exc)[:1000])
        _bump(db, job_id, failed=1)
    except Exception as exc:
        logger.exception("Job %s item %s failed", job_id, row["jira_key"])
        db.update_job_item(item_id, status="error", error=str(exc)[:1000])
        _bump(db, job_id, failed=1)


def _resolve_parent(plane: PlaneConnector, slug: str, project_id: str,
                    parent_key: str | None, key_to_plane: dict[str, str]) -> str | None:
    if not parent_key:
        return None
    if parent_key in key_to_plane:
        return key_to_plane[parent_key]
    # Parent outside this job's selection — maybe migrated earlier
    existing = plane.get_work_item_by_external(slug, project_id, parent_key)
    if existing:
        key_to_plane[parent_key] = str(existing["id"])
        return key_to_plane[parent_key]
    return None


def _link_pass(db: Database, plane: PlaneConnector, job_id: int, slug: str,
               project_id: str, key_to_plane: dict[str, str]) -> int:
    """Create Plane issue relations from the Jira links captured on each item.
    Runs once every work item exists, so both ends can be resolved. Targets
    missing from Plane are skipped with a per-item warning."""
    created = 0
    for row in db.get_job_items(job_id, status="done", limit=100000):
        from_id = key_to_plane.get(row["jira_key"])
        if not from_id:
            continue
        issue = json.loads(row["payload"])
        links = issue.get("links") or []
        if not links:
            continue
        warnings = json.loads(row["warnings"] or "[]")
        grouped: dict[str, set] = {}
        for link in links:
            target = _resolve_parent(plane, slug, project_id, link.get("key"), key_to_plane)
            if not target:
                warnings.append(f"link target {link.get('key')} not in Plane — skipped")
                continue
            rel = map_relation(link.get("type", ""), link.get("side", ""))
            grouped.setdefault(rel, set()).add(target)
        for rel, ids in grouped.items():
            try:
                plane.create_relations(slug, project_id, from_id, rel, list(ids))
                created += len(ids)
            except PlaneApiError as exc:
                warnings.append(f"relation {rel} failed: {exc}")
        db.update_job_item(row["id"], warnings=json.dumps(warnings))
        db.update_migration_job(job_id, heartbeat=datetime.now().isoformat(timespec="seconds"))
    return created


def _cycle_pass(db: Database, plane: PlaneConnector, job_id: int, slug: str,
                project_id: str, key_to_plane: dict[str, str],
                sprint_label_prefix: str = "") -> int:
    """Sprint → Cycle: group migrated work items by their Jira sprint (from the
    Sprint field or a native label prefix), create one Plane cycle per sprint,
    and assign the items to it. Runs after every work item exists."""
    by_sprint: dict[str, list[str]] = {}
    for row in db.get_job_items(job_id, status="done", limit=1000000):
        plane_id = key_to_plane.get(row["jira_key"])
        if not plane_id:
            continue
        sprint = extract_cycle_name(json.loads(row["payload"]), sprint_label_prefix)
        if sprint:
            by_sprint.setdefault(sprint, []).append(plane_id)
    if not by_sprint:
        return 0

    assigned = 0
    cycle_ids: dict[str, str] = {}
    for sprint, issue_ids in by_sprint.items():
        try:
            cid = cycle_ids.get(sprint) or plane.ensure_cycle(slug, project_id, sprint)
            cycle_ids[sprint] = cid
            # cycle-issues accepts batches; chunk to stay friendly to the API
            for i in range(0, len(issue_ids), 50):
                plane.add_cycle_issues(slug, project_id, cid, issue_ids[i:i + 50])
            assigned += len(issue_ids)
        except PlaneApiError as exc:
            logger.warning("Migration job %s cycle %r failed: %s", job_id, sprint, exc)
        db.update_migration_job(job_id, heartbeat=datetime.now().isoformat(timespec="seconds"))
    return assigned


def _bump(db: Database, job_id: int, succeeded: int = 0, failed: int = 0) -> None:
    db._conn.execute(
        "UPDATE migration_jobs SET processed = processed + 1, "
        "succeeded = succeeded + ?, failed = failed + ? WHERE id = ?",
        (succeeded, failed, job_id),
    )
    db._conn.commit()


def _cancel_remaining(db: Database, job_id: int) -> None:
    db._conn.execute(
        "UPDATE migration_job_items SET status='skipped' "
        "WHERE job_id = ? AND status = 'pending'",
        (job_id,),
    )
    db._conn.commit()
    counts = db.count_job_items_by_status(job_id)
    db.update_migration_job(job_id, status="cancelled", skipped=counts.get("skipped", 0))
    logger.info("Migration job %s cancelled", job_id)
