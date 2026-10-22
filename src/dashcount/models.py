"""Plain data objects shared by every part of the bot."""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from typing import Literal

LONG, SHORT = 1, -1


def side_name(direction: int) -> str:
    return "LONG" if direction == LONG else "SHORT"


@dataclass(slots=True)
class Trade:
    symbol: str
    ts: float
    price: float
    size: float


@dataclass(slots=True)
class Quote:
    symbol: str
    ts: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float


@dataclass(slots=True)
class Bar:
    symbol: str
    start: float        # epoch seconds of the minute start
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int = 0

    @property
    def end(self) -> float:
        return self.start + 60


@dataclass
class WatchItem:
    """One watchlist entry produced by the level map."""

    symbol: str
    sector: str = "Unknown"
    easy_to_borrow: bool = True
    prev_close: float = 0.0
    atr_daily: float = 0.0
    avg_daily_volume: float = 0.0
    gap_pct: float = 0.0
    premarket_rel_volume: float = 1.0
    score: float = 0.0
    feed_share: float = 1.0
    # median SIP volume per 5-minute bucket of the regular session (78 buckets)
    volume_profile: list[float] = field(default_factory=list)
    is_market: bool = False


@dataclass
class Candidate:
    """A jump pad fired: a fully specified trade idea."""

    symbol: str
    ts: float
    pad: str
    direction: int
    entry: float
    stop: float
    target: float
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    def r_multiple(self, price: float) -> float:
        return (price - self.entry) * self.direction / self.risk if self.risk > 0 else 0.0


Verdict = Literal["allow", "veto", "unavailable", "skipped"]


@dataclass
class VetoResult:
    source: str                      # "kronos" | "finr1"
    verdict: Verdict
    reason_code: str = ""
    confidence: str = ""
    explanation: str = ""
    latency_s: float = 0.0
    odds: float | None = None        # Kronos only
    details: dict = field(default_factory=dict)
    cached: bool = False


@dataclass
class OrderUpdate:
    """Normalized order event from any broker."""

    order_id: str
    symbol: str
    event: Literal["new", "fill", "partial_fill", "canceled", "rejected", "expired", "replaced"]
    role: Literal["entry", "take_profit", "stop", "exit", "unknown"]
    side: Literal["buy", "sell"]
    filled_qty: float            # cumulative
    filled_avg_price: float
    ts: float
    last_fill_qty: float = 0.0
    last_fill_price: float = 0.0
    client_order_id: str = ""


@dataclass
class Account:
    equity: float
    buying_power: float
    last_equity: float           # equity at the previous close


@dataclass
class Position:
    symbol: str
    qty: float                   # signed: + long, - short
    avg_price: float


_seq = itertools.count()


def next_seq() -> int:
    return next(_seq)
