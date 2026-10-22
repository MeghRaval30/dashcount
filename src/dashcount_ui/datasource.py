"""Read-only access to everything the bot and the Guardian write to disk."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


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


class DataSource:
    def __init__(self, root: Path | None = None, data: Path | None = None) -> None:
        self.root = root or project_root()
        self.data = data or data_root(self.root)
        self.state = self.data / "state"
        self._snap = None
        self._snap_mtime = 0.0
        self._jfile: Path | None = None
        self._joff = 0

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
        if st == "done":
            return "IDLE", "day finished"
        return "IDLE", f"last seen {int(age // 60)} min ago"

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
            with f.open("r", encoding="utf-8") as fh:
                fh.seek(self._joff)
                chunk = fh.read()
                self._joff = fh.tell()
        except OSError:
            return []
        out = []
        for line in chunk.splitlines():
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
            try:
                from dashcount.scoreboard import summarize

                s = summarize((real or runs)[-1])
                out["practice"] = {k: s.get(k) for k in ("run", "days", "trades", "win_rate", "avg_r", "pnl",
                                                          "pnl_realistic", "end_equity", "start_equity")}
            except Exception as e:
                out["practice"] = {"run": (real or runs)[-1].name, "error": str(e)}
        return out

    def latest_report(self) -> Path | None:
        rep = sorted((self.root / "reports").glob("*.html"), key=lambda p: p.stat().st_mtime)
        return rep[-1] if rep else None
