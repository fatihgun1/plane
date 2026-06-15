"""
app/calendar_engine.py — Working-day calendar with holiday and capacity awareness.

Key function:
    add_working_days(start_date, n_days, holidays, overrides) -> end_date
    get_available_days(consultant_id, from_date, to_date, db) -> float
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5  # Mon–Fri


def _holidays_set(holidays: list[dict]) -> set[date]:
    """Flatten holiday ranges into a set of individual dates."""
    result: set[date] = set()
    for h in holidays:
        start = _parse(h["start_date"])
        end = _parse(h["end_date"])
        cursor = start
        while cursor <= end:
            result.add(cursor)
            cursor += timedelta(days=1)
    return result


def _parse(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


# ---------------------------------------------------------------------------
# Public API — standalone functions
# ---------------------------------------------------------------------------

def add_working_days(
    start: str | date,
    n_days: float,
    holiday_dates: Optional[set[date]] = None,
) -> date:
    """
    Return the date that is `n_days` working days after `start`.
    `start` itself counts as day 1 if it is a working day.
    Fractional days are rounded up to the nearest whole day for end-date calc.
    """
    holiday_dates = holiday_dates or set()
    current = _parse(start)
    remaining = int(-(-n_days // 1))  # ceil

    while remaining > 0:
        if _is_weekday(current) and current not in holiday_dates:
            remaining -= 1
        if remaining > 0:
            current += timedelta(days=1)
    return current


def working_days_between(
    start: str | date,
    end: str | date,
    holiday_dates: Optional[set[date]] = None,
) -> float:
    """Count working days from start to end (inclusive)."""
    holiday_dates = holiday_dates or set()
    s, e = _parse(start), _parse(end)
    if s > e:
        return 0.0
    count = 0
    cursor = s
    while cursor <= e:
        if _is_weekday(cursor) and cursor not in holiday_dates:
            count += 1
        cursor += timedelta(days=1)
    return float(count)


def get_available_capacity(
    consultant_id: int,
    from_date: str | date,
    to_date: str | date,
    default_daily_capacity: float,
    holiday_dates: Optional[set[date]] = None,
    overrides: Optional[list[dict]] = None,
) -> float:
    """
    Sum of available capacity-days for a consultant over a date range,
    respecting holidays and any capacity_override rows.

    overrides: list of dicts with keys consultant_id, start_date, end_date, capacity
    """
    holiday_dates = holiday_dates or set()
    overrides = overrides or []

    s, e = _parse(from_date), _parse(to_date)
    total = 0.0
    cursor = s

    # Build override lookup: date -> capacity
    override_map: dict[date, float] = {}
    for ov in overrides:
        if ov["consultant_id"] != consultant_id:
            continue
        ov_start = _parse(ov["start_date"])
        ov_end = _parse(ov["end_date"])
        d = ov_start
        while d <= ov_end:
            override_map[d] = float(ov["capacity"])
            d += timedelta(days=1)

    while cursor <= e:
        if _is_weekday(cursor) and cursor not in holiday_dates:
            cap = override_map.get(cursor, default_daily_capacity)
            total += cap
        cursor += timedelta(days=1)

    return total


def build_holiday_set_from_db(db) -> set[date]:
    """Load all holidays from the DB and return as a set of dates."""
    rows = db.execute("SELECT start_date, end_date FROM holidays").fetchall()
    holidays = [{"start_date": r["start_date"], "end_date": r["end_date"]} for r in rows]
    return _holidays_set(holidays)


def next_working_day(
    from_date: str | date,
    holiday_dates: Optional[set[date]] = None,
) -> date:
    """Return the first working day >= from_date."""
    holiday_dates = holiday_dates or set()
    d = _parse(from_date)
    while not (_is_weekday(d) and d not in holiday_dates):
        d += timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# CalendarEngine — stateful class used by SchedulerEngine
# ---------------------------------------------------------------------------

class CalendarEngine:
    """
    Stateful calendar: wraps a holiday_dates set and exposes
    ``is_working_day()`` for use by SchedulerEngine.
    """

    def __init__(self, holiday_dates: Optional[set[date]] = None) -> None:
        self.holiday_dates: set[date] = holiday_dates or set()

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_holiday_list(cls, holidays: list[dict]) -> "CalendarEngine":
        """Build from a list of {start_date, end_date} dicts (DB/API format)."""
        return cls(_holidays_set(holidays))

    @classmethod
    def from_db(cls, db) -> "CalendarEngine":
        """Build directly from the DB connection."""
        return cls(build_holiday_set_from_db(db))

    # ------------------------------------------------------------------
    # Core predicate
    # ------------------------------------------------------------------

    def is_working_day(self, d: date) -> bool:
        """Return True if *d* is a weekday and not in the holiday set."""
        return _is_weekday(d) and d not in self.holiday_dates
