"""Read-only Alpaca orders and positions for the DESK, polled on a background thread.

Only GET requests: the dashboard never places or cancels orders. The live bot
shares Alpaca's 200 requests/minute, so this stays at 2 requests every 10 s in
market hours and backs off on errors.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
IN_SESSION_S = 10.0
IDLE_S = 300.0
BACKOFF_S = (30.0, 60.0)          # after one failure, after two or more in a row
SESSION_MARGIN_S = 1800.0         # poll fast from 30 min before the open to 30 min after the close
TIMEOUT_S = 5.0
ERROR_WINDOW_S = 600.0


@dataclass(frozen=True)
class FeedState:
    configured: bool
    ok: bool
    orders: list[dict]
    positions: list[dict]
    last_ok_ts: float | None
    last_latency_ms: float | None
    errors_10min: int
    last_error: str


class BrokerFeed:
    def __init__(self, key: str, secret: str, paper: bool = True, client: httpx.Client | None = None,
                 clock=time.time, session: Callable[[], tuple[float, float] | None] = lambda: None) -> None:
        self.client = client or httpx.Client(
            base_url=PAPER_URL if paper else LIVE_URL, timeout=TIMEOUT_S,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        self.clock = clock
        self.session = session
        self._lock = threading.Lock()
        self._state = FeedState(True, False, [], [], None, None, 0, "")
        self._errors: deque[float] = deque()
        self._fails = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- polling
    def poll_once(self) -> float:
        now = self.clock()
        after = dt.datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        t0 = time.perf_counter()
        try:
            r = self.client.get("/v2/orders", params={"status": "all", "nested": "true", "direction": "asc",
                                                      "limit": 500, "after": after})
            if r.status_code != 200:
                return self._fail(f"orders: HTTP {r.status_code}", r)
            orders = r.json()
            p = self.client.get("/v2/positions")
            if p.status_code != 200:
                return self._fail(f"positions: HTTP {p.status_code}", p)
            positions = p.json()
        except Exception as e:                      # timeouts, network, bad JSON: all just errors
            return self._fail(f"{type(e).__name__}: {e}", None)
        self._fails = 0
        self._set(ok=True, orders=orders, positions=positions, last_ok_ts=now,
                  last_latency_ms=(time.perf_counter() - t0) * 1000, last_error="")
        return self._due(IN_SESSION_S if self._in_session(now) else IDLE_S)

    def _in_session(self, now: float) -> bool:
        s = self.session()
        return bool(s) and s[0] - SESSION_MARGIN_S <= now <= s[1] + SESSION_MARGIN_S

    def _due(self, wait: float) -> float:
        self._next_due = self.clock() + wait
        return wait

    def due_in(self) -> float:
        """Seconds until the next poll; at once if the session started during a long idle wait."""
        now = self.clock()
        left = getattr(self, "_next_due", now) - now
        if left > IN_SESSION_S and self._in_session(now):
            return 0.0
        return max(left, 0.0)

    def _fail(self, msg: str, resp: httpx.Response | None) -> float:
        now = self.clock()
        self._fails += 1
        self._errors.append(now)
        self._set(ok=False, last_error=msg[:200])
        if resp is not None and resp.status_code == 429:
            reset = float(resp.headers.get("X-RateLimit-Reset") or now + 5)
            return self._due(max(reset - now, 1.0))
        return self._due(BACKOFF_S[0] if self._fails == 1 else BACKOFF_S[1])

    def _set(self, **kw) -> None:
        now = self.clock()
        while self._errors and now - self._errors[0] > ERROR_WINDOW_S:
            self._errors.popleft()
        with self._lock:
            self._state = replace(self._state, errors_10min=len(self._errors), **kw)

    def state(self) -> FeedState:
        with self._lock:
            return self._state

    # ----------------------------------------------------------------- thread
    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                self.poll_once()
                # wake at least every 30 s, so the start of the session is noticed quickly
                while not self._stop.is_set() and (left := self.due_in()) > 0:
                    self._stop.wait(min(left, 30.0))     # returns at once when stop() is called

        self._thread = threading.Thread(target=loop, daemon=True, name="broker-feed")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def feed_from_env(root: Path, session: Callable[[], tuple[float, float] | None]) -> BrokerFeed | None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(root) / ".env")
    except Exception:
        pass
    key, secret = os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    paper = os.environ.get("ALPACA_PAPER", "true").strip().lower() not in ("false", "0", "no")
    return BrokerFeed(key, secret, paper=paper, session=session)


class DemoFeed:
    """Filled bracket orders made from the demo journal, so the DESK can be previewed offline."""

    def __init__(self, snap: dict, journal: list[dict]) -> None:
        orders = []
        for i, e in enumerate(x for x in journal if x.get("kind") == "taken"):
            d = int(e.get("direction") or 1)
            qty = int(e.get("qty") or 1)
            entry = float(e.get("entry") or 0.0)
            sym = str(e.get("symbol"))
            ts = dt.datetime.fromtimestamp(float(e.get("ts") or 0), dt.timezone.utc).isoformat().replace("+00:00", "Z")
            side, exit_side = ("buy", "sell") if d > 0 else ("sell", "buy")

            def leg(kind: str, typ: str, price, k: int) -> dict:
                return {"id": f"demo-{i}-{kind}", "client_order_id": f"demo-{i}-{kind}", "symbol": sym,
                        "side": exit_side, "type": typ, "qty": str(qty), "filled_qty": "0", "filled_avg_price": None,
                        "limit_price": str(price) if typ == "limit" else None,
                        "stop_price": str(price) if typ == "stop" else None, "status": "new", "submitted_at": ts}

            orders.append({
                "id": f"demo-{i}", "client_order_id": e.get("trade_id") or f"dc-demo-{i}", "symbol": sym, "side": side,
                "type": "limit", "qty": str(qty), "filled_qty": str(qty),
                "filled_avg_price": str(round(entry * (1 + 0.0002 * d), 4)), "limit_price": str(entry),
                "stop_price": None, "status": "filled", "submitted_at": ts,
                "legs": [leg("tp", "limit", e.get("target"), 0), leg("sl", "stop", e.get("stop"), 1)],
            })
        positions = [{"symbol": t["symbol"], "qty": str(int(t.get("qty") or 0) * int(t.get("direction") or 1))}
                     for t in snap.get("trades") or []]
        self._state = FeedState(True, True, orders, positions, time.time(), 12.0, 0, "")

    def state(self) -> FeedState:
        return self._state

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
