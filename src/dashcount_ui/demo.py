"""Demo data: two finished synthetic days plus a synthetic morning in progress,
written exactly like the live bot writes them. Lets you preview the dashboard
without the bot. Fake tickers and prices; never mistaken for real (title says DEMO)."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

FAKE = ["COAL", "IRON", "GOLDN", "RUBY", "JADE", "ONYX", "OPAL", "TOPZ", "SAPH", "AMBR", "PERL", "MICA", "GEMS", "QRTZ"]


def _events(trades: dict, quotes: dict, s, cutoff: float):
    rows = []
    for sym, t in trades.items():
        rows.append(pd.DataFrame({"ts": t["ts"], "k": 1, "sym": sym, "a": t["price"], "b": t["size"], "c": 0.0, "d": 0.0}))
    for sym, q in quotes.items():
        rows.append(pd.DataFrame({"ts": q["ts"], "k": 0, "sym": sym, "a": q["bid"], "b": q["ask"],
                                  "c": q["bid_size"], "d": q["ask_size"]}))
    ev = pd.concat(rows, ignore_index=True)
    ev = ev[(ev["ts"] >= s.open) & (ev["ts"] < min(s.close, cutoff))].sort_values(["ts", "k"], kind="stable")
    return ev


def build_demo(folder: str | Path | None = None, morning_until: str = "11:20") -> Path:
    folder = Path(folder or tempfile.mkdtemp(prefix="dashcount-demo-"))
    os.environ["DASHCOUNT_DATA"] = str(folder)
    from dashcount.broker.sim import SimBroker
    from dashcount.config import load_config
    from dashcount.data.synthetic import make_day
    from dashcount.engine import Engine
    from dashcount.guardian import GuardianRules
    from dashcount.models import Quote, Trade
    from dashcount.recorder import Recorder
    from dashcount.state import state_dir, write_json
    from dashcount.timeutil import calendar, et_ts

    # looser gates so the demo shows some action on fake data
    cfg = load_config(overrides={"gates": {"min_true_count": 0.4}, "hazards": {"cost_mult": 1.5}})
    rec = Recorder(folder / "records", "live-paper", "demo")
    broker = SimBroker(cfg.simulator, starting_equity=cfg.account.capital)
    guardian = GuardianRules(cfg.guardian_mode)
    days = calendar().sessions(dt.date(2026, 10, 19), dt.date(2026, 10, 21))
    st_dir = state_dir()
    for i, day in enumerate(days):
        last = i == len(days) - 1
        plan, tr, qu = make_day(day, FAKE, seed=40 + i)
        jpath = st_dir / f"journal-{day.isoformat()}.jsonl"

        def journal(ev, jpath=jpath):
            with jpath.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, default=str) + "\n")

        eng = Engine(cfg, plan, broker, None, rec, feed="sip", guardian=guardian, journal=journal)
        import dashcount.sizing as sz

        orig = sz.bucket
        sz.bucket = lambda tc: 1 if tc >= 0.4 else 0               # demo only: let small counts bet
        try:
            cutoff = et_ts(day, morning_until) if last else plan.session.close
            ev = _events(tr, qu, plan.session, cutoff)
            for ts, k, sym, a, b, c, d in ev.itertuples(index=False):
                if k:
                    eng.on_trade(Trade(sym, float(ts), float(a), float(b)))
                else:
                    eng.on_quote(Quote(sym, float(ts), float(a), float(b), float(c), float(d)))
        finally:
            sz.bucket = orig
        if not last:
            eng.finish()
            rec.flush(day.isoformat())
            continue
        rec.flush(day.isoformat())
        n_tr = int(np.sum([len(t) for t in tr.values()]))
        snap = eng.snapshot({"account": "paper demo", "events": {"trade": n_tr // 3, "quote": n_tr},
                             "removed_earnings": []})
        write_json(st_dir / "live_snapshot.json", snap)
        write_json(st_dir / "live_bars.json", eng.bars_snapshot())
        write_json(st_dir / "heartbeat.json", {"ts": __import__("time").time(), "status": "trading"})
        guardian.s.save(st_dir / "guardian.json")
    return folder
