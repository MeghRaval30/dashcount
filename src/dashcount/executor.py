"""Executor: turns approved candidates into bracket orders and runs the exits
our own code is responsible for (count flip, stale, checkpoint, end of day)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from . import exits
from .broker import Broker
from .models import Candidate, OrderUpdate

log = logging.getLogger(__name__)


@dataclass
class LiveTrade:
    trade_id: str
    attempt_id: str
    symbol: str
    direction: int
    pad: str
    planned_entry: float
    planned_stop: float
    target: float
    qty_req: int
    submitted_ts: float
    meta: dict = field(default_factory=dict)
    state: str = "pending"            # pending -> open -> closing -> closed
    entry_order_id: str = ""
    qty_filled: float = 0.0
    avg_entry: float = 0.0
    filled_ts: float = 0.0
    track: exits.Track | None = None
    exit_qty: float = 0.0
    exit_value: float = 0.0
    exit_reason: str = ""
    close_reason: str = ""            # reason we asked to close (soft exits)
    closed_ts: float = 0.0
    entry_cancel_requested: bool = False

    @property
    def open_risk(self) -> float:
        qty = self.qty_filled if self.state in ("open", "closing") else self.qty_req
        stop = self.track.stop if self.track else self.planned_stop
        entry = self.avg_entry or self.planned_entry
        return max((entry - stop) * self.direction, 0.0) * qty

    @property
    def exit_avg(self) -> float:
        return self.exit_value / self.exit_qty if self.exit_qty else 0.0

    def pnl(self) -> float:
        return self.direction * (self.exit_avg - self.avg_entry) * min(self.exit_qty, self.qty_filled)


class Executor:
    def __init__(
        self,
        exec_cfg,
        broker: Broker,
        on_closed: Callable[[LiveTrade], None],
        flatten_at: float,
        alert: Callable[[str], None] = lambda m: None,
    ) -> None:
        self.cfg = exec_cfg
        self.broker = broker
        self.on_closed = on_closed
        self.flatten_at = flatten_at
        self.alert = alert
        self.trades: dict[str, LiveTrade] = {}
        self.by_order: dict[str, str] = {}
        self.by_symbol: dict[str, str] = {}
        self.closed: list[LiveTrade] = []
        self._n = 0
        broker.set_listener(self.on_order_update)

    # ------------------------------------------------------------ queries
    def busy(self, symbol: str) -> bool:
        return symbol in self.by_symbol

    def open_risk_dollars(self) -> float:
        return sum(t.open_risk for t in self.trades.values() if t.state != "closed")

    def active(self) -> list[LiveTrade]:
        return [t for t in self.trades.values() if t.state != "closed"]

    # -------------------------------------------------------------- entry
    def enter(self, c: Candidate, qty: int, now: float, meta: dict | None = None) -> LiveTrade:
        self._n += 1
        tid = f"dc-{int(now)}-{self._n}"
        t = LiveTrade(
            trade_id=tid, attempt_id=c.attempt_id, symbol=c.symbol, direction=c.direction, pad=c.pad,
            planned_entry=c.entry, planned_stop=c.stop, target=c.target, qty_req=qty,
            submitted_ts=now, meta=meta or {},
        )
        self.trades[tid] = t
        self.by_symbol[c.symbol] = tid
        limit = c.entry + c.direction * self.cfg.entry_limit_r * c.risk
        try:
            oid = self.broker.submit_entry(c.symbol, c.direction, qty, limit, c.target, c.stop, tid)
        except Exception as e:  # broker said no
            log.warning("entry rejected for %s: %s", c.symbol, e)
            t.state = "closed"
            t.exit_reason = "rejected"
            self._finish(t, now)
            return t
        t.entry_order_id = oid
        self.by_order[oid] = tid
        return t

    # ------------------------------------------------------- order events
    def on_order_update(self, u: OrderUpdate) -> None:
        tid = self.by_order.get(u.order_id)
        if tid is None and u.client_order_id in self.trades:
            tid = u.client_order_id
        if tid is None:
            tid = self.by_symbol.get(u.symbol)
            if tid is None or u.role == "entry":
                return
            self.by_order[u.order_id] = tid
        t = self.trades.get(tid)
        if t is None or t.state == "closed":
            return
        if u.role == "entry":
            self._on_entry_update(t, u)
        elif u.event in ("fill", "partial_fill") and u.last_fill_qty > 0:
            self._on_exit_fill(t, u)

    def _on_entry_update(self, t: LiveTrade, u: OrderUpdate) -> None:
        if u.event in ("fill", "partial_fill"):
            t.qty_filled = u.filled_qty
            t.avg_entry = u.filled_avg_price
            if t.filled_ts == 0.0:
                t.filled_ts = u.ts
            if u.event == "fill" or t.qty_filled >= t.qty_req:
                self._open(t, u.ts)
        elif u.event in ("canceled", "expired", "rejected"):
            if t.qty_filled > 0:
                self._open(t, u.ts)
                if t.close_reason:
                    t.state = "closing"          # a market exit is already on its way
                else:
                    # partial fill then cancel: protect exactly what we hold
                    self.broker.set_exits(t.symbol, int(t.qty_filled), t.direction, t.target, t.track.stop)
            else:
                t.state = "closed"
                t.exit_reason = "unfilled" if u.event != "rejected" else "rejected"
                self._finish(t, u.ts)

    def _open(self, t: LiveTrade, now: float) -> None:
        if t.state != "pending":
            return
        t.state = "open"
        risk = abs(t.avg_entry - t.planned_stop) or abs(t.planned_entry - t.planned_stop)
        t.track = exits.Track(t.direction, t.avg_entry, t.planned_stop, t.target, risk, t.filled_ts or now)

    def _on_exit_fill(self, t: LiveTrade, u: OrderUpdate) -> None:
        t.exit_qty += u.last_fill_qty
        t.exit_value += u.last_fill_qty * u.last_fill_price
        if t.exit_qty + 1e-9 >= t.qty_filled > 0:
            if t.close_reason:
                t.exit_reason = t.close_reason
            elif u.role == "take_profit":
                t.exit_reason = "target"
            elif u.role == "stop":
                t.exit_reason = "checkpoint_stop" if (t.track and t.track.checkpoint_done) else "stop"
            else:
                t.exit_reason = "exit"
            t.state = "closed"
            self._finish(t, u.ts)

    def _finish(self, t: LiveTrade, now: float) -> None:
        t.closed_ts = now
        if self.by_symbol.get(t.symbol) == t.trade_id:
            del self.by_symbol[t.symbol]
        self.closed.append(t)
        self.on_closed(t)

    # ------------------------------------------------------ price / time
    def on_price(self, symbol: str, price: float, now: float, tc_dir: float | None) -> None:
        tid = self.by_symbol.get(symbol)
        if tid is None:
            return
        t = self.trades[tid]
        if t.state != "open" or t.track is None:
            return
        tr = t.track
        exits.update_excursions(tr, price)
        if exits.checkpoint_due(tr, price, self.cfg.checkpoint_r):
            exits.apply_checkpoint(tr)
            try:
                self.broker.set_exits(symbol, int(t.qty_filled), t.direction, t.target, tr.stop)
            except Exception as e:
                log.warning("checkpoint failed for %s: %s", symbol, e)
        reason = exits.soft_exit(tr, now, tc_dir, self.flatten_at, self.cfg)
        if reason:
            self.request_close(t, reason)

    def request_close(self, t: LiveTrade, reason: str) -> None:
        if t.state not in ("open", "pending") or t.close_reason:
            return
        t.close_reason = reason
        if t.state == "open":
            t.state = "closing"
        self.broker.close_position(t.symbol)

    def on_time(self, now: float) -> None:
        for t in list(self.trades.values()):
            if t.state == "pending" and not t.entry_cancel_requested and now - t.submitted_ts >= self.cfg.entry_timeout_s:
                t.entry_cancel_requested = True
                self.broker.cancel(t.entry_order_id)
            elif t.state == "open" and now >= self.flatten_at:
                self.request_close(t, "eod")

    def flatten_all(self, reason: str) -> None:
        for t in list(self.trades.values()):
            if t.state in ("pending", "open"):
                if t.state == "pending" and t.qty_filled == 0:
                    t.entry_cancel_requested = True
                t.close_reason = t.close_reason or reason
                if t.state == "open":
                    t.state = "closing"
        self.broker.close_all()
