"""Market data access.

`MarketData` is what the level map needs. Two implementations:
* `AlpacaData` (live and the downloader) talks to Alpaca's REST API;
* `StoreData` (practice) reads the local Parquet store the downloader filled.
Both return the same DataFrame shapes, so the level map is identical in both.

DataFrame shapes
* daily:   symbol, date (datetime.date), open, high, low, close, volume
* minutes: symbol, ts (float epoch seconds, bar start), open, high, low, close, volume
* assets:  symbol, easy_to_borrow, shortable
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Protocol

import pandas as pd

from ..config import data_dir

# Trade-condition codes that are not normal, in-sequence regular-hours prints.
BAD_CONDITIONS = {"W", "Z", "T", "U", "4", "C", "N", "R", "7", "H", "V", "P", "9"}

DAILY_COLS = ["symbol", "date", "open", "high", "low", "close", "volume"]
MINUTE_COLS = ["symbol", "ts", "open", "high", "low", "close", "volume"]


class MarketData(Protocol):
    def daily(self, symbols: list[str], start: dt.date, end: dt.date, feed: str) -> pd.DataFrame: ...

    def minutes(self, symbols: list[str], start: float, end: float, feed: str) -> pd.DataFrame: ...

    def assets(self) -> pd.DataFrame: ...

    def spread_bps(self, symbols: list[str], day: dt.date) -> dict[str, float]: ...


class Store:
    """Paths of the local historical store."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else data_dir() / "store"

    def daily_path(self, feed: str, symbol: str) -> Path:
        return self.root / "daily" / f"feed={feed}" / f"{symbol}.parquet"

    def minute_path(self, feed: str, day: dt.date) -> Path:
        return self.root / "minute" / f"feed={feed}" / f"{day.isoformat()}.parquet"

    def ticks_dir(self, feed: str, day: dt.date) -> Path:
        return self.root / "ticks" / f"feed={feed}" / day.isoformat()

    def trades_path(self, feed: str, day: dt.date, symbol: str) -> Path:
        return self.ticks_dir(feed, day) / f"{symbol}.trades.parquet"

    def quotes_path(self, feed: str, day: dt.date, symbol: str) -> Path:
        return self.ticks_dir(feed, day) / f"{symbol}.quotes.parquet"

    def assets_path(self) -> Path:
        return self.root / "assets.parquet"

    def earnings_dir(self) -> Path:
        return self.root / "earnings"

    def plans_dir(self) -> Path:
        return self.root / "plans"

    def spreads_path(self, day: dt.date) -> Path:
        return self.root / "spreads" / f"{day.isoformat()}.json"


class StoreData:
    """MarketData backed by the local store (practice runs; no network)."""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()
        self._daily_cache: dict[tuple[str, str], pd.DataFrame] = {}
        self._minute_cache: dict[tuple[str, dt.date], pd.DataFrame] = {}

    def daily(self, symbols, start, end, feed):
        frames = []
        for s in symbols:
            key = (feed, s)
            if key not in self._daily_cache:
                p = self.store.daily_path(feed, s)
                self._daily_cache[key] = pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=DAILY_COLS)
            df = self._daily_cache[key]
            if len(df):
                frames.append(df[(df["date"] >= start) & (df["date"] <= end)])
        if not frames:
            return pd.DataFrame(columns=DAILY_COLS)
        return pd.concat(frames, ignore_index=True)

    def minutes(self, symbols, start, end, feed):
        from ..timeutil import to_et

        d0, d1 = to_et(start).date(), to_et(end).date()
        frames = []
        day = d0
        want = set(symbols)
        while day <= d1:
            key = (feed, day)
            if key not in self._minute_cache:
                p = self.store.minute_path(feed, day)
                self._minute_cache[key] = pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=MINUTE_COLS)
                if len(self._minute_cache) > 64:
                    self._minute_cache.pop(next(iter(self._minute_cache)))
            df = self._minute_cache[key]
            if len(df):
                frames.append(df[df["symbol"].isin(want) & (df["ts"] >= start) & (df["ts"] < end)])
            day += dt.timedelta(days=1)
        if not frames:
            return pd.DataFrame(columns=MINUTE_COLS)
        return pd.concat(frames, ignore_index=True)

    def assets(self):
        p = self.store.assets_path()
        if p.exists():
            return pd.read_parquet(p)
        return pd.DataFrame(columns=["symbol", "easy_to_borrow", "shortable"])

    def spread_bps(self, symbols, day, *_sampling):
        import json

        p = self.store.spreads_path(day)
        if not p.exists():
            return {}
        d = json.loads(p.read_text())
        return {s: float(d[s]) for s in symbols if s in d}
