"""
app/db.py
==========
Database layer – thin SQLite wrapper using sqlite3 directly.

Public surface
--------------
get_db()   – returns a sqlite3.Connection bound to Flask's g (or a standalone conn)
Database   – helper class that wraps a connection and exposes domain methods
init_db()  – creates all tables (called once at app start)
"""

from __future__ import annotations

import sqlite3
import logging
from datetime import date, datetime
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Row factories & helpers
# ---------------------------------------------------------------------------

def _dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


# ---------------------------------------------------------------------------
# Flask g integration
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Return (or create) a per-request SQLite connection stored in Flask g."""
    from flask import g, current_app
    if "db" not in g:
        # Support both DB_PATH (app factory key) and DATABASE (legacy)
        db_path = current_app.config.get("DB_PATH") or current_app.config.get("DATABASE", "optimization.db")
        # Ensure parent directory exists (e.g. instance/)
        import os
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def close_db(e=None):
    """Teardown handler – close the DB connection at end of request."""
    from flask import g
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    jira_key     TEXT UNIQUE,
    summary      TEXT,
    issue_type   TEXT,
    module       TEXT,
    stream       TEXT,
    priority     TEXT,
    effort_days  REAL DEFAULT 0,
    status       TEXT DEFAULT 'Open',
    epic_key     TEXT,
    imported_at  TEXT,
    extra_fields TEXT  -- JSON blob for additional field map values
);

CREATE TABLE IF NOT EXISTS consultants (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    role         TEXT,
    stream       TEXT,
    capacity_pct REAL DEFAULT 100.0,
    active       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS holidays (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT NOT NULL UNIQUE,
    name      TEXT,
    country   TEXT,
    recurring INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workflow_templates (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    content  TEXT   -- YAML content stored as text
);

CREATE TABLE IF NOT EXISTS schedules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT,
    workflow_template TEXT,
    project_start     TEXT,
    max_parallel      INTEGER DEFAULT 3,
    created_at        TEXT,
    status            TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id            INTEGER REFERENCES schedules(id) ON DELETE CASCADE,
    item_id                INTEGER REFERENCES items(id),
    jira_child_key         TEXT,
    phase_name             TEXT,
    required_role          TEXT,
    effort_days            REAL DEFAULT 0,
    assigned_consultant_id INTEGER REFERENCES consultants(id),
    planned_start          TEXT,
    planned_end            TEXT,
    is_locked              INTEGER DEFAULT 0,
    score                  REAL DEFAULT 0,
    status                 TEXT DEFAULT 'planned'
);

CREATE TABLE IF NOT EXISTS scheduler_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at           TEXT,
    mode             TEXT,
    items_scheduled  INTEGER DEFAULT 0,
    items_failed     INTEGER DEFAULT 0,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS push_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER,
    push_type   TEXT,
    dry_run     INTEGER DEFAULT 0,
    pushed      INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    timestamp   TEXT
);

CREATE TABLE IF NOT EXISTS consultant_availability (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consultant_id   INTEGER NOT NULL REFERENCES consultants(id) ON DELETE CASCADE,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    capacity_pct    REAL DEFAULT 100.0,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS jira_field_map (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    field_label   TEXT NOT NULL UNIQUE,
    jira_field_id TEXT NOT NULL,
    notes         TEXT,
    plane_target  TEXT DEFAULT 'description'
);

CREATE TABLE IF NOT EXISTS migration_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by     TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    heartbeat      TEXT,
    status         TEXT DEFAULT 'pending',
    jql            TEXT,
    workspace_slug TEXT,
    project_id     TEXT,
    batch_size     INTEGER DEFAULT 20,
    total_items    INTEGER DEFAULT 0,
    processed      INTEGER DEFAULT 0,
    succeeded      INTEGER DEFAULT 0,
    failed         INTEGER DEFAULT 0,
    skipped        INTEGER DEFAULT 0,
    ingested       INTEGER DEFAULT 0,
    state_map      TEXT,
    options        TEXT,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS migration_job_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER REFERENCES migration_jobs(id) ON DELETE CASCADE,
    jira_key        TEXT,
    parent_jira_key TEXT,
    summary         TEXT,
    order_index     INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'pending',
    plane_issue_id  TEXT,
    op              TEXT,
    error           TEXT,
    warnings        TEXT,
    payload         TEXT,
    attempts        INTEGER DEFAULT 0,
    updated_at      TEXT,
    UNIQUE(job_id, jira_key)
);

CREATE TABLE IF NOT EXISTS page_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by     TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    heartbeat      TEXT,
    status         TEXT DEFAULT 'pending',
    space_key      TEXT,
    workspace_slug TEXT,
    project_id     TEXT,
    batch_size     INTEGER DEFAULT 20,
    total_items    INTEGER DEFAULT 0,
    processed      INTEGER DEFAULT 0,
    succeeded      INTEGER DEFAULT 0,
    failed         INTEGER DEFAULT 0,
    skipped        INTEGER DEFAULT 0,
    ingested       INTEGER DEFAULT 0,
    session        TEXT,
    options        TEXT,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS page_job_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER REFERENCES page_jobs(id) ON DELETE CASCADE,
    conf_id         TEXT,
    parent_conf_id  TEXT,
    title           TEXT,
    order_index     INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'pending',
    plane_page_id   TEXT,
    error           TEXT,
    warnings        TEXT,
    payload         TEXT,
    attempts        INTEGER DEFAULT 0,
    updated_at      TEXT,
    UNIQUE(job_id, conf_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS workflow_effort_overrides (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    template_name    TEXT NOT NULL,
    phase_name       TEXT NOT NULL,
    effort_factor    REAL NOT NULL,
    jira_issue_type  TEXT,
    is_blocking      INTEGER DEFAULT 1,
    effort_source    TEXT,
    blocks_phases    TEXT,
    UNIQUE(template_name, phase_name)
);
"""


def init_db(app=None):
    """Create all tables. Pass a Flask app or call inside app context."""
    if app is not None:
        with app.app_context():
            _create_tables()
    else:
        _create_tables()


def _migrate_db(conn: sqlite3.Connection):
    """Add missing columns to existing tables – safe on any DB version."""

    # ── jira_field_map: old schema used field_key/mapping/field_name/field_type
    #    new schema uses field_label/jira_field_id/notes
    #    If the old columns exist, migrate in-place.
    jfm_cols = {row[1] for row in conn.execute("PRAGMA table_info(jira_field_map)")}
    if "field_key" in jfm_cols and "field_label" not in jfm_cols:
        logger.info("Migration: renaming jira_field_map to new schema")
        conn.execute("ALTER TABLE jira_field_map RENAME TO _jira_field_map_old")
        conn.execute("""
            CREATE TABLE jira_field_map (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                field_label   TEXT NOT NULL UNIQUE,
                jira_field_id TEXT NOT NULL,
                notes         TEXT
            )
        """)
        conn.execute("""
            INSERT INTO jira_field_map (field_label, jira_field_id, notes)
            SELECT field_key, COALESCE(mapping,''), COALESCE(field_name,'')
            FROM _jira_field_map_old
        """)
        conn.execute("DROP TABLE _jira_field_map_old")
        conn.commit()
        logger.info("Migration: jira_field_map schema updated")

    # ── workflow_templates: old schema had per-phase rows; new schema has (name, content YAML)
    wt_cols = {row[1] for row in conn.execute("PRAGMA table_info(workflow_templates)")}
    if "content" not in wt_cols and "phase_name" in wt_cols:
        logger.info("Migration: rebuilding workflow_templates to new (name, content) schema")
        conn.execute("DROP TABLE workflow_templates")
        conn.execute("""
            CREATE TABLE workflow_templates (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL UNIQUE,
                content  TEXT
            )
        """)
        conn.commit()
        logger.info("Migration: workflow_templates rebuilt — YAML files will be loaded on next startup")

    jfm_cols = {row[1] for row in conn.execute("PRAGMA table_info(jira_field_map)")}
    if "plane_target" not in jfm_cols:
        conn.execute("ALTER TABLE jira_field_map ADD COLUMN plane_target TEXT DEFAULT 'description'")
        logger.info("Migration: added jira_field_map.plane_target")

    mj_cols = {row[1] for row in conn.execute("PRAGMA table_info(migration_jobs)")}
    if mj_cols and "ingested" not in mj_cols:
        conn.execute("ALTER TABLE migration_jobs ADD COLUMN ingested INTEGER DEFAULT 0")
        logger.info("Migration: added migration_jobs.ingested")

    item_cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "extra_fields" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN extra_fields TEXT")
        logger.info("Migration: added items.extra_fields")

    cols = {row[1] for row in conn.execute("PRAGMA table_info(consultants)")}
    if "stream" not in cols:
        conn.execute("ALTER TABLE consultants ADD COLUMN stream TEXT")
        logger.info("Migration: added consultants.stream")
    if "active" not in cols:
        conn.execute("ALTER TABLE consultants ADD COLUMN active INTEGER DEFAULT 1")
        logger.info("Migration: added consultants.active")
    if "role" not in cols:
        conn.execute("ALTER TABLE consultants ADD COLUMN role TEXT")
        logger.info("Migration: added consultants.role")
    if "capacity_pct" not in cols:
        conn.execute("ALTER TABLE consultants ADD COLUMN capacity_pct REAL DEFAULT 100.0")
        logger.info("Migration: added consultants.capacity_pct")
    if "level" not in cols:
        conn.execute("ALTER TABLE consultants ADD COLUMN level TEXT DEFAULT 'Consultant'")
        logger.info("Migration: added consultants.level")
    # Legacy columns that no longer exist in new schema but may be in old DB
    # (daily_capacity, seniority_level, is_active) — leave them; they're harmless

    cols = {row[1] for row in conn.execute("PRAGMA table_info(holidays)")}
    # Legacy schema used start_date/end_date instead of a single date column
    if "start_date" in cols and "date" not in cols:
        logger.info("Migration: rebuilding holidays table to new schema")
        conn.execute("ALTER TABLE holidays RENAME TO _holidays_old")
        conn.execute("""
            CREATE TABLE holidays (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date      TEXT NOT NULL UNIQUE,
                name      TEXT,
                country   TEXT,
                recurring INTEGER DEFAULT 0
            )
        """)
        # Copy rows using start_date as the canonical date
        conn.execute("""
            INSERT OR IGNORE INTO holidays (date, name, country, recurring)
            SELECT start_date, name, country, COALESCE(recurring, 0)
            FROM _holidays_old
        """)
        conn.execute("DROP TABLE _holidays_old")
        conn.commit()
        logger.info("Migration: holidays table rebuilt")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(holidays)")}
    if "recurring" not in cols:
        conn.execute("ALTER TABLE holidays ADD COLUMN recurring INTEGER DEFAULT 0")
        logger.info("Migration: added holidays.recurring")
    if "country" not in cols:
        conn.execute("ALTER TABLE holidays ADD COLUMN country TEXT")
        logger.info("Migration: added holidays.country")

    conn.commit()


def _create_tables():
    conn = get_db()
    for statement in SCHEMA.strip().split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()
    _migrate_db(conn)


# ---------------------------------------------------------------------------
# Model imports (lazy to avoid circular)
# ---------------------------------------------------------------------------

def _row_to_item(row):
    from app.models import Item
    keys = row.keys() if hasattr(row, "keys") else []
    return Item(
        id=row["id"],
        jira_key=row["jira_key"],
        summary=row["summary"],
        issue_type=row["issue_type"],
        module=row["module"],
        stream=row["stream"],
        priority=row["priority"],
        effort_days=row["effort_days"] or 0,
        status=row["status"],
        epic_key=row["epic_key"],
        imported_at=row["imported_at"],
        extra_fields=row["extra_fields"] if "extra_fields" in keys else None,
    )


def _row_to_consultant(row):
    from app.models import Consultant
    keys = row.keys()
    return Consultant(
        id=row["id"],
        name=row["name"],
        role=row["role"],
        stream=row["stream"],
        capacity_pct=row["capacity_pct"] or 100.0,
        active=bool(row["active"]),
        level=row["level"] if "level" in keys else "Consultant",
    )


def _row_to_availability(row):
    from app.models import ConsultantAvailability
    return ConsultantAvailability(
        id=row["id"],
        consultant_id=row["consultant_id"],
        start_date=date.fromisoformat(row["start_date"]),
        end_date=date.fromisoformat(row["end_date"]),
        capacity_pct=row["capacity_pct"] or 100.0,
        note=row["note"],
    )


def _row_to_schedule(row):
    from app.models import Schedule
    ps = row["project_start"]
    return Schedule(
        id=row["id"],
        name=row["name"],
        workflow_template=row["workflow_template"],
        project_start=date.fromisoformat(ps) if ps else None,
        max_parallel=row["max_parallel"],
        created_at=row["created_at"],
        status=row["status"],
    )


def _row_to_scheduled_task(row):
    from app.models import ScheduledTask
    def _d(val):
        return date.fromisoformat(val) if val else None
    return ScheduledTask(
        id=row["id"],
        schedule_id=row["schedule_id"],
        item_id=row["item_id"],
        jira_child_key=row["jira_child_key"],
        phase_name=row["phase_name"],
        required_role=row["required_role"],
        effort_days=row["effort_days"] or 0,
        assigned_consultant_id=row["assigned_consultant_id"],
        planned_start=_d(row["planned_start"]),
        planned_end=_d(row["planned_end"]),
        is_locked=bool(row["is_locked"]),
        score=row["score"] or 0.0,
        status=row["status"],
    )


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class Database:
    """Thin wrapper around a sqlite3 connection providing domain methods."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # -----------------------------------------------------------------------
    # Items
    # -----------------------------------------------------------------------

    def get_items(self, **filters) -> list:
        """Return items filtered by keyword args (stream, module, status, issue_type)."""
        allowed = {"stream", "module", "status", "issue_type"}
        where_clauses = []
        params = []
        for key, val in filters.items():
            if key in allowed and val is not None:
                where_clauses.append(f"{key} = ?")
                params.append(val)
        sql = "SELECT * FROM items"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_item(r) for r in rows]

    def get_all_items(self) -> list:
        return self.get_items()

    def get_item(self, item_id: int):
        row = self._conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def update_item(self, item_id: int, **fields) -> bool:
        """Patch mutable fields on an existing item row by primary key."""
        allowed = {"summary", "effort_days", "priority", "status",
                   "module", "stream", "issue_type", "epic_key"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [item_id]
        cur = self._conn.execute(
            f"UPDATE items SET {set_clause} WHERE id = ?", params
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_item(self, item_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def clear_items(self):
        self._conn.execute("DELETE FROM items")
        self._conn.commit()

    def upsert_items(self, items: list) -> list:
        """Insert or replace items. Returns the saved list."""
        now = datetime.utcnow().isoformat()
        saved = []
        for item in items:
            if not item.imported_at:
                item.imported_at = now
            extra = getattr(item, "extra_fields", None)
            cur = self._conn.execute(
                """
                INSERT INTO items (jira_key, summary, issue_type, module, stream,
                                   priority, effort_days, status, epic_key, imported_at, extra_fields)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(jira_key) DO UPDATE SET
                    summary=excluded.summary,
                    issue_type=excluded.issue_type,
                    module=excluded.module,
                    stream=excluded.stream,
                    priority=excluded.priority,
                    effort_days=excluded.effort_days,
                    status=excluded.status,
                    epic_key=excluded.epic_key,
                    imported_at=excluded.imported_at,
                    extra_fields=excluded.extra_fields
                """,
                (
                    item.jira_key, item.summary, item.issue_type, item.module,
                    item.stream, item.priority, item.effort_days, item.status,
                    item.epic_key, item.imported_at, extra,
                ),
            )
            item.id = cur.lastrowid
            saved.append(item)
        self._conn.commit()
        return saved

    # -----------------------------------------------------------------------
    # Consultants
    # -----------------------------------------------------------------------

    def get_all_consultants(self) -> list:
        rows = self._conn.execute(
            "SELECT * FROM consultants WHERE active = 1 ORDER BY id"
        ).fetchall()
        return [_row_to_consultant(r) for r in rows]

    def get_consultant(self, consultant_id: int):
        row = self._conn.execute(
            "SELECT * FROM consultants WHERE id = ?", (consultant_id,)
        ).fetchone()
        return _row_to_consultant(row) if row else None

    def create_consultant(self, consultant) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO consultants (name, role, stream, capacity_pct, active, level)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                consultant.name, consultant.role, consultant.stream,
                consultant.capacity_pct, 1 if consultant.active else 0,
                getattr(consultant, "level", "Consultant") or "Consultant",
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_consultant(self, consultant_id: int, **fields) -> bool:
        allowed = {"name", "role", "stream", "capacity_pct", "active", "level"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [consultant_id]
        cur = self._conn.execute(
            f"UPDATE consultants SET {set_clause} WHERE id = ?", params
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_consultant(self, consultant_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM consultants WHERE id = ?", (consultant_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def upsert_consultant(self, consultant) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO consultants (name, role, stream, capacity_pct, active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                consultant.name, consultant.role, consultant.stream,
                consultant.capacity_pct, 1 if consultant.active else 0,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    # -----------------------------------------------------------------------
    # Consultant availability windows
    # -----------------------------------------------------------------------

    def get_availability(self, consultant_id: int) -> list:
        rows = self._conn.execute(
            "SELECT * FROM consultant_availability WHERE consultant_id = ? ORDER BY start_date",
            (consultant_id,),
        ).fetchall()
        return [_row_to_availability(r) for r in rows]

    def add_availability(self, avail) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO consultant_availability
                (consultant_id, start_date, end_date, capacity_pct, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                avail.consultant_id,
                avail.start_date.isoformat(),
                avail.end_date.isoformat(),
                avail.capacity_pct,
                avail.note,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_availability(self, avail_id: int, **fields) -> bool:
        allowed = {"start_date", "end_date", "capacity_pct", "note"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [avail_id]
        cur = self._conn.execute(
            f"UPDATE consultant_availability SET {set_clause} WHERE id = ?", params
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_availability(self, avail_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM consultant_availability WHERE id = ?", (avail_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_consultant_capacity_on(self, consultant_id: int, check_date: date) -> float:
        """Return effective capacity_pct for a consultant on a given date (availability window overrides base)."""
        iso = check_date.isoformat()
        row = self._conn.execute(
            """
            SELECT capacity_pct FROM consultant_availability
            WHERE consultant_id = ? AND start_date <= ? AND end_date >= ?
            ORDER BY start_date DESC LIMIT 1
            """,
            (consultant_id, iso, iso),
        ).fetchone()
        if row:
            return row["capacity_pct"]
        base = self._conn.execute(
            "SELECT capacity_pct FROM consultants WHERE id = ?", (consultant_id,)
        ).fetchone()
        return base["capacity_pct"] if base else 100.0

    # -----------------------------------------------------------------------
    # Holidays
    # -----------------------------------------------------------------------

    def get_holidays(self) -> list:
        rows = self._conn.execute("SELECT date FROM holidays ORDER BY date").fetchall()
        return [date.fromisoformat(r["date"]) for r in rows]

    def get_holidays_full(self) -> list:
        """Return full holiday rows as dicts (id, date, name, country, recurring)."""
        rows = self._conn.execute("SELECT * FROM holidays ORDER BY date").fetchall()
        return [dict(r) for r in rows]

    def get_holiday(self, holiday_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM holidays WHERE id = ?", (holiday_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_holiday(self, holiday_date: date, name: str = "",
                    country: Optional[str] = None, recurring: int = 0) -> int:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO holidays (date, name, country, recurring) VALUES (?, ?, ?, ?)",
            (holiday_date.isoformat(), name, country, recurring),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_holiday(self, holiday_id: int, **fields) -> bool:
        allowed = {"date", "name", "country", "recurring"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [holiday_id]
        cur = self._conn.execute(
            f"UPDATE holidays SET {set_clause} WHERE id = ?", params
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_holiday(self, holiday_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM holidays WHERE id = ?", (holiday_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # -----------------------------------------------------------------------
    # Workflow templates
    # -----------------------------------------------------------------------

    def get_workflow_templates(self) -> dict:
        """Return {name: yaml_content_str} dict."""
        rows = self._conn.execute("SELECT name, content FROM workflow_templates").fetchall()
        return {r["name"]: r["content"] for r in rows}

    def upsert_workflow_template(self, name: str, content: str):
        self._conn.execute(
            """
            INSERT INTO workflow_templates (name, content) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET content=excluded.content
            """,
            (name, content),
        )
        self._conn.commit()

    # -----------------------------------------------------------------------
    # Schedules
    # -----------------------------------------------------------------------

    def create_schedule(self, schedule) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO schedules (name, workflow_template, project_start,
                                   max_parallel, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                schedule.name, schedule.workflow_template,
                schedule.project_start.isoformat() if schedule.project_start else None,
                schedule.max_parallel, schedule.created_at, schedule.status,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_all_schedules(self) -> list:
        rows = self._conn.execute(
            "SELECT * FROM schedules ORDER BY id DESC"
        ).fetchall()
        return [_row_to_schedule(r) for r in rows]

    def get_schedule(self, schedule_id: int):
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        return _row_to_schedule(row) if row else None

    def delete_schedule(self, schedule_id: int):
        self._conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        self._conn.commit()

    # -----------------------------------------------------------------------
    # Scheduled tasks
    # -----------------------------------------------------------------------

    def save_scheduled_tasks_bulk(self, tasks: list):
        for task in tasks:
            self._conn.execute(
                """
                INSERT INTO scheduled_tasks
                    (schedule_id, item_id, jira_child_key, phase_name, required_role,
                     effort_days, assigned_consultant_id, planned_start, planned_end,
                     is_locked, score, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.schedule_id, task.item_id, task.jira_child_key,
                    task.phase_name, task.required_role, task.effort_days,
                    task.assigned_consultant_id,
                    task.planned_start.isoformat() if task.planned_start else None,
                    task.planned_end.isoformat() if task.planned_end else None,
                    1 if task.is_locked else 0, task.score, task.status,
                ),
            )
        self._conn.commit()

    def get_scheduled_tasks(self, schedule_id: int) -> list:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE schedule_id = ? ORDER BY planned_start",
            (schedule_id,),
        ).fetchall()
        return [_row_to_scheduled_task(r) for r in rows]

    def get_scheduled_task(self, task_id: int):
        row = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _row_to_scheduled_task(row) if row else None

    def delete_unlocked_scheduled_tasks(self, schedule_id: int) -> int:
        """Delete all non-locked tasks for a schedule. Returns row count deleted."""
        cur = self._conn.execute(
            "DELETE FROM scheduled_tasks WHERE schedule_id = ? AND is_locked = 0",
            (schedule_id,),
        )
        self._conn.commit()
        return cur.rowcount

    def update_schedule_status(self, schedule_id: int, status: str) -> bool:
        cur = self._conn.execute(
            "UPDATE schedules SET status = ? WHERE id = ?", (status, schedule_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_scheduled_task(self, task_id: int, **fields) -> bool:
        allowed = {"planned_start", "planned_end", "assigned_consultant_id",
                   "is_locked", "status", "score", "jira_child_key"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [task_id]
        cur = self._conn.execute(
            f"UPDATE scheduled_tasks SET {set_clause} WHERE id = ?", params
        )
        self._conn.commit()
        return cur.rowcount > 0

    # -----------------------------------------------------------------------
    # Scheduler log
    # -----------------------------------------------------------------------

    def log_scheduler_run(self, mode: str, items_scheduled: int, items_failed: int, notes: str = ""):
        self._conn.execute(
            """
            INSERT INTO scheduler_log (run_at, mode, items_scheduled, items_failed, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (datetime.utcnow().isoformat(), mode, items_scheduled, items_failed, notes),
        )
        self._conn.commit()

    # -----------------------------------------------------------------------
    # Push log
    # -----------------------------------------------------------------------

    def log_push(self, schedule_id: int, push_type: str, dry_run: bool,
                 pushed: int, failed: int, timestamp: str):
        self._conn.execute(
            """
            INSERT INTO push_log (schedule_id, push_type, dry_run, pushed, failed, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (schedule_id, push_type, 1 if dry_run else 0, pushed, failed, timestamp),
        )
        self._conn.commit()

    def get_push_log(self) -> list:
        rows = self._conn.execute(
            "SELECT * FROM push_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Jira field map
    # -----------------------------------------------------------------------

    def get_jira_field_map(self) -> dict:
        """Return {field_label: jira_field_id} mapping stored in DB."""
        rows = self._conn.execute(
            "SELECT field_label, jira_field_id FROM jira_field_map"
        ).fetchall()
        return {r["field_label"]: r["jira_field_id"] for r in rows}

    def get_jira_field_map_full(self) -> list:
        """Return full field map rows as list of dicts (insertion order)."""
        rows = self._conn.execute(
            "SELECT * FROM jira_field_map ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_jira_field_map(self, field_label: str, jira_field_id: str,
                               notes: str = "", plane_target: str = "description") -> None:
        """Insert or update a single field mapping entry."""
        self._conn.execute(
            """
            INSERT INTO jira_field_map (field_label, jira_field_id, notes, plane_target)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(field_label) DO UPDATE SET
                jira_field_id=excluded.jira_field_id,
                notes=excluded.notes,
                plane_target=excluded.plane_target
            """,
            (field_label, jira_field_id, notes, plane_target),
        )
        self._conn.commit()

    def bulk_upsert_jira_field_map(self, entries: list[dict]) -> int:
        """
        Upsert many field map entries at once.
        Each entry: {field_label, jira_field_id, notes?, plane_target?}
        Returns count saved.
        """
        count = 0
        for e in entries:
            self._conn.execute(
                """
                INSERT INTO jira_field_map (field_label, jira_field_id, notes, plane_target)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(field_label) DO UPDATE SET
                    jira_field_id=excluded.jira_field_id,
                    notes=excluded.notes,
                    plane_target=excluded.plane_target
                """,
                (
                    e["field_label"], e.get("jira_field_id", ""),
                    e.get("notes", ""),
                    e.get("plane_target") or "description",
                ),
            )
            count += 1
        self._conn.commit()
        return count

    # -----------------------------------------------------------------------
    # Migration jobs (Jira -> Plane push)
    # -----------------------------------------------------------------------

    def create_migration_job(self, *, created_by: str, jql: str, workspace_slug: str,
                             project_id: str, batch_size: int, total_items: int,
                             state_map: str, options: str) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.execute(
            """
            INSERT INTO migration_jobs
                (created_by, created_at, updated_at, status, jql, workspace_slug,
                 project_id, batch_size, total_items, state_map, options)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (created_by, now, now, jql, workspace_slug, project_id,
             batch_size, total_items, state_map, options),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_migration_job(self, job_id: int, **fields) -> None:
        fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE migration_jobs SET {sets} WHERE id = ?",
            (*fields.values(), job_id),
        )
        self._conn.commit()

    def claim_migration_job(self, job_id: int) -> bool:
        """Atomically move a job from pending to running. False if already taken."""
        now = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.execute(
            "UPDATE migration_jobs SET status='running', heartbeat=?, updated_at=? "
            "WHERE id = ? AND status = 'pending'",
            (now, now, job_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def get_migration_job(self, job_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM migration_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_migration_jobs(self, created_by: str = None, limit: int = 50) -> list:
        if created_by:
            rows = self._conn.execute(
                "SELECT * FROM migration_jobs WHERE created_by = ? ORDER BY id DESC LIMIT ?",
                (created_by, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM migration_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_migration_job(self, created_by: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM migration_jobs WHERE created_by = ? "
            "AND status IN ('pending','ingesting','running','cancelling') ORDER BY id DESC LIMIT 1",
            (created_by,),
        ).fetchone()
        return dict(row) if row else None

    def bulk_insert_job_items(self, job_id: int, items: list[dict]) -> int:
        """Append job items idempotently (re-ingest after a crash skips dupes
        via UNIQUE(job_id, jira_key)). order_index is set later by reorder."""
        now = datetime.now().isoformat(timespec="seconds")
        inserted = 0
        for it in items:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO migration_job_items
                    (job_id, jira_key, parent_jira_key, summary, order_index, payload, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (job_id, it["jira_key"], it.get("parent_jira_key"),
                 it.get("summary", ""), it.get("payload", ""), now),
            )
            inserted += cur.rowcount
        self._conn.commit()
        return inserted

    def get_job_item_hierarchy(self, job_id: int) -> list[tuple]:
        """Return (jira_key, parent_jira_key) for every item — for topo-sort."""
        rows = self._conn.execute(
            "SELECT jira_key, parent_jira_key FROM migration_job_items WHERE job_id = ?",
            (job_id,),
        ).fetchall()
        return [(r["jira_key"], r["parent_jira_key"]) for r in rows]

    def reorder_job_items(self, job_id: int, ordered_keys: list[str]) -> None:
        """Assign order_index by the given key order (parents before children)."""
        self._conn.executemany(
            "UPDATE migration_job_items SET order_index = ? WHERE job_id = ? AND jira_key = ?",
            [(idx, job_id, key) for idx, key in enumerate(ordered_keys)],
        )
        self._conn.commit()

    def get_job_items(self, job_id: int, status: str = None,
                      offset: int = 0, limit: int = 200) -> list:
        sql = "SELECT * FROM migration_job_items WHERE job_id = ?"
        params: list = [job_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY order_index LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def update_job_item(self, item_id: int, **fields) -> None:
        fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE migration_job_items SET {sets} WHERE id = ?",
            (*fields.values(), item_id),
        )
        self._conn.commit()

    def count_job_items_by_status(self, job_id: int) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM migration_job_items "
            "WHERE job_id = ? GROUP BY status",
            (job_id,),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -----------------------------------------------------------------------
    # Page jobs (Confluence -> Plane Pages) — mirrors migration jobs
    # -----------------------------------------------------------------------

    def create_page_job(self, *, created_by: str, space_key: str, workspace_slug: str,
                        project_id: str, batch_size: int, session: str, options: str) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.execute(
            """
            INSERT INTO page_jobs
                (created_by, created_at, updated_at, status, space_key, workspace_slug,
                 project_id, batch_size, total_items, session, options)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 0, ?, ?)
            """,
            (created_by, now, now, space_key, workspace_slug, project_id,
             batch_size, session, options),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_page_job(self, job_id: int, **fields) -> None:
        fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE page_jobs SET {sets} WHERE id = ?",
                           (*fields.values(), job_id))
        self._conn.commit()

    def claim_page_job(self, job_id: int) -> bool:
        now = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.execute(
            "UPDATE page_jobs SET status='running', heartbeat=?, updated_at=? "
            "WHERE id = ? AND status = 'pending'",
            (now, now, job_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def get_page_job(self, job_id: int):
        row = self._conn.execute("SELECT * FROM page_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_page_jobs(self, created_by: str = None, limit: int = 50) -> list:
        if created_by:
            rows = self._conn.execute(
                "SELECT * FROM page_jobs WHERE created_by = ? ORDER BY id DESC LIMIT ?",
                (created_by, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM page_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_page_job(self, created_by: str):
        row = self._conn.execute(
            "SELECT * FROM page_jobs WHERE created_by = ? "
            "AND status IN ('pending','ingesting','running','cancelling') ORDER BY id DESC LIMIT 1",
            (created_by,),
        ).fetchone()
        return dict(row) if row else None

    def bulk_insert_page_items(self, job_id: int, items: list) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        inserted = 0
        for it in items:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO page_job_items
                    (job_id, conf_id, parent_conf_id, title, order_index, payload, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (job_id, it["conf_id"], it.get("parent_conf_id"),
                 it.get("title", ""), it.get("payload", ""), now),
            )
            inserted += cur.rowcount
        self._conn.commit()
        return inserted

    def get_page_items(self, job_id: int, status: str = None,
                       offset: int = 0, limit: int = 200) -> list:
        sql = "SELECT * FROM page_job_items WHERE job_id = ?"
        params: list = [job_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY order_index LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def update_page_item(self, item_id: int, **fields) -> None:
        fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE page_job_items SET {sets} WHERE id = ?",
                           (*fields.values(), item_id))
        self._conn.commit()

    def get_page_item_hierarchy(self, job_id: int) -> list:
        rows = self._conn.execute(
            "SELECT conf_id, parent_conf_id FROM page_job_items WHERE job_id = ?", (job_id,)
        ).fetchall()
        return [(r["conf_id"], r["parent_conf_id"]) for r in rows]

    def reorder_page_items(self, job_id: int, ordered_ids: list) -> None:
        self._conn.executemany(
            "UPDATE page_job_items SET order_index = ? WHERE job_id = ? AND conf_id = ?",
            [(idx, job_id, cid) for idx, cid in enumerate(ordered_ids)],
        )
        self._conn.commit()

    def count_page_items_by_status(self, job_id: int) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM page_job_items WHERE job_id = ? GROUP BY status",
            (job_id,),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -----------------------------------------------------------------------
    # Items for schedule
    # -----------------------------------------------------------------------

    def get_items_for_schedule(self, schedule_id: int) -> list:
        """Return all items referenced by tasks in a given schedule."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT i.* FROM items i
            JOIN scheduled_tasks st ON st.item_id = i.id
            WHERE st.schedule_id = ?
            ORDER BY i.id
            """,
            (schedule_id,),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def delete_scheduled_tasks(self, task_ids: list) -> int:
        """Delete specific scheduled tasks by primary key. Returns rows deleted."""
        if not task_ids:
            return 0
        placeholders = ",".join("?" * len(task_ids))
        cur = self._conn.execute(
            f"DELETE FROM scheduled_tasks WHERE id IN ({placeholders})",
            task_ids,
        )
        self._conn.commit()
        return cur.rowcount

    # -----------------------------------------------------------------------
    # App settings (generic key-value store)
    # -----------------------------------------------------------------------

    def get_all_settings(self) -> dict:
        """Return all app_settings rows as {key: value}."""
        rows = self._conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def get_setting(self, key: str, default: str = "") -> str:
        """Return a single setting value, or default if not set."""
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Insert or update a single setting."""
        self._conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    def bulk_upsert_settings(self, settings: dict) -> int:
        """Upsert many settings at once. Returns count saved."""
        count = 0
        for key, value in settings.items():
            self._conn.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(key), str(value) if value is not None else ""),
            )
            count += 1
        self._conn.commit()
        return count

    # -----------------------------------------------------------------------
    # Workflow effort overrides
    # -----------------------------------------------------------------------

    def get_workflow_effort_overrides(self) -> dict:
        """Return {template_name: {phase_name: {effort_factor, jira_issue_type, is_blocking, effort_source, blocks_phases}}} nested dict."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(workflow_effort_overrides)")}
        has_extra = "jira_issue_type" in cols and "is_blocking" in cols
        has_source = "effort_source" in cols
        has_blocks = "blocks_phases" in cols

        select_cols = "template_name, phase_name, effort_factor"
        if has_extra:
            select_cols += ", jira_issue_type, is_blocking"
        if has_source:
            select_cols += ", effort_source"
        if has_blocks:
            select_cols += ", blocks_phases"

        rows = self._conn.execute(
            f"SELECT {select_cols} FROM workflow_effort_overrides"
        ).fetchall()

        result: dict = {}
        for r in rows:
            tname, pname = r["template_name"], r["phase_name"]
            result.setdefault(tname, {})[pname] = {
                "effort_factor": r["effort_factor"],
                "jira_issue_type": r["jira_issue_type"] if has_extra else None,
                "is_blocking": bool(r["is_blocking"]) if has_extra else None,
                "effort_source": r["effort_source"] if has_source else None,
                "blocks_phases": r["blocks_phases"] if has_blocks else None,
            }
        return result

    def bulk_upsert_workflow_effort_overrides(self, entries: list) -> int:
        """
        Upsert many overrides.
        Each entry: {template_name, phase_name, effort_factor, jira_issue_type?, is_blocking?, effort_source?}
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(workflow_effort_overrides)")}
        if "jira_issue_type" not in cols:
            self._conn.execute("ALTER TABLE workflow_effort_overrides ADD COLUMN jira_issue_type TEXT")
        if "is_blocking" not in cols:
            self._conn.execute("ALTER TABLE workflow_effort_overrides ADD COLUMN is_blocking INTEGER DEFAULT 1")
        if "effort_source" not in cols:
            self._conn.execute("ALTER TABLE workflow_effort_overrides ADD COLUMN effort_source TEXT")
        if "blocks_phases" not in cols:
            self._conn.execute("ALTER TABLE workflow_effort_overrides ADD COLUMN blocks_phases TEXT")

        count = 0
        for e in entries:
            jit = e.get("jira_issue_type") or None
            ib = 1 if e.get("is_blocking", True) else 0
            es = e.get("effort_source") or None
            bp = e.get("blocks_phases") or None
            self._conn.execute(
                """
                INSERT INTO workflow_effort_overrides
                    (template_name, phase_name, effort_factor, jira_issue_type, is_blocking, effort_source, blocks_phases)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(template_name, phase_name) DO UPDATE SET
                    effort_factor=excluded.effort_factor,
                    jira_issue_type=excluded.jira_issue_type,
                    is_blocking=excluded.is_blocking,
                    effort_source=excluded.effort_source,
                    blocks_phases=excluded.blocks_phases
                """,
                (e["template_name"], e["phase_name"], float(e["effort_factor"]), jit, ib, es, bp),
            )
            count += 1
        self._conn.commit()
        return count
