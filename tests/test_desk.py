"""The DESK screen's logic: risk meters, blotter, broker feed, health and alerts."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ------------------------------------------------------------------ risk meters
SNAP = {"budget": {"start_equity": 10000.0, "equity": 9788.0, "pnl": -212.0},
        "trades": [{"risk_dollars": 100.0, "qty": 10, "price": 100.0},
                   {"risk_dollars": 200.0, "qty": 20, "price": 50.0}]}
GUARDIAN = {"losers_in_row": 3, "peak_equity": 10500.0}


def _limits():
    from dashcount_ui.risk import Limits

    return Limits(8.0, 4, 25.0, 6.0, 2.0)


def test_level_thresholds():
    from dashcount_ui.risk import level_for

    assert [level_for(x) for x in (0.49, 0.5, 0.74, 0.75)] == ["ok", "warn", "warn", "danger"]


def test_risk_meters_from_snapshot():
    from dashcount_ui.risk import risk_meters

    m = {x.key: x for x in risk_meters(SNAP, GUARDIAN, _limits(), 5.0)}
    assert [x.key for x in risk_meters(SNAP, GUARDIAN, _limits(), 5.0)] == [
        "day_loss", "streak", "fuse", "open_risk", "exposure"]
    assert m["day_loss"].text == "$212 of $800 (27%)" and m["day_loss"].level == "ok"
    assert m["streak"].frac == 0.75 and m["streak"].level == "danger" and m["streak"].text == "3 of 4 losers"
    assert m["fuse"].used == 712.0 and m["fuse"].limit == 2625.0
    assert m["open_risk"].level == "warn"
    assert m["exposure"].used == 2000.0 and m["exposure"].text.endswith(" - per-trade cap, shown for scale")


def test_risk_meters_stale():
    from dashcount_ui.risk import risk_meters

    assert {x.level for x in risk_meters(SNAP, GUARDIAN, _limits(), 120.0)} == {"stale"}
    assert {x.level for x in risk_meters(SNAP, GUARDIAN, _limits(), None)} == {"stale"}


def test_limits_from_config():
    from dashcount.config import load_config
    from dashcount_ui.risk import limits_from_config

    lim = limits_from_config(load_config())
    assert (lim.daily_loss_pct, lim.losers_in_row, lim.drawdown_pct) == (8.0, 4, 25.0)
    assert (lim.max_open_risk_pct, lim.max_notional_x) == (6.0, 2.0)
