"""Live hazards ("observation"): clear dangers that block a candidate.

Every hazard is evaluated and recorded (not just the first), so the dataset
shows everything that was wrong with a setup.
"""

from __future__ import annotations

from dataclasses import dataclass

from .features import SymbolState
from .models import SHORT, Candidate


@dataclass
class HazardContext:
    now: float
    feed: str                      # "iex" | "sip"
    blocked_reason: str | None     # from the day plan (time windows)
    symbol_busy: bool              # open position or pending order in this symbol
    guardian_block: str | None     # day halt / disarmed / panic
    slippage_bps: float
    sec_fee_per_million: float
    taf_per_share: float


def round_trip_cost(price: float, spread: float, slippage_bps: float, sec_fee_per_million: float, taf_per_share: float) -> float:
    """Estimated cost per share of getting in and out with marketable orders."""
    return spread + 2 * price * slippage_bps / 1e4 + price * sec_fee_per_million / 1e6 + taf_per_share


def check(c: Candidate, s: SymbolState, ctx: HazardContext, cfg) -> tuple[list[str], dict]:
    """Return (fired hazard names, measured values)."""
    fired: list[str] = []
    vals: dict = {}
    price = s.last_price or c.entry

    # spread
    spread = s.spread.spread
    normal_spread = s.spread.normal()
    vals["spread"] = spread
    vals["spread_ratio"] = (spread / normal_spread) if (spread and normal_spread) else None
    if spread is not None:
        if spread > cfg.spread_max_pct / 100.0 * price:
            fired.append("spread_blowout")
        elif normal_spread and spread > cfg.spread_mult * normal_spread:
            fired.append("spread_blowout")

    # dead volume: last 5 bars vs normal 5-minute volume
    vol5 = s.bars.recent_volume(5)
    normal_min = s.normal_minute_volume(ctx.now - 150)
    vals["rel_volume_5m"] = (vol5 / (5 * normal_min)) if normal_min else None
    if normal_min and vol5 < cfg.dead_volume_mult * 5 * normal_min:
        fired.append("dead_volume")

    # already ran
    day_open = s.bars.day_open
    atr_d = s.item.atr_daily
    vals["move_from_open_atr"] = ((price - day_open) / atr_d) if (day_open and atr_d) else None
    if day_open and atr_d and abs(price - day_open) > cfg.already_ran_atr * atr_d:
        fired.append("already_ran")

    # too costly
    sp = spread if spread is not None else (normal_spread or 0.0)
    cost = round_trip_cost(price, sp, ctx.slippage_bps, ctx.sec_fee_per_million, ctx.taf_per_share)
    tgt = abs(c.target - c.entry)
    vals["target_to_cost"] = tgt / cost if cost > 0 else None
    if cost > 0 and tgt < cfg.cost_mult * cost:
        fired.append("too_costly")

    # stale / halted
    stale_s = cfg.stale_s_iex if ctx.feed == "iex" else cfg.stale_s_sip
    vals["seconds_since_trade"] = ctx.now - s.last_trade_ts if s.last_trade_ts else None
    if not s.last_trade_ts or ctx.now - s.last_trade_ts > stale_s:
        fired.append("stale")

    if c.direction == SHORT and not s.item.easy_to_borrow:
        fired.append("cant_short")
    if ctx.symbol_busy:
        fired.append("already_in")
    if ctx.now - s.last_loss_exit_ts < cfg.death_cooldown_min * 60:
        fired.append("death_cooldown")
    if ctx.blocked_reason:
        fired.append("blocked_window")
        vals["blocked_reason"] = ctx.blocked_reason
    if ctx.guardian_block:
        fired.append("guardian")
        vals["guardian_block"] = ctx.guardian_block
    return fired, vals
