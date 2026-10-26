"""System health for the DESK: one lamp per part of the system (pure rows + an Ollama check)."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .broker_feed import FeedState

QUIET_S = 180.0                  # the bot's IEX "stale" hazard


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
    return level, f"{age:.0f} s ago"


def health_rows(*, in_session: bool, heartbeat_age: float | None, guardian_age: float | None, snap: dict,
                held: set[str], feed: FeedState | None, ollama: tuple[bool, bool] | None,
                timeouts: dict[str, float], now: float | None = None) -> list[HealthRow]:
    now = time.time() if now is None else now
    rows: list[HealthRow] = []

    def add(key, label, level, text, session_bound=True):
        if session_bound and not in_session and level in ("warn", "danger"):
            level = "idle"                         # outside market hours nothing is expected to run
        rows.append(HealthRow(key, label, level, text))

    add("bot", "BOT", *_age_level(heartbeat_age, 20, 90))
    add("guardian", "GUARDIAN", *_age_level(guardian_age, 20, 60))

    stocks = [s for s in snap.get("symbols") or [] if not s.get("market")]
    quiet = sorted((s for s in stocks if (s.get("last_trade_age") or 0) > QUIET_S),
                   key=lambda s: -(s.get("last_trade_age") or 0))
    if not stocks:
        add("data", "MARKET DATA", "idle", "no watchlist yet")
    elif not quiet:
        add("data", "MARKET DATA", "ok", f"all {len(stocks)} stocks trading")
    else:
        worst = quiet[0]
        level = "danger" if len(quiet) > 3 or any(s["symbol"] in held for s in quiet) else "warn"
        add("data", "MARKET DATA", level, f"{len(quiet)} quiet: {worst['symbol']} {worst['last_trade_age']:.0f} s")

    if feed is None:
        add("alpaca", "ALPACA", "warn", "keys not set (.env)", session_bound=False)
    elif not feed.ok and feed.last_ok_ts is None and feed.errors_10min == 0:
        add("alpaca", "ALPACA", "idle", "connecting...", session_bound=False)
    else:
        silent = feed.last_ok_ts is None or now - feed.last_ok_ts > 60
        if feed.errors_10min >= 3 or (not feed.ok and silent):
            add("alpaca", "ALPACA", "danger", f"{feed.errors_10min} errors in 10 min: {feed.last_error}",
                session_bound=False)
        elif feed.errors_10min or not feed.ok or (feed.last_latency_ms or 0) >= 1500:
            add("alpaca", "ALPACA", "warn", f"{feed.last_latency_ms or 0:.0f} ms, {feed.errors_10min} errors in 10 min",
                session_bound=False)
        else:
            add("alpaca", "ALPACA", "ok", f"{feed.last_latency_ms:.0f} ms", session_bound=False)

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
