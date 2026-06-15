"""
app/services/field_mapping.py
=============================
Pure mapping logic: one normalized Jira issue dict (see
JiraConnector.fetch_issues_for_migration) → Plane work-item payload.

Custom fields are routed by their user-annotated plane_target
(Settings → Field Map): start_date | target_date | priority | labels |
description (default, appended as an HTML table) | skip.
"""

from __future__ import annotations

import re

from app.utils.adf import adf_to_html, render_custom_fields_table

EXTERNAL_SOURCE = "jira"

_PRIORITY_MAP = {
    "highest": "urgent",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "lowest": "low",
}
_PLANE_PRIORITIES = {"urgent", "high", "medium", "low", "none"}

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# Jira statusCategory.key → Plane state group (for auto-create suggestions)
STATUS_CATEGORY_TO_GROUP = {
    "new": "backlog",
    "indeterminate": "started",
    "done": "completed",
}


def map_priority(name: str) -> str:
    return _PRIORITY_MAP.get((name or "").strip().lower(), "none")


_DATE_START_HINTS = ("start", "begin", "kickoff")
_DATE_END_HINTS = ("due", "end", "target", "finish", "deadline", "complete", "delivery")


def suggest_field_target(name: str, schema: dict | None) -> str:
    """Auto-suggest a Plane target for a Jira custom field from its name +
    schema. Plane CE has no native custom fields, so the realistic targets are
    a small set; anything unrecognised defaults to the description table (loss-
    less but not filterable). The user can always override the suggestion."""
    n = (name or "").lower()
    schema = schema or {}
    stype = (schema.get("type") or "").lower()
    custom = (schema.get("custom") or "").lower()
    items = (schema.get("items") or "").lower()

    if "sprint" in n or "gh-sprint" in custom:
        return "cycle"
    if "epic link" in n or custom.endswith((":epic-link", ":epic-label")) or "parent link" in n:
        return "skip"  # hierarchy is migrated via Plane's parent field
    if stype in ("date", "datetime"):
        if any(h in n for h in _DATE_START_HINTS):
            return "start_date"
        if any(h in n for h in _DATE_END_HINTS):
            return "target_date"
        return "description"  # ambiguous date — keep in the description table
    if stype == "array" and items in ("string", "option", "version", "component"):
        return "labels"
    if stype == "option":
        return "labels"
    return "description"


def suggest_state_group(status_category: str) -> str:
    return STATUS_CATEGORY_TO_GROUP.get((status_category or "").lower(), "backlog")


def map_relation(jira_type_name: str, side: str) -> str:
    """Map a Jira issue-link type + side to a Plane relation type.
    'blocks' is directional (outward = this issue blocks the other);
    duplicate is symmetric; everything else (relates, clones, custom) → relates_to."""
    n = (jira_type_name or "").lower()
    if "block" in n:
        return "blocking" if side == "outward" else "blocked_by"
    if "duplicat" in n:
        return "duplicate"
    return "relates_to"


def _parse_date(value: str) -> str | None:
    m = _DATE_RE.match((value or "").strip())
    return m.group(1) if m else None


def build_work_item_payload(issue: dict, ctx: dict) -> tuple[dict, list[str]]:
    """
    ctx keys:
        state_map       dict  jira status name (lower) -> plane state id
        member_map      dict  email (lower) -> plane user id
        label_ids       dict  resolved label name (lower) -> plane label id
                              (pre-resolved by the worker for every label the
                              issue carries, including plane_target='labels')
        parent_plane_id str|None
        push_assignees  bool
        push_labels     bool
    Returns (payload, warnings).
    """
    warnings: list[str] = []
    key = issue["key"]

    name = (issue.get("summary") or "").strip() or f"(no summary) {key}"
    if len(name) > 255:
        name = name[:252] + "..."
        warnings.append("summary truncated to 255 chars")

    payload: dict = {
        "name": name,
        "external_id": key,
        "external_source": EXTERNAL_SOURCE,
    }

    # ── State (resolved before the job started; required) ─────────────
    state_id = ctx["state_map"].get((issue.get("status_name") or "").lower())
    if state_id:
        payload["state"] = state_id
    else:
        warnings.append(f"no state mapping for Jira status {issue.get('status_name')!r}")

    # ── Priority + dates + labels, with custom-field overrides ────────
    priority = map_priority(issue.get("priority_name", ""))
    target_date = _parse_date(issue.get("duedate", ""))
    start_date = None
    extra_label_names: list[str] = []
    description_rows: list[tuple[str, str]] = []

    for label, info in (issue.get("custom") or {}).items():
        value, target = info["value"], info["plane_target"]
        if target == "skip":
            continue
        if target == "priority":
            candidate = value.strip().lower()
            mapped = candidate if candidate in _PLANE_PRIORITIES else map_priority(candidate)
            if mapped != "none" or candidate == "none":
                priority = mapped
            else:
                warnings.append(f"custom priority {value!r} not recognised — kept {priority!r}")
            continue
        if target == "start_date":
            start_date = _parse_date(value)
            if not start_date:
                warnings.append(f"could not parse start_date from {label!r}: {value!r}")
            continue
        if target == "target_date":
            parsed = _parse_date(value)
            if parsed:
                target_date = parsed
            else:
                warnings.append(f"could not parse target_date from {label!r}: {value!r}")
            continue
        if target == "labels":
            extra_label_names += [part.strip() for part in value.split(",") if part.strip()]
            continue
        if target == "cycle":
            # Sprint → Plane Cycle: handled by the worker's cycle pass after
            # all items exist. Skip here so sprint names never become labels
            # or description rows.
            continue
        description_rows.append((label, value))

    payload["priority"] = priority
    if start_date and target_date and start_date > target_date:
        warnings.append(f"start_date {start_date} after target_date {target_date} — dropped")
        start_date = None
    if start_date:
        payload["start_date"] = start_date
    if target_date:
        payload["target_date"] = target_date

    # ── Description: ADF + custom field table ─────────────────────────
    # Only send description_html when there's real content — Plane rejects an
    # empty string with "Invalid HTML passed".
    html = adf_to_html(issue.get("description_adf"))
    html += render_custom_fields_table(description_rows)
    if html.strip():
        payload["description_html"] = html

    # ── Assignee ───────────────────────────────────────────────────────
    if ctx.get("push_assignees", True):
        email = (issue.get("assignee_email") or "").lower()
        if email:
            member_id = ctx["member_map"].get(email)
            if member_id:
                payload["assignees"] = [member_id]
            else:
                warnings.append(f"assignee {email!r} is not a member of the Plane project")

    # ── Labels ─────────────────────────────────────────────────────────
    if ctx.get("push_labels", True):
        sprint_prefix = (ctx.get("sprint_label_prefix") or "").lower()
        label_ids = []
        for lname in list(issue.get("labels") or []) + extra_label_names:
            if sprint_prefix and lname.lower().startswith(sprint_prefix):
                continue  # sprint label → migrated as a cycle, not a label
            lid = ctx["label_ids"].get(lname.lower())
            if lid:
                label_ids.append(lid)
            else:
                warnings.append(f"label {lname!r} could not be resolved")
        if label_ids:
            payload["labels"] = label_ids

    # ── Parent ─────────────────────────────────────────────────────────
    if issue.get("parent_key"):
        if ctx.get("parent_plane_id"):
            payload["parent"] = ctx["parent_plane_id"]
        else:
            warnings.append(
                f"parent {issue['parent_key']} not found in Plane — imported without parent"
            )

    return payload, warnings


def collect_label_names(issues: list[dict], sprint_label_prefix: str = "") -> set[str]:
    """Every label name a set of issues will need (incl. plane_target='labels').
    Native labels starting with *sprint_label_prefix* are excluded — they're
    migrated as cycles, not labels."""
    prefix = (sprint_label_prefix or "").lower()
    names: set[str] = set()
    for issue in issues:
        for lname in issue.get("labels") or []:
            if not (prefix and lname.lower().startswith(prefix)):
                names.add(lname)
        for info in (issue.get("custom") or {}).values():
            if info["plane_target"] == "labels":
                names.update(p.strip() for p in info["value"].split(",") if p.strip())
    return names


def extract_cycle_name(issue: dict, sprint_label_prefix: str = "") -> str | None:
    """The sprint (cycle) name for an issue. Sources, in order:
    1. a custom field mapped to plane_target='cycle' (the Jira Sprint field);
    2. a native label starting with *sprint_label_prefix* (teams that track
       sprints with labels like 'spr7_todo').
    For the custom field, takes the LAST/current sprint and handles Jira's
    legacy string format."""
    for info in (issue.get("custom") or {}).values():
        if info.get("plane_target") == "cycle":
            name = _parse_sprint_name(info.get("value", ""))
            if name:
                return name
    prefix = (sprint_label_prefix or "").lower()
    if prefix:
        for lname in issue.get("labels") or []:
            if lname.lower().startswith(prefix):
                return lname
    return None


def _parse_sprint_name(value: str) -> str | None:
    if not value:
        return None
    legacy = re.findall(r"name=([^,\]]+)", value)  # legacy "...,name=Sprint 1,..."
    if legacy:
        return legacy[-1].strip()
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts[-1] if parts else None


def topo_sort(issues: list[dict]) -> tuple[list[dict], list[str]]:
    """Order issues so parents come before children (Kahn). Cycles are broken
    by dropping the offending parent edge; returns (ordered, warnings)."""
    warnings: list[str] = []
    by_key = {i["key"]: i for i in issues}
    children: dict[str, list[str]] = {k: [] for k in by_key}
    indegree = {k: 0 for k in by_key}
    for issue in issues:
        parent = issue.get("parent_key")
        if parent and parent in by_key:
            children[parent].append(issue["key"])
            indegree[issue["key"]] += 1

    queue = [k for k, d in indegree.items() if d == 0]
    ordered: list[dict] = []
    while queue:
        key = queue.pop(0)
        ordered.append(by_key[key])
        for child in children[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(ordered) < len(issues):  # cycle — append leftovers, drop their edges
        leftover = [k for k in by_key if indegree[k] > 0]
        warnings.append(f"parent cycle detected, broken at: {', '.join(leftover)}")
        ordered += [by_key[k] for k in leftover]
    return ordered, warnings
