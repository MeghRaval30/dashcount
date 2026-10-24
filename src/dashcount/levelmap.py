"""Level map: built before the open ("someone told you in advance").

Picks today's watchlist (top in-play stocks + SPY/QQQ), removes earnings names,
and marks blocked time windows. Uses only data available at the cutoff time,
identically live and in practice.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ROOT, Config
from .data import MarketData
from .earnings import EarningsCalendar
from .events_calendar import is_fomc_day, ten_am_releases
from .models import Bar, WatchItem
from .timeutil import Session, calendar, et_ts, third_friday

log = logging.getLogger(__name__)

UNIVERSE_CSV = ROOT / "config" / "universe.csv"
DEFAULT_FEED_SHARE = 0.025


def load_universe(path: Path = UNIVERSE_CSV) -> pd.DataFrame:
    return pd.read_csv(path)


@dataclass
class DayPlan:
    day: dt.date
    session: Session
    watchlist: list[WatchItem]
    blocked: list[tuple[float, float, str]]
    last_entry: float
    flatten_at: float
    flags: list[str] = field(default_factory=list)
    earnings_known: bool = True
    removed_earnings: list[str] = field(default_factory=list)
    history: dict[str, list[Bar]] = field(default_factory=dict)
    ranking: list[dict] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return [w.symbol for w in self.watchlist]

    def item(self, symbol: str) -> WatchItem | None:
        return next((w for w in self.watchlist if w.symbol == symbol), None)

    def blocked_reason(self, ts: float) -> str | None:
        if ts >= self.last_entry:
            return "after_last_entry"
        for a, b, why in self.blocked:
            if a <= ts < b:
                return why
        return None

    def summary(self) -> dict:
        return {
            "day": self.day.isoformat(), "half_day": self.session.half_day,
            "watchlist": [w.symbol for w in self.watchlist if not w.is_market],
            "flags": self.flags, "earnings_known": self.earnings_known,
            "removed_earnings": self.removed_earnings,
            "blocked": [(round(a), round(b), w) for a, b, w in self.blocked],
            "ranking": self.ranking[:40],
        }


def _zs(x: pd.Series) -> pd.Series:
    x = x.replace([np.inf, -np.inf], np.nan)
    sd = x.std()
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=x.index)
    return ((x - x.mean()) / sd).fillna(0.0)


def daily_stats(daily: pd.DataFrame, atr_period: int = 14) -> pd.DataFrame:
    rows = []
    for sym, g in daily.groupby("symbol"):
        g = g.sort_values("date")
        if len(g) < atr_period + 1:
            continue
        pc = g["close"].shift(1)
        tr = pd.concat([g["high"] - g["low"], (g["high"] - pc).abs(), (g["low"] - pc).abs()], axis=1).max(axis=1)
        last20 = g.tail(20)
        rows.append({
            "symbol": sym, "prev_close": float(g["close"].iloc[-1]),
            "atr": float(tr.tail(atr_period).mean()),
            "avg_volume": float(last20["volume"].mean()),
            "avg_dollar_volume": float((last20["close"] * last20["volume"]).mean()),
        })
    return pd.DataFrame(rows)


def volume_profiles(minutes: pd.DataFrame, sessions: list[Session]) -> dict[str, list[float]]:
    """Median volume per 5-minute bucket of the regular session over full days."""
    full = [s for s in sessions if not s.half_day]
    if minutes.empty or not full:
        return {}
    out: dict[str, list[float]] = {}
    per_day = []
    for s in full:
        m = minutes[(minutes["ts"] >= s.open) & (minutes["ts"] < s.close)]
        if m.empty:
            continue
        b = ((m["ts"] - s.open) // 300).astype(int).clip(0, 77)
        agg = m.assign(bucket=b).groupby(["symbol", "bucket"])["volume"].sum()
        per_day.append(agg)
    if not per_day:
        return {}
    allv = pd.concat(per_day, axis=1).fillna(0.0)
    med = allv.median(axis=1)
    for sym, g in med.groupby(level=0):
        prof = np.zeros(78)
        for (_, bucket), v in g.items():
            prof[int(bucket)] = v
        out[sym] = prof.tolist()
    return out


def to_bars(minutes: pd.DataFrame) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    if minutes.empty:
        return out
    for sym, g in minutes.sort_values("ts").groupby("symbol"):
        out[sym] = [Bar(sym, float(r.ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
                    for r in g.itertuples(index=False)]
    return out


class LevelMap:
    def __init__(self, cfg: Config, data: MarketData, earnings: EarningsCalendar | None,
                 universe: pd.DataFrame | None = None) -> None:
        self.cfg = cfg
        self.data = data
        self.earnings = earnings
        self.universe = universe if universe is not None else load_universe()
        self.cal = calendar()

    def build(self, day: dt.date, feed: str) -> DayPlan:
        cfg, lm = self.cfg, self.cfg.level_map
        session = self.cal.session(day)
        prev = self.cal.previous_sessions(day, 30)
        mkt = cfg.universe.market_symbols
        syms = sorted(set(self.universe["symbol"]))
        sector = dict(zip(self.universe["symbol"], self.universe["sector"]))

        # 1. daily context
        daily = self.data.daily(syms + mkt, prev[0], prev[-1], "sip")
        stats = daily_stats(daily, cfg.features.atr_daily_period)
        if stats.empty:
            raise RuntimeError(f"no daily data before {day}")
        stats = stats[(stats["prev_close"] > cfg.universe.min_price)]
        cands = stats[(stats["avg_dollar_volume"] >= cfg.universe.min_avg_dollar_volume) & (~stats["symbol"].isin(mkt))].copy()

        # 2. pre-market up to the cutoff
        pm = self.data.minutes(list(cands["symbol"]), et_ts(day, "04:00"), et_ts(day, lm.data_cutoff), "sip")
        if not pm.empty:
            pm = pm.sort_values("ts")
            last = pm.groupby("symbol")["close"].last()
            vol = pm.groupby("symbol")["volume"].sum()
        else:
            last, vol = pd.Series(dtype=float), pd.Series(dtype=float)
        cands["pm_price"] = cands["symbol"].map(last)
        cands["pm_volume"] = cands["symbol"].map(vol).fillna(0.0)
        cands["gap_pct"] = ((cands["pm_price"] / cands["prev_close"] - 1) * 100).fillna(0.0)
        cands = cands[cands["gap_pct"].abs() <= 30]                    # splits / bad data
        cands["atr_pct"] = cands["atr"] / cands["prev_close"] * 100
        cands["pm_rel"] = cands["pm_volume"] / cands["avg_volume"].clip(lower=1)
        cands["score"] = (
            _zs(cands["gap_pct"].abs() / cands["atr_pct"].clip(lower=0.1))
            + _zs(np.log1p(cands["pm_rel"] * 100))
            + _zs(cands["atr_pct"])
        )

        # 3. earnings
        window = [prev[-1], day, self.cal.next_session(day)] if lm.earnings_window_days >= 1 else [day]
        removed: list[str] = []
        known = True
        if self.earnings is not None:
            reporters, known = self.earnings.near(window)
            removed = sorted(set(cands["symbol"]) & reporters)
            cands = cands[~cands["symbol"].isin(reporters)]
        else:
            known = False
        if not known:
            cands = cands[cands["gap_pct"].abs() <= lm.earnings_unknown_gap_pct]

        cands = cands.sort_values("score", ascending=False)
        top = cands.head(cfg.universe.watchlist_size)
        chosen = list(top["symbol"]) + [m for m in mkt if m in set(stats["symbol"])]

        # 4. profiles, feed share, borrow flags, Kronos history
        prof_sessions = [self.cal.session(d) for d in prev[-cfg.features.volume_profile_days:]]
        mins = self.data.minutes(chosen, prof_sessions[0].open, prof_sessions[-1].close, "sip")
        profiles = volume_profiles(mins, prof_sessions)
        share = self._feed_shares(chosen, prev[-cfg.features.feed_share_days:], feed)
        assets = self.data.assets()
        etb = dict(zip(assets["symbol"], assets["easy_to_borrow"])) if len(assets) else {}
        last_s = self.cal.session(prev[-1])
        hist = to_bars(self.data.minutes(chosen, last_s.open, last_s.close, feed))
        try:
            spreads = self.data.spread_bps([c for c in chosen if c not in mkt], prev[-1],
                                           tuple(cfg.features.spread_sample_windows),
                                           cfg.features.spread_sample_minutes) if hasattr(self.data, "spread_bps") else {}
        except Exception as e:
            log.warning("NBBO spread sample unavailable (%s); using the default", e)
            spreads = {}

        st = stats.set_index("symbol")
        items = []
        for sym in chosen:
            row = top[top["symbol"] == sym]
            items.append(WatchItem(
                symbol=sym, sector=str(sector.get(sym, "ETF" if sym in mkt else "Unknown")),
                easy_to_borrow=bool(etb.get(sym, False)) if etb else False,
                prev_close=float(st.loc[sym, "prev_close"]), atr_daily=float(st.loc[sym, "atr"]),
                avg_daily_volume=float(st.loc[sym, "avg_volume"]),
                gap_pct=float(row["gap_pct"].iloc[0]) if len(row) else 0.0,
                premarket_rel_volume=float(row["pm_rel"].iloc[0]) if len(row) else 0.0,
                score=float(row["score"].iloc[0]) if len(row) else 0.0,
                feed_share=share.get(sym, 1.0 if feed == "sip" else DEFAULT_FEED_SHARE),
                volume_profile=profiles.get(sym, []),
                is_market=sym in mkt,
                nbbo_spread_bps=spreads.get(sym),
            ))

        blocked, last_entry, flatten_at, flags = self._windows(session)
        ranking = cands.head(40)[["symbol", "score", "gap_pct", "atr_pct", "pm_rel"]].round(3).to_dict("records")
        return DayPlan(day, session, items, blocked, last_entry, flatten_at, flags, known, removed,
                       {s: hist.get(s, []) for s in chosen}, ranking)

    def _feed_shares(self, symbols: list[str], days: list[dt.date], feed: str) -> dict[str, float]:
        if feed == "sip" or not days:
            return {s: 1.0 for s in symbols}
        sip = self.data.daily(symbols, days[0], days[-1], "sip").groupby("symbol")["volume"].sum()
        iex = self.data.daily(symbols, days[0], days[-1], "iex").groupby("symbol")["volume"].sum()
        out = {}
        for s in symbols:
            if s in sip and s in iex and sip[s] > 0:
                out[s] = float(min(max(iex[s] / sip[s], 0.001), 1.0))
        return out

    def _windows(self, s: Session) -> tuple[list, float, float, list[str]]:
        lm = self.cfg.level_map
        day = s.day
        blocked = [(s.open, s.open + lm.observe_only_minutes * 60, "observe_only")]
        if s.half_day:
            last_entry, flatten_at = s.at(lm.half_day_last_entry), s.at(lm.half_day_flatten_at)
        else:
            last_entry, flatten_at = s.at(lm.last_entry), s.at(lm.flatten_at)
        flags = []
        for name in ten_am_releases(day):
            t = s.at("10:00")
            blocked.append((t - lm.release_block_before_min * 60, t + lm.release_block_after_min * 60, f"release:{name}"))
            flags.append(f"10:00 {name}")
        if is_fomc_day(day):
            blocked.append((s.at(lm.fomc_block[0]), s.at(lm.fomc_block[1]), "fomc"))
            flags.append("FOMC day")
        if third_friday(day):
            flags.append("monthly options expiry")
        if day.weekday() == 0:
            flags.append("Monday")
        if day.weekday() == 4:
            flags.append("Friday")
        if s.half_day:
            flags.append("half day")
        return blocked, last_entry, flatten_at, flags
