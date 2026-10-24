"""Alpaca trading-account helpers (paper by default)."""

from __future__ import annotations

import os

from .alpaca_data import keys_from_env


def is_paper() -> bool:
    return os.environ.get("ALPACA_PAPER", "true").strip().lower() not in ("false", "0", "no")


def trading_client(paper: bool | None = None):
    """Alpaca trading client returning raw JSON dicts.

    raw_data=True on purpose: alpaca-py's strict models reject values Alpaca adds
    later (e.g. the TXSE exchange in 2026), which would crash the bot mid-day."""
    from alpaca.trading.client import TradingClient

    k, s = keys_from_env()
    return TradingClient(k, s, paper=is_paper() if paper is None else paper, raw_data=True)


def g(obj, key: str, default=None):
    """Read a field from a raw dict or a model object alike."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def ts_of(v) -> float | None:
    """Epoch seconds from a datetime or an RFC3339 string."""
    import datetime as dt

    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace("Z", "+00:00")
        if "." in s:                      # trim nanoseconds to microseconds
            head, frac = s.split(".", 1)
            digits = "".join(ch for ch in frac if ch.isdigit())
            rest = frac[len(digits):]
            s = f"{head}.{digits[:6]}{rest}"
        return dt.datetime.fromisoformat(s).timestamp()
    return v.timestamp()
