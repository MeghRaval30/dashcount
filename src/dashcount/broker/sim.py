"""Harsh simulated broker for practice runs.

Deliberately pessimistic so practice profit is closer to what real money would
get than a paper account is:
* orders go live only after a latency;
* market and triggered stop orders fill at the worse side of the quote (or the
  triggering print, if worse) plus size-dependent slippage;
* resting limit orders fill only when the price trades *through* the limit;
* SEC fee and FINRA TAF on every sell.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable

from ..models import LONG, Account, OrderUpdate, Position, Quote, Trade
from . import round_price


@dataclass
class SimOrder:
    id: str
    symbol: str
    side: str                 # buy | sell
    qty: float
    kind: str                 # market | limit | stop
    role: str                 # entry | take_profit | stop | exit
    limit: float | None
    stop: float | None
    active_from: float
    client_id: str = ""
    status: str = "pending"   # pending -> open -> filled | canceled
    filled_qty: float = 0.0
    avg_price: float = 0.0
    oco: str | None = None
    bracket: tuple | None = None   # (take_profit, stop) for entries
    children: list[str] = field(default_factory=list)


class SimBroker:
    def __init__(self, cfg_sim, starting_equity: float | None = None) -> None:
        self.cfg = cfg_sim
        self.cash = starting_equity if starting_equity is not None else cfg_sim.starting_equity
        self.pos: dict[str, list[float]] = {}          # symbol -> [qty, avg_price]
        self.orders: dict[str, SimOrder] = {}
        self.marks: dict[str, float] = {}
        self.quotes: dict[str, Quote] = {}
        self.now = 0.0
        self.last_equity = self.cash
        self.fees_paid = 0.0
        self._ids = itertools.count(1)
        self._listener: Callable[[OrderUpdate], None] | None = None
        # set by the engine: expected SIP volume per minute for slippage
        self.normal_volume_fn: Callable[[str, float], float | None] = lambda s, t: None
        # set by the engine on the IEX feed: typical NBBO spread (bps) per symbol
        self.spread_bps_fn: Callable[[str], float | None] = lambda s: None

    # ------------------------------------------------------------------ API
    def set_listener(self, fn: Callable[[OrderUpdate], None]) -> None:
        self._listener = fn

    def start_day(self) -> None:
        self.last_equity = self.equity()

    def account(self) -> Account:
        eq = self.equity()
        gross = sum(abs(q) * self.marks.get(s, p) for s, (q, p) in self.pos.items())
        return Account(equity=eq, buying_power=max(4 * eq - gross, 0.0), last_equity=self.last_equity)

    def positions(self) -> dict[str, Position]:
        return {s: Position(s, q, p) for s, (q, p) in self.pos.items() if q != 0}

    def submit_entry(self, symbol, direction, qty, limit_price, take_profit, stop, client_id) -> str:
        side = "buy" if direction == LONG else "sell"
        o = self._new(symbol, side, qty, "limit", "entry", round_price(limit_price), None, client_id)
        o.bracket = (round_price(take_profit), round_price(stop))
        return o.id

    def cancel(self, order_id: str) -> None:
        o = self.orders.get(order_id)
        if o and o.status in ("pending", "open"):
            o.status = "canceled"
            self._emit(o, "canceled")

    def set_exits(self, symbol, qty, direction, take_profit, stop) -> None:
        self._cancel_symbol(symbol, roles=("take_profit", "stop", "exit"))
        self._bracket_children(symbol, qty, direction, take_profit, stop, self.now + self.cfg.latency_ms / 1000)

    def close_position(self, symbol: str) -> None:
        self._cancel_symbol(symbol, roles=("entry", "take_profit", "stop", "exit"))
        q = self.pos.get(symbol, [0.0, 0.0])[0]
        if q != 0:
            self._new(symbol, "sell" if q > 0 else "buy", abs(q), "market", "exit", None, None, "")

    def close_all(self) -> None:
        syms = {o.symbol for o in self.orders.values() if o.status in ("pending", "open")} | {
            s for s, (q, _) in self.pos.items() if q != 0
        }
        for s in sorted(syms):
            self.close_position(s)

    # ------------------------------------------------------------- market
    def equity(self) -> float:
        return self.cash + sum(q * self.marks.get(s, p) for s, (q, p) in self.pos.items())

    def on_quote(self, q: Quote) -> None:
        self.quotes[q.symbol] = q
        self.advance(q.ts)

    def on_trade(self, t: Trade) -> None:
        self.marks[t.symbol] = t.price
        self.advance(t.ts)
        for o in self._open_for(t.symbol):
            self._match_print(o, t)

    def advance(self, now: float) -> None:
        if now < self.now:
            return
        self.now = now
        for o in list(self.orders.values()):
            if o.status == "pending" and o.active_from <= now:
                o.status = "open"
                self._on_activate(o)

    # ------------------------------------------------------------ internals
    def _new(self, symbol, side, qty, kind, role, limit, stop, client_id, oco=None) -> SimOrder:
        oid = f"sim-{next(self._ids)}"
        o = SimOrder(oid, symbol, side, float(qty), kind, role, limit, stop,
                     self.now + self.cfg.latency_ms / 1000, client_id, oco=oco)
        self.orders[oid] = o
        self._emit(o, "new")
        return o

    def _open_for(self, symbol: str) -> list[SimOrder]:
        return [o for o in self.orders.values() if o.symbol == symbol and o.status == "open"]

    def _cancel_symbol(self, symbol: str, roles) -> None:
        for o in list(self.orders.values()):
            if o.symbol == symbol and o.role in roles and o.status in ("pending", "open"):
                o.status = "canceled"
                self._emit(o, "canceled")

    def _slip(self, symbol: str, qty: float, price: float) -> float:
        nv = self.normal_volume_fn(symbol, self.now)
        bps = self.cfg.base_slippage_bps + (self.cfg.impact_bps * qty / nv if nv else 0.0)
        return price * min(bps, self.cfg.max_slippage_bps) / 1e4

    def _quote(self, symbol: str) -> Quote | None:
        """Latest fresh quote, narrowed to the estimated NBBO when the feed's book
        is only an upper bound (IEX): the real best prices are never worse."""
        q = self.quotes.get(symbol)
        if not (q and q.bid > 0 and q.ask >= q.bid and self.now - q.ts <= 5):
            return None
        bps = self.spread_bps_fn(symbol)
        if bps:
            mid = (q.bid + q.ask) / 2
            half = min((q.ask - q.bid) / 2, max(0.005, mid * bps / 2e4))
            return Quote(q.symbol, q.ts, mid - half, mid + half, q.bid_size, q.ask_size)
        return q

    def _on_activate(self, o: SimOrder) -> None:
        q = self._quote(o.symbol)
        if o.kind == "market":
            ref = (q.ask if o.side == "buy" else q.bid) if q else self.marks.get(o.symbol)
            if ref:
                self._fill_at(o, ref, slip=True)
        elif o.kind == "limit" and q:
            if o.side == "buy" and q.ask <= o.limit:
                self._fill_at(o, min(q.ask + self._slip(o.symbol, o.qty, q.ask), o.limit), slip=False)
            elif o.side == "sell" and q.bid >= o.limit:
                self._fill_at(o, max(q.bid - self._slip(o.symbol, o.qty, q.bid), o.limit), slip=False)

    def _match_print(self, o: SimOrder, t: Trade) -> None:
        if o.status != "open":
            return
        if o.kind == "market":
            q = self._quote(o.symbol)
            ref = max(t.price, q.ask) if (q and o.side == "buy") else (min(t.price, q.bid) if q else t.price)
            self._fill_at(o, ref, slip=True)
        elif o.kind == "limit":
            if o.side == "buy" and t.price < o.limit:
                self._fill_at(o, o.limit, slip=False)
            elif o.side == "sell" and t.price > o.limit:
                self._fill_at(o, o.limit, slip=False)
        elif o.kind == "stop":
            q = self._quote(o.symbol)
            if o.side == "sell" and t.price <= o.stop:
                ref = min(t.price, q.bid) if q else t.price
                self._fill_at(o, ref, slip=True)
            elif o.side == "buy" and t.price >= o.stop:
                ref = max(t.price, q.ask) if q else t.price
                self._fill_at(o, ref, slip=True)

    def _fill_at(self, o: SimOrder, price: float, slip: bool) -> None:
        if slip:
            s = self._slip(o.symbol, o.qty, price)
            price = price + s if o.side == "buy" else price - s
        price = round(price, 4)
        qty = o.qty - o.filled_qty
        if qty <= 0:
            return
        fees = 0.0
        if o.side == "sell":
            fees = price * qty * self.cfg.sec_fee_per_million / 1e6 + min(qty * self.cfg.taf_per_share, self.cfg.taf_cap)
        self._apply(o.symbol, qty if o.side == "buy" else -qty, price, fees)
        o.filled_qty += qty
        o.avg_price = price
        o.status = "filled"
        self._emit(o, "fill", qty, price)
        if o.role == "entry" and o.bracket:
            tp, sl = o.bracket
            direction = 1 if o.side == "buy" else -1
            self._bracket_children(o.symbol, o.filled_qty, direction, tp, sl, self.now)
        if o.oco:
            for sib in list(self.orders.values()):
                if sib.oco == o.oco and sib.id != o.id and sib.status in ("pending", "open"):
                    sib.status = "canceled"
                    self._emit(sib, "canceled")

    def _bracket_children(self, symbol, qty, direction, tp, sl, active_from) -> None:
        if qty <= 0:
            return
        exit_side = "sell" if direction == LONG else "buy"
        group = f"oco-{next(self._ids)}"
        a = self._new(symbol, exit_side, qty, "limit", "take_profit", round_price(tp), None, "", oco=group)
        b = self._new(symbol, exit_side, qty, "stop", "stop", None, round_price(sl), "", oco=group)
        a.active_from = b.active_from = active_from
        if active_from <= self.now:
            for o in (a, b):
                o.status = "open"

    def _apply(self, symbol: str, dq: float, price: float, fees: float) -> None:
        q, avg = self.pos.get(symbol, [0.0, 0.0])
        nq = q + dq
        if q == 0 or (q > 0) == (dq > 0):
            avg = (q * avg + dq * price) / nq if nq != 0 else 0.0
        elif nq != 0 and (nq > 0) != (q > 0):
            avg = price                       # flipped through zero
        self.pos[symbol] = [nq, avg if nq != 0 else 0.0]
        self.cash -= dq * price + fees
        self.fees_paid += fees
        self.marks.setdefault(symbol, price)

    def _emit(self, o: SimOrder, event: str, last_qty: float = 0.0, last_price: float = 0.0) -> None:
        if self._listener:
            self._listener(OrderUpdate(
                order_id=o.id, symbol=o.symbol, event=event, role=o.role, side=o.side,
                filled_qty=o.filled_qty, filled_avg_price=o.avg_price, ts=self.now,
                last_fill_qty=last_qty, last_fill_price=last_price, client_order_id=o.client_id,
            ))
