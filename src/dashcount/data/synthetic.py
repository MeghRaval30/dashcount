"""Synthetic market days for tests and the no-keys demo.

Prices are driven by order flow (each aggressive trade moves the mid), with
trending / choppy regimes and a market factor, so the count, the pads, the
vetoes and the simulator all get exercised. This is NOT real data: never judge
the strategy's edge on it.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ..levelmap import DayPlan
from ..models import Bar, WatchItem
from ..timeutil import calendar


def _u_shape(n: int) -> np.ndarray:
    x = np.linspace(-1, 1, n)
    return 0.6 + 1.4 * x ** 2


def make_day(day: dt.date, symbols: list[str], seed: int = 0, market=("SPY", "QQQ"),
             base_rate: float = 0.6) -> tuple[DayPlan, dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Return (plan, trades by symbol, quotes by symbol) for one synthetic session."""
    from ..config import load_config
    from ..levelmap import LevelMap  # noqa: F401  (windows helper lives there)

    rng = np.random.default_rng(seed)
    s = calendar().session(day)
    secs = int(s.close - s.open)
    intensity = _u_shape(secs)
    # market regime: piecewise drift in buy-probability
    def regimes(strength: float) -> np.ndarray:
        out = np.zeros(secs)
        t = 0
        while t < secs:
            L = int(rng.integers(900, 3600))
            out[t:t + L] = rng.choice([-1, 0, 0, 1]) * strength * rng.uniform(0.4, 1.0)
            t += L
        return out

    mkt_bias = regimes(0.12)
    trades: dict[str, pd.DataFrame] = {}
    quotes: dict[str, pd.DataFrame] = {}
    items: list[WatchItem] = []
    hist: dict[str, list[Bar]] = {}
    for k, sym in enumerate(list(symbols) + list(market)):
        is_mkt = sym in market
        price0 = float(rng.uniform(40, 400)) if not is_mkt else (500.0 if sym == "SPY" else 430.0)
        bias = np.clip((0.5 if not is_mkt else 0.8) * mkt_bias + (regimes(0.18) if not is_mkt else 0), -0.35, 0.35)
        rate = base_rate * (3.0 if is_mkt else rng.uniform(0.6, 1.6)) * intensity
        n = rng.poisson(rate)
        idx = np.repeat(np.arange(secs), n)
        m = len(idx)
        ts = s.open + idx + rng.uniform(0, 1, m)
        p_buy = 0.5 + bias[idx]
        sign = np.where(rng.uniform(0, 1, m) < p_buy, 1, -1)
        size = (rng.geometric(0.012, m)).astype(float)
        tick = 0.01
        impact = price0 * 2e-7
        noise = rng.normal(0, price0 * 1.0e-4, m)
        mid = price0 + np.cumsum(sign * size * impact + noise)
        mid = np.maximum(mid, 1.0)
        spread = np.maximum(tick, np.round(price0 * rng.uniform(1e-4, 3e-4), 2))
        bid = np.round(mid - spread / 2, 2)
        ask = np.maximum(np.round(mid + spread / 2, 2), bid + tick)
        px = np.where(sign > 0, ask, bid)
        trades[sym] = pd.DataFrame({"ts": ts, "price": px, "size": size})
        qts = ts - 0.001
        quotes[sym] = pd.DataFrame({"ts": qts, "bid": bid, "ask": ask,
                                    "bid_size": rng.integers(1, 20, m) * 100.0,
                                    "ask_size": rng.integers(1, 20, m) * 100.0})
        # volume profile from this very day's flow (synthetic "normal")
        minute_vol = pd.Series(size).groupby(((ts - s.open) // 300).astype(int)).sum()
        prof = [float(minute_vol.get(i, minute_vol.mean())) for i in range(78)]
        atr_daily = float(price0 * rng.uniform(0.015, 0.03))
        items.append(WatchItem(sym, sector="Synthetic" if not is_mkt else "ETF", easy_to_borrow=True,
                               prev_close=float(price0), atr_daily=atr_daily,
                               avg_daily_volume=float(size.sum()), volume_profile=prof,
                               feed_share=1.0, is_market=is_mkt, score=float(rng.normal())))
        # previous-session context for Kronos: a gentle random walk
        prev_open = calendar().session(calendar().previous_sessions(day, 1)[0]).open
        p = price0 * (1 + rng.normal(0, 0.01))
        hb = []
        for i in range(390):
            o = p
            p = p * (1 + rng.normal(0, 0.0008))
            hb.append(Bar(sym, prev_open + 60 * i, o, max(o, p) * 1.0003, min(o, p) * 0.9997, p, float(rng.integers(2000, 9000))))
        hb[-1].close = price0
        hist[sym] = hb

    cfg = load_config()
    lm = LevelMap.__new__(LevelMap)
    lm.cfg = cfg
    blocked, last_entry, flatten_at, flags = lm._windows(s)
    plan = DayPlan(day, s, items, blocked, last_entry, flatten_at, flags + ["SYNTHETIC"], True, [], hist, [])
    return plan, trades, quotes
