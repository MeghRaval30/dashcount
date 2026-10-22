import math

from dashcount import exits, sizing
from dashcount.models import LONG, SHORT, Candidate
from dashcount.pads import evaluate_pads, orb, vwap_pullback

from helpers import at, cfg, feed_minutes, session, state


# ---------------------------------------------------------------- pads
def _orb_day(breakout_close: float, vol: float):
    c = cfg()
    st = state(profile_per_5min=50_000, atr_daily=5.0)   # 10k/min normal
    # 15 bars of range 99..101
    feed_minutes(st, at("09:30"), [(100, 101, 99, 100)] * 15, vol_per_bar=10_000)
    feed_minutes(st, at("09:45"), [(100.5, breakout_close, 100.4, breakout_close)], vol_per_bar=vol)
    bar = st.bars.bars[-1]
    return orb(st, bar, session(), c.pads.orb, c.pads.target_r), st


def test_orb_long_breakout_with_volume():
    cands, _ = _orb_day(101.5, vol=20_000)
    assert len(cands) == 1
    cd = cands[0]
    assert cd.direction == LONG and cd.pad == "orb"
    assert cd.stop == 100.0                       # range midpoint
    assert math.isclose(cd.target, 101.5 + 2 * 1.5)


def test_orb_needs_volume_and_fires_once():
    cands, _ = _orb_day(101.5, vol=12_000)        # 1.2x normal < 1.5x
    assert cands == []
    cands, st = _orb_day(101.5, vol=20_000)
    feed_minutes(st, at("09:46"), [(101.5, 102, 101.4, 102)], vol_per_bar=20_000)
    assert orb(st, st.bars.bars[-1], session(), cfg().pads.orb, 2.0) == []


def test_vwap_pullback_long():
    c = cfg()
    st = state()
    # steady uptrend for 40 minutes from 10:00 back to 09:20-ish timing; start at 09:30
    prices = []
    p = 100.0
    for _ in range(40):
        prices.append((p, p + 0.3, p - 0.05, p + 0.25))
        p += 0.25
    feed_minutes(st, at("09:30"), prices)
    vw = st.bars.vwap
    atr = st.bars.atr
    # pullback bar touching VWAP and closing green above it
    low = vw + 0.05 * atr
    feed_minutes(st, at("10:10"), [(vw + 0.2 * atr, vw + 0.6 * atr, low, vw + 0.5 * atr)])
    cands = vwap_pullback(st, st.bars.bars[-1], session(), c.pads.vwap_pullback, c.pads.target_r)
    assert [x.direction for x in cands] == [LONG]
    assert cands[0].stop < min(low, vw)


def test_evaluate_pads_respects_windows():
    st = state()
    feed_minutes(st, at("09:30"), [(100, 101, 99, 100)] * 5)
    assert evaluate_pads(st, st.bars.bars[-1], session(), cfg().pads) == []


# --------------------------------------------------------------- sizing
def _cand(entry=100.0, stop=99.0, direction=LONG):
    return Candidate("TEST", 0.0, "orb", direction, entry, stop, entry + 2 * (entry - stop) * 1)


def test_bet_spread_buckets_tournament():
    c = cfg()
    m = c.sizing_mode
    for tc, pct in ((1.2, 1.0), (2.5, 2.0), (3.4, 4.0)):
        d = sizing.size_trade(_cand(stop=98.0), tc, 100_000, 400_000, 0.0, None, m, c.sizing)
        assert d.risk_pct == pct and d.qty == int(100_000 * pct / 100 / 2.0)
    assert sizing.size_trade(_cand(), 0.5, 100_000, 400_000, 0, None, m, c.sizing).qty == 0


def test_caps_liquidity_notional_open_risk():
    c = cfg()
    m = c.sizing_mode
    d = sizing.size_trade(_cand(), 3.5, 100_000, 400_000, 0.0, 20_000, m, c.sizing)
    assert d.qty == 1000 and "liquidity" in d.capped_by       # 5% of 20k
    d = sizing.size_trade(_cand(entry=100, stop=99.9), 3.5, 100_000, 400_000, 0.0, None, m, c.sizing)
    assert d.qty == 2000 and "notional" in d.capped_by        # 2x equity / $100
    d = sizing.size_trade(_cand(), 3.5, 100_000, 400_000, 5_500, None, m, c.sizing)
    assert d.qty == 500 and "open_risk" in d.capped_by        # only $500 of 6% room left


def test_real_mode_is_smaller():
    c = cfg(mode="real")
    d = sizing.size_trade(_cand(), 3.5, 100_000, 400_000, 0.0, None, c.sizing_mode, c.sizing)
    assert d.risk_pct == 0.5 and d.qty == 500


def test_gates():
    g = cfg().gates
    assert sizing.gate(0.8, 0.0, LONG, g) == "count_too_low"
    assert sizing.gate(2.0, -2.0, LONG, g) == "market_against"
    assert sizing.gate(2.0, 2.0, SHORT, g) == "market_against"
    assert sizing.gate(2.0, 0.5, LONG, g) is None
    assert math.isclose(sizing.true_count(3.0, 0.0, SHORT, g), -2.0, abs_tol=1e-3)


# ---------------------------------------------------------------- exits
def test_checkpoint_and_soft_exits():
    ec = cfg().execution
    t = exits.Track(LONG, 100.0, 99.0, 102.0, 1.0, opened_ts=0.0)
    exits.update_excursions(t, 101.0)
    assert exits.checkpoint_due(t, 101.0, ec.checkpoint_r)
    exits.apply_checkpoint(t)
    assert t.stop == 100.0 and not exits.checkpoint_due(t, 101.5, ec.checkpoint_r)
    assert exits.touched(t, 100.0) == "stop" and exits.touched(t, 102.0) == "target"
    # count flip must persist for 30 s
    assert exits.soft_exit(t, 10.0, -1.5, 1e9, ec) is None
    assert exits.soft_exit(t, 41.0, -1.5, 1e9, ec) == "count_flip"
    # recovery resets the flip timer
    t2 = exits.Track(LONG, 100.0, 99.0, 102.0, 1.0, opened_ts=0.0)
    exits.soft_exit(t2, 10.0, -1.5, 1e9, ec)
    exits.soft_exit(t2, 20.0, 0.5, 1e9, ec)
    assert exits.soft_exit(t2, 45.0, -1.5, 1e9, ec) is None


def test_stale_and_eod():
    ec = cfg().execution
    t = exits.Track(SHORT, 100.0, 101.0, 98.0, 1.0, opened_ts=0.0)
    assert exits.soft_exit(t, 30 * 60 + 1, 0.0, 1e9, ec) == "stale"
    t.mfe_r = 0.6
    assert exits.soft_exit(t, 30 * 60 + 1, 0.0, 1e9, ec) is None
    assert exits.soft_exit(t, 5.0, 0.0, 5.0, ec) == "eod"
