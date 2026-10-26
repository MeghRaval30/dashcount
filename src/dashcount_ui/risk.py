"""Risk meters: how much of each safety limit is used up (pure functions, no Qt)."""

from __future__ import annotations

from dataclasses import dataclass

STALE_AFTER_S = 90.0        # an older snapshot means the bot isn't telling us anything new


@dataclass(frozen=True)
class Limits:
    daily_loss_pct: float
    losers_in_row: int
    drawdown_pct: float
    max_open_risk_pct: float
    max_notional_x: float


def limits_from_config(cfg) -> Limits:
    g, s = cfg.guardian_mode, cfg.sizing_mode
    return Limits(float(g.daily_loss_pct), int(g.losers_in_row), float(g.drawdown_pct),
                  float(s.max_open_risk_pct), float(s.max_notional_x))


@dataclass(frozen=True)
class Meter:
    key: str
    label: str
    used: float
    limit: float
    frac: float
    level: str
    text: str


def level_for(frac: float) -> str:
    return "ok" if frac < 0.5 else ("warn" if frac < 0.75 else "danger")


def _money(used: float, limit: float, frac: float) -> str:
    pct = int(frac * 100 + 0.5 + 1e-9)          # round half up, as people read percentages
    return f"${used:,.0f} of ${limit:,.0f} ({pct}%)"


def risk_meters(snap: dict, guardian: dict, limits: Limits, snapshot_age: float | None) -> list[Meter]:
    budget = snap.get("budget") or {}
    equity = float(budget.get("equity") or 0.0)
    start = float(budget.get("start_equity") or equity)
    trades = snap.get("trades") or []
    peak = float(guardian.get("peak_equity") or 0.0) or equity
    losers = int(guardian.get("losers_in_row") or 0)

    rows = [
        ("day_loss", "DAY LOSS", max(0.0, -float(budget.get("pnl") or 0.0)), limits.daily_loss_pct / 100 * start),
        ("streak", "LOSING STREAK", float(losers), float(limits.losers_in_row)),
        ("fuse", "DRAWDOWN FUSE", max(0.0, peak - equity), limits.drawdown_pct / 100 * peak),
        ("open_risk", "OPEN RISK", sum(float(t.get("risk_dollars") or 0.0) for t in trades),
         limits.max_open_risk_pct / 100 * equity),
        ("exposure", "EXPOSURE", sum(abs(float(t.get("qty") or 0) * float(t.get("price") or 0.0)) for t in trades),
         limits.max_notional_x * equity),
    ]
    stale = snapshot_age is None or snapshot_age > STALE_AFTER_S
    out = []
    for key, label, used, limit in rows:
        frac = used / limit if limit > 0 else 0.0
        if key == "streak":
            text = f"{losers} of {limits.losers_in_row} losers"
        else:
            text = _money(used, limit, frac)
            if key == "exposure":
                text += " - per-trade cap, shown for scale"
        out.append(Meter(key, label, used, limit, frac, "stale" if stale else level_for(frac), text))
    return out
