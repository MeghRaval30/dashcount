"""Brokers: the same interface for Alpaca paper/live and the practice simulator."""

from __future__ import annotations

from typing import Callable, Protocol

from ..models import Account, OrderUpdate, Position


class Broker(Protocol):
    """What the executor and guardian need from a broker."""

    def set_listener(self, fn: Callable[[OrderUpdate], None]) -> None: ...

    def account(self) -> Account: ...

    def positions(self) -> dict[str, Position]: ...

    def submit_entry(
        self, symbol: str, direction: int, qty: int, limit_price: float,
        take_profit: float, stop: float, client_id: str,
    ) -> str: ...

    def cancel(self, order_id: str) -> None: ...

    def set_exits(self, symbol: str, qty: int, direction: int, take_profit: float, stop: float) -> None: ...

    def close_position(self, symbol: str) -> None: ...

    def close_all(self) -> None: ...


def round_price(p: float) -> float:
    """US stocks >= $1 trade in cents."""
    return round(p + 1e-9, 2)
