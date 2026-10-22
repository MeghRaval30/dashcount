"""The live Guardian process: watches the Alpaca account every few seconds and
pulls the kill switches on its own. Run it next to the bot:

    uv run dashcount guardian
"""

from __future__ import annotations

import datetime as dt
import logging
import time

import httpx

from . import notify
from .config import Config
from .data.alpaca_data import keys_from_env
from .data.alpaca_trading import g, is_paper, trading_client
from .guardian import Action, GuardianRules, GuardianState, round_trips
from .models import Account
from .state import Capital, bot_positions_path, capital_path, guardian_path, heartbeat_age, read_json
from .timeutil import ET, calendar

log = logging.getLogger(__name__)


def fills_today(paper: bool, day: dt.date) -> list[dict]:
    k, s = keys_from_env()
    base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
    r = httpx.get(f"{base}/v2/account/activities/FILL", params={"date": day.isoformat(), "direction": "asc"},
                  headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}, timeout=20)
    r.raise_for_status()
    out = []
    for a in r.json():
        side = "buy" if a.get("side") == "buy" else "sell"
        ts = dt.datetime.fromisoformat(a["transaction_time"].replace("Z", "+00:00")).timestamp()
        out.append({"symbol": a["symbol"], "side": side, "qty": float(a["qty"]), "price": float(a["price"]), "ts": ts})
    return out


def trailing_losers(pnls: list[float]) -> int:
    n = 0
    for p in reversed(pnls):
        if p < 0:
            n += 1
        elif p > 0:
            break
    return n


def run_guardian(cfg: Config, once: bool = False) -> None:
    paper = is_paper()
    tc = trading_client(paper)
    cal = calendar()
    path = guardian_path()
    unknown_since: dict[str, float] = {}
    capital = Capital(cfg.account.capital, capital_path())
    alerted_not_flat = False
    log.info("Guardian on (%s account, %s mode)", "paper" if paper else "REAL", cfg.mode)
    while True:
        now = time.time()
        today = dt.datetime.now(ET).date()
        st = GuardianState.load(path)
        rules = GuardianRules(cfg.guardian_mode, st)
        try:
            if not cal.is_session(today):
                if once:
                    return
                time.sleep(300)
                continue
            s = cal.session(today)
            flatten_at = s.at(cfg.level_map.half_day_flatten_at if s.half_day else cfg.level_map.flatten_at)
            if now < s.open - 15 * 60 or now > s.close + 10 * 60:
                if once:
                    return
                time.sleep(30)
                continue
            acct = tc.get_account()
            view = capital.view(Account(float(g(acct, "equity")), float(g(acct, "buying_power") or 0),
                                        float(g(acct, "last_equity") or 0)))
            equity = view.equity
            rules.new_day(today.isoformat(), view.last_equity or equity)
            positions = {g(p, "symbol"): p for p in tc.get_all_positions()}
            exposure = bool(positions)
            actions = rules.evaluate(now, equity, flatten_at, exposure)

            # losing trades in a row, from the broker's own fills
            try:
                pnls = round_trips(fills_today(paper, today))
                st.losers_in_row = trailing_losers(pnls)
                if st.losers_in_row >= cfg.guardian_mode.losers_in_row and st.halted_day != st.day:
                    actions += rules._halt(f"{st.losers_in_row} losing trades in a row", flatten=False)
            except Exception as e:
                log.warning("fills unavailable: %s", e)

            # bot heartbeat
            age = heartbeat_age()
            if exposure and (age is None or age > cfg.guardian.heartbeat_timeout_s):
                actions.append(Action("flatten", f"bot silent for {age or -1:.0f}s"))
                actions.append(Action("alert", "Bot heartbeat lost with open positions: flattening."))

            # positions the bot doesn't know about
            known = set((read_json(bot_positions_path(), {}) or {}).get("symbols", []))
            for sym in positions:
                if sym in known:
                    unknown_since.pop(sym, None)
                    continue
                first = unknown_since.setdefault(sym, now)
                if now - first > cfg.guardian.unknown_position_grace_s:
                    log.warning("unknown position %s: closing", sym)
                    try:
                        tc.close_position(sym)
                    except Exception as e:
                        log.warning("close %s failed: %s", sym, e)
                    notify.send(f"Guardian closed a position the bot didn't know about: {sym}")
                    unknown_since.pop(sym, None)

            flatten_reasons = [a.reason for a in actions if a.kind == "flatten"]
            if flatten_reasons:
                log.warning("FLATTEN: %s", "; ".join(flatten_reasons))
                tc.close_all_positions(cancel_orders=True)
            for a in actions:
                if a.kind == "alert":
                    notify.send(a.reason, priority="high")
            if now >= flatten_at + 180 and exposure and not alerted_not_flat:
                alerted_not_flat = True
                notify.send("NOT FLAT 3 minutes after flatten time! Check the account now.", priority="urgent")
            st.save(path)
        except Exception as e:
            log.exception("guardian loop error: %s", e)
        if once:
            return
        time.sleep(cfg.guardian.poll_s)


def panic(cfg: Config) -> None:
    """Close everything now and keep the bot from trading until re-armed."""
    st = GuardianState.load(guardian_path())
    st.panic = True
    st.save(guardian_path())
    try:
        trading_client().close_all_positions(cancel_orders=True)
        print("PANIC: all orders cancelled and positions closed. Bot blocked until `dashcount arm`.")
    except Exception as e:
        print(f"PANIC flag set, but closing positions failed: {e}. Close them in the Alpaca dashboard NOW.")
    notify.send("PANIC button pressed: everything closed.", priority="urgent")


def arm() -> None:
    st = GuardianState.load(guardian_path())
    st.panic = False
    st.armed = True
    st.disarm_reason = ""
    st.halted_day = ""
    st.save(guardian_path())
    print("Re-armed: the bot may trade again (peak equity kept for the drawdown fuse).")
