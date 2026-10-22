"""Streaming features for one symbol: bars, VWAP, ATR, spread, and the count.

Everything here only ever sees past events, so it is point-in-time by
construction, and the same code runs live and in replay.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .models import Bar, Quote, Trade, WatchItem


# ---------------------------------------------------------------------------
# Bars, VWAP and ATR
# ---------------------------------------------------------------------------
class BarSeries:
    """Builds 1-minute bars from trades. Bars are closed by the engine's clock,
    so a minute with no trades still produces a (zero-volume) bar."""

    def __init__(self, symbol: str, atr_period: int = 14) -> None:
        self.symbol = symbol
        self.atr_period = atr_period
        self.bars: list[Bar] = []          # today's completed bars
        self.vwaps: list[float] = []       # session VWAP at each bar close
        self.history: list[Bar] = []       # previous session(s), for Kronos context
        self._cur: Bar | None = None
        self.atr: float | None = None
        self._pv = 0.0
        self._v = 0.0
        self.day_open: float | None = None
        self.high: float = -math.inf
        self.low: float = math.inf
        self.high_ts: float = 0.0
        self.low_ts: float = 0.0
        self.volume_today = 0.0

    # -- updates -----------------------------------------------------------
    def on_trade(self, t: Trade) -> None:
        start = float(int(t.ts // 60) * 60)
        if self._cur is not None and start > self._cur.start:
            # The engine normally closes bars on its clock; this is a fallback.
            self._finish(self._cur)
            self._cur = None
        if self._cur is None:
            self._cur = Bar(self.symbol, start, t.price, t.price, t.price, t.price, 0.0, 0)
        b = self._cur
        b.high = max(b.high, t.price)
        b.low = min(b.low, t.price)
        b.close = t.price
        b.volume += t.size
        b.trades += 1
        self._pv += t.price * t.size
        self._v += t.size
        self.volume_today += t.size
        if self.day_open is None:
            self.day_open = t.price
        if t.price > self.high:
            self.high, self.high_ts = t.price, t.ts
        if t.price < self.low:
            self.low, self.low_ts = t.price, t.ts

    def close_bar(self, next_start: float) -> Bar | None:
        """Close the minute [next_start-60, next_start). Returns the closed bar,
        a flat zero-volume bar if nothing traded, or None if no price is known."""
        minute = next_start - 60
        if self.bars and self.bars[-1].start >= minute:
            return None
        cur = self._cur
        if cur is not None and cur.start == minute:
            self._cur = None
            return self._finish(cur)
        if cur is not None and cur.start > minute:
            return None
        if cur is not None and cur.start < minute:      # stale bar never closed
            self._finish(cur)
            self._cur = None
        if not self.bars:
            return None
        c = self.bars[-1].close
        return self._finish(Bar(self.symbol, minute, c, c, c, c, 0.0, 0))

    def _finish(self, b: Bar) -> Bar:
        self._update_atr(b)
        self.bars.append(b)
        self.vwaps.append(self.vwap if self.vwap is not None else b.close)
        return b

    def _update_atr(self, b: Bar) -> None:
        prev_close = self.bars[-1].close if self.bars else (self.history[-1].close if self.history else b.open)
        tr = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        if self.atr is None:
            self.atr = tr if tr > 0 else None
        else:
            self.atr = self.atr + (tr - self.atr) / self.atr_period

    # -- reads -------------------------------------------------------------
    @property
    def vwap(self) -> float | None:
        return self._pv / self._v if self._v > 0 else None

    @property
    def last_close(self) -> float | None:
        if self._cur is not None:
            return self._cur.close
        return self.bars[-1].close if self.bars else None

    def recent_volume(self, n: int) -> float:
        return float(sum(b.volume for b in self.bars[-n:]))

    def context(self, n: int) -> list[Bar]:
        """Most recent n completed bars across history + today (Kronos input)."""
        allb = self.history + self.bars
        return allb[-n:]


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------
class SpreadTracker:
    """One spread sample per second; 'normal' = rolling median."""

    def __init__(self, window_s: float = 1800.0, min_samples: int = 30) -> None:
        self.window_s = window_s
        self.min_samples = min_samples
        self._samples: deque[tuple[float, float]] = deque()
        self._last_sec = -1
        self.bid = 0.0
        self.ask = 0.0
        self.bid_size = 0.0
        self.ask_size = 0.0
        self.quote_ts = 0.0
        self.seed_median: float | None = None

    def on_quote(self, q: Quote) -> None:
        if q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
            return
        self.bid, self.ask, self.bid_size, self.ask_size, self.quote_ts = q.bid, q.ask, q.bid_size, q.ask_size, q.ts
        sec = int(q.ts)
        if sec != self._last_sec:
            self._last_sec = sec
            self._samples.append((q.ts, q.ask - q.bid))
            cutoff = q.ts - self.window_s
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    @property
    def spread(self) -> float | None:
        return (self.ask - self.bid) if self.ask > 0 and self.bid > 0 else None

    @property
    def mid(self) -> float | None:
        return (self.ask + self.bid) / 2 if self.ask > 0 and self.bid > 0 else None

    def normal(self) -> float | None:
        if len(self._samples) >= self.min_samples:
            return float(np.median([s for _, s in self._samples]))
        return self.seed_median

    def imbalance(self) -> float:
        tot = self.bid_size + self.ask_size
        return (self.bid_size - self.ask_size) / tot if tot > 0 else 0.0


# ---------------------------------------------------------------------------
# The count
# ---------------------------------------------------------------------------
@dataclass
class FlowCounter:
    """Running count of aggressive buying minus aggressive selling.

    * each trade is signed by the quote rule (tick rule as fallback);
    * running count = signed volume over the last `window_s` seconds;
    * true count (z) = running count / its typical size, where the typical size
      is a session EW RMS seeded by kappa * sqrt(normal window volume * avg trade
      size) and floored at the random-coin-flip level sqrt(V * s).
    """

    window_s: float = 120.0
    sample_every_s: float = 10.0
    halflife_s: float = 1800.0
    kappa: float = 3.0
    quote_fresh_s: float = 5.0
    _flow: deque = field(default_factory=deque)
    _sum: float = 0.0
    _last_price: float = 0.0
    _last_sign: int = 0
    _ms: float | None = None
    _next_sample: float = 0.0
    _n_trades: int = 0
    _vol: float = 0.0
    normal_window_volume: float | None = None   # expected volume in one window (feed-scaled)

    def classify(self, t: Trade, spread: SpreadTracker | None) -> int:
        sign = 0
        if spread is not None and spread.ask > 0 and (t.ts - spread.quote_ts) <= self.quote_fresh_s:
            if t.price >= spread.ask:
                sign = 1
            elif t.price <= spread.bid:
                sign = -1
            else:
                mid = (spread.ask + spread.bid) / 2
                sign = 1 if t.price > mid else (-1 if t.price < mid else 0)
        if sign == 0:
            if t.price > self._last_price > 0:
                sign = 1
            elif 0 < t.price < self._last_price:
                sign = -1
            else:
                sign = self._last_sign
        self._last_price = t.price
        if sign != 0:
            self._last_sign = sign
        return sign

    def on_trade(self, t: Trade, spread: SpreadTracker | None) -> int:
        sign = self.classify(t, spread)
        self._flow.append((t.ts, sign * t.size))
        self._sum += sign * t.size
        self._n_trades += 1
        self._vol += t.size
        self._prune(t.ts)
        self.maybe_sample(t.ts)
        return sign

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        flow = self._flow
        while flow and flow[0][0] < cutoff:
            self._sum -= flow.popleft()[1]

    def maybe_sample(self, now: float) -> None:
        if now < self._next_sample:
            return
        if self._next_sample == 0.0:
            self._next_sample = now
        self._prune(now)
        x2 = self._sum * self._sum
        if self._ms is None:
            seed = self.seed_sd()
            self._ms = seed * seed if seed else x2
        alpha = 1.0 - math.exp(-self.sample_every_s * math.log(2) / self.halflife_s)
        self._ms += alpha * (x2 - self._ms)
        self._next_sample = now + self.sample_every_s

    @property
    def avg_trade_size(self) -> float:
        return self._vol / self._n_trades if self._n_trades else 100.0

    def floor_sd(self) -> float:
        v = self.normal_window_volume or 0.0
        return math.sqrt(max(v, 1.0) * max(self.avg_trade_size, 1.0))

    def seed_sd(self) -> float:
        return self.kappa * self.floor_sd()

    def running(self, now: float) -> float:
        self._prune(now)
        return self._sum

    def z(self, now: float) -> float:
        run = self.running(now)
        sd = math.sqrt(self._ms) if self._ms else self.seed_sd()
        sd = max(sd, self.floor_sd(), 1e-9)
        return run / sd


# ---------------------------------------------------------------------------
# Per-symbol state
# ---------------------------------------------------------------------------
class SymbolState:
    """Everything the engine knows about one watched symbol today."""

    def __init__(self, item: WatchItem, cfg_features, session_open: float, feed: str) -> None:
        f = cfg_features
        self.item = item
        self.symbol = item.symbol
        self.feed = feed
        self.session_open = session_open
        self.bars = BarSeries(item.symbol, f.atr_1m_period)
        self.spread = SpreadTracker(window_s=f.spread_median_window_min * 60)
        self.flow = FlowCounter(
            window_s=f.count_window_s,
            sample_every_s=f.count_sample_every_s,
            halflife_s=f.count_halflife_min * 60,
            kappa=f.count_prior_kappa,
            quote_fresh_s=f.quote_fresh_s,
        )
        self.last_trade_ts = 0.0
        self.last_price: float | None = None
        self.pad_state: dict = {}
        self.last_loss_exit_ts = -1e18
        self._update_normal_window(session_open)

    # normal volume -----------------------------------------------------------
    def normal_minute_volume(self, ts: float, sip: bool = False) -> float | None:
        """Expected volume in one minute at this time of day. Feed-scaled unless sip=True."""
        prof = self.item.volume_profile
        if prof:
            idx = int((ts - self.session_open) // 300)
            idx = min(max(idx, 0), len(prof) - 1)
            v = prof[idx] / 5.0
            return v if sip else v * self.item.feed_share
        # fallback: today's average so far (feed volume)
        mins = (ts - self.session_open) / 60
        if mins >= 15 and self.bars.volume_today > 0:
            v = self.bars.volume_today / mins
            return v if not sip else v / max(self.item.feed_share, 1e-3)
        return None

    def _update_normal_window(self, ts: float) -> None:
        m = self.normal_minute_volume(ts)
        if m is not None:
            self.flow.normal_window_volume = m * self.flow.window_s / 60.0

    # events -----------------------------------------------------------------
    def on_trade(self, t: Trade) -> int:
        self.last_trade_ts = t.ts
        self.last_price = t.price
        self.bars.on_trade(t)
        return self.flow.on_trade(t, self.spread)

    def on_quote(self, q: Quote) -> None:
        self.spread.on_quote(q)

    def on_minute(self, next_start: float) -> Bar | None:
        bar = self.bars.close_bar(next_start)
        self._update_normal_window(next_start)
        self.flow.maybe_sample(next_start)
        return bar

    def z(self, now: float) -> float:
        return self.flow.z(now)
