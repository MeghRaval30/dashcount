"""Earnings calendar from Nasdaq's public calendar endpoint, cached on disk.

Used only as an obstacle map: stocks reporting near today are removed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

URL = "https://api.nasdaq.com/api/calendar/earnings"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json",
}


class EarningsCalendar:
    def __init__(self, cache_dir: Path, offline: bool = False) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline

    def symbols_on(self, day: dt.date) -> set[str] | None:
        """Symbols reporting on `day`, or None if unknown (fetch failed / offline)."""
        f = self.cache_dir / f"{day.isoformat()}.json"
        if f.exists():
            return set(json.loads(f.read_text()))
        if self.offline:
            return None
        try:
            r = httpx.get(URL, params={"date": day.isoformat()}, headers=HEADERS, timeout=20)
            r.raise_for_status()
            rows = (r.json().get("data") or {}).get("rows") or []
            syms = sorted({row["symbol"].strip().upper() for row in rows if row.get("symbol")})
        except Exception as e:
            log.warning("earnings calendar unavailable for %s: %s", day, e)
            return None
        # only cache days that are in the past or today (future lists can change)
        if day <= dt.date.today():
            f.write_text(json.dumps(syms))
        return set(syms)

    def near(self, days: list[dt.date]) -> tuple[set[str], bool]:
        """Union of reporters over several days, and whether every day was known."""
        out: set[str] = set()
        known = True
        for d in days:
            s = self.symbols_on(d)
            if s is None:
                known = False
            else:
                out |= s
        return out, known
