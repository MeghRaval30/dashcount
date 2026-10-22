# DashCount — design spec (v1)

Date: 2026-10-22 · Status: approved in chat (parts 1–6), built overnight.

Geometry Dash × card counting: an intraday, fully algorithmic US-stock bot that
**skips obstacles**, **counts aggressive order flow**, **bets big only when the
count is extreme**, lets **local AI models veto** trades, and **records every
attempt** as a training dataset.

## 0. Context and goals

* Three-friend competition. Each method must differ; the friends are not doing
  pure algorithmic trading. Scoring: **highest profit**. The contest runs on
  **paper money**; the winner trades **real money** afterwards.
* US stocks, regular hours (09:30–16:00 ET), long **and** short, intraday only
  (flat before the close).
* Exits are profit target / stop / count flip / stale / end-of-day — no fixed
  5-minute cap.
* The LLM never trades; it can only veto. The veto models run **locally**.
* Success = the bot runs unattended on paper, its paper profit is what the
  contest counts, its *realistic* profit (after spread, fees, slippage) tells
  whether the win would survive real money, and every decision is recorded.

## 1. Architecture

```
 Level map (pre-market) ─► watchlist (≤30) + blocked windows
        │
 Market data ─► Features ─► Brain ─► Veto chain ─► Executor ─► Broker
 (live|replay)  bars, VWAP   pads,     Kronos →      brackets,   (Alpaca paper |
                hazards      gates,    Fin-R1        exits,       harsh simulator)
                count        sizing                  checkpoints
        └───────────── Recorder (flight recorder) ─► dataset ─► Scoreboard
 Guardian: account kill switches, independent of Brain/LLM; can flatten anytime.
```

Principles:
1. **Practice and live run the same code.** Only the data source (live stream vs
   replay) and the broker (Alpaca paper vs simulator) are swapped.
2. **Safety never depends on intelligence.** Guardian and broker-side stops work
   if the Brain, the models or the laptop die.
3. **Only clear dangers block.** Everything uncertain becomes a recorded feature.
4. **Point-in-time.** Every feature is computed by streaming code that has only
   seen past events.

## 2. Data

* Broker/data: **Alpaca** (alpaca-py). Free plan: live stream = IEX only,
  max 30 symbols; historical since 2016 (latest 15 min restricted on SIP).
* Live feed: IEX trades + quotes for the watchlist. Bars are **built locally from
  trades** (identical in live and replay).
* Historical store (`data/store/`, Parquet):
  * `daily/feed=sip|iex/` — daily bars for the universe.
  * `minute/feed=sip/date=YYYY-MM-DD/` — 1-min bars incl. extended hours for the
    universe (ranking, volume profiles, liquidity).
  * `minute/feed=iex/` — previous-session 1-min bars for watchlist symbols
    (Kronos context, same feed as live).
  * `ticks/feed=iex|sip/date=…/symbol=…` — trades (+ aggressor side) and quotes
    for each day's watchlist, regular hours.
  * `earnings/date=….json` — earnings calendar cache (Nasdaq public calendar).
* Universe: S&P 500 ∪ Nasdaq-100 (current constituents, with GICS sector),
  price > $10, plus SPY and QQQ. Known bias: current constituents (survivorship);
  acceptable for an intraday strategy, documented.
* Downloads are resumable and rate-limited (≤190 req/min). Start with 3 months,
  backfill in the background.

## 3. Level map (pre-market, "told in advance")

Run at 09:20 ET using data up to 09:05 ET (respects the free-plan 15-min delay;
identical in practice).

* Candidates: universe members with prev close > $10 and 20-day avg dollar
  volume above a floor.
* **In-play score** = z(|pre-market gap %| / ATR%) + z(log pre-market rel. volume)
  + z(ATR%). Top **28** + SPY + QQQ.
* Removed: earnings within ±1 trading day (if the calendar is unavailable, drop
  stocks with |gap| > 4% as a proxy and flag the day).
* Shorts allowed only for Alpaca `easy_to_borrow` names.
* Blocked windows (ET): 09:30–09:35 observe only; no new entries after 15:45;
  10:00 scheduled releases −2/+10 min; FOMC decision days 13:55–15:00;
  half days from the exchange calendar (entries stop 12:45, flatten 12:55).
* Flags recorded, not blocked: monthly options expiry, Monday, Friday.

## 4. Features

* **Bars**: 1-min OHLCV built from trades; session VWAP; 1-min ATR(14).
* **Daily context**: ATR(14) daily, prev close, today's open.
* **Spread**: sampled once per second; "normal" = rolling median of the last
  30 minutes of samples (seeded from stored per-symbol medians by time bucket).
* **Volume normal**: 20-day median volume per 5-min bucket from SIP minute bars,
  scaled by the symbol's feed share (IEX volume / SIP volume, 5-day) when the
  live feed is IEX.
* **The count**:
  * Trade sign: quote rule (≥ ask → +, ≤ bid → −, else vs mid), tick rule when
    no fresh quote (≤ 5 s) or at the mid.
  * Stock count = signed volume over the last 120 s.
  * Normalisation = session EW RMS of the 120-s signed volume (sampled every
    10 s, half-life 30 min), floored by a prior
    κ·sqrt(normal 2-min volume × avg trade size) (κ = 3).
  * Market count = mean of SPY and QQQ z-counts.
  * **True count** (in trade direction) = ⅔·stock_z + ⅓·market_z.
  * Order-book imbalance recorded as a feature only.

## 5. Jump pads (setups) — evaluated at each 1-min bar close

* **Opening-range breakout (ORB)**: range = 09:30–09:45. Window 09:45–11:30.
  Long when a bar closes above OR high with bar volume ≥ 1.5× normal 1-min
  volume; short mirrored. Entry = bar close; stop = OR midpoint; target = 2R.
  One per direction per symbol per day. Skip if risk > 0.5× daily ATR or
  risk < 0.05% of price.
* **VWAP pullback**: window 10:00–15:30. Trend up = ≥ 80% of the last 30 closes
  above VWAP and session high within the last 45 min. Trigger = bar low within
  0.15× ATR1m of VWAP, close > VWAP, close > open. Entry = close; stop =
  min(bar low, VWAP) − 0.5× ATR1m; target = 2R. Mirror for shorts. Cooldown
  20 min per symbol/pad.
* Candidate = {symbol, direction, pad, entry, stop, target, R}.

## 6. Hazards ("observation") — any one ⇒ skip

| Hazard | Rule |
|---|---|
| spread_blowout | spread > 2× normal, or > 0.5% of price |
| dead_volume | last 5 bars' volume < 0.5× normal |
| already_ran | |price − open| > 3× daily ATR |
| too_costly | target distance < 4× est. round-trip cost (spread + 2× slippage + fees) |
| stale | no trade for 60 s (SIP) / 180 s (IEX) |
| cant_short | short and not easy-to-borrow |
| already_in | open position, pending order, or ghost-free cooldown in symbol |
| death_cooldown | < 15 min since a losing exit in this symbol |
| blocked_window | level-map window |
| halted_by_guardian | Guardian day-halt, fuse or panic |

## 7. Count gates and sizing ("the bet spread")

* Skip if true count < 1.0, or market count ≤ −1.5 against the trade.
* Risk (% of equity lost if stopped):

| True count | Tournament | Real |
|---|---|---|
| < 1.0 | skip | skip |
| 1.0–2.0 | 1% | 0.25% |
| 2.0–3.0 | 2% | 0.375% |
| ≥ 3.0 | 4% (GO BIG) | 0.5% |

* Caps: qty ≤ 5% of normal SIP 5-min volume (both modes); notional ≤ 2× equity
  (tournament) / 1× (real); total open risk ≤ 6% / 1.5%; broker buying power
  (shorts valued at ask × 1.03).
* Size depends only on the count — never on recent wins/losses.

## 8. Veto chain (local models) — veto-only

1. **Kronos** (NeoQuasar Kronos-small, MIT). Context: up to 400 most recent
   1-min bars (previous session + today, same feed). Sample **30** paths ×
   **30** minutes (batch of 30 independent samples). Odds = paths hitting target
   before stop / paths that hit either (same-bar ambiguity = stop). Veto if
   odds < 0.40 with ≥ 6 decided paths; else allow (recorded "uncertain").
   Deterministic seed per candidate. Judged only on dates ≥ 2024-07-01.
2. **Fin-R1** (SUFE-AIFLM-Lab, bartowski Q4_K_M GGUF) via Ollama, only if
   Kronos allows. Anonymised briefing (no ticker/dates; prices rescaled so
   entry = 100; sector kept), JSON-schema constrained answer
   `{reasoning, verdict, reason_code, confidence, explanation}`, temperature 0.
   Memory = 5 most similar resolved past attempts (same pad; standardised
   feature distance; point-in-time).
* Timeout (Kronos 10 s, Fin-R1 20 s), error, or bad output ⇒ skip
  (`veto_unavailable`). Answers cached by briefing hash.
* "Missed the jump": after allow, skip if price moved > 0.25R past entry.
* Ghost scoring per veto: profit saved = −Σ ghost R of vetoed trades. Coward
  check: veto rate > 60%. Switch off a veto after 50 vetoes if it saves ≤ 0.

## 9. Execution and exits

* Entry: limit = entry ± 0.05R (marketable), bracket with take-profit limit and
  stop (market) legs, DAY. Unfilled after 30 s ⇒ cancel. Partial ⇒ cancel rest,
  resize exits to filled qty.
* Exits (first wins): stop (broker), target (broker), **count flip** (true count
  in trade direction ≤ −1.0 for 30 s), **stale** (30 min and MFE < 0.5R),
  **EOD** 15:55 (12:55 half days).
* **Checkpoint**: at +1R move stop to entry (cancel legs, submit OCO).
* Same-stock cooldown 15 min after a losing exit.
* Records MFE/MAE in R for every trade and ghost.

## 10. Guardian (separate process live; same rules in practice)

| Trigger | Tournament | Real | Action |
|---|---|---|---|
| Daily loss vs start-of-day equity | −8% | −2% | flatten, halt for the day |
| Losing trades in a row | 4 | 3 | halt for the day |
| Drawdown from peak equity | −25% | −10% | flatten, disarm until `dashcount arm` |
| Bot heartbeat missing 60 s with positions | | | flatten |
| Unknown position (> 30 s) | | | close it, alert |
| 15:55 ET (12:55 half days) | | | flatten; alert if not flat by 15:58 |
| Panic (`dashcount panic`) | | | flatten, stop |

State lives in `data/state/` (heartbeat, halt flags, peak equity, armed).

## 11. Flight recorder and scoreboard

Tables (Parquet under `data/records/<run>/<table>/`): `attempts`, `orders`,
`trades`, `veto_calls`, `guardian_events`, `daily`, plus the tick store.
Every row has `run_id` and `version` (config hash + git commit). Ghost outcomes
are flagged `is_ghost`. Scoreboard (HTML + console): paper vs realistic P&L,
equity curve, win rate / avg R by pad / count bucket / time of day, deaths by
cause, death map, veto scorecards, IEX vs SIP comparison.

## 12. Practice mode

* Replay engine + harsh simulator: market/stop fills at the worse side of the
  quote + slippage (2 bps + 10 bps × qty / normal 1-min volume, cap 50 bps);
  limit fills only when price trades through; 300 ms order latency; SEC fee
  ($27.80/M on sells) and FINRA TAF ($0.000166/share, cap $8.30); shorts only on
  easy-to-borrow names (current flags; approximate for old dates).
* Periods: train 2024-07-01 → 2025-12-31; held-back test 2026-01-01 → 2026-09-30.
* Veto latency is simulated: the decision lands at t + measured latency.

## 13. Operations

* Runs on the user's laptop (RTX 5060 for Fin-R1). Processes: bot, guardian,
  Ollama. Windows Task Scheduler task at 09:00 America/New_York
  (19:00/20:00 IST). Laptop: plugged in, GPU Standard/Ultimate, no sleep.
* Alerts: ntfy push (optional `NTFY_TOPIC`).
* Paper by default. Real money needs `ALPACA_PAPER=false` **and** the
  `--real-money` flag. Before real money from India: check RBI LRS limits on
  margin/shorting and the US PDT rule.

## 14. Implementation notes (v1 build, 2026-10-22)

Decisions made while building, measured on the RTX 5060 laptop:

* **Count normalisation**: the session EW RMS is *seeded* at
  κ·sqrt(V·s) (κ = 3) and *floored* at the coin-flip level sqrt(V·s).
* **Kronos**: Kronos-small (≈4.6 s for 30 paths × 30 min; Kronos-base ≈16 s).
  Odds use predicted *closes* only — the model's predicted highs/lows are noisy
  (high < low happens) and would fake touches. Runs are serialised (the model
  is not thread-safe) and the weights are loaded before the first decision.
* **Fin-R1**: ≈6 s per verdict (Q4_K_M). A first prompt vetoed clean setups
  ("coward"); the prompt now makes ALLOW the default and lists explicit,
  numeric red flags, and the briefing adds pre-computed facts (net move, steps
  with the trade, direction changes, memory wins/losses).
* **Live safety extras**: Windows is asked not to sleep while trading; 25 engine
  errors in a day ⇒ flatten and halt; synthetic demo runs never enter memory.
* **Data quirks handled**: pandas 3 time units (epoch conversion is
  unit-independent), quote de-duplication keeps the first quote, late live
  prints are folded into the open minute.

## 15. Out of scope for v1

Singapore, a third pad, models trained on the dataset, Kronos fine-tuning, paid
SIP live feed (decided by the scoreboard).
