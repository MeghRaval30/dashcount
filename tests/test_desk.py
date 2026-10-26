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


# ------------------------------------------------------------------ blotter + reconciliation
def _order(oid, cid, sym, side, typ, qty, filled, avg, status, ts, limit=None, stop=None, legs=None):
    return {"id": oid, "client_order_id": cid, "symbol": sym, "side": side, "type": typ, "qty": str(qty),
            "filled_qty": str(filled), "filled_avg_price": None if avg is None else str(avg),
            "limit_price": None if limit is None else str(limit), "stop_price": None if stop is None else str(stop),
            "status": status, "submitted_at": ts, "legs": legs}


def test_long_entry_and_legs_slippage():
    from dashcount_ui.reconcile import blotter_rows, plans_from_journal

    plans = plans_from_journal([{"kind": "taken", "trade_id": "dc-1-1", "entry": 100.0, "stop": 99.0,
                                 "target": 102.0, "qty": 10, "direction": 1}, {"kind": "setup"}])
    legs = [_order("tp", "x1", "AAA", "sell", "limit", 10, 10, 102.03, "filled", "2026-10-26T14:40:00Z", limit=102.0),
            _order("sl", "x2", "AAA", "sell", "stop", 10, 0, None, "canceled", "2026-10-26T14:31:00Z", stop=99.0)]
    orders = [_order("e1", "dc-1-1", "AAA", "buy", "limit", 10, 10, 100.05, "filled",
                     "2026-10-26T14:30:00.123456789Z", limit=100.1, legs=legs),
              _order("x9", "auto", "AAA", "sell", "market", 10, 10, 101.0, "filled", "2026-10-26T15:00:00Z")]
    rows = blotter_rows(orders, plans)
    assert [r.role for r in rows] == ["ENTRY", "TARGET", "STOP", "EXIT"]
    entry, tgt, stp, ext = rows
    assert entry.slip == pytest.approx(0.05) and entry.slip_r == pytest.approx(0.05) and entry.side == "BUY"
    assert tgt.slip == pytest.approx(-0.03) and tgt.parent_id == "e1" and tgt.trade_id == "dc-1-1"
    assert stp.fill is None and stp.slip is None
    assert ext.parent_id == "e1" and ext.trade_id == "dc-1-1" and ext.slip is None
    assert entry.ts == pytest.approx(1793025000.123456, abs=1e-3)       # 2026-10-26 14:30:00.123456 UTC


def test_short_slippage_signs():
    from dashcount_ui.reconcile import blotter_rows

    plans = {"dc-2-1": {"entry": 50.0, "stop": 50.5, "target": 49.0, "qty": 20, "direction": -1}}
    legs = [_order("tp", "y1", "BBB", "buy", "limit", 20, 20, 48.97, "filled", "2026-10-26T15:10:00Z", limit=49.0)]
    rows = blotter_rows([_order("e2", "dc-2-1", "BBB", "sell", "limit", 20, 20, 49.98, "filled",
                                "2026-10-26T15:00:00Z", limit=49.97, legs=legs)], plans)
    assert rows[0].slip == pytest.approx(0.02) and rows[0].slip_r == pytest.approx(0.04)
    assert rows[1].slip == pytest.approx(-0.03)


def test_foreign_and_partial_orders():
    from dashcount_ui.reconcile import blotter_rows, blotter_summary

    rows = blotter_rows([_order("m1", "manual-1", "CCC", "buy", "limit", 5, 5, 20.0, "filled",
                                "2026-10-26T14:00:00Z", limit=20.0)], {})
    assert rows[0].role == "ENTRY" and rows[0].trade_id is None and rows[0].slip is None
    part = blotter_rows([_order("p1", "dc-3-1", "DDD", "buy", "limit", 100, 60, 10.0, "canceled",
                                "2026-10-26T14:00:00Z", limit=10.0, legs=[])], {})
    assert part[0].filled_qty == 60
    s = blotter_summary(part)
    assert s["fills"] == 1 and s["rejected"] == 0 and s["orders"] == 1
    rej = blotter_rows([_order("r1", "dc-4-1", "EEE", "buy", "limit", 10, 0, None, "rejected",
                               "2026-10-26T14:00:00Z", limit=5.0)], {})
    assert blotter_summary(rej)["rejected"] == 1


def test_reconciler_grace_and_reset():
    from dashcount_ui.reconcile import Reconciler

    r = Reconciler()
    assert r.update({"AAA"}, set(), 0.0) == []
    assert r.update({"AAA"}, set(), 31.0) == ["AAA: held at the broker but unknown to the bot"]
    assert r.update(set(), set(), 40.0) == []
    assert r.update(set(), {"BBB"}, 41.0) == []
    assert r.update(set(), {"BBB"}, 72.0) == ["BBB: the bot has a trade but the broker has no position"]


# ------------------------------------------------------------------ broker feed
NOW = 1_793_025_000.0          # 2026-10-26 14:30 UTC (10:30 New York)


def _feed(handler, session=lambda: (NOW - 600, NOW + 3600)):
    import httpx

    from dashcount_ui.broker_feed import BrokerFeed

    client = httpx.Client(base_url="https://paper-api.alpaca.markets", transport=httpx.MockTransport(handler))
    return BrokerFeed("k", "s", client=client, clock=lambda: NOW, session=session)


def test_feed_polls_orders_and_positions_read_only():
    import httpx

    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path == "/v2/orders":
            return httpx.Response(200, json=[{"id": "o1", "symbol": "AAA"}])
        return httpx.Response(200, json=[{"symbol": "AAA", "qty": "10"}])

    f = _feed(handler)
    assert f.poll_once() == 10.0
    assert {m for m, _, _ in seen} == {"GET"} and {p for _, p, _ in seen} == {"/v2/orders", "/v2/positions"}
    params = next(p for _, path, p in seen if path == "/v2/orders")
    assert params["status"] == "all" and params["nested"] == "true" and params["direction"] == "asc"
    assert params["after"].startswith("2026-10-26T00:00:00")
    st = f.state()
    assert st.configured and st.ok and len(st.orders) == 1 and len(st.positions) == 1
    assert st.last_latency_ms is not None and st.errors_10min == 0


def test_feed_backoff_and_rate_limit():
    import httpx

    replies = [httpx.Response(500), httpx.Response(500),
               httpx.Response(429, headers={"X-RateLimit-Reset": str(int(NOW + 7))}),
               httpx.Response(200, json=[])]

    def handler(req):
        if req.url.path == "/v2/positions":
            return httpx.Response(200, json=[])
        return replies.pop(0)

    f = _feed(handler)
    assert f.poll_once() == 30.0 and f.state().errors_10min == 1 and not f.state().ok
    assert f.poll_once() == 60.0
    assert f.poll_once() == pytest.approx(7.0, abs=1.0)
    assert f.poll_once() == 10.0 and f.state().ok


def test_feed_outside_session_polls_every_5_minutes():
    import httpx

    f = _feed(lambda req: httpx.Response(200, json=[]), session=lambda: (NOW + 20_000, NOW + 40_000))
    assert f.poll_once() == 300.0


def test_feed_from_env_without_keys(tmp_path, monkeypatch):
    from dashcount_ui.broker_feed import feed_from_env

    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert feed_from_env(tmp_path, lambda: None) is None


def test_demo_feed_has_orders():
    from dashcount_ui.broker_feed import DemoFeed

    journal = [{"kind": "taken", "ts": NOW, "symbol": "AAA", "trade_id": "dc-1-1", "entry": 12.5, "stop": 12.0,
                "target": 13.5, "qty": 10, "direction": 1}]
    st = DemoFeed({"trades": [{"symbol": "AAA", "qty": 10, "direction": 1}]}, journal).state()
    assert st.configured and st.ok and st.orders and st.orders[0]["client_order_id"] == "dc-1-1"
    assert st.positions == [{"symbol": "AAA", "qty": "10"}]


# ------------------------------------------------------------------ health
def _rows(**kw):
    from dashcount_ui.broker_feed import FeedState
    from dashcount_ui.health import health_rows

    base = dict(in_session=True, heartbeat_age=5.0, guardian_age=5.0, snap={}, held=set(),
                feed=FeedState(True, True, [], [], NOW, 300.0, 0, ""), ollama=(True, True),
                timeouts={"kronos": 10.0, "finr1": 20.0}, now=NOW)
    base.update(kw)
    return {r.key: r for r in health_rows(**base)}


def test_bot_and_guardian_levels():
    assert [_rows(heartbeat_age=a)["bot"].level for a in (5.0, 45.0, 120.0)] == ["ok", "warn", "danger"]
    assert [_rows(guardian_age=a)["guardian"].level for a in (5.0, 45.0, 70.0)] == ["ok", "warn", "danger"]
    assert _rows(heartbeat_age=120.0, in_session=False)["bot"].level == "idle"
    assert _rows(heartbeat_age=5.0)["bot"].text == "5 s ago"


def test_market_data_levels():
    snap = {"symbols": [{"symbol": "A", "last_trade_age": 10.0}, {"symbol": "B", "last_trade_age": 200.0},
                        {"symbol": "C", "last_trade_age": 300.0}, {"symbol": "SPY", "market": True,
                                                                     "last_trade_age": 999.0}]}
    assert _rows(snap=snap)["data"].level == "warn"
    assert _rows(snap=snap)["data"].text == "2 quiet: C 300 s"
    assert _rows(snap=snap, held={"B"})["data"].level == "danger"
    assert _rows(snap={"symbols": [{"symbol": "A", "last_trade_age": 10.0}]})["data"].level == "ok"


def test_alpaca_levels():
    from dashcount_ui.broker_feed import FeedState

    assert _rows()["alpaca"].level == "ok" and _rows()["alpaca"].text == "300 ms"
    assert _rows(feed=FeedState(True, True, [], [], NOW, 300.0, 1, "x"))["alpaca"].level == "warn"
    assert _rows(feed=FeedState(True, False, [], [], NOW - 10, 300.0, 3, "HTTP 500"))["alpaca"].level == "danger"
    assert _rows(feed=FeedState(True, False, [], [], NOW - 90, 300.0, 1, "x"))["alpaca"].level == "danger"
    none = _rows(feed=None)["alpaca"]
    assert none.level == "warn" and none.text == "keys not set (.env)"


def test_ai_rows():
    snap = {"veto_health": {"kronos": {"last_s": 9.0, "avg_s": 6.0, "calls": 5, "errors": 0, "timeouts": 0}}}
    r = _rows(snap=snap)
    assert r["kronos"].level == "warn" and r["kronos"].label == "KRONOS"
    assert r["finr1"].level == "idle" and r["finr1"].text == "not reported"
    snap["veto_health"]["kronos"].update(errors=2, timeouts=1)
    assert _rows(snap=snap)["kronos"].level == "danger"
    ok = {"veto_health": {"kronos": {"last_s": 5.1, "avg_s": 5.4, "calls": 12, "errors": 0, "timeouts": 0}}}
    assert _rows(snap=ok)["kronos"].level == "ok" and _rows(snap=ok)["kronos"].text == "5.1 s (avg 5.4, 12 calls)"
    assert _rows(snap=ok, in_session=False)["kronos"].level == "idle"


def test_ollama_rows_and_check():
    import httpx

    from dashcount_ui.health import OllamaCheck

    assert _rows(ollama=(True, False))["ollama"].level == "warn"
    assert _rows(ollama=(False, False))["ollama"].level == "danger"
    assert _rows(ollama=(False, False), in_session=False)["ollama"].level == "idle"
    good = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"models": [{"name": "m"}]})))
    assert OllamaCheck("http://127.0.0.1:11434", "m", client=good).check() == (True, True)

    def down(req):
        raise httpx.ConnectError("refused")

    bad = httpx.Client(transport=httpx.MockTransport(down))
    assert OllamaCheck("http://127.0.0.1:11434", "m", client=bad).check() == (False, False)


# ------------------------------------------------------------------ alerts
DAY = "2026-10-26"


def _meter(key, frac, level=None):
    from dashcount_ui.risk import Meter, level_for

    return Meter(key, key.upper(), frac * 100, 100.0, frac, level or level_for(frac), f"${frac * 100:.0f} of $100")


def _inputs(**kw):
    from dashcount_ui.alerts import AlertInputs

    base = dict(in_session=True, meters=[_meter("day_loss", 0.1), _meter("fuse", 0.1)], losers_in_row=0,
                streak_limit=4, heartbeat_age=5.0, held_stale=[], rejected=[], alpaca_errors_10min=0,
                alpaca_ok=True, mismatches=[], ollama_up=True, guardian={"armed": True}, day=DAY)
    base.update(kw)
    return AlertInputs(**base)


def test_condition_fires_once_and_rearms(tmp_path):
    from dashcount_ui.alerts import AlertEngine

    eng = AlertEngine(tmp_path)
    eng.start_day(DAY)

    def ids(frac):
        return [a.id for a in eng.evaluate(NOW, _inputs(meters=[_meter("day_loss", frac), _meter("fuse", 0.1)]))]

    assert ids(0.6) == ["day_loss_50"] and ids(0.6) == []
    assert ids(0.4) == [] and ids(0.6) == ["day_loss_50"]
    assert ids(0.8) == ["day_loss_75"]
    a = eng.evaluate(NOW, _inputs(losers_in_row=3))
    assert [x.text for x in a if x.id == "streak_3"] == ["3 losing trades in a row: one more halts the day"]


def test_no_session_alerts_outside_market_hours(tmp_path):
    from dashcount_ui.alerts import AlertEngine

    eng = AlertEngine(tmp_path)
    eng.start_day(DAY)
    stale = [_meter("day_loss", 0.9, "stale"), _meter("fuse", 0.9, "stale")]
    assert eng.evaluate(NOW, _inputs(in_session=False, heartbeat_age=9999.0, ollama_up=False, meters=stale)) == []
    assert [a.id for a in eng.evaluate(NOW, _inputs(heartbeat_age=61.0))] == ["bot_silent"]


def test_journal_events_and_restart(tmp_path):
    from dashcount_ui.alerts import AlertEngine

    ev = [{"kind": "taken", "ts": NOW, "trade_id": "dc-1-1", "symbol": "AAA", "direction": 1, "qty": 10,
           "entry": 12.5}, {"kind": "exit", "ts": NOW + 60, "symbol": "AAA", "pnl": 25.0, "r": 0.5},
          {"kind": "setup", "ts": NOW, "symbol": "BBB"}]
    eng = AlertEngine(tmp_path)
    eng.start_day(DAY)
    got = eng.from_journal(ev, NOW + 61)
    assert [(a.id, a.level, a.text) for a in got] == [
        ("trade_open", "info", "AAA LONG opened: 10 sh at 12.50"),
        ("trade_close", "info", "AAA closed: +25.00 (+0.50R)")]
    again = AlertEngine(tmp_path)
    assert [a.id for a in again.start_day(DAY)] == ["trade_close", "trade_open"]      # newest first
    assert again.from_journal(ev, NOW + 120) == []


def test_guardian_and_rejections(tmp_path):
    from dashcount_ui.alerts import AlertEngine
    from dashcount_ui.reconcile import BlotterRow

    eng = AlertEngine(tmp_path)
    eng.start_day(DAY)
    a = eng.evaluate(NOW, _inputs(guardian={"armed": True, "halted_day": DAY, "halt_reason": "4 losers"}))
    assert [x.text for x in a] == ["Guardian: halted for the day (4 losers)"] and a[0].level == "danger"
    rej = BlotterRow("r1", None, NOW, "EEE", "BUY", "ENTRY", 10.0, 0.0, 5.0, "rejected", None, None, None, None)
    first = eng.evaluate(NOW, _inputs(rejected=[rej]))
    assert [(x.id, x.text) for x in first] == [("order_rejected", "Order rejected: EEE BUY 10")]
    assert eng.evaluate(NOW + 1, _inputs(rejected=[rej])) == []
    assert [x.id for x in eng.evaluate(NOW, _inputs(alpaca_errors_10min=3, alpaca_ok=False))] == ["alpaca_errors"]


# ------------------------------------------------------------------ the DESK screen
def _desk_view(**kw):
    from dashcount_ui.alerts import Alert
    from dashcount_ui.broker_feed import FeedState
    from dashcount_ui.desk import DeskView
    from dashcount_ui.reconcile import blotter_rows, blotter_summary
    from dashcount_ui.risk import risk_meters

    legs = [_order("tp", "x1", "AAA", "sell", "limit", 10, 10, 102.03, "filled", "2026-10-26T14:40:00Z", limit=102.0),
            _order("sl", "x2", "AAA", "sell", "stop", 10, 0, None, "canceled", "2026-10-26T14:31:00Z", stop=99.0)]
    rows = blotter_rows([_order("e1", "dc-1-1", "AAA", "buy", "limit", 10, 10, 100.05, "filled",
                                "2026-10-26T14:30:00Z", limit=100.1, legs=legs)],
                        {"dc-1-1": {"entry": 100.0, "stop": 99.0, "target": 102.0, "qty": 10, "direction": 1}})
    base = dict(meters=risk_meters(SNAP, GUARDIAN, _limits(), 5.0), stale_text="", rows=rows,
                summary=blotter_summary(rows), feed=FeedState(True, True, [], [], NOW, 300.0, 0, ""),
                banner=["AAA: held at the broker but unknown to the bot"], health=list(_rows().values()),
                alerts=[Alert(NOW, "day_loss_50", "warn", "Day loss at 50% of the limit ($400 of $800)", "day_loss_50"),
                        Alert(NOW - 60, "trade_open", "info", "AAA LONG opened: 10 sh at 100.00", "trade_open:dc-1-1")])
    base.update(kw)
    return DeskView(**base)


def test_desk_screen_renders_every_state(tmp_path):
    from PySide6.QtWidgets import QApplication

    from dashcount_ui import pixel as px
    from dashcount_ui.desk import DeskScreen

    app = QApplication.instance() or QApplication([])
    px.load_fonts()
    assert not px.icon_pixmap("heart", 2).isNull()
    d = DeskScreen()
    d.resize(1400, 760)
    v = _desk_view()
    assert len(v.meters) == 5 and len(v.rows) == 3 and len(v.health) == 7
    d.show_view(v)
    d.show()
    app.processEvents()
    img = d.grab()
    img.save(str(tmp_path / "desk.png"))
    assert d.panel_errors() == [] and img.width() >= 1000
    d.show_view(_desk_view(feed=None, rows=[], banner=[], stale_text="bot last seen 3 days ago"))
    app.processEvents()
    d.grab()
    assert d.panel_errors() == []
    d.show_view(_desk_view(meters=[object()]))                   # one broken panel, the rest keep working
    app.processEvents()
    d.grab()
    assert d.panel_errors() == ["RISK METERS"]
    d.close()


def test_blotter_shows_whole_groups_when_cut():
    from dashcount_ui.desk import visible_rows

    rows = [("e1", False), ("t1", True), ("s1", True), ("e2", False), ("t2", True), ("s2", True)]
    assert visible_rows(rows, 4, lambda r: r[1]) == [("e2", False), ("t2", True), ("s2", True)]
    assert visible_rows(rows, 10, lambda r: r[1]) == rows
    assert visible_rows([("t", True), ("s", True)], 1, lambda r: r[1]) == [("s", True)]   # nothing whole fits
