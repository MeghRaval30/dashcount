import datetime as dt

import pytest

from dashcount.broker.sim import SimBroker
from dashcount.data.synthetic import make_day
from dashcount.engine import Engine
from dashcount.guardian import GuardianRules
from dashcount.models import VetoResult
from dashcount.recorder import Recorder, read_table
from dashcount.replay import replay
from dashcount.veto import VetoChain

from helpers import cfg

DAY = dt.date(2025, 3, 12)


@pytest.fixture(scope="module")
def market():
    return make_day(DAY, [f"S{i:02d}" for i in range(12)], seed=3)


@pytest.fixture()
def any_count(monkeypatch):
    """Let every candidate get the smallest bet regardless of the count (to exercise trading)."""
    import dashcount.sizing as sz

    monkeypatch.setattr(sz, "bucket", lambda tc: 1)


def _run(market, tmp_path, chain=None, guardian=True, **over):
    c = cfg(**over)
    plan, tr, qu = market
    rec = Recorder(tmp_path, "t", "v")
    b = SimBroker(c.simulator)
    eng = Engine(c, plan, b, chain, rec, feed="sip", guardian=GuardianRules(c.guardian_mode) if guardian else None)
    replay(eng, tr, qu)
    rec.flush("d")
    return eng, b, tmp_path / "t"


def test_full_day_every_attempt_resolves_and_ends_flat(market, tmp_path):
    eng, b, run = _run(market, tmp_path)
    att = read_table(run, "attempts")
    out = read_table(run, "outcomes")
    assert len(att) > 0
    assert set(att["attempt_id"]) == set(out["attempt_id"])
    assert b.positions() == {}
    assert not any(o.status in ("open", "pending") for o in b.orders.values() if o.role in ("entry", "exit"))
    daily = read_table(run, "daily")
    assert len(daily) == 1 and daily["end_equity"].iloc[0] == pytest.approx(b.equity())


class VetoAll:
    def assess(self, c, bars):
        return VetoResult("kronos", "veto", "kronos_odds_low", odds=0.1, latency_s=1.0)


def test_veto_everything_means_no_trades_but_ghosts(market, tmp_path, any_count):
    c = cfg()
    chain = VetoChain(c.veto, VetoAll(), None, None)
    eng, b, run = _run(market, tmp_path, chain=chain, pads={"target_r": 2.0},
                       gates={"min_true_count": -99.0, "market_against": -99.0},
                       hazards={"cost_mult": 0.0})
    att = read_table(run, "attempts")
    assert (att["action"] == "vetoed:kronos").sum() > 0
    assert (att["action"] == "taken").sum() == 0
    assert read_table(run, "trades").empty
    vc = read_table(run, "veto_calls")
    assert set(vc["verdict"]) == {"veto"}


def test_ghosts_pay_costs_and_veto_score_uses_net_r(market, tmp_path, any_count):
    c = cfg()
    chain = VetoChain(c.veto, VetoAll(), None, None)
    eng, b, run = _run(market, tmp_path, chain=chain, pads={"target_r": 2.0},
                       gates={"min_true_count": -99.0, "market_against": -99.0},
                       hazards={"cost_mult": 0.0})
    out = read_table(run, "outcomes")
    ghosts = out[out["is_ghost"] == True]  # noqa: E712
    assert len(ghosts) and (ghosts["r_net"] < ghosts["r"]).all()
    vetoed = ghosts[ghosts["action"] == "vetoed:kronos"]
    assert len(vetoed) and eng.veto_score.d["kronos"]["saved_r"] == pytest.approx(-vetoed["r_net"].sum())


def test_loose_gates_trade_and_record(market, tmp_path, any_count):
    eng, b, run = _run(market, tmp_path, gates={"min_true_count": -99.0, "market_against": -99.0},
                       hazards={"cost_mult": 0.0})
    trades = read_table(run, "trades")
    assert len(trades) > 0
    filled = trades[trades["qty_filled"] > 0]
    assert set(filled["exit_reason"]) <= {"target", "stop", "checkpoint_stop", "count_flip", "stale", "eod",
                                          "guardian:end of day", "exit"}
    assert b.positions() == {}


def test_guardian_halt_blocks_new_entries(market, tmp_path, any_count):
    c = cfg(gates={"min_true_count": -99.0, "market_against": -99.0}, hazards={"cost_mult": 0.0})
    plan, tr, qu = market
    rec = Recorder(tmp_path, "g", "v")
    b = SimBroker(c.simulator)
    g = GuardianRules(c.guardian_mode)
    eng = Engine(c, plan, b, None, rec, feed="sip", guardian=g)
    g.s.halted_day = DAY.isoformat()
    g.s.halt_reason = "test"
    replay(eng, tr, qu)
    rec.flush("d")
    att = read_table(tmp_path / "g", "attempts")
    assert (att["action"] == "taken").sum() == 0
    assert att["action"].str.contains("guardian").any()


class SlowAllow:
    def assess(self, c, bars):
        import time as _t

        _t.sleep(0.01)
        return VetoResult("kronos", "allow", "none", odds=0.7, latency_s=0.01)


def test_live_mode_async_veto_path(market, tmp_path, any_count):
    """Live mode runs vetoes on a worker thread and applies results via the inbox."""
    import time as _t

    from dashcount.models import Quote, Trade

    c = cfg(gates={"min_true_count": -99.0, "market_against": -99.0}, hazards={"cost_mult": 0.0})
    plan, tr, qu = market
    rec = Recorder(tmp_path, "live", "v")
    b = SimBroker(c.simulator)
    eng = Engine(c, plan, b, VetoChain(c.veto, SlowAllow(), None, None), rec, feed="sip", live=True)
    # replay by hand, draining the inbox like the live loop does
    sym = sorted(tr)
    events = []
    for s in sym:
        t = tr[s]
        events += [(ts, 1, s, p, z) for ts, p, z in zip(t["ts"], t["price"], t["size"])]
    events.sort()
    for i, (ts, _, s, p, z) in enumerate(events):
        eng.on_trade(Trade(s, ts, p, z))
        if i % 200 == 0:
            _t.sleep(0.002)
            eng.drain_inbox()
    eng._outer.shutdown(wait=True)
    eng.drain_inbox()
    eng.finish()
    rec.flush("d")
    att = read_table(tmp_path / "live", "attempts")
    assert len(att) > 0
    assert not eng.pending_veto                      # every veto came back
    assert att["action"].isin(["taken"]).any() or att["action"].str.startswith(("skip", "missed")).any()


def test_snapshot_and_journal_for_dashboard(market, tmp_path, any_count):
    import json as _json

    c = cfg(gates={"min_true_count": -99.0, "market_against": -99.0}, hazards={"cost_mult": 0.0})
    plan, tr, qu = market
    notes = []
    rec = Recorder(tmp_path, "snap", "v")
    eng = Engine(c, plan, SimBroker(c.simulator), None, rec, feed="sip", journal=notes.append)
    replay(eng, tr, qu)
    snap = eng.snapshot({"account": "paper"})
    text = _json.dumps(snap, default=str)                 # must be JSON-able
    assert '"symbols"' in text and snap["budget"]["capital"] == 10000
    assert len(snap["symbols"]) == len(plan.watchlist)
    kinds = {n["kind"] for n in notes}
    assert {"setup", "taken", "exit"} <= kinds
