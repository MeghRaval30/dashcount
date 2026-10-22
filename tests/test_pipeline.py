"""Download -> store -> level map -> replay -> practice -> scoreboard, all offline."""

import datetime as dt

import pytest

from dashcount import download as download_mod
from dashcount import levelmap as levelmap_mod
from dashcount.data import Store, StoreData
from dashcount.earnings import EarningsCalendar
from dashcount.levelmap import LevelMap
from dashcount.practice import run_practice
from dashcount.recorder import read_table
from dashcount.scoreboard import summarize

from fakes import FakeAlpacaData, universe
from helpers import cfg

D1, D2 = dt.date(2025, 3, 11), dt.date(2025, 3, 12)


@pytest.fixture()
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHCOUNT_DATA", str(tmp_path))
    uni = universe()
    monkeypatch.setattr(levelmap_mod, "load_universe", lambda path=None: uni)
    monkeypatch.setattr(download_mod, "load_universe", lambda path=None: uni)

    def fake_symbols_on(self, day):
        return {"T05"}                                  # T05 always "reports earnings"

    monkeypatch.setattr(EarningsCalendar, "symbols_on", fake_symbols_on)
    return tmp_path


def test_download_then_practice(offline):
    c = cfg(universe={"watchlist_size": 6, "min_avg_dollar_volume": 1e6})
    fake = FakeAlpacaData()
    download_mod.download(c, D1, D2, feeds=("iex",), data=fake)
    store = Store()
    assert store.minute_path("sip", D1).exists()
    assert store.trades_path("iex", D2, "SPY").exists()

    plan = LevelMap(c, StoreData(store), EarningsCalendar(store.earnings_dir(), offline=True)).build(D2, "iex")
    assert len(plan.watchlist) == 8                       # 6 stocks + SPY + QQQ
    assert "T05" not in plan.symbols and "T05" in plan.removed_earnings
    stocks = [w for w in plan.watchlist if not w.is_market]
    assert all(len(w.volume_profile) == 78 for w in stocks)
    assert all(0.0 < w.feed_share < 0.1 for w in stocks)   # IEX ~3% of SIP in the fake
    assert all(plan.history[w.symbol] for w in plan.watchlist)

    run = run_practice(c, D1, D2, feed="iex", vetoes="none", label="t")
    daily = read_table(run, "daily")
    assert len(daily) == 2
    att = read_table(run, "attempts")
    out = read_table(run, "outcomes")
    assert set(att["attempt_id"]) <= set(out["attempt_id"])          # every attempt resolved
    s = summarize(run)
    assert s["days"] == 2 and "attempts" in s


def test_level_map_windows(offline):
    c = cfg()
    lm = LevelMap.__new__(LevelMap)
    lm.cfg = c
    from dashcount.timeutil import calendar

    s = calendar().session(dt.date(2025, 3, 19))         # FOMC decision day
    blocked, last_entry, flatten_at, flags = lm._windows(s)
    assert any(w == "fomc" for _, _, w in blocked) and "FOMC day" in flags
    h = calendar().session(dt.date(2024, 11, 29))        # day after Thanksgiving: half day
    blocked, last_entry, flatten_at, flags = lm._windows(h)
    assert h.half_day and "half day" in flags and flatten_at < h.close
    first = calendar().session(dt.date(2025, 4, 1))      # 1st business day: ISM manufacturing
    blocked, *_ = lm._windows(first)
    assert any(w.startswith("release:ISM") for _, _, w in blocked)
