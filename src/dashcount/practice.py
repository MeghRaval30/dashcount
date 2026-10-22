"""Practice mode: replay historical (or synthetic) days through the exact live
pipeline, with the harsh simulator, the guardian and the vetoes."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path

from .broker.sim import SimBroker
from .config import Config
from .data import Store, StoreData
from .earnings import EarningsCalendar
from .engine import Engine, VetoScore
from .factory import make_chain, records_root
from .guardian import GuardianRules
from .levelmap import LevelMap
from .recorder import Recorder
from .replay import load_ticks, replay
from .timeutil import calendar, parse_date
from .veto.memory import AttemptMemory

log = logging.getLogger(__name__)


def run_practice(cfg: Config, start, end, feed: str = "iex", vetoes: str = "full", label: str = "",
                 synthetic: bool = False, n_symbols: int = 28, seed: int = 0) -> Path:
    start, end = parse_date(start), parse_date(end)
    days = calendar().sessions(start, end)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"practice-{'synthetic-' if synthetic else ''}{label + '-' if label else ''}{stamp}"
    rec = Recorder(records_root(), run_id, cfg.version())
    rec.write_meta({
        "run_id": run_id, "kind": "practice", "synthetic": synthetic, "start": start, "end": end, "feed": feed,
        "vetoes": vetoes, "mode": cfg.mode, "version": cfg.version(), "config": cfg.model_dump(),
    })
    if not synthetic and start < parse_date(cfg.veto.kronos.judge_from) and vetoes in ("full", "kronos"):
        log.warning("practice starts before %s: Kronos saw this period in training; judge its veto only from %s on",
                    cfg.veto.kronos.judge_from, cfg.veto.kronos.judge_from)

    broker = SimBroker(cfg.simulator)
    guardian = GuardianRules(cfg.guardian_mode)
    chain = make_chain(cfg, vetoes)
    memory = AttemptMemory()
    score = VetoScore()
    store = Store()
    lm = None if synthetic else LevelMap(cfg, StoreData(store), EarningsCalendar(store.earnings_dir(), offline=True))
    plans_file = rec.root / "plans.jsonl"
    t_run = time.perf_counter()
    done = 0
    for i, day in enumerate(days):
        t0 = time.perf_counter()
        if synthetic:
            from .data.synthetic import make_day

            plan, trades, quotes = make_day(day, [f"SYN{k:02d}" for k in range(n_symbols)], seed=seed + i)
            missing = []
        else:
            try:
                plan = lm.build(day, feed)
            except Exception as e:
                log.warning("%s: no level map (%s) - skipped", day, e)
                continue
            trades, quotes, missing = load_ticks(store, feed, day, plan.symbols)
            if not trades:
                log.warning("%s: no tick data for feed=%s - run `dashcount download` first; skipped", day, feed)
                continue
            if missing:
                log.warning("%s: missing ticks for %s", day, ",".join(missing))
        with plans_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(plan.summary(), default=str) + "\n")
        eng = Engine(cfg, plan, broker, chain, rec, feed=feed, guardian=guardian, memory=memory, veto_score=score)
        n = replay(eng, trades, quotes)
        if broker.positions():
            log.error("%s: positions left open after the close: %s", day, broker.positions())
        rec.flush(day.isoformat())
        done += 1
        acct = broker.account()
        log.info("%s  equity %12s  trades %3d  attempts %3d  events %8d  (%.1fs)", day, f"{acct.equity:,.2f}",
                 len([t for t in eng.executor.closed if t.qty_filled]), eng.counts["attempts"], n,
                 time.perf_counter() - t0)
        if not guardian.s.armed:
            log.error("drawdown fuse blown on %s: %s. Practice run stops here (live, you'd have to re-arm).",
                      day, guardian.s.disarm_reason)
            break
    (rec.root / "veto_score.json").write_text(json.dumps(score.d, indent=1))
    log.info("practice run %s: %d days in %.0fs", run_id, done, time.perf_counter() - t_run)
    return rec.root
