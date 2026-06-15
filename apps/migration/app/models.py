"""
app/models.py — Pure-Python dataclasses for all domain objects.

No ORM — models are plain dataclasses; DB layer converts rows ↔ dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# Consultants
# ---------------------------------------------------------------------------

@dataclass
class Consultant:
    id: Optional[int]
    name: str
    role: Optional[str] = None
    stream: Optional[str] = None
    capacity_pct: float = 100.0
    active: bool = True
    level: Optional[str] = "Consultant"  # Consultant | Senior Consultant | Expert Consultant


@dataclass
class ConsultantAvailability:
    """Date-bounded capacity override for a consultant (spec 3.2a)."""
    id: Optional[int]
    consultant_id: int
    start_date: date
    end_date: date
    capacity_pct: float = 100.0   # 0–100 override for this window
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Work items (imported from Jira or flat file)
# ---------------------------------------------------------------------------

@dataclass
class Item:
    id: Optional[int]
    jira_key: Optional[str] = None
    summary: Optional[str] = None
    issue_type: Optional[str] = None
    module: Optional[str] = None
    stream: Optional[str] = None
    priority: Optional[int] = None
    effort_days: Optional[float] = None
    status: Optional[str] = None
    epic_key: Optional[str] = None
    imported_at: Optional[str] = None
    extra_fields: Optional[str] = None   # JSON blob: {field_label: value}


# ---------------------------------------------------------------------------
# Workflow templates (DB row model — one row per phase)
# ---------------------------------------------------------------------------

@dataclass
class WorkflowTemplate:
    id: Optional[int]
    template_name: str
    issue_type: str
    phase_order: int
    phase_name: str
    required_role: str
    effort_factor: float = 1.0
    is_blocking: bool = True


# ---------------------------------------------------------------------------
# Workflow phases / templates (in-memory, loaded from YAML for scheduling)
# ---------------------------------------------------------------------------

@dataclass
class WorkflowPhase:
    """Single phase entry within a Workflow, loaded from YAML."""
    name: str
    order: int
    default_effort_days: int
    effort_scale: Optional[float] = None   # multiplier vs item.effort_days
    required_roles: list[str] = field(default_factory=list)
    is_blocking: bool = True


@dataclass
class Workflow:
    """
    Aggregated workflow object — one per YAML file.
    Stored in ``app.config["WORKFLOWS"]`` keyed by template_name.
    Passed directly to SchedulerEngine.run().
    """
    template_name: str
    applies_to: list[str] = field(default_factory=list)
    phases: list[WorkflowPhase] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Item dependencies
# ---------------------------------------------------------------------------

@dataclass
class ItemDependency:
    id: Optional[int]
    blocking_item_jira_key: str
    blocked_item_jira_key: str
    dependency_type: str = "blocks"


# ---------------------------------------------------------------------------
# Scheduled tasks — one row per (item × phase)
# ---------------------------------------------------------------------------

@dataclass
class ScheduledTask:
    id: Optional[int]
    item_id: int
    phase_name: str
    schedule_id: Optional[int] = None          # FK → schedules.id
    planned_start: Optional[date] = None       # date object (not string)
    planned_end: Optional[date] = None         # date object (not string)
    effort_days: Optional[float] = None
    jira_child_key: Optional[str] = None
    required_role: Optional[str] = None
    assigned_consultant_id: Optional[int] = None
    is_locked: bool = False
    score: Optional[float] = None
    status: str = "Scheduled"


# ---------------------------------------------------------------------------
# Schedule (metadata for a scheduler run)
# ---------------------------------------------------------------------------

@dataclass
class Schedule:
    id: Optional[int]
    name: str
    workflow_template: str
    project_start: Optional[date] = None
    max_parallel: int = 3
    created_at: Optional[str] = None
    status: str = "pending"
