"""Orders and fills from the broker, measured against the bot's plan (pure, no Qt).

Slippage is positive when it cost us: a buy filled above the plan, a sell below it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BlotterRow:
    order_id: str
    parent_id: str | None
    ts: float
    symbol: str
    side: str
    role: str
    qty: float
    filled_qty: float
    price: float | None
    status: str
    fill: float | None
    slip: float | None
    slip_r: float | None
    trade_id: str | None


def _f(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _ts(s: str | None) -> float:
    """Alpaca timestamps carry nanoseconds; datetime keeps microseconds."""
    if not s:
        return 0.0
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
    return dt.datetime.fromisoformat(s).timestamp()


def plans_from_journal(entries: list[dict]) -> dict[str, dict]:
    return {e["trade_id"]: {k: e.get(k) for k in ("entry", "stop", "target", "qty", "direction")}
            for e in entries if e.get("kind") == "taken" and e.get("trade_id")}


def _row(o: dict, role: str, parent: str | None, trade_id: str | None, plan: dict | None, d: int) -> BlotterRow:
    fill = _f(o.get("filled_avg_price"))
    filled = _f(o.get("filled_qty")) or 0.0
    price = _f(o.get("stop_price")) if role == "STOP" else _f(o.get("limit_price"))
    if fill is None or not filled:
        fill = None
    slip = slip_r = None
    if fill is not None and plan and plan.get("entry") is not None:
        if role == "ENTRY":
            slip = d * (fill - float(plan["entry"]))
        elif role in ("TARGET", "STOP") and price is not None:
            slip = d * (price - fill)
        risk = abs(float(plan["entry"]) - float(plan.get("stop") or plan["entry"]))
        if slip is not None and risk > 0:
            slip_r = slip / risk
    return BlotterRow(str(o.get("id")), parent, _ts(o.get("submitted_at")), str(o.get("symbol")),
                      str(o.get("side", "")).upper(), role, _f(o.get("qty")) or 0.0, filled, price,
                      str(o.get("status", "")), fill, slip, slip_r, trade_id)


def blotter_rows(orders: list[dict], plans: dict[str, dict]) -> list[BlotterRow]:
    groups: list[list[BlotterRow]] = []          # an entry, its legs, then any later exits
    last_entry: dict[str, list[BlotterRow]] = {}
    for o in sorted(orders, key=lambda o: _ts(o.get("submitted_at"))):
        legs = o.get("legs") or []
        is_exit = not legs and o.get("type") == "market"
        if is_exit and o.get("symbol") in last_entry:
            g = last_entry[o["symbol"]]
            e = g[0]
            plan = plans.get(e.trade_id) if e.trade_id else None
            d = int((plan or {}).get("direction") or (1 if e.side == "BUY" else -1))
            g.append(_row(o, "EXIT", e.order_id, e.trade_id, plan, d))
            continue
        cid = str(o.get("client_order_id") or "")
        trade_id = cid if cid.startswith("dc-") else None
        plan = plans.get(trade_id) if trade_id else None
        d = int((plan or {}).get("direction") or (1 if str(o.get("side")) == "buy" else -1))
        if is_exit:
            groups.append([_row(o, "EXIT", None, None, None, d)])
            continue
        g = [_row(o, "ENTRY", None, trade_id, plan, d)]
        for leg in legs:
            role = "STOP" if leg.get("type") in ("stop", "stop_limit") else "TARGET"
            g.append(_row(leg, role, g[0].order_id, trade_id, plan, d))
        groups.append(g)
        last_entry[g[0].symbol] = g
    return [r for g in groups for r in g]


def blotter_summary(rows: list[BlotterRow]) -> dict:
    entries = [r for r in rows if r.role == "ENTRY" and r.slip is not None and r.fill]
    return {
        "orders": len(rows),
        "fills": sum(1 for r in rows if r.filled_qty > 0),
        "entry_slip_bps": (sum(r.slip / r.fill * 1e4 for r in entries) / len(entries)) if entries else None,
        "entry_slip_r": (sum(r.slip_r for r in entries if r.slip_r is not None) / len(entries)) if entries else None,
        "rejected": sum(1 for r in rows if r.status == "rejected"),
    }


class Reconciler:
    """Broker positions vs the bot's view; a mismatch counts once it has lasted grace_s."""

    def __init__(self, grace_s: float = 30.0) -> None:
        self.grace_s = grace_s
        self._since: dict[tuple[str, str], float] = {}

    def update(self, broker_symbols: set[str], bot_symbols: set[str], now: float) -> list[str]:
        current = {(s, "broker") for s in broker_symbols - bot_symbols} | {(s, "bot") for s in bot_symbols - broker_symbols}
        self._since = {k: self._since.get(k, now) for k in current}
        out = []
        for (sym, side), t0 in sorted(self._since.items()):
            if now - t0 >= self.grace_s:
                out.append(f"{sym}: held at the broker but unknown to the bot" if side == "broker"
                           else f"{sym}: the bot has a trade but the broker has no position")
        return out
