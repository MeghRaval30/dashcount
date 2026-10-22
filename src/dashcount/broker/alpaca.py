"""Alpaca broker (paper by default) behind the same interface as the simulator.

Order updates arrive on Alpaca's trade-updates websocket in a background thread;
they are handed to the engine thread through `post` (the engine's inbox), so the
executor only ever runs on one thread.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from ..data.alpaca_data import keys_from_env
from ..data.alpaca_trading import is_paper
from ..models import LONG, Account, OrderUpdate, Position
from . import round_price

log = logging.getLogger(__name__)


def _f(x) -> float:
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


class AlpacaBroker:
    def __init__(self, paper: bool | None = None, client=None, start_stream: bool = True) -> None:
        from alpaca.trading.client import TradingClient

        self.paper = is_paper() if paper is None else paper
        k, s = keys_from_env()
        self.client = client or TradingClient(k, s, paper=self.paper)
        self._listener: Callable[[OrderUpdate], None] | None = None
        self.post: Callable[[Callable[[], None]], None] = lambda fn: fn()
        self.roles: dict[str, str] = {}
        self._stream = None
        self._thread: threading.Thread | None = None
        if start_stream:
            self._start_stream(k, s)

    # ---------------------------------------------------------- stream
    def _start_stream(self, k: str, s: str) -> None:
        from alpaca.trading.stream import TradingStream

        self._stream = TradingStream(k, s, paper=self.paper)
        self._stream.subscribe_trade_updates(self._on_stream)
        self._thread = threading.Thread(target=self._run_stream, name="alpaca-orders", daemon=True)
        self._thread.start()

    def _run_stream(self) -> None:
        while True:
            try:
                self._stream.run()
            except Exception as e:  # reconnect forever
                log.warning("trade-updates stream dropped: %s; reconnecting", e)
                time.sleep(3)

    async def _on_stream(self, data) -> None:
        try:
            u = self._to_update(data)
        except Exception:
            log.exception("bad trade update")
            return
        if u is not None and self._listener is not None:
            self.post(lambda: self._listener(u))

    def _to_update(self, data) -> OrderUpdate | None:
        o = data.order
        oid = str(o.id)
        ev = str(getattr(data.event, "value", data.event))
        mapping = {"new": "new", "fill": "fill", "partial_fill": "partial_fill", "canceled": "canceled",
                   "expired": "expired", "rejected": "rejected", "replaced": "replaced", "done_for_day": "expired"}
        if ev not in mapping:
            return None
        role = self.roles.get(oid) or self._infer_role(o)
        side = str(getattr(o.side, "value", o.side))
        ts = data.timestamp.timestamp() if getattr(data, "timestamp", None) else time.time()
        return OrderUpdate(
            order_id=oid, symbol=o.symbol, event=mapping[ev], role=role, side=side,
            filled_qty=_f(o.filled_qty), filled_avg_price=_f(o.filled_avg_price), ts=ts,
            last_fill_qty=_f(getattr(data, "qty", 0)) if ev in ("fill", "partial_fill") else 0.0,
            last_fill_price=_f(getattr(data, "price", 0)) if ev in ("fill", "partial_fill") else 0.0,
            client_order_id=o.client_order_id or "",
        )

    def _infer_role(self, o) -> str:
        t = str(getattr(o.order_type, "value", o.order_type) or getattr(o.type, "value", o.type))
        if t in ("stop", "stop_limit", "trailing_stop"):
            return "stop"
        if t == "limit":
            return "take_profit"
        return "exit"

    # ------------------------------------------------------------- API
    def set_listener(self, fn: Callable[[OrderUpdate], None]) -> None:
        self._listener = fn

    def account(self) -> Account:
        a = self.client.get_account()
        bp = _f(a.daytrading_buying_power) or _f(a.buying_power)
        return Account(equity=_f(a.equity), buying_power=bp, last_equity=_f(a.last_equity))

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self.client.get_all_positions():
            q = _f(p.qty)
            side = str(getattr(p.side, "value", p.side))
            if side == "short" and q > 0:
                q = -q
            out[p.symbol] = Position(p.symbol, q, _f(p.avg_entry_price))
        return out

    def submit_entry(self, symbol, direction, qty, limit_price, take_profit, stop, client_id) -> str:
        from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY if direction == LONG else OrderSide.SELL,
            type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            limit_price=round_price(limit_price),
            take_profit=TakeProfitRequest(limit_price=round_price(take_profit)),
            stop_loss=StopLossRequest(stop_price=round_price(stop)),
            client_order_id=client_id,
        )
        o = self.client.submit_order(req)
        self.roles[str(o.id)] = "entry"
        for leg in (o.legs or []):
            self.roles[str(leg.id)] = self._infer_role(leg)
        return str(o.id)

    def cancel(self, order_id: str) -> None:
        try:
            self.client.cancel_order_by_id(order_id)
        except Exception as e:
            log.info("cancel %s: %s", order_id, e)

    def _open_orders(self, symbol: str):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        return self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], nested=True))

    def _cancel_symbol_orders(self, symbol: str, wait_s: float = 4.0) -> None:
        ids = []
        for o in self._open_orders(symbol):
            ids.append(str(o.id))
            for leg in (o.legs or []):
                ids.append(str(leg.id))
        for oid in ids:
            self.cancel(oid)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if not list(self._open_orders(symbol)):
                return
            time.sleep(0.25)
        log.warning("orders for %s still open after cancel", symbol)

    def set_exits(self, symbol, qty, direction, take_profit, stop) -> None:
        from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

        self._cancel_symbol_orders(symbol)
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL if direction == LONG else OrderSide.BUY,
            type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, order_class=OrderClass.OCO,
            limit_price=round_price(take_profit),
            take_profit=TakeProfitRequest(limit_price=round_price(take_profit)),
            stop_loss=StopLossRequest(stop_price=round_price(stop)),
        )
        o = self.client.submit_order(req)
        self.roles[str(o.id)] = "take_profit"
        for leg in (o.legs or []):
            self.roles[str(leg.id)] = self._infer_role(leg)

    def close_position(self, symbol: str) -> None:
        self._cancel_symbol_orders(symbol)
        try:
            o = self.client.close_position(symbol)
            if hasattr(o, "id"):
                self.roles[str(o.id)] = "exit"
        except Exception as e:
            log.info("close_position %s: %s", symbol, e)

    def close_all(self) -> None:
        try:
            for r in self.client.close_all_positions(cancel_orders=True) or []:
                body = getattr(r, "body", None)
                if body is not None and hasattr(body, "id"):
                    self.roles[str(body.id)] = "exit"
        except Exception as e:
            log.warning("close_all: %s", e)
