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
