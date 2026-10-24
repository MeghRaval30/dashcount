import datetime as dt
from types import SimpleNamespace

import httpx

from dashcount.broker.alpaca import AlpacaBroker
from dashcount.data.alpaca_data import AlpacaData


def _client(handler):
    return httpx.Client(base_url="https://data.alpaca.markets", transport=httpx.MockTransport(handler))


def test_bars_pagination_and_parsing():
    pages = []

    def handler(req):
        pages.append(dict(req.url.params))
        if "page_token" not in req.url.params:
            return httpx.Response(200, json={"bars": {"AAPL": [{"t": "2025-03-12T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}]},
                                              "next_page_token": "abc"})
        return httpx.Response(200, json={"bars": {"AAPL": [{"t": "2025-03-12T13:31:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 50}],
                                                  "MSFT": [{"t": "2025-03-12T13:30:00Z", "o": 3, "h": 3, "l": 3, "c": 3, "v": 7}]},
                                         "next_page_token": None})

    ad = AlpacaData("k", "s", client=_client(handler))
    df = ad.minutes(["AAPL", "MSFT"], 1741786200.0, 1741786400.0, "iex")
    assert len(pages) == 2 and pages[0]["feed"] == "iex" and pages[0]["timeframe"] == "1Min"
    assert pages[0]["limit"] == "10000" and pages[1]["page_token"] == "abc"
    assert sorted(df["symbol"]) == ["AAPL", "AAPL", "MSFT"]
    assert df[df.symbol == "AAPL"]["ts"].tolist() == [1741786200.0, 1741786260.0]


def test_daily_dates_are_new_york_dates():
    def handler(req):
        return httpx.Response(200, json={"bars": {"SPY": [{"t": "2025-03-11T04:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}})

    df = AlpacaData("k", "s", client=_client(handler)).daily(["SPY"], dt.date(2025, 3, 11), dt.date(2025, 3, 11), "sip")
    assert df["date"].tolist() == [dt.date(2025, 3, 11)]


def test_trades_filter_bad_conditions_and_quotes_dedupe():
    def handler(req):
        if req.url.path.endswith("/trades"):
            return httpx.Response(200, json={"trades": {"X": [
                {"t": "2025-03-12T14:00:00.000000001Z", "p": 10.0, "s": 100, "c": ["@"]},
                {"t": "2025-03-12T14:00:01Z", "p": 99.0, "s": 5, "c": ["@", "W"]},      # average-price print: dropped
                {"t": "2025-03-12T14:00:02Z", "p": 10.01, "s": 50, "c": ["@", "I"]},
            ]}})
        return httpx.Response(200, json={"quotes": {"X": [
            {"t": "2025-03-12T14:00:00Z", "bp": 9.99, "ap": 10.01, "bs": 1, "as": 2},
            {"t": "2025-03-12T14:00:00.5Z", "bp": 9.99, "ap": 10.01, "bs": 1, "as": 2},   # unchanged: dropped
            {"t": "2025-03-12T14:00:01Z", "bp": 10.0, "ap": 10.02, "bs": 1, "as": 2},
        ]}})

    ad = AlpacaData("k", "s", client=_client(handler))
    t = ad.trades(["X"], 0, 1, "iex")["X"]
    assert t["price"].tolist() == [10.0, 10.01]
    q = ad.quotes(["X"], 0, 1, "iex")["X"]
    assert len(q) == 2



def test_ticks_stream_page_by_page_and_dedupe_across_pages():
    # big SIP days are converted one page at a time; an unchanged quote at the top
    # of the next page is still a duplicate
    def handler(req):
        last = "page_token" in req.url.params
        if req.url.path.endswith("/trades"):
            rows = [{"t": "2025-03-12T14:00:03Z", "p": 10.02, "s": 10, "c": ["@"]}] if last else                    [{"t": "2025-03-12T14:00:00Z", "p": 10.0, "s": 100, "c": ["@"]}]
            return httpx.Response(200, json={"trades": {"X": rows}, "next_page_token": None if last else "p2"})
        rows = [{"t": "2025-03-12T14:00:02Z", "bp": 9.99, "ap": 10.01, "bs": 1, "as": 2},      # same top: dropped
                {"t": "2025-03-12T14:00:03Z", "bp": 10.0, "ap": 10.02, "bs": 1, "as": 2}] if last else                [{"t": "2025-03-12T14:00:00Z", "bp": 9.99, "ap": 10.01, "bs": 1, "as": 2}]
        return httpx.Response(200, json={"quotes": {"X": rows}, "next_page_token": None if last else "p2"})

    ad = AlpacaData("k", "s", client=_client(handler))
    assert ad.trades(["X"], 0, 1, "sip")["X"]["price"].tolist() == [10.0, 10.02]
    q = ad.quotes(["X"], 0, 1, "sip")["X"]
    assert [round(float(b), 2) for b in q["bid"]] == [9.99, 10.0] and ad.requests == 4

def test_rate_limit_retry(monkeypatch):
    import dashcount.data.alpaca_data as m

    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "0"})
        return httpx.Response(200, json={"bars": {}})

    AlpacaData("k", "s", client=_client(handler)).minutes(["A"], 0, 60, "iex")
    assert calls["n"] == 2


def _broker():
    b = AlpacaBroker.__new__(AlpacaBroker)
    b.roles = {"e1": "entry"}
    b._listener = None
    b.post = lambda fn: fn()
    return b


def test_trade_update_mapping():
    b = _broker()
    order = SimpleNamespace(id="e1", symbol="AAPL", side=SimpleNamespace(value="buy"), filled_qty="40",
                            filled_avg_price="190.5", client_order_id="dc-1", order_type="limit", type="limit")
    data = SimpleNamespace(event="partial_fill", order=order, qty="40", price="190.5",
                           timestamp=dt.datetime(2025, 3, 12, 14, 0, tzinfo=dt.timezone.utc))
    u = b._to_update(data)
    assert (u.role, u.event, u.filled_qty, u.last_fill_qty, u.side) == ("entry", "partial_fill", 40.0, 40.0, "buy")
    leg = SimpleNamespace(id="x9", symbol="AAPL", side="sell", filled_qty="40", filled_avg_price="188",
                          client_order_id=None, order_type="stop", type="stop")
    u = b._to_update(SimpleNamespace(event="fill", order=leg, qty="40", price="188", timestamp=None))
    assert u.role == "stop" and u.last_fill_price == 188.0
    assert b._to_update(SimpleNamespace(event="pending_new", order=leg, qty=None, price=None, timestamp=None)) is None
