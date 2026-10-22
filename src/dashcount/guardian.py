"""Guardian: account-level kill switches.

The rules are pure and shared: practice runs evaluate them in-process against
the simulator; live, a separate `dashcount guardian` process evaluates them
against the Alpaca account and writes its state to disk, which the bot reads
before every entry. Nothing here depends on the Brain or the AI models.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class GuardianState:
    day: str = ""
    start_equity: float = 0.0
    peak_equity: float = 0.0
    halted_day: str = ""            # date string when halted for the day
    halt_reason: str = ""
    armed: bool = True
    disarm_reason: str = ""
    panic: bool = False
    losers_in_row: int = 0
    events: list = field(default_factory=list)

    # persistence (live) -------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "GuardianState":
        try:
            return cls(**json.loads(Path(path).read_text()))
        except Exception:
            return cls()

    def save(self, path: Path) -> None:
        from .state import atomic_write

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        d["events"] = d["events"][-50:]
        atomic_write(path, json.dumps(d, indent=1))

    def entry_block(self, today: str) -> str | None:
        if self.panic:
            return "panic"
        if not self.armed:
            return f"disarmed:{self.disarm_reason}"
        if self.halted_day == today:
            return f"halted:{self.halt_reason}"
        return None


@dataclass
class Action:
    kind: str        # flatten | halt | disarm | alert
    reason: str


class GuardianRules:
    def __init__(self, mode_cfg, state: GuardianState | None = None) -> None:
        self.m = mode_cfg
        self.s = state or GuardianState()

    def new_day(self, day: str, equity: float) -> None:
        if self.s.day != day:
            self.s.day = day
            self.s.start_equity = equity
            self.s.losers_in_row = 0
        self.s.peak_equity = max(self.s.peak_equity, equity)

    def on_trade_closed(self, pnl: float) -> list[Action]:
        if pnl < 0:
            self.s.losers_in_row += 1
        elif pnl > 0:
            self.s.losers_in_row = 0
        if self.s.losers_in_row >= self.m.losers_in_row and self.s.halted_day != self.s.day:
            return self._halt(f"{self.s.losers_in_row} losing trades in a row", flatten=False)
        return []

    def evaluate(self, now: float, equity: float, flatten_at: float, has_exposure: bool) -> list[Action]:
        s = self.s
        acts: list[Action] = []
        s.peak_equity = max(s.peak_equity, equity)
        if s.panic:
            return [Action("flatten", "panic")] if has_exposure else []
        if not s.armed:
            return [Action("flatten", "disarmed")] if has_exposure else []
        if s.peak_equity > 0 and (equity - s.peak_equity) / s.peak_equity * 100 <= -self.m.drawdown_pct:
            s.armed = False
            s.disarm_reason = f"drawdown {self.m.drawdown_pct}% from peak {s.peak_equity:,.0f}"
            acts += [Action("disarm", s.disarm_reason), Action("flatten", "drawdown fuse"),
                     Action("alert", f"FUSE BLOWN: {s.disarm_reason}. Bot is off until you run `dashcount arm`.")]
            self._log(acts)
            return acts
        if s.start_equity > 0 and s.halted_day != s.day:
            dd = (equity - s.start_equity) / s.start_equity * 100
            if dd <= -self.m.daily_loss_pct:
                acts += self._halt(f"daily loss {dd:.1f}%", flatten=True)
        if now >= flatten_at and has_exposure:
            acts.append(Action("flatten", "end of day"))
        return acts

    def _halt(self, reason: str, flatten: bool) -> list[Action]:
        self.s.halted_day = self.s.day
        self.s.halt_reason = reason
        acts = [Action("halt", reason)]
        if flatten:
            acts.append(Action("flatten", reason))
        acts.append(Action("alert", f"Trading halted for today: {reason}"))
        self._log(acts)
        return acts

    def _log(self, acts: list[Action]) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for a in acts:
            self.s.events.append({"at": stamp, "kind": a.kind, "reason": a.reason})


def round_trips(fills: list[dict]) -> list[float]:
    """Realized P&L of each flat-to-flat round trip from a list of fills
    ({symbol, side, qty, price, ts}) in time order. Used by the live Guardian
    to count losing trades independently of the bot."""
    out: list[float] = []
    book: dict[str, list[float]] = {}   # symbol -> [qty, avg, realized]
    for f in sorted(fills, key=lambda x: x["ts"]):
        sym = f["symbol"]
        dq = f["qty"] if f["side"] == "buy" else -f["qty"]
        q, avg, real = book.get(sym, [0.0, 0.0, 0.0])
        if q == 0 or (q > 0) == (dq > 0):
            nq = q + dq
            avg = (q * avg + dq * f["price"]) / nq
            q = nq
        else:
            closing = min(abs(dq), abs(q)) * (1 if q > 0 else -1)
            real += closing * (f["price"] - avg)
            q += dq
            if abs(q) < 1e-9:
                out.append(real)
                q, avg, real = 0.0, 0.0, 0.0
            elif (q > 0) != (closing > 0):     # flipped through zero
                out.append(real)
                avg, real = f["price"], 0.0
        book[sym] = [q, avg, real]
    return out


def wait(seconds: float) -> None:
    time.sleep(seconds)
