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
