"""A fake Alpaca data source: deterministic, offline, same DataFrame shapes."""

from __future__ import annotations

import datetime as dt
import hashlib

import numpy as np
import pandas as pd

from dashcount.data.synthetic import make_day
from dashcount.timeutil import calendar, et_ts

MARKET = ("SPY", "QQQ")


def _rng(*parts) -> np.random.Generator:
    h = int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)
    return np.random.default_rng(h)


def universe(n: int = 40) -> pd.DataFrame:
    return pd.DataFrame({"symbol": [f"T{i:02d}" for i in range(n)], "name": "x",
                         "sector": ["Tech", "Health", "Energy", "Retail"] * (n // 4), "index": "SP500"})


class FakeAlpacaData:
    def __init__(self) -> None:
        self.requests = 0
        self.calls: list[str] = []

    def daily(self, symbols, start, end, feed):
        self.calls.append(f"daily:{feed}")
        rows = []
        for s in symbols:
            r = _rng("d", s)
            p = 50 + r.uniform(0, 200)
            for d in calendar().sessions(start, end):
                rr = _rng("d", s, d)
                o = p
                p = max(5.0, p * (1 + rr.normal(0, 0.02)))
                v = float(rr.uniform(0.8e6, 3e6)) * (0.03 if feed == "iex" else 1.0)
                rows.append((s, d, o, max(o, p) * 1.01, min(o, p) * 0.99, p, v))
        return pd.DataFrame(rows, columns=["symbol", "date", "open", "high", "low", "close", "volume"])

    def minutes(self, symbols, start, end, feed):
        self.calls.append(f"minutes:{feed}")
        rows = []
        d0 = dt.datetime.fromtimestamp(start).date() - dt.timedelta(days=1)
        d1 = dt.datetime.fromtimestamp(end).date() + dt.timedelta(days=1)
        for d in calendar().sessions(d0, d1):
            s = calendar().session(d)
            ts_list = np.arange(et_ts(d, "08:00"), s.close, 60.0)
            for sym in symbols:
                r = _rng("m", sym, d)
                p = 50 + _rng("d", sym).uniform(0, 200)
                closes = p * np.cumprod(1 + r.normal(0, 0.001, len(ts_list)))
                vol = r.uniform(1000, 20000, len(ts_list)) * (0.03 if feed == "iex" else 1.0)
                for t, c, v in zip(ts_list, closes, vol):
                    if start <= t < end:
                        rows.append((sym, float(t), c, c * 1.001, c * 0.999, c, float(v)))
        return pd.DataFrame(rows, columns=["symbol", "ts", "open", "high", "low", "close", "volume"])

    def _ticks(self, symbols, start):
        d = dt.datetime.fromtimestamp(start).date()
        stocks = [s for s in symbols if s not in MARKET]
        mkt = tuple(s for s in symbols if s in MARKET)
        _, trades, quotes = make_day(d, stocks, seed=int(start) % 1000, market=mkt)
        return trades, quotes

    def trades(self, symbols, start, end, feed):
        self.calls.append(f"trades:{feed}")
        return self._ticks(symbols, start)[0]

    def quotes(self, symbols, start, end, feed):
        self.calls.append(f"quotes:{feed}")
        return self._ticks(symbols, start)[1]

    def assets(self):
        syms = [f"T{i:02d}" for i in range(40)] + list(MARKET)
        return pd.DataFrame({"symbol": syms, "easy_to_borrow": True, "shortable": True})
