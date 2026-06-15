"""
app/services/page_migration_worker.py
=====================================
Background worker for the Confluence → Plane Pages migration. Mirrors the Jira
migration worker (claim / two-phase ingest / topo-sort / resume / heartbeat),
but the source is Confluence and the target is Plane Pages (app API, session).

Phases: ingest (Confluence → SQLite) → order (parents first) → push (create +
PATCH parent) → link-rewrite (Confluence page links → Plane page URLs).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime

from app.connectors import build_confluence
from app.connectors.plane_pages import PlanePagesConnector, PlanePagesError
from app.db import Database
from app.services.field_mapping import topo_sort
from app.utils.adf import (
    adf_to_html,
    build_subpages_html,
    rewrite_conf_links,
)

logger = logging.getLogger(__name__)

_threads: dict[int, threading.Thread] = {}
_threads_lock = threading.Lock()
HEARTBEAT_STALE_SECONDS = 120


class _Cancelled(Exception):
    pass


def start_worker(db_path: str, job_id: int, plane_base_url: str) -> bool:
    with _threads_lock:
        existing = _threads.get(job_id)
        if existing and existing.is_alive():
            return False
        conn = _open_conn(db_path)
        try:
            if not Database(conn).claim_page_job(job_id):
                return False
        finally:
            conn.close()
        thread = threading.Thread(target=_run_job, args=(db_path, job_id, plane_base_url),
                                  name=f"page-job-{job_id}", daemon=True)
        _threads[job_id] = thread
        thread.start()
        return True


def is_job_stalled(job: dict) -> bool:
    if job.get("status") not in ("ingesting", "running"):
        return False
    with _threads_lock:
        t = _threads.get(job["id"])
        if t and t.is_alive():
            return False
    hb = job.get("heartbeat") or job.get("updated_at") or ""
    try:
        return (datetime.now() - datetime.fromisoformat(hb)).total_seconds() > HEARTBEAT_STALE_SECONDS
    except ValueError:
        return True


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _beat(db: Database, job_id: int) -> None:
    db.update_page_job(job_id, heartbeat=datetime.now().isoformat(timespec="seconds"))


def _cancelling(db: Database, job_id: int) -> bool:
    return db.get_page_job(job_id)["status"] == "cancelling"


def _run_job(db_path: str, job_id: int, plane_base_url: str) -> None:
    conn = _open_conn(db_path)
    db = Database(conn)
    try:
        job = db.get_page_job(job_id)
        slug, project_id = job["workspace_slug"], job["project_id"]
        session = job["session"]
        if not session:
            raise RuntimeError("Session expired — reopen the Pages tab and start again.")
        plane = PlanePagesConnector(plane_base_url, session)

        if not job["ingested"]:
            _ingest(db, job_id, job)
            _order(db, job_id)
            job = db.get_page_job(job_id)

        db.update_page_job(job_id, status="running")
        # conf_id -> plane page id (carry over already-pushed items on resume)
        id_map = {r["conf_id"]: r["plane_page_id"]
                  for r in db.get_page_items(job_id, status="done", limit=1000000)
                  if r["plane_page_id"]}

        logger.info("Page job %s push starting (%s pages)", job_id, job["total_items"])
        while True:
            if _cancelling(db, job_id):
                _cancel_remaining(db, job_id)
                return
            batch = db.get_page_items(job_id, status="pending", limit=job["batch_size"])
            if not batch:
                break
            for row in batch:
                if _cancelling(db, job_id):
                    _cancel_remaining(db, job_id)
                    return
                _push_page(db, plane, job_id, row, slug, project_id, id_map)
                _beat(db, job_id)

        linked = _finalize_pass(db, plane, job_id, slug, project_id, id_map)
        logger.info("Page job %s finalized %s page(s) (breadcrumbs/links/sub-pages)", job_id, linked)

        counts = db.count_page_items_by_status(job_id)
        final = "completed" if not counts.get("error") else "completed_with_errors"
        db.update_page_job(job_id, status=final, session="")  # drop the session
        logger.info("Page job %s finished: %s (%s)", job_id, final, counts)
    except _Cancelled:
        _cancel_remaining(db, job_id)
    except Exception as exc:
        logger.exception("Page job %s crashed", job_id)
        try:
            db.update_page_job(job_id, status="failed", error=str(exc)[:1000], session="")
        except Exception:
            pass
    finally:
        conn.close()
        with _threads_lock:
            _threads.pop(job_id, None)


def _ingest(db: Database, job_id: int, job: dict) -> None:
    """Phase 1 — stream every Confluence page in the space into SQLite, with its
    body converted to HTML up front."""
    db.update_page_job(job_id, status="ingesting")
    conf = build_confluence(db, job["created_by"])
    space_id = conf.get_space_id(job["space_key"])
    if not space_id:
        raise RuntimeError(f"Confluence space {job['space_key']!r} not found.")
    total = 0
    for batch in conf.iter_pages(space_id):
        if _cancelling(db, job_id):
            raise _Cancelled()
        db.bulk_insert_page_items(job_id, [{
            "conf_id": p["id"], "parent_conf_id": p["parent_id"] or None,
            "title": p["title"],
            "payload": json.dumps({"html": adf_to_html(p["adf"])}),
        } for p in batch])
        total += len(batch)
        db.update_page_job(job_id, total_items=total,
                           heartbeat=datetime.now().isoformat(timespec="seconds"))
    logger.info("Page job %s ingested %s pages", job_id, total)


def _order(db: Database, job_id: int) -> None:
    """Phase 1b — topo-sort so parent pages are created before their children."""
    hierarchy = db.get_page_item_hierarchy(job_id)
    ordered, warns = topo_sort([{"key": c, "parent_key": p or ""} for c, p in hierarchy])
    db.reorder_page_items(job_id, [i["key"] for i in ordered])
    counts = db.count_page_items_by_status(job_id)
    db.update_page_job(job_id, ingested=1, total_items=sum(counts.values()))
    if warns:
        logger.warning("Page job %s ordering: %s", job_id, "; ".join(warns))


def _push_page(db: Database, plane: PlanePagesConnector, job_id: int, row: dict,
               slug: str, project_id: str, id_map: dict) -> None:
    db.update_page_item(row["id"], status="in_progress", attempts=row["attempts"] + 1)
    try:
        html = json.loads(row["payload"]).get("html", "<p></p>")
        page_id = plane.create_page(slug, project_id, row["title"], html)
        id_map[row["conf_id"]] = page_id
        # Set the parent so Plane's native breadcrumb shows the hierarchy. (The
        # CE list filter was patched to keep child pages visible in the flat
        # list.) Parent is created before children via the topo-sort order.
        warnings = []
        parent_conf = row["parent_conf_id"]
        if parent_conf:
            parent_plane = id_map.get(parent_conf)
            if parent_plane:
                plane.set_parent(slug, project_id, page_id, parent_plane)
            else:
                warnings.append(f"parent {parent_conf} not in Plane — page kept at top level")
        db.update_page_item(row["id"], status="done", plane_page_id=page_id,
                            error=None, warnings=json.dumps(warnings))
        _bump(db, job_id, succeeded=1)
    except PlanePagesError as exc:
        db.update_page_item(row["id"], status="error", error=str(exc)[:1000])
        _bump(db, job_id, failed=1)
    except Exception as exc:
        logger.exception("Page job %s page %s failed", job_id, row["conf_id"])
        db.update_page_item(row["id"], status="error", error=str(exc)[:1000])
        _bump(db, job_id, failed=1)


def _finalize_pass(db: Database, plane: PlanePagesConnector, job_id: int, slug: str,
                   project_id: str, id_map: dict) -> int:
    """Phase 4 — rewrite cross-page links and append a 'Sub-pages' list.

    The upward hierarchy is shown by Plane's native breadcrumb (driven by the
    parent set in _push_page), so it is NOT duplicated in the content. For every
    migrated page: rewrite Confluence page links to the migrated Plane URLs in
    the body, and append a 'Sub-pages' list of child links for downward
    navigation. URLs are relative so they work on any host.
    """
    conf_to_url = {cid: f"/{slug}/projects/{project_id}/pages/{pid}/"
                   for cid, pid in id_map.items()}
    rows = db.get_page_items(job_id, status="done", limit=1000000)
    title_by_conf = {r["conf_id"]: r["title"] for r in rows}
    children_by_conf: dict[str, list[str]] = {}
    for r in rows:
        if r["parent_conf_id"]:
            children_by_conf.setdefault(r["parent_conf_id"], []).append(r["conf_id"])

    updated = 0
    for row in rows:
        conf_id = row["conf_id"]
        page_id = id_map.get(conf_id)
        if not page_id:
            continue
        original = json.loads(row["payload"]).get("html", "")
        body = rewrite_conf_links(original, conf_to_url)
        kids = sorted(children_by_conf.get(conf_id, []),
                      key=lambda c: title_by_conf.get(c, "").lower())
        subpages = build_subpages_html(
            [(conf_to_url[c], title_by_conf.get(c, "Untitled")) for c in kids if c in conf_to_url])
        if not (subpages or body != original):
            continue  # leaf with no children and no cross-links — leave as-is
        new_html = f"{body or '<p></p>'}{subpages}"
        try:
            plane.update_description(slug, project_id, page_id, new_html)
            updated += 1
        except PlanePagesError as exc:
            logger.warning("Page job %s finalize %s failed: %s", job_id, conf_id, exc)
        _beat(db, job_id)
    return updated


def _bump(db: Database, job_id: int, succeeded: int = 0, failed: int = 0) -> None:
    db._conn.execute(
        "UPDATE page_jobs SET processed = processed + 1, succeeded = succeeded + ?, "
        "failed = failed + ? WHERE id = ?", (succeeded, failed, job_id))
    db._conn.commit()


def _cancel_remaining(db: Database, job_id: int) -> None:
    db._conn.execute("UPDATE page_job_items SET status='skipped' "
                     "WHERE job_id = ? AND status = 'pending'", (job_id,))
    db._conn.commit()
    counts = db.count_page_items_by_status(job_id)
    db.update_page_job(job_id, status="cancelled", skipped=counts.get("skipped", 0), session="")
    logger.info("Page job %s cancelled", job_id)
