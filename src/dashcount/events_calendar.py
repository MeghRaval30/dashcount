"""Scheduled market events ("someone told you in advance").

* FOMC decision days (statement at 14:00 ET) from the Federal Reserve calendar
  (fetched 2026-10-22; 2027 dates are tentative).
* 10:00 ET data releases, by rule: ISM Manufacturing (1st business day),
  ISM Services (3rd business day), Conference Board consumer confidence
  (last Tuesday). Approximate by design: a wrong guess only blocks 12 minutes.
"""

from __future__ import annotations

import datetime as dt

from .timeutil import calendar

FOMC_DECISION_DAYS = {
    dt.date.fromisoformat(d) for d in [
        # 2024
        "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
        "2024-09-18", "2024-11-07", "2024-12-18",
        # 2025
        "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
        "2025-09-17", "2025-10-29", "2025-12-10",
        # 2026
        "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
        "2026-09-16", "2026-10-28", "2026-12-09",
        # 2027 (tentative)
        "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28",
        "2027-09-15", "2027-10-27", "2027-12-08",
    ]
}


def is_fomc_day(day: dt.date) -> bool:
    return day in FOMC_DECISION_DAYS


def _business_days_of_month(day: dt.date) -> list[dt.date]:
    first = day.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return calendar().sessions(first, nxt - dt.timedelta(days=1))


def ten_am_releases(day: dt.date) -> list[str]:
    """Names of 10:00 ET releases expected on this day."""
    out: list[str] = []
    bdays = _business_days_of_month(day)
    if bdays and day == bdays[0]:
        out.append("ISM Manufacturing")
    if len(bdays) >= 3 and day == bdays[2]:
        out.append("ISM Services")
    if day.weekday() == 1:  # Tuesday
        last_tue = max(d for d in bdays if d.weekday() == 1) if any(d.weekday() == 1 for d in bdays) else None
        if day == last_tue:
            out.append("Consumer Confidence")
    return out
