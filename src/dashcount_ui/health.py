"""System health for the DESK: one lamp per part of the system (pure rows + an Ollama check)."""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import httpx

from .broker_feed import FeedState
from .datasource import ago

ET = ZoneInfo("America/New_York")
QUIET_S = 180.0                  # the bot's IEX "stale" hazard
FRESH_SNAPSHOT_S = 90.0          # an older snapshot no longer says what the market is doing


@dataclass(frozen=True)
class SessionState:
    in_session: bool = False         # open - 30 min .. close + 30 min: market data, Alpaca, AI, Ollama matter
    bot_expected: bool = False       # open - 30 min .. close + 3 min, unless the bot said it is done
    guardian_expected: bool = False  # open - 15 min .. close + 10 min (the Guardian's own working window)
    bot_done: bool = False           # heartbeat "done" today: the day is finished (or STOP was pressed)


def session_state(now: float, session: tuple[float, float] | None, heartbeat: dict | None) -> SessionState:
    if not session:
        return SessionState()
    o, c = session
    done = (bool(heartbeat) and str(heartbeat.get("status", "")).startswith("done")
            and float(heartbeat.get("ts") or 0) >= o - 3600)
    return SessionState(in_session=o - 1800 <= now <= c + 1800,
                        bot_expected=(o - 1800 <= now <= c + 180) and not done,
                        guardian_expected=o - 900 <= now <= c + 600,
                        bot_done=done)


def today_session(now: float) -> tuple[float, float] | None:
    """Today's regular session from the exchange calendar (None on weekends and holidays):
    known even when the bot never started, unlike the bot's own snapshot."""
    from dashcount.timeutil import calendar

    day = dt.datetime.fromtimestamp(now, ET).date()
    cal = calendar()
    if not cal.is_session(day):
        return None
    s = cal.session(day)
    return float(s.open), float(s.close)


@dataclass(frozen=True)
class HealthRow:
    key: str
    label: str
    level: str          # ok | warn | danger | idle
    text: str


class OllamaCheck:
    def __init__(self, url: str, model: str, client: httpx.Client | None = None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.client = client or httpx.Client(timeout=3.0)

    def check(self) -> tuple[bool, bool]:
        try:
            r = self.client.get(f"{self.url}/api/tags")
            if r.status_code != 200:
                return False, False
            names = {m.get("name") for m in (r.json().get("models") or [])}
            return True, self.model in names
        except Exception:
            return False, False


def _age_level(age: float | None, warn_at: float, danger_at: float) -> tuple[str, str]:
    if age is None:
        return "danger", "not running"
    level = "ok" if age < warn_at else ("warn" if age < danger_at else "danger")
    return level, (f"{age:.0f} s ago" if age < 120 else f"{ago(age)} ago")


def health_rows(*, in_session: bool, heartbeat_age: float | None, guardian_age: float | None, snap: dict,
                held: set[str], feed: FeedState | None, ollama: tuple[bool, bool] | None,
                timeouts: dict[str, float], now: float | None = None, bot_expected: bool | None = None,
                guardian_expected: bool | None = None, bot_done: bool = False,
                snapshot_age: float | None = 0.0) -> list[HealthRow]:
    now = time.time() if now is None else now
    rows: list[HealthRow] = []

    def add(key, label, level, text, expected=None):
        expected = in_session if expected is None else expected
        if not expected and level in ("warn", "danger"):
            level = "idle"                         # nothing is expected to run right now
        rows.append(HealthRow(key, label, level, text))

    if bot_done:
        add("bot", "BOT", "idle", "day finished")
    else:
        add("bot", "BOT", *_age_level(heartbeat_age, 20, 90), expected=bot_expected)
    add("guardian", "GUARDIAN", *_age_level(guardian_age, 20, 60), expected=guardian_expected)

    stocks = [s for s in snap.get("symbols") or [] if not s.get("market")]
    if not stocks:
        add("data", "MARKET DATA", "idle", "no watchlist yet")
    elif not in_session:
        add("data", "MARKET DATA", "idle", "market closed")
    elif snapshot_age is None or snapshot_age > FRESH_SNAPSHOT_S:
        add("data", "MARKET DATA", "idle", "no fresh data from the bot")
    else:
        # ages are as of the snapshot: add how old the snapshot itself is
        age = {s["symbol"]: (s.get("last_trade_age") or 0) + snapshot_age for s in stocks}
        quiet = sorted((sym for sym, a in age.items() if a > QUIET_S), key=lambda sym: -age[sym])
        if not quiet:
            add("data", "MARKET DATA", "ok", f"all {len(stocks)} stocks trading")
        else:
            level = "danger" if len(quiet) > 3 or any(sym in held for sym in quiet) else "warn"
            add("data", "MARKET DATA", level, f"{len(quiet)} quiet: {quiet[0]} {age[quiet[0]]:.0f} s")

    if feed is None:
        add("alpaca", "ALPACA", "warn", "keys not set (.env)", expected=True)
    elif not feed.ok and feed.last_ok_ts is None and feed.errors_10min == 0:
        add("alpaca", "ALPACA", "idle", "connecting...", expected=True)
    else:
        silent = feed.last_ok_ts is None or now - feed.last_ok_ts > 60
        if feed.errors_10min >= 3 or (not feed.ok and silent):
            add("alpaca", "ALPACA", "danger", f"{feed.errors_10min} errors in 10 min: {feed.last_error}",
                expected=True)
        elif feed.errors_10min or not feed.ok or (feed.last_latency_ms or 0) >= 1500:
            add("alpaca", "ALPACA", "warn", f"{feed.last_latency_ms or 0:.0f} ms, {feed.errors_10min} errors in 10 min",
                expected=True)
        else:
            add("alpaca", "ALPACA", "ok", f"{feed.last_latency_ms:.0f} ms", expected=True)

    if ollama is None:
        add("ollama", "OLLAMA", "idle", "checking...")
    else:
        up, listed = ollama
        add("ollama", "OLLAMA", *(("ok", "up, Fin-R1 ready") if up and listed else
                                  ("warn", "up, Fin-R1 model missing") if up else ("danger", "down")))

    vh = snap.get("veto_health") or {}
    for key, label in (("kronos", "KRONOS"), ("finr1", "FIN-R1")):
        h = vh.get(key)
        if not h:
            rows.append(HealthRow(key, label, "idle", "not reported"))
            continue
        problems = int(h.get("errors") or 0) + int(h.get("timeouts") or 0)
        slow = float(h.get("last_s") or 0) >= 0.8 * float(timeouts.get(key) or 1e9)
        level = "danger" if problems >= 3 else ("warn" if problems or slow else "ok")
        text = f"{h.get('last_s', 0):.1f} s (avg {h.get('avg_s', 0):.1f}, {h.get('calls', 0)} calls)"
        if problems:
            text += f", {h.get('errors', 0)} errors, {h.get('timeouts', 0)} timeouts"
        add(key, label, level, text)
        if not in_session:
            rows[-1] = HealthRow(key, label, "idle", text)
    return rows
