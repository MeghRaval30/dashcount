"""Read-only access to everything the bot and the Guardian write to disk."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


def project_root() -> Path:
    env = os.environ.get("DASHCOUNT_HOME")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        home = exe.parent / "home.txt"
        if home.exists():
            return Path(home.read_text(encoding="utf-8").strip())
        for p in exe.parents:
            if (p / "config" / "default.yaml").exists():
                return p
    return Path(__file__).resolve().parents[2]


def data_root(root: Path) -> Path:
    return Path(os.environ.get("DASHCOUNT_DATA", root / "data"))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ago(secs: float) -> str:
    if secs < 90 * 60:
        return f"{int(secs // 60)} min"
    if secs < 48 * 3600:
        return f"{int(secs // 3600)} h"
    return f"{int(secs // 86400)} days"


class DataSource:
    FRESH_S = 60                          # re-fetch an unfinished session's history this often

    def __init__(self, root: Path | None = None, data: Path | None = None) -> None:
        self.root = root or project_root()
        self.data = data or data_root(self.root)
        self.state = self.data / "state"
        self.clock = time.time
        self._snap = None
        self._snap_mtime = 0.0
        self._bars = None
        self._bars_mtime = 0.0
        self._fallback: dict = {}          # (day, symbols) -> bars dict fetched from Alpaca
        self._fetching: set = set()
        self._retry_at: dict = {}          # (day, symbols) -> when a failed fetch may be retried
        self.chart_source = "none"
        self._jfile: Path | None = None
        self._joff = 0
        self._practice_key = None
        self._practice = None

    # ---------------------------------------------------------- live state
    def snapshot(self) -> dict | None:
        p = self.state / "live_snapshot.json"
        try:
            m = p.stat().st_mtime
        except OSError:
            return self._snap
        if m != self._snap_mtime:
            s = _read_json(p)
            if s is not None:                    # keep the last good one on a partial write
                self._snap, self._snap_mtime = s, m
        return self._snap

    def live_bars(self) -> dict | None:
        """Minute bars written by the running bot (every ~15 s)."""
        p = self.state / "live_bars.json"
        try:
            m = p.stat().st_mtime
        except OSError:
            return self._bars
        if m != self._bars_mtime:
            b = _read_json(p)
            if b is not None:
                self._bars, self._bars_mtime = b, m
        return self._bars

    def bars(self) -> dict | None:
        """Bars for the day on screen: the bot's own when it saved them, otherwise
        that session's minute bars fetched once from Alpaca (read-only) and cached."""
        snap = self.snapshot() or {}
        day = snap.get("day")
        live = self.live_bars()
        if live and (day is None or live.get("day") == day):
            self.chart_source = "bot"
            return live
        syms = [s["symbol"] for s in snap.get("symbols") or []]
        if not day or not syms:
            return None
        key = (day, tuple(syms))
        cache = self.state / f"bars-{day}.json"
        now = self.clock()
        data = self._fallback.get(key)
        if data is None:
            cached = _read_json(cache)
            if cached and set(syms) <= set(cached.get("symbols", {})):
                data = self._fallback[key] = cached
        if data is not None:
            # the free plan serves SIP history up to 15 minutes ago: until then the day is unfinished
            if not self._complete(data) and now - float(data.get("ts", 0)) >= self.FRESH_S:
                self._start_fetch(key, day, syms, snap, cache)        # keep showing these meanwhile
            self.chart_source = "alpaca"
            return data
        if now >= self._retry_at.get(key, 0.0):
            self._start_fetch(key, day, syms, snap, cache)
            self.chart_source = "loading"
        else:
            self.chart_source = "none"
        return None

    @staticmethod
    def _complete(data: dict) -> bool:
        return float(data.get("ts", 0)) >= float(data.get("close") or 0) + 16 * 60

    def _start_fetch(self, key, day: str, syms: list[str], snap: dict, cache: Path) -> None:
        if key in self._fetching:
            return
        self._fetching.add(key)
        prev = {s["symbol"]: (s["price"] / (1 + s["change_pct"] / 100))
                for s in snap.get("symbols") or [] if s.get("price") and s.get("change_pct") is not None}
        threading.Thread(target=self._fetch, args=(key, day, syms, prev, cache), daemon=True).start()

    def _fetch(self, key, day: str, syms: list[str], prev: dict, cache: Path) -> None:
        try:
            import datetime as dt

            from dotenv import load_dotenv

            load_dotenv(self.root / ".env")
            from dashcount.data.alpaca_data import AlpacaData
            from dashcount.timeutil import calendar

            s = calendar().session(dt.date.fromisoformat(day))
            df = AlpacaData(per_minute=60).minutes(syms, s.open, s.close, "sip")
            out = {}
            for sym, g in df.sort_values("ts").groupby("symbol"):
                pv = v = 0.0
                rows, vw = [], []
                for r in g.itertuples(index=False):
                    rows.append([float(r.ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)])
                    pv += (r.high + r.low + r.close) / 3 * r.volume
                    v += r.volume
                    vw.append(round(pv / v, 4) if v else float(r.close))
                out[sym] = {"prev_close": prev.get(sym), "bars": rows, "vwap": vw}
            data = {"ts": self.clock(), "day": day, "open": s.open, "close": s.close, "symbols": out, "source": "alpaca"}
            self._fallback[key] = data
            try:
                cache.write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                pass
        except Exception as e:                      # no keys / offline: charts stay empty, retried later
            log.warning("chart bars unavailable: %s", e)
            self._retry_at[key] = self.clock() + self.FRESH_S
        finally:
            self._fetching.discard(key)

    def snapshot_age(self) -> float | None:
        return time.time() - self._snap_mtime if self._snap_mtime else None

    def heartbeat(self) -> dict | None:
        return _read_json(self.state / "heartbeat.json")

    def guardian(self) -> dict:
        return _read_json(self.state / "guardian.json") or {}

    def capital(self) -> dict:
        return _read_json(self.state / "capital.json") or {}

    def bot_state(self) -> tuple[str, str]:
        """(LIVE | WAITING | IDLE | OFFLINE, detail)."""
        hb = self.heartbeat()
        if not hb:
            return "OFFLINE", "bot has never run here"
        age = time.time() - float(hb.get("ts", 0))
        st = str(hb.get("status", ""))
        if age < 20 and st == "trading":
            return "LIVE", "trading"
        if age < 90 and st.startswith("waiting"):
            return "WAITING", st.split(":", 1)[-1]
        if st == "done" and age < 12 * 3600:
            return "IDLE", "day finished"
        return "IDLE", f"last seen {_ago(age)} ago"

    # ------------------------------------------------------------- journal
    def _journal_file(self) -> Path | None:
        snap = self.snapshot()
        if snap and snap.get("day"):
            p = self.state / f"journal-{snap['day']}.jsonl"
            if p.exists():
                return p
        files = sorted(self.state.glob("journal-*.jsonl"))
        return files[-1] if files else None

    def journal_new(self) -> list[dict]:
        """Entries appended since the last call (resets when the day changes)."""
        f = self._journal_file()
        if f is None:
            return []
        if f != self._jfile:
            self._jfile, self._joff = f, 0
        try:
            with f.open("rb") as fh:
                fh.seek(self._joff)
                chunk = fh.read()
        except OSError:
            return []
        # the bot may be half-way through writing the last line: leave it for next time
        end = chunk.rfind(b"\n") + 1
        self._joff += end
        out = []
        for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    # ------------------------------------------------------------- history
    def history(self) -> dict:
        """Multi-day live stats + latest practice run (slow: call every ~30 s)."""
        out: dict = {"days": [], "trades": [], "practice": None, "veto_score": {}}
        try:
            from dashcount.recorder import read_table

            live = self.data / "records" / "live-paper"
            d = read_table(live, "daily")
            if len(d):
                d = d.sort_values("day").drop_duplicates("day", keep="last")
                out["days"] = [{"day": str(r.day), "start": float(r.start_equity), "end": float(r.end_equity),
                                "trades": int(r.trades)} for r in d.itertuples()]
            t = read_table(live, "trades")
            if len(t):
                t = t[t["qty_filled"] > 0]
                out["trades"] = [{"day": str(r.day), "symbol": r.symbol, "pnl": float(r.pnl), "r": float(r.r),
                                  "exit": r.exit_reason, "closed_ts": float(r.closed_ts)} for r in t.itertuples()]
        except Exception as e:                                   # history is a nice-to-have
            out["error"] = str(e)
        out["veto_score"] = _read_json(self.state / "veto_score.json") or {}
        runs = sorted((p for p in (self.data / "records").glob("practice-*") if p.is_dir()),
                      key=lambda p: p.stat().st_mtime) if (self.data / "records").exists() else []
        real = [r for r in runs if "synthetic" not in r.name]
        if real or runs:
            run = (real or runs)[-1]
            # summarising a long run reads every table: only redo it when the run changes
            key = (run, max([p.stat().st_mtime for p in run.iterdir()] + [run.stat().st_mtime]))
            if key != self._practice_key:
                try:
                    from dashcount import scoreboard

                    s = scoreboard.summarize(run)
                    self._practice = {k: s.get(k) for k in ("run", "days", "trades", "win_rate", "avg_r", "pnl",
                                                            "pnl_realistic", "end_equity", "start_equity")}
                except Exception as e:
                    self._practice = {"run": run.name, "error": str(e)}
                self._practice_key = key
            out["practice"] = self._practice
        return out

    def practice_range(self, n: int = 15) -> tuple[str, str] | None:
        """The last n downloaded trading days (IEX ticks), for the PRACTICE dialog."""
        d = self.data / "store" / "ticks" / "feed=iex"
        days = sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []
        return (days[-n] if len(days) >= n else days[0], days[-1]) if days else None

    def latest_report(self) -> Path | None:
        rep = sorted((self.root / "reports").glob("*.html"), key=lambda p: p.stat().st_mtime)
        return rep[-1] if rep else None
