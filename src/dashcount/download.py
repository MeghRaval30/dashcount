"""Historical downloader: fills the local store for practice runs. Resumable.

Order of work (each step skips what is already on disk):
1. daily bars (SIP + IEX) for the universe + SPY/QQQ;
2. Alpaca asset flags (easy-to-borrow);
3. 1-minute SIP bars (incl. extended hours) for every session from ~21 sessions
   before `start` to `end` (ranking, volume profiles, liquidity);
4. earnings calendar for every day touched;
5. per practice day: build the level map from the store, then download that
   day's trades + quotes for the watchlist (per feed) and the previous
   session's 1-minute bars on the same feed (Kronos context).
"""

from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd

from .config import Config
from .data import Store, StoreData
from .data.alpaca_data import AlpacaData
from .earnings import EarningsCalendar
from .levelmap import LevelMap, load_universe
from .timeutil import calendar, parse_date

log = logging.getLogger(__name__)


def _merge_daily(store: Store, feed: str, df: pd.DataFrame) -> None:
    for sym, g in df.groupby("symbol"):
        p = store.daily_path(feed, sym)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            g = pd.concat([pd.read_parquet(p), g]).drop_duplicates(["date"], keep="last")
        g.sort_values("date").to_parquet(p, index=False)


def download(cfg: Config, start, end, feeds: tuple[str, ...] = ("iex",), ticks: bool = True,
             data: AlpacaData | None = None) -> None:
    start, end = parse_date(start), parse_date(end)
    cal = calendar()
    store = Store()
    data = data or AlpacaData()
    uni = load_universe()
    syms = sorted(set(uni["symbol"]) | set(cfg.universe.market_symbols))
    days = cal.sessions(start, end)
    if not days:
        log.warning("no sessions between %s and %s", start, end)
        return
    first = cal.previous_sessions(days[0], 31)[0]
    t0 = time.perf_counter()

    # 1. daily bars ------------------------------------------------------
    for feed in ("sip", "iex"):
        have = [s for s in syms if store.daily_path(feed, s).exists()]
        cover_ok = False
        if len(have) == len(syms):
            sample = pd.read_parquet(store.daily_path(feed, syms[0]))
            cover_ok = len(sample) and sample["date"].min() <= first and sample["date"].max() >= days[-1]
        if not cover_ok:
            log.info("daily bars (%s) %s..%s for %d symbols", feed, first, end, len(syms))
            _merge_daily(store, feed, data.daily(syms, first - dt.timedelta(days=10), end, feed))

    # 2. assets ------------------------------------------------------------
    if not store.assets_path().exists():
        try:
            a = data.assets()
            store.assets_path().parent.mkdir(parents=True, exist_ok=True)
            a.to_parquet(store.assets_path(), index=False)
            log.info("asset flags: %d", len(a))
        except Exception as e:
            log.warning("could not fetch asset flags (%s); shorts will be disabled in practice", e)

    # 3. minute SIP bars ----------------------------------------------------
    minute_days = cal.sessions(first, end)
    for i, d in enumerate(minute_days):
        p = store.minute_path("sip", d)
        if p.exists():
            continue
        s = cal.session(d)
        df = data.minutes(syms, s.open - 5.5 * 3600, s.close + 4 * 3600, "sip")
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p, index=False)
        log.info("minute bars %s: %d rows (%d/%d, %d requests so far)", d, len(df), i + 1, len(minute_days), data.requests)

    # 4. earnings ------------------------------------------------------------
    ec = EarningsCalendar(store.earnings_dir())
    for d in cal.sessions(first, cal.next_session(end)):
        ec.symbols_on(d)

    if not ticks:
        return
    # 5. per-day watchlist ticks -------------------------------------------
    lm = LevelMap(cfg, StoreData(store), EarningsCalendar(store.earnings_dir(), offline=True), uni)
    for i, d in enumerate(days):
        s = cal.session(d)
        for feed in feeds:
            plan = lm.build(d, feed)
            want = [x for x in plan.symbols if not store.trades_path(feed, d, x).exists()]
            if want:
                tdir = store.ticks_dir(feed, d)
                tdir.mkdir(parents=True, exist_ok=True)
                trades = data.trades(want, s.open, s.close, feed)
                quotes = data.quotes(want, s.open, s.close, feed)
                for x in want:
                    q = quotes.get(x, pd.DataFrame(columns=["ts", "bid", "ask", "bid_size", "ask_size"]))
                    q.to_parquet(store.quotes_path(feed, d, x), index=False)
                    t = trades.get(x, pd.DataFrame(columns=["ts", "price", "size"]))
                    t.to_parquet(store.trades_path(feed, d, x), index=False)
            if feed != "sip":
                prev = cal.previous_sessions(d, 1)[0]
                pp = store.minute_path(feed, prev)
                existing = pd.read_parquet(pp) if pp.exists() else pd.DataFrame(columns=["symbol"])
                need = [x for x in plan.symbols if x not in set(existing["symbol"])]
                if need:
                    ps = cal.session(prev)
                    df = data.minutes(need, ps.open, ps.close, feed)
                    pp.parent.mkdir(parents=True, exist_ok=True)
                    pd.concat([existing, df], ignore_index=True).to_parquet(pp, index=False)
        log.info("ticks %s done (%d/%d) - %d requests, %.0f min elapsed", d, i + 1, len(days), data.requests,
                 (time.perf_counter() - t0) / 60)
