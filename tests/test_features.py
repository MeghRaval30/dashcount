from dashcount.features import FlowCounter, SpreadTracker

from helpers import at, feed_minutes, quote, state, trade


def test_bars_vwap_and_flat_minutes():
    st = state()
    t0 = at("09:30")
    st.on_trade(trade(t0 + 1, 100.0, 100))
    st.on_trade(trade(t0 + 30, 101.0, 300))
    bar = st.on_minute(t0 + 60)
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100.0, 101.0, 100.0, 101.0, 400)
    assert abs(st.bars.vwap - (100 * 100 + 101 * 300) / 400) < 1e-9
    # a minute with no trades still closes as a flat bar
    flat = st.on_minute(t0 + 120)
    assert flat.volume == 0 and flat.close == 101.0
    assert len(st.bars.bars) == 2 and len(st.bars.vwaps) == 2
    # closing the same minute twice is a no-op
    assert st.on_minute(t0 + 120) is None


def test_atr_tracks_true_range():
    st = state()
    feed_minutes(st, at("09:30"), [(100, 101, 99, 100)] * 20)
    assert 1.9 < st.bars.atr < 2.1


def test_quote_rule_signs_and_tick_fallback():
    fc = FlowCounter()
    sp = SpreadTracker()
    sp.on_quote(quote(at("10:00"), 99.99, 100.01))
    assert fc.on_trade(trade(at("10:00", 1), 100.01), sp) == 1      # paid the ask
    assert fc.on_trade(trade(at("10:00", 2), 99.99), sp) == -1      # hit the bid
    # stale quote -> tick rule
    assert fc.on_trade(trade(at("10:00", 30), 100.05), sp) == 1
    assert fc.on_trade(trade(at("10:00", 31), 100.05), sp) == 1     # unchanged -> last sign


def test_count_window_and_z_sign():
    fc = FlowCounter(window_s=120)
    fc.normal_window_volume = 20_000
    sp = SpreadTracker()
    t = at("10:00")
    for i in range(60):
        sp.on_quote(quote(t + i, 99.99, 100.01))
        fc.on_trade(trade(t + i, 100.01, 500), sp)   # steady aggressive buying
    assert fc.running(t + 59) == 30_000
    assert fc.z(t + 59) > 0
    # after the window passes with no trades the running count decays to zero
    assert fc.running(t + 59 + 121) == 0


def test_spread_normal_needs_samples():
    sp = SpreadTracker(min_samples=30)
    t = at("10:00")
    for i in range(29):
        sp.on_quote(quote(t + i, 99.99, 100.01))
    assert sp.normal() is None
    sp.on_quote(quote(t + 29, 99.99, 100.01))
    assert abs(sp.normal() - 0.02) < 1e-9


def test_normal_minute_volume_scales_with_feed_share():
    st = state(profile_per_5min=50_000, feed_share=0.02)
    assert st.normal_minute_volume(at("10:00")) == 50_000 / 5 * 0.02
    assert st.normal_minute_volume(at("10:00"), sip=True) == 10_000


def test_seed_today_backfills_vwap_open_and_atr():
    from dashcount.models import Bar

    st = state()
    t0 = at("09:30")
    bars = [Bar("TEST", t0 + 60 * i, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000) for i in range(30)]
    st.bars.seed(bars)
    assert st.bars.day_open == 100 and len(st.bars.bars) == 30 and len(st.bars.vwaps) == 30
    assert st.bars.vwap is not None and 100 < st.bars.vwap < 130
    assert st.bars.atr is not None and st.bars.high == 130
    st.bars.seed(bars[:5])                       # idempotent for already-seen minutes
    assert len(st.bars.bars) == 30


def test_est_spread_iex_uses_nbbo_model_and_sip_uses_quote():
    from dashcount.features import SymbolState
    from dashcount.models import WatchItem
    from helpers import cfg as _cfg, session as _session

    c = _cfg()
    iex = SymbolState(WatchItem("X", nbbo_spread_bps=5.0), c.features, _session().open, "iex")
    iex.on_quote(quote(at("10:00"), 99.0, 101.0, symbol="X"))       # IEX book: 200 bps wide
    assert abs(iex.est_spread() - 0.05) < 1e-9                        # 5 bps of 100
    iex.on_quote(quote(at("10:00", 1), 99.99, 100.01, symbol="X"))  # IEX at the inside: narrower wins
    assert abs(iex.est_spread() - 0.02) < 1e-9
    sip = SymbolState(WatchItem("X"), c.features, _session().open, "sip")
    sip.on_quote(quote(at("10:00"), 99.0, 101.0, symbol="X"))
    assert sip.est_spread() == 2.0


def test_iex_inside_prints_use_the_tick_rule():
    # real case (MU): IEX book 889.79 / 932.81 (mid 911) while the stock prints ~890
    sp = SpreadTracker()
    sp.on_quote(quote(0.0, 889.79, 932.81))
    tick, mid = FlowCounter(inside_rule="tick"), FlowCounter(inside_rule="mid")
    for fc in (tick, mid):
        fc.classify(trade(0.5, 889.90), sp)
    assert tick.classify(trade(1.0, 890.00), sp) == 1          # an up-tick is a buy
    assert mid.classify(trade(1.0, 890.00), sp) == -1          # "below IEX's midpoint" called it a sell
    assert tick.classify(trade(1.5, 932.81), sp) == 1          # at the ask the quote rule still decides


def test_iex_state_uses_tick_rule_scaled_atr_and_records_the_other_count():
    from dashcount.features import SymbolState
    from dashcount.models import WatchItem
    from helpers import cfg as _cfg, session as _session

    c = _cfg()
    iex = SymbolState(WatchItem("X"), c.features, _session().open, "iex")
    sip = SymbolState(WatchItem("X"), c.features, _session().open, "sip")
    assert (iex.flow.inside_rule, iex.flow_alt.inside_rule, iex.bars.range_scale) == ("tick", "mid", 1.3)
    assert (sip.flow.inside_rule, sip.flow_alt.inside_rule, sip.bars.range_scale) == ("mid", "tick", 1.0)
    feed_minutes(iex, at("09:30"), [(100, 101, 99, 100)] * 20)
    assert 2.5 < iex.bars.atr < 2.7                             # 2.0 real-range ATR x 1.3
