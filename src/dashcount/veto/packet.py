"""The anonymised briefing shown to the reasoning model.

No ticker, no dates, prices rescaled so the entry is 100. Identical live and in
practice, so the model can't recall what happened on a historical day.
"""

from __future__ import annotations

import hashlib

from ..models import side_name

PAD_NAMES = {"orb": "opening-range breakout", "vwap_pullback": "VWAP pullback"}


def build(b: dict) -> str:
    k = 100.0 / b["entry"]
    p = lambda x: f"{x * k:.2f}" if x is not None else "n/a"  # noqa: E731
    lines = []
    risk = abs(b["entry"] - b["stop"])
    lines.append(
        f"TRADE: {side_name(b['direction'])} via {PAD_NAMES.get(b['pad'], b['pad'])} in a large-cap {b.get('sector') or 'US'} stock. "
        f"Entry {p(b['entry'])}, stop {p(b['stop'])} (risk {risk * k:.2f} = 1R), target {p(b['target'])} (+{abs(b['target'] - b['entry']) / risk:.1f}R)."
    )
    lines.append(
        f"COUNT (aggressive buying minus selling, in standard deviations; + favours this trade): true count {b['tc']:+.2f} "
        f"(stock flow {b['stock_z_dir']:+.2f}, market flow {b['market_z_dir']:+.2f}); order-book tilt {b.get('imbalance', 0):+.2f}."
    )
    lines.append(
        "STOCK TODAY (prices rescaled): "
        f"open {p(b.get('day_open'))}, high {p(b.get('high'))}, low {p(b.get('low'))}, VWAP {p(b.get('vwap'))}; "
        f"move from open {_f(b.get('move_from_open_atr'))}x its usual daily range; "
        f"volume last 5 min {_f(b.get('rel_volume_5m'))}x normal; spread {_f(b.get('spread_ratio'))}x normal."
    )
    closes = b.get("last_closes") or []
    if closes:
        lines.append("LAST 30 MINUTES (1-min closes, oldest to newest): " + " ".join(f"{c * k:.2f}" for c in closes))
    lines.append(
        f"MARKET: S&P 500 fund {_pct(b.get('spy_change'))} today, Nasdaq-100 fund {_pct(b.get('qqq_change'))}."
    )
    flags = ", ".join(b.get("flags") or []) or "none"
    lines.append(f"CLOCK: {b['minutes_since_open']:.0f} min since the open, {b['minutes_to_close']:.0f} min to the close. Flags: {flags}.")
    pos = b.get("open_positions") or []
    pos_txt = "; ".join(f"{side_name(d)} {s}" for d, s in pos) or "none"
    lines.append(
        f"BOT: open positions: {pos_txt}. Today so far: {b.get('today_r', 0):+.1f}R, losing trades today: {b.get('losers_today', 0)}."
    )
    if b.get("kronos_odds") is not None:
        lines.append(f"PATTERN MODEL: {b['kronos_odds'] * 100:.0f}% of its simulated paths hit the target before the stop.")
    mem = b.get("memory") or []
    if mem:
        lines.append("MEMORY (most similar past attempts):")
        lines += [f"  {i + 1}) {m}" for i, m in enumerate(mem)]
    else:
        lines.append("MEMORY: no similar past attempts yet.")
    return "\n".join(lines)


def _f(x) -> str:
    return f"{x:.1f}" if isinstance(x, (int, float)) else "n/a"


def _pct(x) -> str:
    return f"{x:+.2f}%" if isinstance(x, (int, float)) else "n/a"


def digest(text: str, model: str) -> str:
    return hashlib.sha256((model + "\n" + text).encode()).hexdigest()[:24]
