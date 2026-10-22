"""Time helpers. Internally every timestamp is a float: seconds since the UTC epoch.

Exchange-local (New York) times only appear at the edges: config strings like
"09:45", the market calendar, and Kronos's time features.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


def hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def et_ts(day: dt.date, t: str | dt.time) -> float:
    """Epoch seconds of a New York wall-clock time on a given date."""
    tt = hhmm(t) if isinstance(t, str) else t
    return dt.datetime.combine(day, tt, tzinfo=ET).timestamp()


def to_et(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=ET)


def et_minutes(ts: float) -> int:
    """Minutes since midnight New York time."""
    d = to_et(ts)
    return d.hour * 60 + d.minute


def fmt_et(ts: float) -> str:
    return to_et(ts).strftime("%Y-%m-%d %H:%M:%S ET")


def minute_floor(ts: float) -> float:
    return float(int(ts // 60) * 60)


def parse_date(s: str | dt.date) -> dt.date:
    return s if isinstance(s, dt.date) else dt.date.fromisoformat(s)


@dataclass(frozen=True)
class Session:
    day: dt.date
    open: float      # epoch seconds
    close: float     # epoch seconds
    half_day: bool

    def at(self, t: str) -> float:
        return et_ts(self.day, t)

    @property
    def minutes_long(self) -> int:
        return int((self.close - self.open) // 60)


class MarketCalendar:
    """NYSE sessions via exchange_calendars (handles holidays and early closes)."""

    def __init__(self) -> None:
        import exchange_calendars as xcals

        self._cal = xcals.get_calendar("XNYS")

    def is_session(self, day: dt.date) -> bool:
        try:
            return bool(self._cal.is_session(pd.Timestamp(day)))
        except Exception:
            return False

    def session(self, day: dt.date) -> Session:
        ts = pd.Timestamp(day)
        o = self._cal.session_open(ts).timestamp()
        c = self._cal.session_close(ts).timestamp()
        return Session(day=day, open=o, close=c, half_day=(c - o) < 6 * 3600)

    def sessions(self, start: dt.date, end: dt.date) -> list[dt.date]:
        idx = self._cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
        return [d.date() for d in idx]

    def previous_sessions(self, day: dt.date, n: int) -> list[dt.date]:
        """The n sessions strictly before `day`, oldest first."""
        ts = pd.Timestamp(day)
        if self._cal.is_session(ts):
            prev = self._cal.sessions_window(ts, -(n + 1))[:-1]
        else:
            last = self._cal.date_to_session(ts, direction="previous")
            prev = self._cal.sessions_window(last, -n)
        return [d.date() for d in prev]

    def next_session(self, day: dt.date) -> dt.date:
        ts = pd.Timestamp(day)
        return self._cal.date_to_session(ts + pd.Timedelta(days=1), direction="next").date()


@lru_cache(maxsize=1)
def calendar() -> MarketCalendar:
    return MarketCalendar()


def third_friday(day: dt.date) -> bool:
    """Monthly options expiry (third Friday)."""
    return day.weekday() == 4 and 15 <= day.day <= 21
