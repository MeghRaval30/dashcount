"""DashCount dashboard: main window, live refresh and the hotbar actions."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QApplication, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QMessageBox, QScrollArea, QVBoxLayout, QWidget)

from . import pixel as px
from .datasource import DataSource, ago, project_root
from .sound import Sounds
from .widgets import (Footer, Header, HotbarButton, InventoryView, JournalView, LevelView, ParchmentPanel,
                      PixelPanel, PriceChart, ScoreView, TreasureMap, countdown, money)

ET = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

STYLE = """
QToolTip { background: #e8d5a6; color: #3d2b14; border: 2px solid #6b4f2a; font-family: 'VT323'; font-size: 18px; }
QMessageBox, QDialog { background: #34343a; }
QMessageBox QLabel, QDialog QLabel { color: #f2f2f2; font-family: 'VT323'; font-size: 22px; }
QPushButton { background: #8b8b8b; color: #0b0b0d; border: 2px solid #0b0b0d; padding: 8px 16px;
              font-family: 'Press Start 2P'; font-size: 10px; }
QPushButton:hover { border: 2px solid #f2f2f2; }
QDateEdit, QComboBox { background: #1f1f24; color: #f2f2f2; border: 2px solid #0b0b0d; padding: 4px 8px;
                       font-family: 'VT323'; font-size: 22px; min-width: 160px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: #1f1f24; width: 12px; }
QScrollBar::handle:vertical { background: #7d7d7d; min-height: 24px; }
"""


def bot_command(root: Path, *args: str) -> list[str]:
    """How to run a `dashcount` command for this project: its own virtualenv first, then uv.
    Never sys.executable when packaged: that is DashCount.exe, so PANIC would just open
    another dashboard instead of closing positions."""
    for py in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if py.exists():
            return [str(py), "-m", "dashcount.cli", *args]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--no-sync", "dashcount", *args]
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "dashcount.cli", *args]
    raise RuntimeError(f"can't find the bot: no .venv in {root} and uv is not on PATH")


class Signals(QObject):
    toast = Signal(str)


class Stone(QWidget):
    """Background: a wall of dark stone blocks."""

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.drawPixmap(0, 0, px.texture("stone", self.width(), self.height(), 12))
        p.end()


class PracticeDialog(QDialog):
    def __init__(self, parent=None, days: tuple[str, str] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Run a practice")
        form = QFormLayout(self)
        form.addRow(QLabel("Replay history through the bot (needs `dashcount download` first)."))
        first, last = days or ("2026-09-28", "2026-10-16")           # default: the latest downloaded days
        self.start = QDateEdit(QDate.fromString(first, "yyyy-MM-dd"))
        self.end = QDateEdit(QDate.fromString(last, "yyyy-MM-dd"))
        for d in (self.start, self.end):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        self.vetoes = QComboBox()
        self.vetoes.addItems(["full", "kronos", "finr1", "none"])
        form.addRow("Start", self.start)
        form.addRow("End", self.end)
        form.addRow("AI vetoes", self.vetoes)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class MainWindow(QWidget):
    def __init__(self, ds: DataSource, demo: bool = False) -> None:
        super().__init__()
        self.ds = ds
        self.demo = demo
        self.settings = QSettings("DashCount", "Dashboard")
        self.sounds = Sounds()
        self.sounds.enabled = self.settings.value("sound", "false") == "true"
        self.sig = Signals()
        self.sig.toast.connect(self.toast)
        self.practice_proc: subprocess.Popen | None = None
        self.history: dict = {}
        self.intraday: list[tuple[float, float]] = []
        self.day: str | None = None
        self.show_all_days = False
        self._toast_until = 0.0

        self.setWindowTitle("DashCount - the treasure hunter's trading table" + ("  [DEMO DATA]" if demo else ""))
        self.setWindowIcon(px.app_icon())
        self.resize(1440, 900)
        self.setMinimumSize(1200, 760)
        self.selected: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = Header()
        self.header.chip_clicked.connect(self.on_chip)
        outer.addWidget(self.header)

        stone = Stone()
        outer.addWidget(stone, 1)
        rows = QVBoxLayout(stone)
        rows.setContentsMargins(10, 10, 10, 6)
        rows.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(10)
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        rows.addLayout(top, 11)
        rows.addLayout(bottom, 7)

        # top row: the level | price chart | journal
        self.level = LevelView()
        self.level.symbol_clicked.connect(self.select_symbol)
        lp = PixelPanel("THE LEVEL", "spike")
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(self.level)
        sa.viewport().setAutoFillBackground(False)
        self.level.setAutoFillBackground(False)
        lp.body.addWidget(sa)
        lp.setToolTip("Click a stock to chart it")
        top.addWidget(lp, 6)
        cp = PixelPanel("PRICE CHART", "compass")
        self.chart = PriceChart()
        cp.body.addWidget(self.chart)
        top.addWidget(cp, 6)
        jp = ParchmentPanel("JOURNAL")
        self.journal = JournalView()
        jp.body.addWidget(self.journal)
        top.addWidget(jp, 5)

        # bottom row: inventory | scoreboard | treasure map
        ip = PixelPanel("INVENTORY", "sword")
        self.inventory = InventoryView(2)
        ip.body.addWidget(self.inventory)
        bottom.addWidget(ip, 6)
        sp = PixelPanel("SCOREBOARD", "flag")
        self.score = ScoreView()
        sp.body.addWidget(self.score)
        bottom.addWidget(sp, 6)
        self.map = TreasureMap()
        self.map.mousePressEvent = lambda e: self.toggle_map()
        self.map.setToolTip("Click to switch between today and all live days")
        bottom.addWidget(self.map, 5)

        bar = QWidget()
        bar.setStyleSheet("background: #1f1f24;")
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(10, 8, 10, 8)
        hb.setSpacing(8)
        hb.addStretch(1)
        for label, icon, fn, tnt, tip in (
            ("START", "play", self.start_day, False, "Start today's run: Guardian + bot (paper)"),
            ("STOP", "stop", self.stop_day, False, "Close all positions and end today's run"),
            ("PANIC", "tnt", self.panic, True, "Close EVERYTHING now and block trading until re-armed"),
            ("RE-ARM", "shield", self.rearm, False, "Re-arm after PANIC or a blown drawdown fuse"),
            ("PRACTICE", "compass", self.practice, False, "Replay downloaded history through the bot"),
            ("REPORT", "book", self.open_report, False, "Open the newest HTML report"),
        ):
            b = HotbarButton(label, icon, tnt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            hb.addWidget(b)
        hb.addStretch(1)
        outer.addWidget(bar)
        self.footer = Footer()
        outer.addWidget(self.footer)

        self.journal.add(self.ds.journal_new())
        self.journal.render()
        self.refresh_history()
        self.refresh()
        self.fast = QTimer(self)
        self.fast.timeout.connect(self.refresh)
        self.fast.start(1000)
        self.slow = QTimer(self)
        self.slow.timeout.connect(self.refresh_history)
        self.slow.start(30_000)

    # ---------------------------------------------------------------- refresh
    def refresh(self) -> None:
        snap = self.ds.snapshot() or {}
        state, detail = self.ds.bot_state()
        if self.demo and snap:
            state, detail = "LIVE", "demo data"
        g = self.ds.guardian()
        today = snap.get("day") or dt.datetime.now(ET).date().isoformat()
        if snap.get("day") and snap["day"] != self.day:
            if self.day is not None:                # left open overnight: today's map and journal start fresh
                self.intraday = []
                self.journal.entries = []
                self.journal.render()
            self.day = snap["day"]

        new = self.ds.journal_new()
        if new:
            self.journal.add(new)
            for ev in new:
                if ev.get("kind") == "taken":
                    self.sounds.play("taken")
                elif ev.get("kind") == "exit":
                    self.sounds.play("win" if (ev.get("pnl") or 0) > 0 else "loss")

        budget = snap.get("budget") or {}
        eq = budget.get("equity")
        pnl = budget.get("pnl")
        if eq is not None and snap.get("ts"):
            if not self.intraday or self.intraday[-1][0] < snap["ts"]:
                self.intraday.append((snap["ts"], eq))
                self.intraday = self.intraday[-5000:]

        chips = []
        chips.append(("state", {"LIVE": "LIVE", "WAITING": "WAITING", "IDLE": "IDLE", "OFFLINE": "OFFLINE"}[state],
                      {"LIVE": "emerald", "WAITING": "gold", "IDLE": "slot", "OFFLINE": "stone_lo"}[state],
                      "black" if state != "OFFLINE" else "text"))
        if g.get("panic"):
            chips.append(("guard", "PANIC", "redstone", "text"))
        elif g and not g.get("armed", True):
            chips.append(("guard", "FUSE BLOWN", "redstone", "text"))
        elif g.get("halted_day") == today:
            chips.append(("guard", "HALTED", "warn", "black"))
        else:
            chips.append(("guard", "ARMED", "lapis", "text"))
        chips.append(("acct", str(snap.get("account", "paper")).upper(), "deepslate", "parch"))
        if eq is not None:
            chips.append(("eq", f"${eq:,.2f}", "gold", "ink"))
            r = snap.get("today_r") or 0.0
            chips.append(("pnl", f"{money(pnl, True)}  {r:+.1f}R", "black", "good" if (pnl or 0) >= 0 else "bad"))
        now = time.time()
        chips.append(("clock", f"NY {dt.datetime.now(ET):%H:%M}  IST {dt.datetime.now(IST):%H:%M}", "deepslate", "text"))
        sess = snap.get("session") or {}
        if sess.get("open") and sess.get("close") and sess["open"] - 3600 * 12 < now < sess["close"]:
            if now < sess["open"]:
                chips.append(("cd", f"OPENS IN {countdown(sess['open'] - now)}", "deepslate", "gold"))
            else:
                chips.append(("cd", f"CLOSES IN {countdown(sess['close'] - now)}", "deepslate", "gold"))
        if self.practice_proc is not None:
            if self.practice_proc.poll() is None:
                chips.append(("prac", "PRACTICE...", "amethyst", "black"))
            else:
                self.practice_proc = None
                self.toast("Practice run finished: press REPORT to open it.")
                self.refresh_history()
        chips.append(("sound", "SOUND ON" if self.sounds.enabled else "SOUND OFF", "slot", "black"))
        self.header.set_chips(chips)

        bars = self.ds.bars() or {}
        bars_by_sym = bars.get("symbols") or {}
        self.level.set_data(snap.get("symbols") or [], snap.get("market") or {}, bars_by_sym,
                            live=state == "LIVE")
        self.inventory.set_trades(snap.get("trades") or [])
        self.update_chart(snap, bars)
        self.update_score(snap)
        self.update_map(snap)

        ev = snap.get("events") or {}
        age = self.ds.snapshot_age()
        left = f"{detail}  |  {ev.get('trade', 0):,} trades + {ev.get('quote', 0):,} quotes received"
        if snap.get("blocked_now"):
            left += f"  |  window: {snap['blocked_now'].replace('_', ' ')}"
        if snap.get("guardian_block"):
            left += f"  |  blocked: {snap['guardian_block']}"
        if age is not None:
            right = f"snapshot {f'{age:.0f}s' if age < 120 else ago(age)} old  |  {self.ds.data}"
        else:
            right = str(self.ds.data)
        if now > self._toast_until:
            self.footer.toast = ""
        self.footer.set_text(left, right)

    def select_symbol(self, sym: str) -> None:
        self.selected = sym
        self.update_chart(self.ds.snapshot() or {}, self.ds.bars() or {})

    def update_chart(self, snap: dict, bars: dict) -> None:
        syms = [s for s in (snap.get("symbols") or []) if not s.get("market")]
        if self.selected is None and syms:
            busy = [s["symbol"] for s in syms if s.get("busy")]
            self.selected = busy[0] if busy else max(syms, key=lambda s: s.get("score") or 0)["symbol"]
            self.level.selected = self.selected
        sym = self.selected
        live = next((s for s in syms if s["symbol"] == sym), None)
        trade = next((t for t in (snap.get("trades") or []) if t["symbol"] == sym), None)
        marks = [e for e in self.journal.entries if e.get("symbol") == sym and e.get("kind") in ("taken", "exit")]
        session = (bars["open"], bars["close"]) if bars.get("open") else None
        src = self.ds.chart_source
        self.chart.empty_text = {
            "loading": f"Loading {sym}'s minute bars from Alpaca...",
            "none": f"No bars for {sym} yet: they appear once the bot runs (or when Alpaca keys are set).",
        }.get(src, f"No bars for {sym} on this day.")
        self.chart.source = src
        self.chart.set_data(sym, (bars.get("symbols") or {}).get(sym), session, trade, marks, live)

    def refresh_history(self) -> None:
        try:
            self.history = self.ds.history()
        except Exception as e:
            self.history = {"error": str(e)}

    def update_score(self, snap: dict) -> None:
        lines = [("#TODAY", "", "text")]
        right: list[tuple[str, str, str]] = []
        closed = snap.get("closed_today") or []
        wins = sum(1 for t in closed if (t.get("pnl") or 0) > 0)
        counts = snap.get("counts") or {}
        lines += [
            ("trades (won/lost)", f"{len(closed)} ({wins}/{len(closed) - wins})", "text"),
            ("setups seen / taken", f"{counts.get('attempts', 0)} / {counts.get('taken', 0)}", "text"),
            ("profit today", money((snap.get("budget") or {}).get("pnl"), True),
             "good" if ((snap.get("budget") or {}).get("pnl") or 0) >= 0 else "bad"),
        ]
        tr = self.history.get("trades") or []
        days = self.history.get("days") or []
        lines.append(("#ALL LIVE DAYS", "", "text"))
        if tr:
            w = sum(1 for t in tr if t["pnl"] > 0)
            tot = sum(t["pnl"] for t in tr)
            lines += [("days / trades", f"{len(days)} / {len(tr)}", "text"),
                      ("win rate", f"{w / len(tr):.0%}", "text"),
                      ("total profit", money(tot, True), "good" if tot >= 0 else "bad")]
        else:
            lines.append(("no finished trades yet", "", "muted"))
        vs = (snap.get("veto_score") or self.history.get("veto_score") or {})
        left, lines = lines, right
        lines.append(("#AI VETOES (GHOST-SCORED)", "", "text"))
        for src, name in (("kronos", "Kronos"), ("finr1", "Fin-R1")):
            s = vs.get(src)
            if s:
                lines.append((f"{name}: {s.get('vetoes', 0)}/{s.get('decisions', 0)} vetoed",
                              f"saved {s.get('saved_r', 0.0):+.1f}R", "good" if s.get("saved_r", 0) >= 0 else "bad"))
            else:
                lines.append((f"{name}: no decisions yet", "", "muted"))
        pr = self.history.get("practice")
        lines.append(("#LATEST PRACTICE", "", "text"))
        if pr and not pr.get("error"):
            lines.append((pr["run"].removeprefix("practice-")[:30], "", "muted"))
            if pr.get("trades"):
                lines.append((f"{pr.get('days')} days, {pr['trades']} trades", f"{pr.get('win_rate') or 0:.0%} won", "text"))
                pv = pr.get("pnl_realistic") or 0.0
                lines.append(("realistic profit", money(pv, True), "good" if pv >= 0 else "bad"))
            else:
                lines.append((f"{pr.get('days')} days, no trades", "", "muted"))
        else:
            lines.append(("none yet: press PRACTICE", "", "muted"))
        self.score.set_lines(left, right)

    def update_map(self, snap: dict) -> None:
        if self.show_all_days:
            days = self.history.get("days") or []
            pts = [(i, d["end"]) for i, d in enumerate(days)]
            if days:
                pts = [(-1, days[0]["start"])] + pts
            self.map.set_path(pts, [], "all live days")
            return
        budget = snap.get("budget") or {}
        start = budget.get("start_equity")
        closed = sorted(snap.get("closed_today") or [], key=lambda t: t.get("closed_ts") or 0)
        pts: list[tuple[float, float]] = []
        marks = []
        sess = snap.get("session") or {}
        if start is not None:
            pts.append((sess.get("open") or (closed[0]["closed_ts"] - 60 if closed else time.time() - 60), start))
            run = start
            for t in closed:
                run += t.get("pnl") or 0.0
                pts.append((t["closed_ts"], run))
                marks.append((t["closed_ts"], run, (t.get("pnl") or 0) > 0))
        pts += [p for p in self.intraday if not pts or p[0] > pts[-1][0]]
        self.map.set_path(sorted(pts), marks, "today")

    def toggle_map(self) -> None:
        self.show_all_days = not self.show_all_days
        self.update_map(self.ds.snapshot() or {})

    # ---------------------------------------------------------------- actions
    def toast(self, msg: str) -> None:
        self.footer.toast = msg
        self._toast_until = time.time() + 8
        self.footer.update()

    def on_chip(self, key: str) -> None:
        if key == "sound":
            self.sounds.enabled = not self.sounds.enabled
            self.settings.setValue("sound", "true" if self.sounds.enabled else "false")
            self.sounds.play("click")
            self.refresh()
        elif key == "state":
            self.toast(" | ".join(self.ds.bot_state()))

    def _cmd(self, *args: str) -> list[str]:
        return bot_command(self.ds.root, *args)

    def _spawn(self, args: list[str], log_name: str) -> subprocess.Popen | None:
        try:
            cmd = self._cmd(*args)
        except RuntimeError as e:
            self.toast(f"Can't start it: {e}")
            return None
        logs = self.ds.root / "logs"
        logs.mkdir(exist_ok=True)
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        with open(logs / log_name, "a", encoding="utf-8") as out:      # the child keeps its own handle
            return subprocess.Popen(cmd, cwd=self.ds.root, stdout=out, stderr=subprocess.STDOUT, creationflags=flags)

    def _confirm(self, title: str, text: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setIconPixmap(px.icon_pixmap("tnt" if "PANIC" in title else "compass", 6))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _run_bg(self, args: list[str], done_msg: str) -> None:
        def work():
            try:
                r = subprocess.run(self._cmd(*args), cwd=self.ds.root, capture_output=True, text=True, timeout=120,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
                if r.returncode != 0:
                    self.sig.toast.emit(f"{args[0].upper()} FAILED (exit {r.returncode}): {tail[-1] if tail else ''}"
                                        f" - run `dashcount {' '.join(args)}` in a terminal")
                else:
                    self.sig.toast.emit(tail[-1] if tail else done_msg)
            except Exception as e:
                self.sig.toast.emit(f"{args[0].upper()} FAILED: {e} - run `dashcount {' '.join(args)}` in a terminal")
        threading.Thread(target=work, daemon=True).start()

    def start_day(self) -> None:
        if self.demo:
            return self.toast("Demo mode: START is disabled.")
        state, _ = self.ds.bot_state()
        if state in ("LIVE", "WAITING"):
            return self.toast("The bot is already running.")
        if self._spawn(["run-day"], "run-day-console.log"):
            self.toast("Starting: Guardian + bot. It builds the level map at 09:20 New York time.")

    def stop_day(self) -> None:
        if self.demo:
            return self.toast("Demo mode: STOP is disabled.")
        if self.ds.bot_state()[0] not in ("LIVE", "WAITING"):
            return self.toast("The bot isn't running.")
        if self._confirm("STOP", "Close all open positions and end today's run?\nThe report is written at the end."):
            (self.ds.state / "stop.flag").write_text(json.dumps({"ts": time.time()}))
            self.toast("Stop requested: the bot is closing positions and ending the day.")

    def panic(self) -> None:
        if self.demo:
            return self.toast("Demo mode: PANIC is disabled.")
        if self._confirm("PANIC", "TNT!\n\nCancel every order, close every position NOW,\n"
                                  "and block trading until you press RE-ARM?"):
            self.sounds.play("panic")
            self._run_bg(["panic"], "Panic done: everything closed.")
            self.toast("PANIC sent...")

    def rearm(self) -> None:
        if self.demo:
            return self.toast("Demo mode: RE-ARM is disabled.")
        if self._confirm("RE-ARM", "Allow the bot to trade again?\n(Use after PANIC or a blown drawdown fuse.)"):
            self._run_bg(["arm"], "Re-armed.")

    def practice(self) -> None:
        if self.practice_proc is not None and self.practice_proc.poll() is None:
            return self.toast("A practice run is already going.")
        d = PracticeDialog(self, self.ds.practice_range())
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        s, e = d.start.date().toString("yyyy-MM-dd"), d.end.date().toString("yyyy-MM-dd")
        self.practice_proc = self._spawn(["practice", "--start", s, "--end", e, "--vetoes", d.vetoes.currentText()],
                                         "practice-console.log")
        if self.practice_proc:
            self.toast(f"Practice {s} to {e} started (vetoes: {d.vetoes.currentText()}).")

    def open_report(self) -> None:
        rep = self.ds.latest_report()
        if rep is None:
            return self.toast("No reports yet: run a practice or finish a trading day.")
        if os.name == "nt":
            os.startfile(str(rep))  # noqa: S606 - opening the user's own report
        self.toast(f"Opened {rep.name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dashcount-ui")
    ap.add_argument("--demo", action="store_true", help="show a synthetic morning (no bot needed)")
    ap.add_argument("--data", help="data folder (default: <project>/data)")
    ap.add_argument("--screenshot", help="render the window to this PNG and exit")
    args = ap.parse_args(argv)

    root = project_root()
    os.environ.setdefault("DASHCOUNT_HOME", str(root))        # so bot modules find config/ when packaged
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")                            # Alpaca keys for chart history
    except Exception:
        pass
    data = Path(args.data) if args.data else None
    if args.demo:
        from .demo import build_demo

        data = build_demo()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    px.load_fonts()
    app.setStyleSheet(STYLE)
    app.setWindowIcon(px.app_icon())
    win = MainWindow(DataSource(root, data), demo=args.demo)
    if args.screenshot:
        win.resize(1440, 900)
        win.show()
        for _ in range(5):
            app.processEvents()
        win.grab().save(args.screenshot)
        return 0
    win.show()
    return app.exec()
