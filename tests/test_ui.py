"""Dashboard smoke tests (offscreen Qt)."""

import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    from dashcount_ui.demo import build_demo

    old = os.environ.get("DASHCOUNT_DATA")
    d = build_demo(tmp_path_factory.mktemp("demo"))
    yield d
    if old is None:
        os.environ.pop("DASHCOUNT_DATA", None)
    else:
        os.environ["DASHCOUNT_DATA"] = old


def test_datasource_reads_demo(demo_dir):
    from dashcount_ui.datasource import DataSource

    ds = DataSource(data=demo_dir)
    snap = ds.snapshot()
    assert snap and snap["budget"]["capital"] == 10000 and len(snap["symbols"]) == 16
    assert ds.bot_state()[0] == "LIVE"
    j = ds.journal_new()
    assert j and {"ts", "kind", "text"} <= set(j[0])
    assert ds.journal_new() == []                    # incremental
    b = ds.bars()
    assert b and len(b["symbols"]) == 16 and b["symbols"]["IRON"]["bars"]
    h = ds.history()
    assert len(h["days"]) == 2 and "error" not in h


def test_bot_state_variants(tmp_path):
    from dashcount_ui.datasource import DataSource

    ds = DataSource(data=tmp_path)
    assert ds.bot_state()[0] == "OFFLINE"
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "heartbeat.json").write_text(json.dumps({"ts": time.time(), "status": "waiting:the open"}))
    assert ds.bot_state()[0] == "WAITING"
    (tmp_path / "state" / "heartbeat.json").write_text(json.dumps({"ts": time.time() - 999, "status": "trading"}))
    assert ds.bot_state()[0] == "IDLE"


def test_window_renders(demo_dir, tmp_path):
    from PySide6.QtWidgets import QApplication

    from dashcount_ui import pixel as px
    from dashcount_ui.app import MainWindow
    from dashcount_ui.datasource import DataSource

    app = QApplication.instance() or QApplication([])
    px.load_fonts()
    w = MainWindow(DataSource(data=demo_dir), demo=True)
    w.resize(1360, 860)
    w.show()
    app.processEvents()
    img = w.grab()
    assert img.width() >= 1100 and img.height() >= 700
    assert w.header.chips and any(k == "eq" for k, *_ in w.header.chips)
    w.toggle_map()
    w.toggle_map()
    assert w.chart.symbol and w.chart.data.get("bars")         # a default stock is charted
    other = [r["symbol"] for r in w.level.rows if r["symbol"] != w.chart.symbol][0]
    w.select_symbol(other)
    assert w.chart.symbol == other and w.chart.data.get("bars")
    w.start_day()                                     # demo mode: refuses, shows a toast
    assert "Demo" in w.footer.toast
    w.close()


def test_sounds_synthesise(tmp_path):
    import wave

    from dashcount_ui.sound import write_tunes

    paths = write_tunes(tmp_path)
    with wave.open(str(paths["win"])) as f:
        assert f.getnframes() > 1000


def test_icon_and_fonts(tmp_path):
    from PySide6.QtWidgets import QApplication

    from dashcount_ui import pixel as px

    QApplication.instance() or QApplication([])
    assert (px.FONT_DIR / "PressStart2P-Regular.ttf").exists()
    px.save_ico(tmp_path / "x.ico")
    assert (tmp_path / "x.ico").exists() or (tmp_path / "x.png").exists()


def test_chart_falls_back_to_alpaca_history(tmp_path, monkeypatch):
    """No bars from the bot for the day on screen -> fetch that session once, cache it."""
    import time as _t

    import pandas as pd

    import dashcount.data.alpaca_data as ad
    from dashcount_ui.datasource import DataSource

    st = tmp_path / "state"
    st.mkdir()
    (st / "live_snapshot.json").write_text(json.dumps({
        "day": "2026-10-22", "symbols": [{"symbol": "AAA", "price": 110.0, "change_pct": 10.0}]}))

    class FakeAD:
        def __init__(self, *a, **k):
            pass

        def minutes(self, syms, start, end, feed):
            return pd.DataFrame({"symbol": ["AAA", "AAA"], "ts": [start, start + 60], "open": [100.0, 101.0],
                                 "high": [102.0, 103.0], "low": [99.0, 100.0], "close": [101.0, 102.0],
                                 "volume": [1000.0, 2000.0]})

    monkeypatch.setattr(ad, "AlpacaData", FakeAD)
    ds = DataSource(data=tmp_path)
    assert ds.bars() is None and ds.chart_source == "loading"
    for _ in range(50):
        _t.sleep(0.1)
        b = ds.bars()
        if b:
            break
    assert b and ds.chart_source == "alpaca"
    aaa = b["symbols"]["AAA"]
    assert len(aaa["bars"]) == 2 and abs(aaa["prev_close"] - 100.0) < 1e-6 and len(aaa["vwap"]) == 2
    assert (st / "bars-2026-10-22.json").exists()


def _wait(cond, tries=60):
    for _ in range(tries):
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_unfinished_session_history_refreshes_and_failures_retry(tmp_path, monkeypatch):
    """Today's history is only complete after the close: keep re-fetching it, and
    never give up for the day after one failed fetch."""
    import datetime as dt

    import pandas as pd

    import dashcount.data.alpaca_data as ad
    from dashcount.timeutil import calendar
    from dashcount_ui.datasource import DataSource

    s = calendar().session(dt.date(2026, 10, 22))
    clock = {"t": s.open + 3600}                                   # an hour into the session
    calls = []

    class FakeAD:
        def __init__(self, *a, **k):
            pass

        def minutes(self, syms, start, end, feed):
            calls.append(clock["t"])
            if len(calls) == 1:
                raise RuntimeError("network down")
            n = len(calls)
            return pd.DataFrame({"symbol": ["AAA"] * n, "ts": [start + 60 * i for i in range(n)],
                                 "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
                                 "close": [100.5] * n, "volume": [1000.0] * n})

    monkeypatch.setattr(ad, "AlpacaData", FakeAD)
    st = tmp_path / "state"
    st.mkdir()
    (st / "live_snapshot.json").write_text(json.dumps({
        "day": "2026-10-22", "symbols": [{"symbol": "AAA", "price": 100.5, "change_pct": 0.5}]}))
    ds = DataSource(data=tmp_path)
    ds.clock = lambda: clock["t"]
    assert ds.bars() is None
    assert _wait(lambda: len(calls) == 1 and not ds._fetching)
    assert ds.bars() is None and len(calls) == 1                  # failed: waits before retrying
    clock["t"] += 61
    ds.bars()
    assert _wait(lambda: ds.bars() is not None)                   # retried and succeeded
    assert len(ds.bars()["symbols"]["AAA"]["bars"]) == 2
    clock["t"] += 30
    ds.bars()
    assert len(calls) == 2                                        # still fresh: no new request
    clock["t"] += 31
    old = ds.bars()                                               # stale: refresh, keep showing the old bars
    assert old is not None
    assert _wait(lambda: len(calls) == 3 and not ds._fetching)
    assert len(ds.bars()["symbols"]["AAA"]["bars"]) == 3


def test_journal_never_loses_a_half_written_line(tmp_path):
    from dashcount_ui.datasource import DataSource

    st = tmp_path / "state"
    st.mkdir()
    j = st / "journal-2026-10-26.jsonl"
    j.write_bytes(b'{"ts": 1, "kind": "setup", "text": "a"}\n{"ts": 2, "kind": "ta')      # bot mid-write
    ds = DataSource(data=tmp_path)
    assert [e["text"] for e in ds.journal_new()] == ["a"]
    with j.open("ab") as fh:
        fh.write(b'ken", "text": "b"}\n')
    assert [e["text"] for e in ds.journal_new()] == ["b"]


def test_bot_commands_never_relaunch_the_dashboard(tmp_path, monkeypatch):
    import shutil
    import sys

    from dashcount_ui.app import bot_command

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "DashCount.exe"))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    py = tmp_path / ".venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")
    assert bot_command(tmp_path, "panic") == [str(py), "-m", "dashcount.cli", "panic"]
    py.unlink()
    monkeypatch.setattr(shutil, "which", lambda name: "C:/tools/uv.exe" if name == "uv" else None)
    assert bot_command(tmp_path, "panic") == ["C:/tools/uv.exe", "run", "--no-sync", "dashcount", "panic"]
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):                             # never run DashCount.exe as the bot
        bot_command(tmp_path, "panic")


def test_new_day_starts_a_fresh_map_and_journal(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    import dashcount_ui.app as appmod
    from dashcount_ui import pixel as px
    from dashcount_ui.app import MainWindow
    from dashcount_ui.datasource import DataSource

    class NoNetwork:                                     # a real (non-demo) window must not call out in tests
        def __init__(self, *a, **k):
            pass

        def check(self):
            return True, True

    monkeypatch.setattr(appmod, "feed_from_env", lambda *a, **k: None)
    monkeypatch.setattr(appmod, "OllamaCheck", NoNetwork)

    app = QApplication.instance() or QApplication([])
    px.load_fonts()
    st = tmp_path / "state"
    st.mkdir()

    def day(d, ts, eq):
        (st / "live_snapshot.json").write_text(json.dumps({
            "day": d, "ts": ts, "budget": {"equity": eq, "start_equity": 10000.0, "pnl": eq - 10000.0}}))
        (st / f"journal-{d}.jsonl").write_text(json.dumps({"ts": ts, "kind": "setup", "text": d}) + "\n")
        os.utime(st / "live_snapshot.json", (ts, ts))

    day("2026-10-23", 1000.0, 10100.0)
    w = MainWindow(DataSource(data=tmp_path))
    w.refresh()
    assert w.intraday and w.journal.entries
    day("2026-10-26", 2000.0, 10050.0)
    w.refresh()
    assert [eq for _, eq in w.intraday] == [10050.0]
    assert [e["text"] for e in w.journal.entries] == ["2026-10-26"]
    w.close()
    app.processEvents()


def test_practice_summary_is_cached_until_the_run_changes(tmp_path, monkeypatch):
    import dashcount.scoreboard as sb
    from dashcount_ui.datasource import DataSource

    run = tmp_path / "records" / "practice-x-20261026-120000"
    (run / "daily").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(sb, "summarize", lambda p: calls.append(p) or {"run": p.name, "days": 1})
    ds = DataSource(data=tmp_path)
    assert ds.history()["practice"]["run"] == run.name
    ds.history()
    assert len(calls) == 1
    (run / "trades").mkdir()
    t = time.time() + 5
    os.utime(run / "trades", (t, t))
    ds.history()
    assert len(calls) == 2


def test_last_seen_reads_naturally_and_practice_defaults_follow_the_data(tmp_path):
    from dashcount_ui.datasource import DataSource

    st = tmp_path / "state"
    st.mkdir()
    (st / "heartbeat.json").write_text(json.dumps({"ts": time.time() - 3 * 86400, "status": "done:x"}))
    ds = DataSource(data=tmp_path)
    assert ds.bot_state() == ("IDLE", "last seen 3 days ago")
    for d in ("2026-06-01", "2026-06-02", "2026-10-15", "2026-10-16"):
        (tmp_path / "store" / "ticks" / "feed=iex" / d).mkdir(parents=True)
    assert ds.practice_range(3) == ("2026-06-02", "2026-10-16")


def _window(data_dir):
    from PySide6.QtWidgets import QApplication

    from dashcount_ui import pixel as px
    from dashcount_ui.app import MainWindow
    from dashcount_ui.datasource import DataSource

    app = QApplication.instance() or QApplication([])
    px.load_fonts()
    w = MainWindow(DataSource(data=data_dir), demo=True)
    w.resize(1440, 900)
    w.show()
    app.processEvents()
    return app, w


def test_desk_tab_in_demo(demo_dir):
    app, w = _window(demo_dir)
    w.show_screen("desk")
    app.processEvents()
    w.desk.grab()
    assert w.desk.isVisible() and w.desk.panel_errors() == []
    v = w.desk.view
    assert len(v.meters) == 5 and len(v.rows) >= 1 and len(v.health) == 7
    w.show_screen("table")
    app.processEvents()
    assert w.level.isVisible() and not w.desk.isVisible()
    w.close()


def test_alert_badge_counts_while_on_table(demo_dir, tmp_path):
    import shutil

    d = tmp_path / "demo"
    shutil.copytree(demo_dir, d)
    for f in (d / "state").glob("alerts-*.jsonl"):
        f.unlink()
    app, w = _window(d)
    assert w.tabs.badges.get("desk", 0) == 0                        # the journal backlog is not news
    jf = sorted((d / "state").glob("journal-*.jsonl"))[-1]
    with jf.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "kind": "taken", "symbol": "ZZZ", "trade_id": "dc-9-9", "entry": 10.0,
                             "stop": 9.5, "target": 11.0, "qty": 5, "direction": 1, "text": "x"}) + "\n")
    w.refresh()
    assert w.tabs.badges.get("desk") == 1
    w.show_screen("desk")
    assert w.tabs.badges.get("desk") == 0
    w.close()


def test_one_failing_desk_input_only_breaks_its_own_panel(demo_dir, monkeypatch):
    import dashcount_ui.app as appmod

    def boom(*a, **k):
        raise ValueError("bad budget")

    app, w = _window(demo_dir)
    monkeypatch.setattr(appmod, "risk_meters", boom)
    w.refresh()
    w.show_screen("desk")
    app.processEvents()
    w.desk.grab()
    assert w.desk.panel_errors() == ["RISK METERS"]
    assert len(w.desk.view.health) == 7                          # the other panels still update
    w.close()


def test_screenshot_desk(tmp_path):
    from dashcount_ui.app import main

    p = tmp_path / "desk.png"
    assert main(["--demo", "--screenshot", str(p), "--screen", "desk"]) == 0
    assert p.exists() and p.stat().st_size > 10_000
