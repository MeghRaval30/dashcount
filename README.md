# DashCount 🎮🃏

**Geometry Dash × card counting**: a fully algorithmic, intraday US-stock bot.

It **skips obstacles** (hazards, earnings, scheduled releases), **counts aggressive order flow** like a card counter, **bets big only when the count is extreme**, lets **two local AI models veto** trades, and **records every attempt**, including the ones it skipped, as a training dataset.

> ⚠️ This is a competition and research tool, not financial advice. Nothing here guarantees profit. Run it on **paper money**, and judge it only on practice runs over **real historical data**. The synthetic demo only proves the machinery works.

---

## What you need to do (once)

1. **Create a free Alpaca paper account.** Go to <https://app.alpaca.markets>, switch to **Paper Trading**, open **API Keys**, then **Generate New Keys**.
2. **Put the keys in `.env`:**
   ```
   copy .env.example .env
   ```
   Then open `.env` and paste `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. Keep `ALPACA_PAPER=true`.
3. **Check everything:**
   ```
   uv run dashcount check
   ```
   Every line should say `[OK]`. `--` lines are optional.
4. **Optional phone alerts:** install the **ntfy** app, subscribe to a long random topic name, and put it in `.env` as `NTFY_TOPIC=...`.

## Daily use

| What | Command |
|---|---|
| See it work right now, no keys needed (fake market) | `uv run dashcount demo --days 3` |
| Download history for practice (resumable) | `uv run dashcount download --start 2026-08-01 --end 2026-09-30` |
| Practice on that history | `uv run dashcount practice --start 2026-08-01 --end 2026-09-30` |
| Today's level map (14-stock watchlist + blocked windows) | `uv run dashcount plan` |
| Trade today on paper (Ollama + Guardian + bot) | `uv run dashcount run-day` |
| Automatic every weekday at 18:25 IST | `powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1` |
| Latest report | `uv run dashcount scoreboard` |
| **PANIC**: close everything now | `uv run dashcount panic` |
| Re-arm after panic / drawdown fuse | `uv run dashcount arm` |

Reports open in any browser from `reports/`. The US market runs **19:00–01:30 IST** until 1 November, then **20:00–02:30 IST**. On trading nights, keep the laptop plugged in with the lid open, and keep the GPU in Standard/Ultimate mode.

Useful flags:
- `--mode real` uses smaller bets.
- `--vetoes kronos|finr1|none` switches the AI vetoes.
- `--feed sip` makes practice use full-market data (download it with `--feed iex,sip`).

## How it works (the level)

```
 Level map (09:20 ET) ─► 14 in-play stocks + SPY/QQQ, earnings removed, blocked windows
        │
 Market data ─► Features ─► Jump pads ─► Hazards ─► Count gates ─► Bet spread ─► Kronos ─► Fin-R1 ─► Bracket order
 (IEX live |    bars, VWAP   ORB,          spread,    true count    1%/2%/4%      30 paths   anonymised  target + stop
  replay)       ATR, count   VWAP pullback cost...    ≥ +1.0        of equity     odds ≥40%  briefing    at the broker
        └──────── Flight recorder: every attempt, ghost outcome, order, veto call ─► data/records ─► scoreboard
 Guardian (separate process): daily loss, losers in a row, drawdown fuse, heartbeat, end of day, panic
```

- **Told in advance:** the level map removes earnings names and blocks the first 5 minutes, 10:00 ET data releases, FOMC afternoons, half days, and new entries after 15:45.
- **Observation (hazards):** spread blowout, dead volume, already ran 3× its daily range, too costly (target < 4× round-trip cost), stale, can't short, already in, 15-minute death cooldown.
- **Card counting:** every trade is signed as an aggressive buy (+) or sell (−). The running count covers the last 2 minutes. The *true count* is that count measured against the stock's own normal flow, combined as ⅔ stock + ⅓ market (SPY/QQQ).
- **Bet spread** (tournament mode):

  | True count | Bet |
  |---|---|
  | < 1 | sit out |
  | 1–2 | 1% of equity at risk |
  | 2–3 | 2% at risk |
  | ≥ 3 | **4% at risk (GO BIG)** |

  Caps: 5% of normal 5-minute volume, 2× equity notional, 6% total open risk. **Real mode** is ¼ of these.
- **Vetoes (local, veto-only):**
  - **Kronos-small** samples 30 next-30-minute paths from the last ~400 one-minute candles. It vetoes if fewer than 40% hit the target before the stop. About 5 s on the RTX 5060.
  - **Fin-R1** (7B finance reasoning model, run in Ollama) reads an anonymised briefing: no ticker, no dates, prices rescaled so the entry is 100. It vetoes only on explicit red flags. About 6 s.
  - Timeout, error or bad output always means *skip*.
- **Exits:**
  - stop and target sit at the broker
  - **checkpoint:** at +1R the stop moves to the entry
  - count flips against you for 30 s
  - stale after 30 minutes
  - flat by 15:55
- **Ghosts:** every setup the bot did *not* take is followed with the same exit rules. That's how vetoes and hazards get scored ("profit saved"), and how the dataset gets outcomes for skips too.

## The dataset (for training other models)

`data/records/<run>/<table>/*.parquet`, one folder per practice run, plus `live-paper` for live days:

| Table | One row per |
|---|---|
| `attempts` | jump pad that fired: every feature at that moment, hazards fired, gate, size, Kronos odds, Fin-R1 verdict, action taken |
| `outcomes` | resolved attempt (real or ghost): exit reason, R multiple, max favourable/adverse excursion, hold time |
| `trades` | real trade: fills, P&L, realistic P&L (after spread/slippage/fees) |
| `orders`, `veto_calls`, `guardian_events`, `daily` | the rest of the story; `veto_calls` keeps Fin-R1's full briefing and reasoning |

Every row has `run_id` and `version` (config hash + git commit). To load:
```python
import pandas as pd, glob
att = pd.concat(map(pd.read_parquet, glob.glob("data/records/*/attempts/*.parquet")))
out = pd.concat(map(pd.read_parquet, glob.glob("data/records/*/outcomes/*.parquet")))
df = att.merge(out, on="attempt_id")      # features -> outcome, ready for training
```

## Settings

All tuning lives in `config/default.yaml`. Changing any value gives a new version tag, so runs stay comparable. To override for a single run, write a small YAML file and pass `--config my.yaml`.

## Project layout

```
config/default.yaml      every threshold
config/universe.csv      S&P 500 + Nasdaq-100 with sectors (scripts/build_universe.py refreshes it)
src/dashcount/           the bot (engine.py is the heart; cli.py is the entry point)
vendor/Kronos/           Kronos model code (MIT); weights download on first use
tests/                   uv run pytest
docs/specs/              the design we agreed on
```

## Honest limits

- **The free plan streams 30 subscriptions, and trades and quotes count separately**, so the bot watches 14 stocks (trades + quotes) plus SPY/QQQ (trades). The paid plan lifts this: set `universe.watchlist_size` higher.
- **Live data is IEX-only on the free plan.** That's one exchange's slice, so the live count is noisier. Practice can compare `--feed iex` vs `--feed sip` to decide whether the $99/month full feed is worth it.
- **Kronos was trained on data up to June 2024.** Only judge it on July 2024 onwards. Its edge is small: think card counting, not a crystal ball.
- **The universe is today's index members** (survivorship bias in old practice runs).
- **Before real money** (only if you win): check RBI LRS rules on margin and shorting from India, and the US pattern-day-trader rule.
