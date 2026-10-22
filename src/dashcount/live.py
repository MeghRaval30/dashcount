"""Live runner: one trading day on Alpaca (paper by default).

09:00 ET start -> warm up the models -> 09:20 build the level map ->
stream IEX trades/quotes for the watchlist -> run the engine until the close ->
write the scoreboard. The Guardian runs as a separate process.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import queue
import threading
import time

from . import notify
from .broker.alpaca import AlpacaBroker
from .config import ROOT, Config
from .data import Store
from .data.alpaca_data import AlpacaData, keys_from_env
from .data.alpaca_trading import is_paper
from .earnings import EarningsCalendar
from .engine import Engine, VetoScore
from .factory import load_memory, make_chain, records_root
from .guardian import GuardianState
from .levelmap import LevelMap
from .models import Quote, Trade
from .recorder import Recorder
from .scoreboard import console, write_report
from .state import (Capital, beat, bot_positions_path, capital_path, guardian_path, journal_path, read_json,
                    snapshot_path, stop_path, veto_score_path, write_json)
from .timeutil import ET, calendar, et_ts, fmt_et

log = logging.getLogger(__name__)


def _sleep_until(ts: float, why: str) -> None:
    while True:
        left = ts - time.time()
        if left <= 0:
            return
        log.info("waiting %.0f min for %s (%s)", left / 60, why, fmt_et(ts))
        beat({"status": f"waiting:{why}"})
        time.sleep(min(left, 60))


class LiveFeed:
    """Alpaca IEX websocket -> thread-safe event queue."""

    def __init__(self, symbols: list[str], out: queue.Queue, trades_only: tuple[str, ...] = ()) -> None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream

        k, s = keys_from_env()
        self.stream = StockDataStream(k, s, feed=DataFeed.IEX)
        self.out = out
        self.stream.subscribe_trades(self._on_trade, *symbols)
        # the market funds only feed the count, so they don't need quotes (saves stream slots)
        self.stream.subscribe_quotes(self._on_quote, *[s for s in symbols if s not in trades_only])
        self.thread = threading.Thread(target=self._run, name="alpaca-data", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                self.stream.run()
            except Exception as e:
                log.warning("market-data stream dropped: %s; reconnecting", e)
                time.sleep(3)

    async def _on_trade(self, t) -> None:
        conds = set(t.conditions or [])
        from .data import BAD_CONDITIONS

        if conds & BAD_CONDITIONS:
            return
        self.out.put(Trade(t.symbol, t.timestamp.timestamp(), float(t.price), float(t.size)))

    async def _on_quote(self, q) -> None:
        self.out.put(Quote(q.symbol, q.timestamp.timestamp(), float(q.bid_price), float(q.ask_price),
                           float(q.bid_size), float(q.ask_size)))

    def stop(self) -> None:
        try:
            self.stream.stop()
        except Exception:
            pass


def run_live(cfg: Config, real_money: bool = False, vetoes: str = "full", day: dt.date | None = None) -> None:
    paper = is_paper()
    if not paper and not real_money:
        raise SystemExit("ALPACA_PAPER=false but --real-money was not given. Refusing to trade real money.")
    if paper and real_money:
        raise SystemExit("--real-money given but ALPACA_PAPER is not false. Set it in .env deliberately.")
    cal = calendar()
    today = dt.datetime.now(ET).date() if day is None else day
    if not cal.is_session(today):
        log.info("%s is not a trading day. Nothing to do.", today)
        return
    session = cal.session(today)
    keep_awake(True)
    acct_kind = "paper" if paper else "REAL-MONEY"
    notify.send(f"DashCount starting ({acct_kind}, {cfg.mode} mode) for {today}")

    chain = make_chain(cfg, vetoes, preload=False)
    if chain is not None:
        threading.Thread(target=_warm, args=(chain,), daemon=True).start()

    _sleep_until(et_ts(today, cfg.level_map.rank_time), "level map")
    store = Store()
    lm = LevelMap(cfg, AlpacaData(), EarningsCalendar(store.earnings_dir()))
    plan = lm.build(today, "iex")
    log.info("watchlist: %s", ", ".join(plan.symbols))
    log.info("flags: %s | earnings known: %s | removed for earnings: %s", plan.flags, plan.earnings_known,
             plan.removed_earnings)

    run_id = f"live-{acct_kind.lower()}"
    rec = Recorder(records_root(), run_id, cfg.version())
    rec.write_meta({"run_id": run_id, "kind": "live", "account": acct_kind, "mode": cfg.mode, "feed": "iex",
                    "vetoes": vetoes, "version": cfg.version(), "config": cfg.model_dump()})
    with (rec.root / "plans.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(plan.summary(), default=str) + "\n")

    broker = AlpacaBroker(paper=paper)
    score = VetoScore(read_json(veto_score_path(), {}))
    memory = load_memory()
    gpath = guardian_path()
    day_s = today.isoformat()
    capital = Capital(cfg.account.capital, capital_path())
    jpath = journal_path(day_s)

    def journal(ev: dict) -> None:
        with jpath.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, default=str) + "\n")
    engine = Engine(cfg, plan, broker, chain, rec, feed="iex", live=True,
                    guardian_block=lambda: GuardianState.load(gpath).entry_block(day_s),
                    memory=memory, veto_score=score, alert=notify.send, capital=capital, journal=journal)
    log.info("trading budget: %s (budget equity now %s)",
             f"${cfg.account.capital:,.0f}" if cfg.account.capital else "whole account", f"{engine.account().equity:,.2f}")
    broker.post = engine.inbox.put

    if time.time() > session.open + 120:
        from .levelmap import to_bars

        today_bars = to_bars(AlpacaData().minutes(plan.symbols, session.open, time.time(), "iex"))
        n = engine.seed_today(today_bars, time.time())
        log.info("started after the open: backfilled %d minute bars of today's session", n)

    events: queue.Queue = queue.Queue()
    feed = LiveFeed(plan.symbols, events, trades_only=tuple(cfg.universe.market_symbols))
    _sleep_until(session.open - 60, "the open")
    feed.start()
    notify.send(f"Live on {len(plan.symbols)} symbols: {', '.join(s for s in plan.symbols if s not in cfg.universe.market_symbols)}")

    last_beat = last_flush = last_status = last_snap = 0.0
    stop_path().unlink(missing_ok=True)
    journal({"ts": time.time(), "kind": "status", "symbol": "",
             "text": f"live on {len(plan.symbols)} symbols, budget ${cfg.account.capital or 0:,.0f}"})
    end_at = session.close + 120
    errors = 0
    n_events = {"trade": 0, "quote": 0}

    def safely(fn, *a) -> None:
        nonlocal errors
        try:
            fn(*a)
        except Exception:
            errors += 1
            log.exception("engine error #%d", errors)
            if errors == 25:
                notify.send("25 engine errors today: flattening and blocking new entries. Check the logs.", priority="high")
                st = GuardianState.load(gpath)
                st.halted_day, st.halt_reason = day_s, "engine errors"
                st.save(gpath)
                engine.executor.flatten_all("engine errors")

    try:
        while time.time() < end_at:
            try:
                ev = events.get(timeout=0.2)
                n = 0
                while True:
                    is_trade = isinstance(ev, Trade)
                    n_events["trade" if is_trade else "quote"] += 1
                    safely(engine.on_trade if is_trade else engine.on_quote, ev)
                    n += 1
                    if n > 5000:
                        break
                    ev = events.get_nowait()
            except queue.Empty:
                pass
            safely(engine.drain_inbox)
            safely(engine.advance_to, time.time())
            now = time.time()
            if now - last_beat >= 5:
                last_beat = now
                syms = [t.symbol for t in engine.executor.active()]
                safely(beat, {"status": "trading", "positions": syms})
                safely(write_json, bot_positions_path(), {"ts": now, "symbols": syms})
            if now - last_snap >= 2:
                last_snap = now
                safely(lambda: write_json(snapshot_path(), engine.snapshot(
                    {"account": acct_kind, "events": dict(n_events), "watchlist_ranking": plan.ranking[:20],
                     "removed_earnings": plan.removed_earnings})))
            if stop_path().exists():
                log.warning("stop requested from the dashboard: flattening and ending the day")
                journal({"ts": now, "kind": "status", "symbol": "", "text": "STOP pressed: closing everything"})
                engine.executor.flatten_all("stopped by user")
                t_end = time.time() + 30
                while engine.executor.active() and time.time() < t_end:
                    time.sleep(0.5)
                    safely(engine.drain_inbox)
                    safely(engine.advance_to, time.time())
                stop_path().unlink(missing_ok=True)
                break
            if now - last_status >= 300:
                last_status = now
                acct = engine.account()
                log.info("status: %d trades + %d quotes received, %d setups seen, %d taken, %d open, "
                         "budget equity %.2f, queue %d", n_events["trade"], n_events["quote"],
                         engine.counts["attempts"], engine.counts["taken"], len(engine.executor.active()),
                         acct.equity, events.qsize())
            if now - last_flush >= 60:
                last_flush = now
                safely(rec.flush, day_s)
                safely(write_json, veto_score_path(), score.d)
    except KeyboardInterrupt:
        log.warning("interrupted: flattening")
        engine.executor.flatten_all("interrupted")
        time.sleep(3)
        engine.drain_inbox()
    finally:
        feed.stop()
        engine.drain_inbox()
        row = engine.finish()
        rec.flush(day_s)
        try:
            write_json(snapshot_path(), engine.snapshot({"account": acct_kind, "events": dict(n_events), "finished": True}))
        except Exception:
            log.exception("final snapshot failed")
        write_json(veto_score_path(), score.d)
        beat({"status": "done"})
        keep_awake(False)
    path, s = write_report(rec.root, ROOT / "reports")
    log.info("\n%s\nreport: %s", console(s), path)
    if row:
        notify.send(f"Day done {today}: P&L {row['pnl']:+,.2f}, trades {row['trades']} "
                    f"({row['wins']}W/{row['losses']}L), equity {row['end_equity']:,.2f}")


def keep_awake(on: bool = True) -> None:
    """Ask Windows not to sleep while this process is trading (released on exit)."""
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def _warm(chain) -> None:
    try:
        if chain.kronos is not None:
            _ = chain.kronos.predictor
        if chain.finr1 is not None:
            chain.finr1.warm_up()
        log.info("models warmed up")
    except Exception as e:
        log.warning("model warm-up failed: %s", e)
