"""Test helpers: build trades/quotes/sessions without any external data."""

from __future__ import annotations

import datetime as dt

from dashcount.config import load_config
from dashcount.features import SymbolState
from dashcount.models import Quote, Trade, WatchItem
from dashcount.timeutil import Session, et_ts

DAY = dt.date(2025, 3, 12)   # a normal Wednesday session


def cfg(**overrides):
    return load_config(overrides=overrides or None)


def session(day: dt.date = DAY) -> Session:
    return Session(day=day, open=et_ts(day, "09:30"), close=et_ts(day, "16:00"), half_day=False)


def at(t: str, sec: float = 0.0, day: dt.date = DAY) -> float:
    return et_ts(day, t) + sec


def state(symbol: str = "TEST", profile_per_5min: float = 50_000, feed_share: float = 1.0,
          atr_daily: float = 2.0, etb: bool = True, c=None) -> SymbolState:
    c = c or cfg()
    item = WatchItem(symbol=symbol, atr_daily=atr_daily, easy_to_borrow=etb,
                     volume_profile=[profile_per_5min] * 78, feed_share=feed_share, prev_close=100.0)
    return SymbolState(item, c.features, session().open, "sip")


def trade(ts: float, price: float, size: float = 100, symbol: str = "TEST") -> Trade:
    return Trade(symbol, ts, price, size)


def quote(ts: float, bid: float, ask: float, bs: float = 500, asz: float = 500, symbol: str = "TEST") -> Quote:
    return Quote(symbol, ts, bid, ask, bs, asz)


def feed_minutes(st: SymbolState, start: float, prices: list[tuple[float, float, float, float]],
                 vol_per_bar: float = 10_000, spread: float = 0.02) -> None:
    """Feed one bar per minute: (open, high, low, close) as four trades, then close the bar."""
    for i, (o, h, l, c) in enumerate(prices):
        t0 = start + 60 * i
        for j, p in enumerate((o, h, l, c)):
            ts = t0 + 5 + j * 10
            st.on_quote(quote(ts - 1, p - spread / 2, p + spread / 2, symbol=st.symbol))
            st.on_trade(trade(ts, p, vol_per_bar / 4, symbol=st.symbol))
        st.on_minute(t0 + 60)
