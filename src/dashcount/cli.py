"""DashCount command line.

    uv run dashcount check                    # is everything ready?
    uv run dashcount demo --days 3            # synthetic practice run (no keys needed)
    uv run dashcount download --start 2024-07-01 --end 2024-09-30
    uv run dashcount practice --start 2024-07-01 --end 2024-09-30
    uv run dashcount plan                     # today's level map (needs keys)
    uv run dashcount run-day                  # Ollama + Guardian + bot for today (Task Scheduler)
    uv run dashcount live                     # just the bot
    uv run dashcount guardian                 # just the Guardian
    uv run dashcount scoreboard [RUN]         # report for a run (default: latest)
    uv run dashcount panic | arm
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .config import ROOT, load_config


def _setup_logging(name: str, verbose: bool = False) -> None:
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(logs / f"{name}-{dt.date.today().isoformat()}.log", encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, handlers=handlers, force=True)
    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "websockets", "alpaca", "asyncio", "filelock",
                  "huggingface_hub", "huggingface_hub.utils._http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _latest_run() -> Path | None:
    from .factory import records_root

    root = records_root()
    runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime) if root.exists() else []
    return runs[-1] if runs else None


# ---------------------------------------------------------------- commands
def cmd_check(args, cfg) -> int:
    ok = True

    def line(good: bool, what: str, detail: str = "", optional: bool = False) -> None:
        nonlocal ok
        if not optional:
            ok &= good
        mark = "OK" if good else ("--" if optional else "!!")
        print(f"  [{mark}] {what}" + (f" - {detail}" if detail else ""))

    print("DashCount readiness check")
    try:
        import torch

        cuda = torch.cuda.is_available()
        line(cuda, "GPU for the models", torch.cuda.get_device_name(0) if cuda else
             "no CUDA GPU: Kronos/Fin-R1 will be slow. Set the laptop GPU mode to Standard/Ultimate and restart.")
    except Exception as e:
        line(False, "PyTorch", str(e))
    if args.models:
        from .models import LONG, Bar, Candidate
        from .veto.kronos_veto import KronosVeto

        try:
            kv = KronosVeto(cfg.veto.kronos)
            bars = [Bar("X", 1e9 + 60 * i, 100 + i * 0.01, 100.2 + i * 0.01, 99.8 + i * 0.01, 100 + i * 0.01, 1000) for i in range(200)]
            r = kv.assess(Candidate("X", 1e9 + 12000, "orb", LONG, 102.0, 101.5, 103.0), bars)
            line(r.latency_s < cfg.veto.kronos.timeout_s, "Kronos", f"{r.verdict}, {r.latency_s:.1f}s per decision")
        except Exception as e:
            line(False, "Kronos", str(e))
        from .veto.finr1 import FinR1Veto

        fv = FinR1Veto(cfg.veto.finr1)
        if fv.available():
            fv.warm_up()
            r = fv.assess("TRADE: LONG via opening-range breakout. Entry 100.00, stop 99.50, target 101.00. "
                          "COUNT: true count +2.0 (stock flow +2.5, market flow +1.0). MEMORY: no similar past attempts yet.")
            line(r.verdict in ("allow", "veto") and r.latency_s < cfg.veto.finr1.timeout_s, "Fin-R1 via Ollama",
                 f"{r.verdict} ({r.reason_code}), {r.latency_s:.1f}s")
        else:
            line(False, "Fin-R1 via Ollama", f"model {cfg.veto.finr1.model} not found at {cfg.veto.finr1.ollama_url}. "
                 "Start Ollama, then: ollama pull " + cfg.veto.finr1.model)
    have_keys = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
    line(have_keys, "Alpaca keys in .env", "" if have_keys else "copy .env.example to .env and paste your PAPER keys")
    if have_keys:
        try:
            from .data.alpaca_trading import g, is_paper, trading_client

            a = trading_client().get_account()
            eq = float(g(a, "equity"))
            kind = "paper" if is_paper() else "REAL"
            short = "on" if g(a, "shorting_enabled") else "OFF"
            line(True, f"Alpaca {kind} account", f"equity ${eq:,.2f}, shorting {short}")
            line(eq >= 25000, "Pattern-day-trader room", "" if eq >= 25000 else "account under $25k: day trades may be limited")
            cap = cfg.account.capital
            line(True, "Trading budget", f"${cap:,.0f} of the account" if cap else "whole account")
        except Exception as e:
            line(False, "Alpaca trading API", str(e)[:200])
        try:
            from .data.alpaca_data import AlpacaData
            from .timeutil import calendar

            ad = AlpacaData()
            d = calendar().previous_sessions(dt.date.today(), 2)[0]
            s = calendar().session(d)
            tr = ad.trades(["SPY"], s.open, s.open + 60, "sip")
            line(bool(tr.get("SPY") is not None and len(tr["SPY"])), "Historical SIP trades (free plan)", f"{len(tr.get('SPY', []))} SPY prints in one minute")
            q = ad.quotes(["SPY"], s.open, s.open + 60, "iex")
            line(bool(len(q.get("SPY", []))), "Historical IEX quotes", f"{len(q.get('SPY', []))} quote changes")
        except Exception as e:
            line(False, "Alpaca market data", str(e)[:200])
    from .earnings import EarningsCalendar
    from .data import Store

    e = EarningsCalendar(Store().earnings_dir()).symbols_on(dt.date.today() - dt.timedelta(days=7))
    line(e is not None, "Earnings calendar (Nasdaq)", f"{len(e)} reporters a week ago" if e is not None else "unreachable")
    line(bool(os.environ.get("NTFY_TOPIC")), "Phone alerts (ntfy)",
         "on" if os.environ.get("NTFY_TOPIC") else "optional: set NTFY_TOPIC in .env", optional=True)
    print("READY" if ok else "Some checks need attention (see !! lines).")
    return 0 if ok else 1


def cmd_demo(args, cfg) -> int:
    from .practice import run_practice
    from .scoreboard import console, write_report

    start = dt.date(2025, 3, 10)
    from .timeutil import calendar

    days = calendar().sessions(start, start + dt.timedelta(days=args.days * 2 + 5))[: args.days]
    run = run_practice(cfg, days[0], days[-1], feed="sip", vetoes=args.vetoes, label="demo", synthetic=True,
                       n_symbols=args.symbols, seed=args.seed)
    path, s = write_report(run, ROOT / "reports")
    print(console(s))
    print(f"report: {path}")
    return 0


def cmd_download(args, cfg) -> int:
    from .download import download

    from .data.alpaca_data import AlpacaData

    feeds = tuple(args.feed.split(","))
    download(cfg, args.start, args.end, feeds=feeds, ticks=not args.no_ticks, data=AlpacaData(per_minute=args.rate))
    return 0


def cmd_practice(args, cfg) -> int:
    from .practice import run_practice
    from .scoreboard import console, write_report

    run = run_practice(cfg, args.start, args.end, feed=args.feed, vetoes=args.vetoes, label=args.label)
    path, s = write_report(run, ROOT / "reports")
    print(console(s))
    print(f"report: {path}")
    return 0


def cmd_plan(args, cfg) -> int:
    from .data import Store
    from .data.alpaca_data import AlpacaData
    from .earnings import EarningsCalendar
    from .levelmap import LevelMap
    from .timeutil import ET

    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(ET).date()
    plan = LevelMap(cfg, AlpacaData(), EarningsCalendar(Store().earnings_dir())).build(day, "iex")
    print(f"Level map for {day} ({'half day' if plan.session.half_day else 'full day'})")
    print(f"  flags: {', '.join(plan.flags) or 'none'}; earnings known: {plan.earnings_known}; removed: {', '.join(plan.removed_earnings) or 'none'}")
    for w in plan.watchlist:
        print(f"  {w.symbol:6s} {w.sector[:22]:22s} score {w.score:+.2f} gap {w.gap_pct:+.2f}% "
              f"ATR {w.atr_daily:.2f} share {w.feed_share:.3f} ETB {'y' if w.easy_to_borrow else 'n'} profile {'y' if w.volume_profile else 'n'}")
    return 0


def cmd_live(args, cfg) -> int:
    from .live import run_live

    run_live(cfg, real_money=args.real_money, vetoes=args.vetoes,
             day=dt.date.fromisoformat(args.date) if args.date else None)
    return 0


def cmd_guardian(args, cfg) -> int:
    from .guardian_live import run_guardian

    run_guardian(cfg, once=args.once)
    return 0


def cmd_run_day(args, cfg) -> int:
    """Start everything for today: Ollama (if needed), the Guardian, then the bot."""
    log = logging.getLogger("run-day")
    if args.vetoes in ("full", "finr1"):
        from .factory import ensure_ollama

        if not ensure_ollama(cfg.veto.finr1.ollama_url):
            log.warning("Ollama did not start: Fin-R1 will answer 'unavailable' and trades will be skipped")
    gargs = [sys.executable, "-m", "dashcount.cli"]
    if args.config:
        gargs += ["--config", args.config]
    if args.mode:
        gargs += ["--mode", args.mode]
    gargs.append("guardian")
    guardian = subprocess.Popen(gargs, cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log.info("guardian pid %s", guardian.pid)
    try:
        return cmd_live(args, cfg)
    finally:
        time.sleep(60 * 10)            # keep the Guardian watching for a while after the bot ends
        guardian.terminate()


def cmd_scoreboard(args, cfg) -> int:
    from .factory import records_root
    from .scoreboard import console, write_report

    run = Path(args.run) if args.run and Path(args.run).exists() else (records_root() / args.run if args.run else _latest_run())
    if run is None or not run.exists():
        print("no runs recorded yet")
        return 1
    path, s = write_report(run, ROOT / "reports")
    print(console(s))
    print(f"report: {path}")
    return 0


def cmd_panic(args, cfg) -> int:
    from .guardian_live import panic

    panic(cfg)
    return 0


def cmd_arm(args, cfg) -> int:
    from .guardian_live import arm

    arm()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    p = argparse.ArgumentParser(prog="dashcount", description="Geometry Dash x card counting stock bot")
    p.add_argument("--config", help="override YAML merged over config/default.yaml")
    p.add_argument("--mode", choices=["tournament", "real"], help="risk dial (overrides config)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="readiness check")
    c.add_argument("--no-models", dest="models", action="store_false", help="skip the (slow) model checks")
    c.set_defaults(fn=cmd_check)

    c = sub.add_parser("demo", help="practice run on synthetic days (no keys needed)")
    c.add_argument("--days", type=int, default=3)
    c.add_argument("--symbols", type=int, default=28)
    c.add_argument("--seed", type=int, default=0)
    c.add_argument("--vetoes", choices=["full", "kronos", "finr1", "none"], default="full")
    c.set_defaults(fn=cmd_demo)

    c = sub.add_parser("download", help="download history for practice runs")
    c.add_argument("--start", required=True)
    c.add_argument("--end", required=True)
    c.add_argument("--feed", default="iex", help="tick feed(s): iex, sip or iex,sip")
    c.add_argument("--no-ticks", action="store_true", help="only daily/minute bars and earnings")
    c.add_argument("--rate", type=int, default=190, help="max Alpaca requests per minute (use ~100 while trading live)")
    c.set_defaults(fn=cmd_download)

    c = sub.add_parser("practice", help="replay downloaded history through the bot")
    c.add_argument("--start", required=True)
    c.add_argument("--end", required=True)
    c.add_argument("--feed", default="iex", choices=["iex", "sip"])
    c.add_argument("--vetoes", choices=["full", "kronos", "finr1", "none"], default="full")
    c.add_argument("--label", default="")
    c.set_defaults(fn=cmd_practice)

    c = sub.add_parser("plan", help="show a day's level map (needs keys)")
    c.add_argument("--date")
    c.set_defaults(fn=cmd_plan)

    for name, fn in (("live", cmd_live), ("run-day", cmd_run_day)):
        c = sub.add_parser(name, help="trade today (paper unless ALPACA_PAPER=false and --real-money)")
        c.add_argument("--real-money", action="store_true")
        c.add_argument("--vetoes", choices=["full", "kronos", "finr1", "none"], default="full")
        c.add_argument("--date", help=argparse.SUPPRESS)
        c.set_defaults(fn=fn)

    c = sub.add_parser("guardian", help="run the Guardian kill-switch process")
    c.add_argument("--once", action="store_true")
    c.set_defaults(fn=cmd_guardian)

    c = sub.add_parser("scoreboard", help="report for a run (default: latest)")
    c.add_argument("run", nargs="?")
    c.set_defaults(fn=cmd_scoreboard)

    sub.add_parser("panic", help="close everything now and block trading").set_defaults(fn=cmd_panic)
    sub.add_parser("arm", help="re-arm after the drawdown fuse or panic").set_defaults(fn=cmd_arm)

    args = p.parse_args(argv)
    _setup_logging(args.cmd, args.verbose)
    overrides = {"mode": args.mode} if args.mode else None
    cfg = load_config(args.config, overrides)
    from .data.alpaca_data import MissingKeys

    try:
        return int(args.fn(args, cfg) or 0)
    except MissingKeys as e:
        print()
        print(e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
