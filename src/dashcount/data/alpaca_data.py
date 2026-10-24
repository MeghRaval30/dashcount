"""Alpaca market-data REST client (raw HTTP for speed on bulk downloads).

Docs: https://docs.alpaca.markets/reference/stockbars
Free plan: SIP history except the latest 15 minutes, IEX unrestricted, 200 req/min.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from collections import deque

import httpx
import pandas as pd

from . import BAD_CONDITIONS, DAILY_COLS, MINUTE_COLS
from ..timeutil import ET

log = logging.getLogger(__name__)

DATA_URL = "https://data.alpaca.markets"


class MissingKeys(RuntimeError):
    pass


def keys_from_env() -> tuple[str, str]:
    k, s = os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    if not k or not s:
        raise MissingKeys("Alpaca keys are not set. Copy .env.example to .env and paste your PAPER "
                          "API key and secret (app.alpaca.markets -> Paper -> API Keys).")
    return k, s


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._t: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._t and now - self._t[0] > 60:
                self._t.popleft()
            if len(self._t) >= self.per_minute:
                sleep = 60 - (now - self._t[0]) + 0.05
                time.sleep(max(sleep, 0))
            self._t.append(time.monotonic())


def _rfc(ts: float | dt.datetime) -> str:
    if isinstance(ts, (int, float)):
        ts = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(series: pd.Series) -> pd.Series:
    """RFC3339 strings -> float epoch seconds (independent of pandas' time unit)."""
    t = pd.to_datetime(series, utc=True, format="ISO8601")
    return (t - pd.Timestamp(0, tz="UTC")).dt.total_seconds()


class AlpacaData:
    def __init__(self, key: str | None = None, secret: str | None = None, per_minute: int = 190,
                 client: httpx.Client | None = None) -> None:
        if key is None:
            key, secret = keys_from_env()
        self.client = client or httpx.Client(
            base_url=DATA_URL, timeout=60,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        )
        self.limiter = RateLimiter(per_minute)
        self.requests = 0

    # ------------------------------------------------------------- core
    def _get(self, path: str, params: dict) -> dict:
        for attempt in range(8):
            self.limiter.wait()
            try:
                r = self.client.get(path, params=params)
                self.requests += 1
            except httpx.HTTPError as e:
                log.warning("network error %s (attempt %d)", e, attempt + 1)
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 429:
                reset = float(r.headers.get("X-RateLimit-Reset", time.time() + 5))
                time.sleep(max(reset - time.time(), 1) + 0.5)
                continue
            if r.status_code >= 500:
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Alpaca {path} {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"Alpaca {path}: too many retries")

    def _pages(self, path: str, key: str, params: dict):
        """Yield (symbol, rows) one page at a time, so a big day never sits in memory as raw JSON."""
        token = None
        while True:
            p = dict(params, limit=10000)
            if token:
                p["page_token"] = token
            js = self._get(path, p)
            for sym, rows in (js.get(key) or {}).items():
                if rows:
                    yield sym, rows
            token = js.get("next_page_token")
            if not token:
                return

    def _paged(self, path: str, key: str, params: dict) -> dict[str, list]:
        out: dict[str, list] = {}
        for sym, rows in self._pages(path, key, params):
            out.setdefault(sym, []).extend(rows)
        return out

    # ------------------------------------------------------------- bars
    def bars(self, symbols: list[str], timeframe: str, start, end, feed: str) -> pd.DataFrame:
        frames = []
        for i in range(0, len(symbols), 100):
            batch = symbols[i:i + 100]
            got = self._paged("/v2/stocks/bars", "bars", {
                "symbols": ",".join(batch), "timeframe": timeframe, "start": _rfc(start), "end": _rfc(end),
                "adjustment": "raw", "feed": feed, "sort": "asc",
            })
            for sym, rows in got.items():
                if rows:
                    df = pd.DataFrame(rows)
                    df["symbol"] = sym
                    frames.append(df)
        if not frames:
            return pd.DataFrame(columns=["symbol", "t", "o", "h", "l", "c", "v"])
        return pd.concat(frames, ignore_index=True)

    def daily(self, symbols, start: dt.date, end: dt.date, feed: str) -> pd.DataFrame:
        s = dt.datetime.combine(start, dt.time(0), tzinfo=ET)
        e = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), tzinfo=ET)
        df = self.bars(list(symbols), "1Day", s, e, feed)
        if df.empty:
            return pd.DataFrame(columns=DAILY_COLS)
        out = pd.DataFrame({
            "symbol": df["symbol"],
            "date": pd.to_datetime(df["t"], utc=True, format="ISO8601").dt.tz_convert(ET).dt.date,
            "open": df["o"], "high": df["h"], "low": df["l"], "close": df["c"], "volume": df["v"].astype(float),
        })
        return out[(out["date"] >= start) & (out["date"] <= end)].reset_index(drop=True)

    def minutes(self, symbols, start: float, end: float, feed: str) -> pd.DataFrame:
        # the free plan rejects SIP requests touching the latest 15 minutes
        if feed == "sip":
            end = min(end, time.time() - 16 * 60)
            if end <= start:
                return pd.DataFrame(columns=MINUTE_COLS)
        df = self.bars(list(symbols), "1Min", start, end, feed)
        if df.empty:
            return pd.DataFrame(columns=MINUTE_COLS)
        return pd.DataFrame({
            "symbol": df["symbol"], "ts": _epoch(df["t"]),
            "open": df["o"], "high": df["h"], "low": df["l"], "close": df["c"], "volume": df["v"].astype(float),
        })

    # ------------------------------------------------------------- ticks
    def trades(self, symbols: list[str], start: float, end: float, feed: str) -> dict[str, pd.DataFrame]:
        parts: dict[str, list[pd.DataFrame]] = {}
        for sym, rows in self._pages("/v2/stocks/trades", "trades", {
            "symbols": ",".join(symbols), "start": _rfc(start), "end": _rfc(end), "feed": feed, "sort": "asc",
        }):
            df = pd.DataFrame(rows)
            conds = df["c"] if "c" in df else pd.Series([[]] * len(df))
            ok = conds.map(lambda c: not (set(c or []) & BAD_CONDITIONS))
            df = df[ok]
            parts.setdefault(sym, []).append(pd.DataFrame({
                "ts": _epoch(df["t"]).to_numpy(), "price": df["p"].astype(float).to_numpy(),
                "size": df["s"].astype(float).to_numpy(),
            }))
        return {sym: pd.concat(p, ignore_index=True) for sym, p in parts.items()}

    def quotes(self, symbols: list[str], start: float, end: float, feed: str) -> dict[str, pd.DataFrame]:
        def changed(q: pd.DataFrame) -> pd.DataFrame:
            # keep only rows where the top of book actually changed
            chg = (q[["bid", "ask", "bid_size", "ask_size"]].diff().abs().sum(axis=1) != 0).to_numpy().copy()
            chg[0] = True
            return q[chg]

        parts: dict[str, list[pd.DataFrame]] = {}
        for sym, rows in self._pages("/v2/stocks/quotes", "quotes", {
            "symbols": ",".join(symbols), "start": _rfc(start), "end": _rfc(end), "feed": feed, "sort": "asc",
        }):
            df = pd.DataFrame(rows)
            parts.setdefault(sym, []).append(changed(pd.DataFrame({
                "ts": _epoch(df["t"]).to_numpy(),
                "bid": df["bp"].astype("float32").to_numpy(), "ask": df["ap"].astype("float32").to_numpy(),
                "bid_size": df["bs"].astype("float32").to_numpy(), "ask_size": df["as"].astype("float32").to_numpy(),
            })))
        # a page can start with the same top of book the previous page ended on
        return {sym: changed(pd.concat(p, ignore_index=True)).reset_index(drop=True) for sym, p in parts.items()}

    def spread_bps(self, symbols: list[str], day, windows=("09:50", "11:00", "14:00"), minutes: int = 2) -> dict[str, float]:
        """Median NBBO spread (bps) per symbol over a few short windows of `day` (SIP quotes)."""
        import numpy as np

        from ..timeutil import et_ts

        per: dict[str, list[float]] = {}
        for w in windows:
            t0 = et_ts(day, w)
            got = self.quotes(list(symbols), t0, t0 + 60 * minutes, "sip")
            for s, q in got.items():
                q = q[(q["bid"] > 0) & (q["ask"] > q["bid"])]
                if len(q):
                    mid = (q["ask"] + q["bid"]) / 2
                    per.setdefault(s, []).append(float(((q["ask"] - q["bid"]) / mid * 1e4).median()))
        return {s: float(np.median(v)) for s, v in per.items() if v}

    # ------------------------------------------------------------ assets
    def assets(self) -> pd.DataFrame:
        from .alpaca_trading import trading_client

        tc = trading_client()
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        from .alpaca_trading import g

        rows = tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE))
        return pd.DataFrame([
            {"symbol": g(a, "symbol"), "easy_to_borrow": bool(g(a, "easy_to_borrow")),
             "shortable": bool(g(a, "shortable"))}
            for a in rows if g(a, "tradable")
        ])
