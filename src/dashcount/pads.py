"""Jump pads: the setups the bot is allowed to take. Evaluated at each 1-min bar close."""

from __future__ import annotations

from .features import SymbolState
from .models import LONG, SHORT, Bar, Candidate
from .timeutil import Session


def _make(state: SymbolState, now: float, pad: str, direction: int, entry: float, stop: float, target_r: float) -> Candidate | None:
    risk = abs(entry - stop)
    if risk <= 0 or (stop - entry) * direction >= 0:
        return None
    target = entry + direction * target_r * risk
    return Candidate(state.symbol, now, pad, direction, entry, stop, target)


def orb(state: SymbolState, bar: Bar, session: Session, cfg, target_r: float) -> list[Candidate]:
    """Opening-range breakout."""
    ps = state.pad_state.setdefault("orb", {"done": set()})
    range_end = session.open + cfg.range_minutes * 60
    if "high" not in ps:
        if bar.end < range_end:
            return []
        rng = [b for b in state.bars.bars if b.start < range_end and b.volume > 0]
        if not rng:
            ps["high"] = None
            return []
        ps["high"] = max(b.high for b in rng)
        ps["low"] = min(b.low for b in rng)
    if ps.get("high") is None:
        return []
    w0, w1 = session.at(cfg.window[0]), session.at(cfg.window[1])
    if not (w0 < bar.end <= w1) or bar.volume <= 0:
        return []
    normal = state.normal_minute_volume(bar.start)
    if normal is None:
        today = [b.volume for b in state.bars.bars[:-1] if b.volume > 0]
        normal = sum(today) / len(today) if today else None
    if normal is None or bar.volume < cfg.volume_mult * normal:
        return []
    hi, lo = ps["high"], ps["low"]
    mid = (hi + lo) / 2
    out = []
    for direction, broke in ((LONG, bar.close > hi), (SHORT, bar.close < lo)):
        if not broke or direction in ps["done"]:
            continue
        ps["done"].add(direction)
        risk = abs(bar.close - mid)
        atr_d = state.item.atr_daily
        if atr_d > 0 and risk > cfg.max_risk_atr_frac * atr_d:
            continue
        if risk < cfg.min_risk_pct / 100.0 * bar.close:
            continue
        c = _make(state, bar.end, "orb", direction, bar.close, mid, target_r)
        if c:
            out.append(c)
    return out


def vwap_pullback(state: SymbolState, bar: Bar, session: Session, cfg, target_r: float) -> list[Candidate]:
    """Trend pullback to VWAP that bounces."""
    ps = state.pad_state.setdefault("vwap", {"last": {}})
    w0, w1 = session.at(cfg.window[0]), session.at(cfg.window[1])
    if not (w0 < bar.end <= w1) or bar.volume <= 0:
        return []
    bars, vwaps = state.bars.bars, state.bars.vwaps
    n = cfg.trend_lookback
    atr = state.bars.atr
    vwap = state.bars.vwap
    if len(bars) < n or atr is None or atr <= 0 or vwap is None:
        return []
    closes = [b.close for b in bars[-n:]]
    vw = vwaps[-n:]
    frac_above = sum(c > v for c, v in zip(closes, vw)) / n
    frac_below = sum(c < v for c, v in zip(closes, vw)) / n
    recent = bar.end - cfg.high_within_min * 60
    out = []
    # long
    if (
        frac_above >= cfg.trend_frac
        and state.bars.high_ts >= recent
        and bar.low <= vwap + cfg.touch_atr * atr
        and bar.close > vwap
        and bar.close > bar.open
        and bar.end - ps["last"].get(LONG, -1e18) >= cfg.cooldown_min * 60
    ):
        ps["last"][LONG] = bar.end
        stop = min(bar.low, vwap) - cfg.stop_atr * atr
        c = _make(state, bar.end, "vwap_pullback", LONG, bar.close, stop, target_r)
        if c:
            out.append(c)
    # short
    if (
        frac_below >= cfg.trend_frac
        and state.bars.low_ts >= recent
        and bar.high >= vwap - cfg.touch_atr * atr
        and bar.close < vwap
        and bar.close < bar.open
        and bar.end - ps["last"].get(SHORT, -1e18) >= cfg.cooldown_min * 60
    ):
        ps["last"][SHORT] = bar.end
        stop = max(bar.high, vwap) + cfg.stop_atr * atr
        c = _make(state, bar.end, "vwap_pullback", SHORT, bar.close, stop, target_r)
        if c:
            out.append(c)
    return out


def evaluate_pads(state: SymbolState, bar: Bar, session: Session, pads_cfg) -> list[Candidate]:
    out: list[Candidate] = []
    if pads_cfg.orb.enabled:
        out += orb(state, bar, session, pads_cfg.orb, pads_cfg.target_r)
    if pads_cfg.vwap_pullback.enabled:
        out += vwap_pullback(state, bar, session, pads_cfg.vwap_pullback, pads_cfg.target_r)
    return out
