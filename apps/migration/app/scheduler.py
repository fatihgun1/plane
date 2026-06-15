"""
app/scheduler.py
=================
Core scheduling engine for the Optimization Tool.

Public API (consumed by app/routes/schedule.py):

    SchedulerInput   – dataclass bundling all scheduling inputs
    SchedulerResult  – .tasks (list[ScheduledTask]), .failed_items (list[str])
    Scheduler        – main class; call Scheduler(inp).run() → SchedulerResult
"""

from __future__ import annotations

import logging
import yaml
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Set

from app.models import Consultant, Item, ScheduledTask

logger = logging.getLogger(__name__)

# Lower number = higher priority
_PRIORITY_ORDER: Dict[str, int] = {
    "Critical": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
}


# ---------------------------------------------------------------------------
# Internal workflow representation
# ---------------------------------------------------------------------------


@dataclass
class _Phase:
    order: int
    name: str
    required_roles: List[str]
    effort_factor: float
    is_blocking: bool


@dataclass
class _Workflow:
    template_name: str
    applies_to: List[str]
    phases: List[_Phase]


# ---------------------------------------------------------------------------
# Public input / output dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SchedulerInput:
    """All data required to produce a schedule."""

    items: List[Item]
    consultants: List[Consultant]
    # Mapping of template name → raw YAML string, as returned by
    # db.get_workflow_templates()
    workflow_templates: Dict[str, str]
    holidays: List[date]
    project_start: date
    max_parallel: int = 3
    # Effort overrides: {template_name: {phase_name: effort_factor}}
    effort_overrides: Optional[Dict[str, Dict[str, any]]] = None
    # Issue type → template name mapping from settings
    # e.g. {"Geliştirme": "development", "Story": "design"}
    type_map: Optional[Dict[str, str]] = None
    # Child relation settings: {template_name: {child_relation, link_type}}
    # e.g. {"development": {"child_relation": "linked", "link_type": "is parent task of"}}
    child_relation_map: Optional[Dict[str, Dict[str, str]]] = None


@dataclass
class SchedulerResult:
    """Output of Scheduler.run()."""

    tasks: List[ScheduledTask] = field(default_factory=list)
    # jira_key (or str(item.id) as fallback) for items that could not be
    # fully scheduled.
    failed_items: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """
    Greedy phase-by-phase scheduler.

    Algorithm
    ---------
    1. Parse all YAML workflow templates.
    2. Sort items by priority (Critical → High → Medium → Low → unknown).
    3. For each item:
       a. Match its issue_type to a workflow (falls back to the first available
          workflow if no exact match is found).
       b. For each phase (in order):
          - Calculate phase effort  = max(1, round(item.effort_days × effort_factor))
          - Pick the consultant whose role matches and who becomes free soonest.
          - Schedule start = max(project_start, consultant_free_date), advanced
            to the next working day.
          - Schedule end   = start + (effort_days working days).
          - Update consultant's next-free date.
          - If is_blocking, subsequent phases cannot start before this phase ends.
       c. If any phase has no matching consultant, the item is added to
          failed_items and the remaining phases are skipped.
    """

    def __init__(self, inp: SchedulerInput) -> None:
        self._inp = inp
        self._holidays: Set[date] = set(inp.holidays or [])
        self._workflows: List[_Workflow] = self._parse_workflows(inp.workflow_templates)
        # Slot-based timeline: consultant_id → {date: remaining_capacity}
        # Enables backfill — lower priority items fill gaps left by higher priority items
        self._slots: Dict[int, Dict[date, float]] = {
            c.id: {} for c in inp.consultants
        }
        # Running total of effort assigned per consultant (for load balancing)
        self._assigned_effort: Dict[int, float] = {
            c.id: 0.0 for c in inp.consultants
        }
        # Pre-compute fair-share caps per stream:
        # cap[stream_keyword] = total_stream_effort / num_consultants_in_stream
        self._stream_caps: Dict[str, float] = self._compute_stream_caps(inp)

    def _compute_stream_caps(self, inp: SchedulerInput) -> Dict[str, float]:
        """
        Pre-compute effort totals per stream and divide by consultant count.
        Returns {stream_keyword: cap_per_consultant}.
        A stream keyword is the first token of a consultant's stream field (lowercase).
        """
        import json as _j
        if not inp.effort_overrides:
            return {}

        # Map phase_stream → total effort across all items
        stream_effort: Dict[str, float] = {}
        for item in inp.items:
            for wf_name, phases in inp.effort_overrides.items():
                for phase_name, ov in phases.items():
                    if not isinstance(ov, dict):
                        continue
                    jit = (ov.get("jira_issue_type") or "").split(",")[0].strip().lower()
                    if not jit:
                        continue
                    # Compute expected effort for this item/phase
                    factor = float(ov.get("effort_factor") or 0.1)
                    effort_source = ov.get("effort_source")
                    base = 0.0
                    if effort_source:
                        extra: dict = {}
                        if getattr(item, "extra_fields", None):
                            try:
                                extra = _j.loads(item.extra_fields)
                            except Exception:
                                pass
                        raw = extra.get(effort_source)
                        if raw is None:
                            el = effort_source.lower()
                            for k, v in extra.items():
                                if k.lower() == el:
                                    raw = v
                                    break
                        if raw is not None:
                            try:
                                base = float(raw)
                            except Exception:
                                pass
                    if base <= 0:
                        base = max(1.0, item.effort_days or 1.0)
                    effort = max(0.1, round(base * factor * 10) / 10)
                    stream_effort[jit] = stream_effort.get(jit, 0.0) + effort

        # Map stream keyword → consultants (using first stream token)
        stream_cons: Dict[str, List[int]] = {}
        for c in inp.consultants:
            if not c.stream:
                continue
            for token in [t.strip().lower() for t in c.stream.split(",") if t.strip()]:
                if token not in stream_cons:
                    stream_cons[token] = []
                stream_cons[token].append(c.id)

        caps: Dict[str, float] = {}
        for stream, total in stream_effort.items():
            cons_list = stream_cons.get(stream, [])
            if cons_list:
                cap = round((total / len(cons_list)) * 10) / 10
                caps[stream] = cap
                logger.debug("Stream cap: %s = %.1fd total / %d cons = %.1fd each",
                             stream, total, len(cons_list), cap)
        return caps

    # ------------------------------------------------------------------
    # YAML parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_workflows(templates: Dict[str, str]) -> List[_Workflow]:
        workflows: List[_Workflow] = []
        for name, yaml_str in (templates or {}).items():
            if not yaml_str:
                continue
            try:
                data = yaml.safe_load(yaml_str)
                raw_phases = sorted(
                    data.get("phases", []), key=lambda p: p.get("order", 0)
                )
                phases = [
                    _Phase(
                        order=int(p["order"]),
                        name=str(p["name"]),
                        required_roles=list(p.get("required_roles", [])),
                        effort_factor=float(p.get("effort_factor", 0.1)),
                        is_blocking=bool(p.get("is_blocking", True)),
                    )
                    for p in raw_phases
                ]
                workflows.append(
                    _Workflow(
                        template_name=data.get("template_name", name),
                        applies_to=[str(t) for t in data.get("applies_to", [])],
                        phases=phases,
                    )
                )
                logger.debug("Loaded workflow template '%s' (%d phases)", name, len(phases))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse workflow template '%s': %s", name, exc)
        return workflows

    # ------------------------------------------------------------------
    # Date helpers
    # ------------------------------------------------------------------

    def _is_working_day(self, d: date) -> bool:
        """Return True if *d* is a weekday that is not a holiday."""
        return d.weekday() < 5 and d not in self._holidays

    def _next_working_day(self, d: date) -> date:
        """Advance *d* forward until it lands on a working day."""
        while not self._is_working_day(d):
            d += timedelta(days=1)
        return d

    def _add_working_days(self, start: date, days: float) -> date:
        """
        Return the date that is *days* working days after (and including)
        *start*.  Uses ceiling of fractional days for calendar calculation.
        """
        import math
        full_days = max(1, math.ceil(days))
        current = start
        counted = 0
        while counted < full_days:
            if self._is_working_day(current):
                counted += 1
            if counted < full_days:
                current += timedelta(days=1)
        return current

    # ------------------------------------------------------------------
    # Workflow matching
    # ------------------------------------------------------------------

    def _find_workflow(self, item: Item) -> Optional[_Workflow]:
        issue_type = (item.issue_type or "").strip()

        # 1. Check type_map from settings (highest priority)
        if self._inp.type_map:
            mapped_template = self._inp.type_map.get(issue_type)
            if mapped_template:
                for wf in self._workflows:
                    if wf.template_name == mapped_template:
                        return wf

        # 2. Check applies_to in YAML
        for wf in self._workflows:
            if issue_type in wf.applies_to:
                return wf

        # 3. No match → item is skipped (None = not scheduled)
        return None

    def _get_phase_effort(self, template_name: str, phase_name: str,
                          default_factor: float, item: "Item") -> float:
        """
        Return the effort in days for a phase, considering effort_source and effort_factor.

        Priority:
        1. If effort_source is set → look up that field in item.extra_fields, multiply by factor
        2. Otherwise → item.effort_days * factor (legacy behaviour)
        """
        import json as _json

        factor = default_factor
        effort_source: Optional[str] = None

        if self._inp.effort_overrides:
            override = self._inp.effort_overrides.get(template_name, {}).get(phase_name)
            if override is not None:
                if isinstance(override, dict):
                    f = override.get("effort_factor")
                    if f is not None:
                        factor = float(f)
                    effort_source = override.get("effort_source")
                else:
                    factor = float(override)

        if effort_source:
            # Try to read the source field from item's extra_fields JSON
            # Use case-insensitive lookup to handle label case mismatches
            extra: dict = {}
            if getattr(item, "extra_fields", None):
                try:
                    extra = _json.loads(item.extra_fields)
                except Exception:
                    pass
            # First try exact match, then case-insensitive
            raw_val = extra.get(effort_source)
            if raw_val is None:
                effort_source_lower = effort_source.lower()
                for k, v in extra.items():
                    if k.lower() == effort_source_lower:
                        raw_val = v
                        break
            if raw_val is not None:
                try:
                    base = float(raw_val)
                    if base <= 0:
                        # Zero effort for this phase → return 0 to signal skip
                        return 0.0
                    return max(0.1, base * factor)
                except (TypeError, ValueError):
                    pass
            # effort_source specified but not found → fall back to total effort
            logger.debug(
                "effort_source '%s' not found in item %s extra_fields — using total effort",
                effort_source, item.jira_key or item.id,
            )

        # Default: item.effort_days * factor
        total = max(1.0, item.effort_days or 1.0)
        return total * factor

    # ------------------------------------------------------------------
    # Consultant selection
    # ------------------------------------------------------------------

    def _earliest_available(self, consultant_id: int, earliest: date, effort: float) -> date:
        """
        Find the earliest working day >= earliest where this consultant has capacity.
        Days not in _slots default to 1.0 (full capacity available).
        Days in _slots have partial remaining capacity (0 < remaining < 1.0).
        """
        d = self._next_working_day(earliest)
        max_scan = 3650  # safety: never scan more than 10 years
        scanned = 0
        while scanned < max_scan:
            scanned += 1
            # Default remaining = 1.0 (day not in slots means no tasks assigned yet)
            remaining = self._slots[consultant_id].get(d, 1.0)
            if effort > 1.0:
                # Multi-day task: start anywhere there's any remaining capacity
                if remaining > 0.001:
                    return d
            else:
                # Sub-day or full-day: need enough remaining for the whole task
                if remaining >= effort - 0.001:
                    return d
            d = self._next_working_day(d + timedelta(days=1))
        # Fallback — should never reach here
        return self._next_working_day(earliest)

    def _pick_consultant(
        self, required_roles: List[str], earliest: date,
        responsible_module: Optional[str] = None,
        phase_stream: Optional[str] = None,
        effort: float = 0.1,
        cap_stream: Optional[str] = None,
    ) -> Optional[Consultant]:
        """
        Pick a consultant. Sort by load-balanced slot (backfill + fair-share cap aware).
        Priority:
        1. If phase_stream is set → technical specialists (ABAP, CPI/PO, Fiori)
        2. If responsible_module is set → functional module consultants
        3. Consultants whose role matches required_roles
        4. Any consultant (fallback)
        Sort key: (is_over_cap, total_assigned_effort, earliest_available_slot, id)
        This ensures load is distributed evenly — over-cap consultants only used when
        all in-group consultants are over cap (last resort).
        """
        all_consultants = list(self._inp.consultants)
        stream_key = (cap_stream or phase_stream or "").lower()
        cap = self._stream_caps.get(stream_key, float("inf"))

        def _stream_match(candidates: list, keyword: str) -> list:
            return [c for c in candidates if c.stream and keyword.lower() in c.stream.lower()]

        def _sort_balanced(candidates: list) -> list:
            return sorted(candidates, key=lambda c: (
                1 if self._assigned_effort[c.id] >= cap else 0,  # over-cap goes last
                self._assigned_effort[c.id],                      # least loaded first
                self._earliest_available(c.id, earliest, effort),# earliest slot second
                c.id,
            ))

        # 1. Technical phase stream takes priority (ABAP, CPI, Fiori etc.)
        if phase_stream:
            tech_candidates = _stream_match(all_consultants, phase_stream)
            if tech_candidates:
                return _sort_balanced(tech_candidates)[0]

        # 2. Responsible Module → functional consultant stream
        if responsible_module:
            module_candidates = _stream_match(all_consultants, responsible_module)
            if module_candidates:
                return _sort_balanced(module_candidates)[0]

        # 3. Try to match by required_roles
        if required_roles:
            role_candidates = [c for c in all_consultants if c.role in required_roles]
            if role_candidates:
                return _sort_balanced(role_candidates)[0]

        # 4. Fallback — any consultant
        if not all_consultants:
            return None
        return _sort_balanced(all_consultants)[0]

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> SchedulerResult:
        """Execute the greedy scheduling algorithm and return a SchedulerResult."""
        result = SchedulerResult()

        if not self._workflows:
            logger.error(
                "No workflow templates loaded — all items will be marked as failed."
            )
            for item in self._inp.items:
                result.failed_items.append(item.jira_key or str(item.id))
            return result

        # Sort by priority — item.priority is stored as int (1=Highest…5=Lowest)
        # or as a string name. Handle both.
        def _priority_key(item):
            p = item.priority
            if p is None:
                return 99
            if isinstance(p, int):
                return p  # 1=Highest, 2=High, 3=Medium, 4=Low, 5=Lowest
            # String form
            return _PRIORITY_ORDER.get(str(p), 99)

        items_sorted = sorted(self._inp.items, key=_priority_key)

        for item in items_sorted:
            wf = self._find_workflow(item)
            if wf is None:
                logger.warning(
                    "No matching workflow for item %s (issue_type='%s') — skipping.",
                    item.jira_key or item.id,
                    item.issue_type,
                )
                result.failed_items.append(item.jira_key or str(item.id))
                continue

            item_failed = False
            # Track per-phase end dates for explicit blocks_phases dependencies
            # {phase_name: end_date} — used to enforce "phase A blocks phase B" rules
            phase_end_dates: Dict[str, date] = {}
            # last_blocking_end kept for backwards compatibility when blocks_phases is not configured
            last_blocking_end: Optional[date] = None
            _has_any_blocks_phases = False  # will be set True if any phase has blocks_phases configured
            # Track consultant assigned per phase (for same-consultant rule)
            # FS consultant is reused for TS and Consultant Test
            item_assigned_consultants: Dict[str, int] = {}  # phase_name → consultant_id

            # Get Responsible Module from item extra_fields
            import json as _json
            responsible_module: Optional[str] = None
            if getattr(item, "extra_fields", None):
                try:
                    ef = _json.loads(item.extra_fields)
                    responsible_module = ef.get("Responsible Module")
                except Exception:
                    pass

            # Status → remaining effort multiplier
            # B_Not_Started / G_Ready = 1.0 (full effort)
            # H_In_Progress             = 0.5 (50% remaining)
            # A_Customer_Approval / N_Done = 0.0 (skip)
            _STATUS_MULTIPLIER: Dict[str, float] = {}
            _STATUS_DONE: set = set()

            # Build child key lookup: phase_name → child item jira_key
            # Also build phase_child_status_map: phase_name → child item Jira status
            # Priority:
            # 1. Linked Issues (child_relation=linked) → match via _issue_links in extra_fields
            # 2. Subtasks (child_relation=subtasks) → match via parent_key in extra_fields (future)
            # 3. Summary-based fallback (last resort)
            phase_child_key_map: dict = {}
            phase_child_status_map: dict = {}  # phase_name → child item Jira status string
            parent_jira_key = item.jira_key or ""
            parent_summary = (item.summary or "").strip().lower()

            # Get child relation config for this workflow
            cr_config = (self._inp.child_relation_map or {}).get(wf.template_name, {})
            child_relation = cr_config.get("child_relation", "linked")
            link_type_setting = (cr_config.get("link_type") or "").strip().lower()

            if self._inp.effort_overrides:
                wf_overrides = self._inp.effort_overrides.get(wf.template_name, {})
                for pname, ov in wf_overrides.items():
                    if not (isinstance(ov, dict) and ov.get("jira_issue_type")):
                        continue
                    phase_jit_list = [t.strip() for t in ov["jira_issue_type"].split(",") if t.strip()]
                    best_match = None

                    # Method 1: Linked Issues — check child's _issue_links for parent key
                    if child_relation == "linked" and parent_jira_key and link_type_setting:
                        for child_item in self._inp.items:
                            if child_item.issue_type not in phase_jit_list:
                                continue
                            if not child_item.jira_key or child_item.jira_key == parent_jira_key:
                                continue
                            # Check _issue_links in child's extra_fields
                            child_ef = {}
                            if getattr(child_item, "extra_fields", None):
                                try:
                                    child_ef = _json.loads(child_item.extra_fields)
                                except Exception:
                                    pass
                            links = child_ef.get("_issue_links", [])
                            for lnk in links:
                                lt = (lnk.get("link_type") or "").strip().lower()
                                lnk_key = lnk.get("key", "")
                                # Child has a link of type "is parent task of" pointing to parent
                                # OR parent has outward link of configured type to child
                                if lt == link_type_setting and lnk_key == parent_jira_key:
                                    best_match = child_item.jira_key
                                    break
                            if best_match:
                                break

                    # Method 2: Fallback — summary-based matching (last resort)
                    if not best_match and parent_summary:
                        for child_item in self._inp.items:
                            if child_item.issue_type not in phase_jit_list:
                                continue
                            if not child_item.jira_key or child_item.jira_key == parent_jira_key:
                                continue
                            child_summary = (child_item.summary or "").strip().lower()
                            if parent_summary in child_summary:
                                best_match = child_item.jira_key
                                break

                    if best_match:
                        phase_child_key_map[pname] = best_match
                        # Also record child item's Jira status
                        child_for_match = next(
                            (ci for ci in self._inp.items if ci.jira_key == best_match), None
                        )
                        if child_for_match:
                            phase_child_status_map[pname] = child_for_match.status or ""

            for phase in wf.phases:
                phase_effort_raw = self._get_phase_effort(
                    wf.template_name, phase.name, phase.effort_factor, item
                )
                # If phase effort is 0, skip this phase (e.g. CPI with 0 PI/PO effort)
                if phase_effort_raw == 0.0:
                    logger.debug(
                        "Skipping phase '%s' for item %s — effort source is 0",
                        phase.name, item.jira_key or item.id,
                    )
                    continue
                # Apply Jira status penalty to effort:
                # In Progress → 50% effort remaining; Done/Approved → skip phase
                child_jira_status = phase_child_status_map.get(phase.name, "")
                status_upper = child_jira_status.upper()
                if any(s in status_upper for s in ("DONE", "COMPLETE", "CLOSED", "N_DONE", "N_DONE")):
                    # Phase already complete — skip entirely
                    logger.debug("Skipping phase '%s' for item %s — child status: %s",
                                 phase.name, item.jira_key or item.id, child_jira_status)
                    continue
                elif any(s in status_upper for s in ("IN PROGRESS", "IN_PROGRESS", "H_IN", "H_IN_PROGRESS")):
                    # Phase in progress — apply 50% remaining effort
                    phase_effort_raw = phase_effort_raw * 0.5
                    logger.debug("Phase '%s' for item %s is in progress — effort halved to %.1f",
                                 phase.name, item.jira_key or item.id, phase_effort_raw)

                # Keep 1 decimal precision, no artificial rounding to 0.5
                effort_days = max(0.1, round(phase_effort_raw * 10) / 10)

                # Earliest start respects:
                # 1. project_start
                # 2. explicit blocks_phases dependencies from any previously scheduled phase (primary)
                # 3. Blocks Next (last_blocking_end) — only used as fallback when no blocks_phases configured
                earliest = self._next_working_day(self._inp.project_start)

                # Check explicit blocks_phases dependencies first
                if self._inp.effort_overrides:
                    wf_ov = self._inp.effort_overrides.get(wf.template_name, {})
                    # Detect if any phase in this workflow has blocks_phases configured
                    _has_any_blocks_phases = any(
                        isinstance(ov, dict) and ov.get("blocks_phases")
                        for ov in wf_ov.values()
                    )
                    phase_name_lower = phase.name.lower()
                    # Also get this phase's jira_issue_type for alternative matching
                    # e.g. CPI Development has jira_issue_type="PO", user may type "PO"
                    cur_phase_ov = wf_ov.get(phase.name, {})
                    cur_jit_lower = ""
                    if isinstance(cur_phase_ov, dict) and cur_phase_ov.get("jira_issue_type"):
                        cur_jit_lower = cur_phase_ov["jira_issue_type"].split(",")[0].strip().lower()
                    for prev_phase_name, prev_end in phase_end_dates.items():
                        prev_ov = wf_ov.get(prev_phase_name, {})
                        bp_str = prev_ov.get("blocks_phases") or "" if isinstance(prev_ov, dict) else ""
                        blocked_tokens = [b.strip().lower() for b in bp_str.split(",") if b.strip()]
                        # Match if:
                        # 1. exact match with phase name
                        # 2. token is substring of phase name (e.g. "ABAP" in "ABAP Development")
                        # 3. phase name starts with token
                        # 4. token matches the phase's jira_issue_type (e.g. "PO" = CPI Development's jit)
                        is_blocked = any(
                            phase_name_lower == tok or
                            phase_name_lower.startswith(tok) or
                            tok in phase_name_lower or
                            (cur_jit_lower and cur_jit_lower == tok)
                            for tok in blocked_tokens
                        )
                        if is_blocked:
                            after_dep = prev_end + timedelta(days=1)
                            earliest = self._next_working_day(max(earliest, after_dep))

                # Fallback: use Blocks Next sequential constraint only when
                # no blocks_phases are configured for this workflow template
                if not _has_any_blocks_phases and last_blocking_end is not None:
                    after_prev = last_blocking_end + timedelta(days=1)
                    earliest = self._next_working_day(max(earliest, after_prev))

                # Determine phase_stream from phase's jira_issue_type override
                # e.g. ABAP Development → jira_issue_type="ABAP" → stream="ABAP"
                phase_stream: Optional[str] = None
                if self._inp.effort_overrides:
                    pov = self._inp.effort_overrides.get(wf.template_name, {}).get(phase.name)
                    if isinstance(pov, dict) and pov.get("jira_issue_type"):
                        jit = pov["jira_issue_type"].split(",")[0].strip()
                        if jit:
                            phase_stream = jit

                # Apply same-consultant rule:
                # TS and Consultant Test must use the same consultant as FS
                _FS_PHASE = "Functional Specification (FS)"
                _SAME_AS_FS = {"Technical Specification (TS)", "Consultant Test"}
                forced_consultant_id = None
                if phase.name in _SAME_AS_FS and _FS_PHASE in item_assigned_consultants:
                    forced_consultant_id = item_assigned_consultants[_FS_PHASE]

                # Cap stream: the stream keyword for cap lookup
                # (for module phases like SD, use responsible_module; for tech phases use phase_stream)
                cap_stream = phase_stream or (responsible_module or "").lower()

                if forced_consultant_id:
                    # Forced phase (TS/CT same-consultant rule): bypass cap — item continuity wins
                    consultant = next(
                        (c for c in self._inp.consultants if c.id == forced_consultant_id),
                        None
                    )
                    if consultant is None:
                        consultant = self._pick_consultant(
                            phase.required_roles, earliest, responsible_module,
                            phase_stream, effort_days, cap_stream
                        )
                else:
                    # Normal selection: apply cap + load balancing
                    consultant = self._pick_consultant(
                        phase.required_roles, earliest, responsible_module,
                        phase_stream, effort_days, cap_stream
                    )
                if consultant is None:
                    logger.warning(
                        "No consultant available for roles %s "
                        "(item=%s, phase='%s') — item marked failed.",
                        phase.required_roles,
                        item.jira_key or item.id,
                        phase.name,
                    )
                    item_failed = True
                    break

                # Find the earliest slot with enough capacity (backfill-aware)
                start = self._earliest_available(consultant.id, earliest, effort_days)

                # Apply capacity scaling for reduced-capacity consultants
                base_cap = getattr(consultant, "capacity_pct", 100) or 100
                if base_cap < 100:
                    effort_days = max(0.1, round((effort_days / (base_cap / 100)) * 10) / 10)
                    start = self._earliest_available(consultant.id, earliest, effort_days)

                end = self._add_working_days(start, effort_days)

                # Look up child jira key for this phase
                child_jira_key = phase_child_key_map.get(phase.name)

                task = ScheduledTask(
                    id=None,
                    schedule_id=None,          # filled by the route after DB insert
                    item_id=item.id,
                    jira_child_key=child_jira_key,
                    phase_name=phase.name,
                    required_role=(
                        phase.required_roles[0] if phase.required_roles else ""
                    ),
                    effort_days=effort_days,
                    assigned_consultant_id=consultant.id,
                    planned_start=start,
                    planned_end=end,
                    is_locked=False,
                    score=0.0,
                    status="planned",
                )
                result.tasks.append(task)

                # Record this phase's consultant for same-consultant rule
                item_assigned_consultants[phase.name] = consultant.id
                # Update running effort total for load balancing
                self._assigned_effort[consultant.id] = round(
                    (self._assigned_effort[consultant.id] + effort_days) * 10
                ) / 10

                # Track this phase's end date for blocks_phases dependency resolution
                phase_end_dates[phase.name] = end

                # Update last_blocking_end for Blocks Next fallback
                if phase.is_blocking:
                    if last_blocking_end is None or end > last_blocking_end:
                        last_blocking_end = end

                # Consume capacity from the slot timeline (day-by-day)
                consumed = effort_days
                work_date = start
                iters = 0
                while consumed > 0.001 and iters < 1000:
                    iters += 1
                    remaining = self._slots[consultant.id].get(work_date, 1.0)
                    cap_today = min(remaining, consumed)
                    consumed = round((consumed - cap_today) * 1000) / 1000
                    new_remaining = round((remaining - cap_today) * 1000) / 1000
                    if new_remaining <= 0.001:
                        # Day fully consumed — remove so it defaults to 1.0 next time
                        self._slots[consultant.id].pop(work_date, None)
                    else:
                        # Day partially consumed — record remaining capacity
                        self._slots[consultant.id][work_date] = new_remaining
                    # Always advance to next working day when more to consume
                    if consumed > 0.001:
                        work_date = self._next_working_day(work_date + timedelta(days=1))

        logger.info(
            "Scheduling complete: %d tasks created, %d items failed.",
            len(result.tasks),
            len(result.failed_items),
        )
        return result
