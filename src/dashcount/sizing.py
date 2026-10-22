"""Count gates and the bet spread. Size depends only on the true count."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import SHORT, Candidate


def true_count(stock_z: float, market_z: float, direction: int, gates_cfg) -> float:
    """True count in the trade's direction (positive = in favour)."""
    return direction * (gates_cfg.stock_weight * stock_z + gates_cfg.market_weight * market_z)


def gate(tc: float, market_z: float, direction: int, gates_cfg) -> str | None:
    if tc < gates_cfg.min_true_count:
        return "count_too_low"
    if direction * market_z <= gates_cfg.market_against:
        return "market_against"
    return None


def bucket(tc: float) -> int:
    if tc >= 3.0:
        return 3
    if tc >= 2.0:
        return 2
    if tc >= 1.0:
        return 1
    return 0


@dataclass
class SizeDecision:
    qty: int
    bucket: int
    risk_pct: float
    risk_dollars: float
    capped_by: list[str] = field(default_factory=list)
    skip_reason: str | None = None


def size_trade(
    c: Candidate,
    tc: float,
    equity: float,
    buying_power: float,
    open_risk_dollars: float,
    sip_normal_5min_volume: float | None,
    mode_cfg,
    sizing_cfg,
) -> SizeDecision:
    b = bucket(tc)
    if b == 0:
        return SizeDecision(0, 0, 0.0, 0.0, skip_reason="count_too_low")
    pct = mode_cfg.risk_pct[str(b)]
    risk_dollars = equity * pct / 100.0
    r = c.risk
    if r <= 0:
        return SizeDecision(0, b, pct, 0.0, skip_reason="zero_risk")
    qty = math.floor(risk_dollars / r)
    capped: list[str] = []

    if sip_normal_5min_volume:
        cap = math.floor(sizing_cfg.liquidity_frac_of_5min * sip_normal_5min_volume)
        if qty > cap:
            qty, _ = cap, capped.append("liquidity")

    cap = math.floor(mode_cfg.max_notional_x * equity / c.entry)
    if qty > cap:
        qty, _ = cap, capped.append("notional")

    room = mode_cfg.max_open_risk_pct / 100.0 * equity - open_risk_dollars
    cap = math.floor(max(room, 0.0) / r)
    if qty > cap:
        qty, _ = cap, capped.append("open_risk")

    px = c.entry * (sizing_cfg.short_buying_power_markup if c.direction == SHORT else 1.0)
    cap = math.floor(max(buying_power, 0.0) / px)
    if qty > cap:
        qty, _ = cap, capped.append("buying_power")

    if qty < 1:
        return SizeDecision(0, b, pct, risk_dollars, capped, skip_reason="size_zero:" + (capped[-1] if capped else "risk"))
    return SizeDecision(int(qty), b, pct, qty * r, capped)
