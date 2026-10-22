"""Small JSON state files shared between the bot and the Guardian (live)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import data_dir


def state_dir() -> Path:
    d = data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def guardian_path() -> Path:
    return state_dir() / "guardian.json"


def heartbeat_path() -> Path:
    return state_dir() / "heartbeat.json"


def bot_positions_path() -> Path:
    return state_dir() / "bot_positions.json"


def veto_score_path() -> Path:
    return state_dir() / "veto_score.json"


def capital_path() -> Path:
    return state_dir() / "capital.json"


class Capital:
    """The bot's money budget inside a bigger account.

    budget equity = capital + (account equity - account equity when the budget started).
    With capital None the account is used as-is."""

    def __init__(self, capital: float | None, path: Path | None = None) -> None:
        self.capital = capital
        self.path = path
        self.baseline: float | None = None
        if capital and path is not None:
            st = read_json(path, {}) or {}
            if st.get("capital") == capital and st.get("baseline") is not None:
                self.baseline = float(st["baseline"])

    def view(self, acct):
        from .models import Account

        if not self.capital:
            return acct
        if self.baseline is None:
            self.baseline = acct.equity
            if self.path is not None:
                write_json(self.path, {"capital": self.capital, "baseline": self.baseline, "since": time.time()})
        eq = self.capital + (acct.equity - self.baseline)
        last = self.capital + (acct.last_equity - self.baseline) if acct.last_equity else eq
        return Account(equity=eq, buying_power=max(min(acct.buying_power, 4 * eq), 0.0), last_equity=last)


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def beat(extra: dict | None = None) -> None:
    write_json(heartbeat_path(), {"ts": time.time(), **(extra or {})})


def heartbeat_age() -> float | None:
    hb = read_json(heartbeat_path())
    return time.time() - hb["ts"] if hb and "ts" in hb else None
