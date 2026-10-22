from dashcount.guardian import GuardianRules, GuardianState, round_trips
from dashcount.guardian_live import trailing_losers

from helpers import cfg


def rules(mode="tournament"):
    c = cfg(mode=mode)
    r = GuardianRules(c.guardian_mode)
    r.new_day("2025-03-12", 100_000)
    return r


def kinds(acts):
    return [a.kind for a in acts]


def test_daily_loss_halts_and_flattens():
    r = rules()
    assert r.evaluate(0, 95_000, 1e12, True) == []
    acts = r.evaluate(0, 91_900, 1e12, True)               # -8.1%
    assert "halt" in kinds(acts) and "flatten" in kinds(acts)
    assert r.s.entry_block("2025-03-12").startswith("halted")
    assert r.s.entry_block("2025-03-13") is None            # new day, new chances
    # doesn't fire twice
    assert "halt" not in kinds(r.evaluate(0, 91_000, 1e12, True))


def test_real_mode_is_tighter():
    r = rules("real")
    assert "halt" in kinds(r.evaluate(0, 97_900, 1e12, True))


def test_losers_in_row_halts_without_flatten():
    r = rules()
    for _ in range(3):
        assert r.on_trade_closed(-100) == []
    acts = r.on_trade_closed(-100)
    assert "halt" in kinds(acts) and "flatten" not in kinds(acts)


def test_winner_resets_streak():
    r = rules()
    for p in (-1, -1, -1, 5, -1, -1, -1):
        r.on_trade_closed(p)
    assert r.s.halted_day == ""


def test_drawdown_fuse_disarms_until_rearmed():
    r = rules()
    r.evaluate(0, 120_000, 1e12, False)                     # new peak
    acts = r.evaluate(0, 89_000, 1e12, True)                # -25.8% from peak
    assert "disarm" in kinds(acts) and "flatten" in kinds(acts)
    assert r.s.entry_block("2025-03-12").startswith("disarmed")
    assert r.s.entry_block("2099-01-01").startswith("disarmed")


def test_eod_and_panic():
    r = rules()
    assert kinds(r.evaluate(100, 100_000, 50, True)) == ["flatten"]
    assert r.evaluate(100, 100_000, 50, False) == []
    r.s.panic = True
    assert kinds(r.evaluate(0, 100_000, 1e12, True)) == ["flatten"]
    assert r.s.entry_block("x") == "panic"


def test_state_roundtrip(tmp_path):
    s = GuardianState(day="d", peak_equity=5.0, armed=False, disarm_reason="x")
    s.save(tmp_path / "g.json")
    t = GuardianState.load(tmp_path / "g.json")
    assert t.peak_equity == 5.0 and not t.armed


def test_round_trips_long_short_and_flip():
    fills = [
        {"symbol": "A", "side": "buy", "qty": 10, "price": 100, "ts": 1},
        {"symbol": "A", "side": "sell", "qty": 10, "price": 99, "ts": 2},     # -10
        {"symbol": "B", "side": "sell", "qty": 5, "price": 50, "ts": 3},
        {"symbol": "B", "side": "buy", "qty": 5, "price": 48, "ts": 4},       # +10
        {"symbol": "C", "side": "buy", "qty": 4, "price": 10, "ts": 5},
        {"symbol": "C", "side": "buy", "qty": 4, "price": 12, "ts": 6},       # avg 11
        {"symbol": "C", "side": "sell", "qty": 8, "price": 10, "ts": 7},      # -8
    ]
    assert round_trips(fills) == [-10, 10, -8]
    assert trailing_losers([5, -1, -2]) == 2 and trailing_losers([-1, 3]) == 0
