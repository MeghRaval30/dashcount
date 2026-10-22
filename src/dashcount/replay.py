"""Replay a day's trades and quotes through the engine in time order."""

from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from .data import Store
from .engine import Engine
from .models import Quote, Trade

log = logging.getLogger(__name__)


def load_ticks(store: Store, feed: str, day, symbols: list[str]) -> tuple[dict, dict, list[str]]:
    trades, quotes, missing = {}, {}, []
    for s in symbols:
        tp, qp = store.trades_path(feed, day, s), store.quotes_path(feed, day, s)
        if not tp.exists():
            missing.append(s)
            continue
        trades[s] = pd.read_parquet(tp)
        quotes[s] = pd.read_parquet(qp) if qp.exists() else pd.DataFrame(columns=["ts", "bid", "ask", "bid_size", "ask_size"])
    return trades, quotes, missing


def replay(engine: Engine, trades: dict[str, pd.DataFrame], quotes: dict[str, pd.DataFrame],
           progress: bool = False) -> int:
    """Feed every event between the open and the close to the engine. Returns the event count."""
    s = engine.session
    syms = sorted(set(trades) | set(quotes))
    sym_id = {x: i for i, x in enumerate(syms)}
    parts = []
    for x in syms:
        q = quotes.get(x)
        if q is not None and len(q):
            parts.append(pd.DataFrame({"ts": q["ts"].to_numpy(), "kind": 0, "sym": sym_id[x],
                                       "a": q["bid"].to_numpy(), "b": q["ask"].to_numpy(),
                                       "c": q["bid_size"].to_numpy(), "d": q["ask_size"].to_numpy()}))
        t = trades.get(x)
        if t is not None and len(t):
            parts.append(pd.DataFrame({"ts": t["ts"].to_numpy(), "kind": 1, "sym": sym_id[x],
                                       "a": t["price"].to_numpy(), "b": t["size"].to_numpy(),
                                       "c": 0.0, "d": 0.0}))
    if not parts:
        engine.finish()
        return 0
    ev = pd.concat(parts, ignore_index=True)
    ev = ev[(ev["ts"] >= s.open) & (ev["ts"] < s.close)]
    ev = ev.sort_values(["ts", "kind"], kind="stable")
    ts = ev["ts"].to_numpy(dtype=np.float64).tolist()
    kind = ev["kind"].to_numpy().tolist()
    sid = ev["sym"].to_numpy().tolist()
    a = ev["a"].to_numpy(dtype=np.float64).tolist()
    b = ev["b"].to_numpy(dtype=np.float64).tolist()
    c = ev["c"].to_numpy(dtype=np.float64).tolist()
    d = ev["d"].to_numpy(dtype=np.float64).tolist()
    on_trade, on_quote = engine.on_trade, engine.on_quote
    t0 = time.perf_counter()
    n = len(ts)
    for i in range(n):
        if kind[i]:
            on_trade(Trade(syms[sid[i]], ts[i], a[i], b[i]))
        else:
            on_quote(Quote(syms[sid[i]], ts[i], a[i], b[i], c[i], d[i]))
        if progress and i % 500_000 == 0 and i:
            log.info("  %d/%d events (%.0f/s)", i, n, i / (time.perf_counter() - t0))
    engine.finish()
    return n
