"""Alerts centre: rules over the DESK's inputs. A condition alert fires once and
re-arms when its condition clears; an event alert fires once per day. Every alert
is saved to data/state/alerts-<day>.jsonl so a restart doesn't repeat or lose it."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from .reconcile import BlotterRow
from .risk import Meter

log = logging.getLogger(__name__)

SILENT_S = 60.0


@dataclass(frozen=True)
class Alert:
    ts: float
    id: str
    level: str
    text: str
    key: str


@dataclass
class AlertInputs:
    in_session: bool
    meters: list[Meter]
    losers_in_row: int
    streak_limit: int
    heartbeat_age: float | None
    held_stale: list[str]
    rejected: list[BlotterRow]
    alpaca_errors_10min: int
    alpaca_ok: bool
    mismatches: list[str]
    ollama_up: bool | None
    guardian: dict
    day: str


class AlertEngine:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.day: str | None = None
        self._seen: set[str] = set()       # event keys already raised today
        self._active: set[str] = set()     # condition keys currently raised

    # --------------------------------------------------------------- storage
    def _path(self) -> Path:
        return self.folder / f"alerts-{self.day}.jsonl"

    def start_day(self, day: str) -> list[Alert]:
        self.day, self._seen, self._active = day, set(), set()
        alerts: list[Alert] = []
        try:
            lines = self._path().read_text(encoding="utf-8").splitlines() if self._path().exists() else []
        except OSError:
            lines = []
        for line in lines:
            try:
                d = json.loads(line)
                alerts.append(Alert(float(d["ts"]), d["id"], d["level"], d["text"], d["key"]))
            except Exception:
                continue
        for a in alerts:
            self._seen.add(a.key)
            self._active.add(a.key)
        return sorted(alerts, key=lambda a: -a.ts)

    def _emit(self, out: list[Alert], ts: float, id_: str, level: str, text: str, key: str | None = None) -> None:
        a = Alert(ts, id_, level, text, key or id_)
        out.append(a)
        if self.day is None:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self._path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(a)) + "\n")
        except OSError as e:
            log.warning("could not save alert: %s", e)

    # ---------------------------------------------------------------- events
    def from_journal(self, entries: list[dict], now: float) -> list[Alert]:
        out: list[Alert] = []
        for e in entries:
            kind, sym = e.get("kind"), e.get("symbol") or ""
            if kind == "taken":
                key = f"trade_open:{e.get('trade_id') or e.get('ts')}"
                side = "LONG" if (e.get("direction") or 1) > 0 else "SHORT"
                text = (f"{sym} {side} opened: {e.get('qty')} sh at {float(e['entry']):.2f}" if e.get("entry") is not None
                        else f"{sym} opened: {e.get('text', '')}")
                id_ = "trade_open"
            elif kind == "exit":
                key = f"trade_close:{e.get('ts')}:{sym}"
                text = f"{sym} closed: {float(e.get('pnl') or 0):+.2f} ({float(e.get('r') or 0):+.2f}R)"
                id_ = "trade_close"
            else:
                continue
            if key in self._seen:
                continue
            self._seen.add(key)
            self._emit(out, float(e.get("ts") or now), id_, "info", text, key)
        return out

    # ------------------------------------------------------------ conditions
    def evaluate(self, now: float, x: AlertInputs) -> list[Alert]:
        conds: list[tuple[str, str, str, str]] = []          # (key, id, level, text) true right now
        meters = {m.key: m for m in x.meters if m.level != "stale"}
        for key, name, what in (("day_loss", "day_loss", "Day loss at {p}% of the limit ({t})"),
                                ("fuse", "fuse", "Drawdown at {p}% of the fuse ({t})")):
            m = meters.get(key)
            if m is None:
                continue
            if m.frac >= 0.5:
                conds.append((f"{name}_50", f"{name}_50", "warn", what.format(p=50, t=m.text)))
            if m.frac >= 0.75:
                conds.append((f"{name}_75", f"{name}_75", "danger", what.format(p=75, t=m.text)))
        if x.streak_limit > 1 and x.losers_in_row == x.streak_limit - 1:
            conds.append(("streak_3", "streak_3", "warn",
                          f"{x.losers_in_row} losing trades in a row: one more halts the day"))
        if x.in_session and (x.heartbeat_age is None or x.heartbeat_age > SILENT_S):
            conds.append(("bot_silent", "bot_silent", "danger",
                          "The bot is not running" if x.heartbeat_age is None
                          else f"The bot has been silent for {x.heartbeat_age:.0f} s"))
        for sym in x.held_stale:
            conds.append((f"data_stale_held:{sym}", "data_stale_held", "danger",
                          f"No data for {sym} (held) for over 3 minutes"))
        if x.alpaca_errors_10min >= 3 and not x.alpaca_ok:
            conds.append(("alpaca_errors", "alpaca_errors", "warn", f"Alpaca errors: {x.alpaca_errors_10min} in 10 minutes"))
        for msg in x.mismatches:
            conds.append((f"mismatch:{msg}", "mismatch", "danger", msg))
        if x.in_session and x.ollama_up is False:
            conds.append(("ollama_down", "ollama_down", "warn", "Ollama is down: Fin-R1 vetoes will be skipped"))
        g = x.guardian or {}
        if g.get("halted_day") == x.day:
            conds.append(("guardian_stop:halt", "guardian_stop", "danger",
                          f"Guardian: halted for the day ({g.get('halt_reason', '')})"))
        if g.get("panic"):
            conds.append(("guardian_stop:panic", "guardian_stop", "danger", "Guardian: PANIC"))
        if g.get("armed") is False:
            conds.append(("guardian_stop:fuse", "guardian_stop", "danger",
                          f"Guardian: fuse blown ({g.get('disarm_reason', '')})"))

        out: list[Alert] = []
        for key, id_, level, text in conds:
            if key not in self._active:
                self._active.add(key)
                self._emit(out, now, id_, level, text, key)
        self._active &= {c[0] for c in conds}                   # cleared conditions re-arm
        for r in x.rejected:
            key = f"order_rejected:{r.order_id}"
            if key not in self._seen:
                self._seen.add(key)
                self._emit(out, now, "order_rejected", "danger", f"Order rejected: {r.symbol} {r.side} {r.qty:g}", key)
        return out
