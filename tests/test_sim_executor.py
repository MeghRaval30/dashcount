from dashcount.broker.sim import SimBroker
from dashcount.executor import Executor
from dashcount.models import LONG, SHORT, Candidate

from helpers import at, cfg, quote, trade


def _setup(flatten="15:55"):
    c = cfg()
    b = SimBroker(c.simulator)
    closed = []
    ex = Executor(c.execution, b, closed.append, flatten_at=at(flatten))
    return c, b, ex, closed


def _tick(b, ex, ts, price, spread=0.02, size=100, tc=1.0):
    b.on_quote(quote(ts - 0.1, price - spread / 2, price + spread / 2))
    b.on_trade(trade(ts, price, size))
    ex.on_price("TEST", price, ts, tc)
    ex.on_time(ts)


def test_long_bracket_hits_target_harsh_fills():
    c, b, ex, closed = _setup()
    t0 = at("10:00")
    _tick(b, ex, t0, 100.0)
    cand = Candidate("TEST", t0, "orb", LONG, 100.0, 99.0, 102.0)
    ex.enter(cand, 100, t0)
    _tick(b, ex, t0 + 1, 100.0)                  # entry goes live after latency and fills at ask+slip
    tr = ex.trades[ex.by_symbol["TEST"]]
    assert tr.state == "open" and 100.0 < tr.avg_entry <= 100.05
    _tick(b, ex, t0 + 5, 102.0)                  # touching the limit is not enough
    assert tr.state == "open"
    _tick(b, ex, t0 + 6, 102.01)                 # trading through fills it
    assert tr.state == "closed" and tr.exit_reason == "target"
    assert closed and closed[0].pnl() > 0
    assert b.positions() == {}


def test_checkpoint_moves_stop_to_entry_then_breakeven_exit():
    c, b, ex, closed = _setup()
    t0 = at("10:00")
    _tick(b, ex, t0, 100.0)
    ex.enter(Candidate("TEST", t0, "orb", LONG, 100.0, 99.0, 102.0), 100, t0)
    _tick(b, ex, t0 + 1, 100.0)
    tr = ex.trades[ex.by_symbol["TEST"]]
    _tick(b, ex, t0 + 10, 101.2)                 # +1R -> checkpoint
    assert tr.track.checkpoint_done and tr.track.stop == tr.avg_entry
    _tick(b, ex, t0 + 20, 101.0)
    _tick(b, ex, t0 + 30, 99.9)                  # falls through the moved stop
    assert tr.state == "closed" and tr.exit_reason == "checkpoint_stop"


def test_short_stop_out_with_gap_fills_at_print():
    c, b, ex, closed = _setup()
    t0 = at("11:00")
    _tick(b, ex, t0, 50.0)
    ex.enter(Candidate("TEST", t0, "vwap_pullback", SHORT, 50.0, 50.5, 49.0), 200, t0)
    _tick(b, ex, t0 + 1, 50.0)
    tr = ex.trades[ex.by_symbol["TEST"]]
    assert tr.state == "open"
    _tick(b, ex, t0 + 2, 51.5)                   # gaps through the stop
    assert tr.state == "closed" and tr.exit_reason == "stop"
    assert closed[0].exit_avg >= 51.5            # filled at the gap print or worse
    assert closed[0].pnl() < -200 * 0.5


def test_unfilled_entry_cancels_after_timeout():
    c, b, ex, closed = _setup()
    t0 = at("10:00")
    _tick(b, ex, t0, 100.0)
    ex.enter(Candidate("TEST", t0, "orb", LONG, 100.0, 99.0, 102.0), 100, t0)
    # price runs away before the order is live -> not marketable, never trades through
    _tick(b, ex, t0 + 0.2, 100.5)
    _tick(b, ex, t0 + 1, 100.5)
    _tick(b, ex, t0 + 31, 100.6)
    assert closed and closed[0].exit_reason == "unfilled"
    assert not ex.busy("TEST")


def test_count_flip_and_eod_exits():
    c, b, ex, closed = _setup(flatten="10:30")
    t0 = at("10:00")
    _tick(b, ex, t0, 100.0)
    ex.enter(Candidate("TEST", t0, "orb", LONG, 100.0, 99.0, 102.0), 100, t0)
    _tick(b, ex, t0 + 1, 100.0)
    _tick(b, ex, t0 + 2, 100.1, tc=-1.5)
    _tick(b, ex, t0 + 40, 100.1, tc=-1.5)       # flip held 30 s -> market exit
    _tick(b, ex, t0 + 41, 100.1, tc=-1.5)
    assert closed[-1].exit_reason == "count_flip"
    # EOD
    ex.enter(Candidate("TEST", t0 + 60, "orb", LONG, 100.1, 99.1, 102.1), 100, t0 + 60)
    _tick(b, ex, t0 + 61, 100.1)
    _tick(b, ex, at("10:30") + 1, 100.2)
    _tick(b, ex, at("10:30") + 2, 100.2)
    assert closed[-1].exit_reason == "eod"
    assert b.positions() == {}


def test_fees_charged_on_sells():
    c, b, ex, closed = _setup()
    t0 = at("10:00")
    _tick(b, ex, t0, 100.0)
    ex.enter(Candidate("TEST", t0, "orb", LONG, 100.0, 99.0, 102.0), 1000, t0)
    _tick(b, ex, t0 + 1, 100.0)
    _tick(b, ex, t0 + 2, 102.5)
    assert b.fees_paid > 0


def test_sim_fills_against_nbbo_estimate_on_iex():
    c, b, ex, closed = _setup()
    b.spread_bps_fn = lambda s: 4.0                  # real spread ~4 bps; IEX shows 2%
    t0 = at("10:00")
    b.on_quote(quote(t0, 99.0, 101.0))
    b.on_trade(trade(t0, 100.0))
    ex.enter(Candidate("TEST", t0, "orb", LONG, 100.0, 99.0, 102.0), 100, t0)
    b.on_quote(quote(t0 + 1, 99.0, 101.0))
    b.on_trade(trade(t0 + 1, 100.0))
    tr = ex.trades[ex.by_symbol["TEST"]]
    assert tr.state == "open" and tr.avg_entry < 100.1      # not filled at IEX's 101 ask


def test_lopsided_iex_book_entry_fills_near_the_last_print():
    # real case (SNDK 2026-10-15): IEX book 1305.00 / 1326.93 (mid 1316), stock printing 1326.75
    c, b, ex, closed = _setup()
    b.spread_bps_fn = lambda s: 4.0
    t0 = at("10:00")
    b.on_trade(trade(t0, 1326.75))
    b.on_quote(quote(t0, 1305.00, 1326.93))
    ex.enter(Candidate("TEST", t0, "orb", LONG, 1327.28, 1300.33, 1381.19), 4, t0)
    b.on_trade(trade(t0 + 1, 1326.75))
    tr = ex.trades[ex.by_symbol["TEST"]]
    assert tr.state == "open" and 1326.5 < tr.avg_entry <= 1328.63     # not at the 1316 IEX midpoint


def test_lopsided_iex_book_exit_is_not_a_phantom_profit():
    # real case (MU 2026-10-15): IEX book 889.79 / 932.81 (mid 911), stock printing 890
    c, b, ex, closed = _setup()
    b.spread_bps_fn = lambda s: 3.0
    t0 = at("10:00")
    _tick(b, ex, t0, 880.0)
    ex.enter(Candidate("TEST", t0, "orb", LONG, 880.0, 868.0, 904.0), 8, t0)
    _tick(b, ex, t0 + 1, 880.0)
    assert ex.trades[ex.by_symbol["TEST"]].state == "open"
    b.on_trade(trade(t0 + 10, 890.0))
    b.on_quote(quote(t0 + 10, 889.79, 932.81))
    b.close_position("TEST")
    b.on_trade(trade(t0 + 11, 890.0))
    exit_fill = [o for o in b.orders.values() if o.role == "exit"][0]
    assert exit_fill.status == "filled" and exit_fill.avg_price <= 890.0
