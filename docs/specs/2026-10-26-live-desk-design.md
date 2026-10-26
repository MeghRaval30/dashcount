# DashCount dashboard: the live DESK (design spec)

Date: 2026-10-26 · Status: design approved in chat (parts 1–3); spec awaiting review.

The first of three "professional tools" for the dashboard. The other two, the
analysis LAB and the pre-open MAP ROOM, get their own spec → plan → build
cycles later. The retro Minecraft × Uncharted style stays.

## 1. Intent

What the user said they want the tools for: **watch and control the live
session**, **analyse and improve the bot**, **show it off**, and **plan before
the open**. The DESK covers the first and must look the part (the third).

Success means that during a live session the user can see at a glance:
1. how close each safety limit is;
2. every order and fill at the broker, and how far fills slipped from the plan;
3. whether every part of the system is working;
4. anything that needs attention, as an alert they can't miss.

Constraints:
- **The method must be fully algorithmic:** no manual trading controls. STOP
  and PANIC remain the only overrides, through the bot's own commands.
- The app **never places or cancels orders**. Every Alpaca call is a read.
- **Leave the live bot room:** Alpaca allows 200 requests a minute per account,
  shared with the bot, so the DESK uses at most about 12.
- Nothing may freeze the window: network and process checks run off the UI
  thread.

## 2. Layout

- A row of pixel tabs under the header:
  - **TABLE:** today's screen, unchanged.
  - **DESK:** new.
  - LAB and MAP ROOM appear only once they are built.
- Keys `1` and `2` switch screens.
- The header chips and the hotbar stay visible on every screen.
- When alerts arrive while TABLE is showing, the DESK tab shows an unread
  count ("DESK 3"). Opening DESK clears it.
- DESK is a 2 × 2 grid: RISK METERS (top left), ORDERS & FILLS (top right,
  widest), SYSTEM HEALTH (bottom left), ALERTS (bottom right).
- Panels use the existing pixel kit: stone panels, gold titles, parchment for
  the alerts list.

## 3. Data sources

| Panel | Source |
|---|---|
| Risk meters | `live_snapshot.json` (budget equity, start equity, open trades' `risk_dollars`, qty, price) + `guardian.json` (`peak_equity`, `losers_in_row`, `armed`, `halted_day`, `panic`) + limits from `config/default.yaml` for the current mode |
| Orders & fills | **Alpaca paper account (read-only):** today's orders with bracket legs, and positions. Compared against `live_snapshot.json`, `bot_positions.json` and the journal |
| System health | File ages (`heartbeat.json`, `live_snapshot.json`, `guardian.json`), snapshot per-stock `last_trade_age`, the Alpaca poller's own timings and errors, a local Ollama check, and the new snapshot field `veto_health` |
| Alerts | Rules evaluated every second over all of the above |

### 3.1 Bot additions (small, additive; older dashboards ignore them)

1. **`veto_health`** in the snapshot, per veto source (`kronos`, `finr1`):
   ```json
   {"last_s": 5.1, "avg_s": 5.4, "calls": 12, "errors": 0, "timeouts": 0, "last_ts": 1792699280.2}
   ```
   - Updated in `Engine._after_veto` from each `VetoResult` (its latency and
     verdict).
   - An `unavailable` verdict counts as an error, or as a timeout when its
     reason says so.
2. **The journal's `taken` event** gains:
   - `trade_id` (the client order id, `dc-<ts>-<n>`);
   - `entry` (the planned price), `stop`, `target` and `qty`.

   The blotter uses them for slippage. The Guardian needs no change: it re-saves
   `guardian.json` every 5 s during the session, so the file's age is its
   heartbeat.

### 3.2 Alpaca polling (`broker_feed.py`)

- **Base URL:** `https://paper-api.alpaca.markets`, or the live URL if
  `ALPACA_PAPER=false`. Keys come from `.env`, as the chart fallback already
  does.
- **Endpoints:**
  - `GET /v2/orders?status=all&after=<today 00:00 ET>&nested=true&direction=asc&limit=500`
  - `GET /v2/positions`
- **How often:** every 10 s from 30 minutes before the open to 30 minutes after
  the close, and every 5 minutes otherwise. That's at most 12 requests a minute.
- **Errors:**
  - timeout 5 s;
  - on an error, back off to 30 s, then 60 s, and return to normal after a success;
  - HTTP 429 means wait for the reset header.
- **Raw JSON only** (alpaca-py's models crash on the TXSE exchange).
- **Runs on its own daemon thread.** It publishes an immutable snapshot
  (`orders`, `positions`, `ok`, `last_ok_ts`, `last_latency_ms`,
  `errors_10min`, `last_error`) that the UI reads under a lock.

## 4. Panels

### 4.1 Risk meters

Each meter is a pixel bar showing the *share of the limit used*: green below
50%, gold 50–75%, red at 75% or more. The limits come from the config for the
current mode (tournament / real).

| Meter | Used | Limit |
|---|---|---|
| Day loss | `max(0, −pnl)` | `daily_loss_pct` × start-of-day equity |
| Losing streak | `losers_in_row` (shown as hearts) | `losers_in_row` limit |
| Drawdown fuse | `max(0, peak − equity)` | `drawdown_pct` × peak |
| Open risk | Σ `risk_dollars` of open trades | `max_open_risk_pct` × equity |
| Exposure | Σ \|qty × price\| of open trades | `max_notional_x` × equity, labelled "per-trade cap, shown for scale" |

- Each meter shows a text label, for example "$212 of $800 (27%)".
- **Greyed out** with "bot last seen N min ago" when the snapshot is more than
  90 s old.

### 4.2 Orders & fills blotter

**Rows**
- **One row per entry order:** time (NY), stock, side, quantity filled/ordered,
  type, limit price, status, average fill price, slippage.
- **Bracket legs** (take-profit and stop) are indented under their entry.
- **Exits:** a later market exit for the same trade appears as a leg.

**Slippage, positive = worse for us**
- **Entry:** against the journal's planned `entry` for that `trade_id`.
- **Take-profit:** against its limit price.
- **Stop and market exits:** against the stop price, or the last planned stop.
- Shown in $ per share and in R (using the planned risk, |entry − stop|).
- No plan found (an older bot, or a foreign order) means "—".

**Status colours**
- filled: green
- open, accepted or new: gold
- canceled or expired: grey
- rejected: red

**Summary line:** orders today, fills, average entry slippage (bps and R), and
rejections.

**Reconciliation**
- Alpaca's open positions are compared with the bot's view (snapshot `trades`
  and `bot_positions.json`).
- A symbol held at the broker that the bot doesn't know, or a bot trade with
  no broker position after 30 s, shows a **red banner** naming the symbol.
  (The Guardian already closes unknown positions after 30 s; the banner makes
  it visible.)

**Empty states:** "No orders today", "Alpaca keys not set (add them to .env)",
and "Alpaca not reachable: last good data N min ago".

### 4.3 System health

One row each, with a pixel lamp (green / gold / red; grey for "not expected
right now", e.g. outside the session):

| Row | Green | Gold | Red |
|---|---|---|---|
| Bot (heartbeat age) | < 20 s | < 90 s | ≥ 90 s during the session |
| Guardian (`guardian.json` age) | < 20 s | < 60 s | ≥ 60 s during the session |
| Market data | no stock silent > 180 s | 1–3 silent | > 3 silent, or a held stock silent |
| Alpaca | last call OK, < 1.5 s | an error in 10 min or > 1.5 s | ≥ 3 errors in 10 min or nothing OK for 60 s |
| Ollama (`GET 127.0.0.1:11434/api/tags` every 30 s, off the UI thread) | up and the Fin-R1 model listed | up, model missing | down during the session |
| Kronos / Fin-R1 (`veto_health`) | last < 80% of the timeout, no errors | slow, or 1–2 errors/timeouts | ≥ 3 errors/timeouts today |

- Each row shows its number, for example "Kronos 5.1 s (avg 5.4, 12 calls)".
- An older bot without `veto_health` shows "not reported".

### 4.4 Alerts centre

**Levels:** info, warn, danger. Each alert has a level, a pixel icon, the time
(NY and IST) and a sentence.

**Default rules**

| id | Level | Fires when | Re-arms when |
|---|---|---|---|
| `trade_open` | info | a `taken` journal event arrives | (each event) |
| `trade_close` | info | an `exit` journal event arrives (P&L and R in the text) | (each event) |
| `day_loss_50` / `day_loss_75` | warn / danger | Day-loss meter ≥ 50% / ≥ 75% | it drops below again |
| `streak_3` | warn | losing streak = limit − 1 | streak resets |
| `fuse_50` / `fuse_75` | warn / danger | Drawdown meter ≥ 50% / ≥ 75% | it drops below again |
| `bot_silent` | danger | heartbeat > 60 s during the session | heartbeat fresh |
| `data_stale_held` | danger | a held stock silent > 180 s | data resumes |
| `order_rejected` | danger | any order status `rejected` | (each order) |
| `alpaca_errors` | warn | ≥ 3 Alpaca errors in 10 min | an OK call after a calm minute |
| `mismatch` | danger | the reconciliation banner is up | banner clears |
| `ollama_down` | warn | Ollama down during the session | Ollama up |
| `guardian_stop` | danger | `guardian.json` shows halted today, panic, or not armed | state clears |

**Behaviour**
- **No spamming:** a condition alert fires once and re-arms only after its
  condition clears.
- **Sounds:** info → "click", warn → "taken", danger → "panic", through the
  existing SOUND chip (off by default).
- **Saved:** alerts are appended to `data/state/alerts-<day>.jsonl` (`ts`, `id`,
  `level`, `text`) and reloaded on start, so the list survives restarts and
  shows what happened while the app was closed.
- **Display:** newest first, with the unread count on the DESK tab.
- **Phone push** stays with the bot (existing ntfy), which works when the app
  is closed.

## 5. Code units

| Unit | Does | Depends on |
|---|---|---|
| `dashcount_ui/risk.py` | Pure: snapshot + guardian + limits → list of meters (`name, used, limit, frac, text, level`) | nothing UI |
| `dashcount_ui/alerts.py` | Pure rule engine: previous state + current inputs → new alerts; persistence helpers | nothing UI |
| `dashcount_ui/reconcile.py` | Pure: broker orders/positions + bot view + journal plans → blotter rows, slippage, mismatches | nothing UI |
| `dashcount_ui/broker_feed.py` | Read-only Alpaca poller thread (httpx), backoff, timings | httpx, `.env` |
| `dashcount_ui/health.py` | File ages, Ollama check (thread), row levels | stdlib, httpx |
| `dashcount_ui/desk.py` | DESK screen: four panel widgets drawn with `pixel.py` | the above |
| `dashcount_ui/app.py` | Tab bar, stacked screens, keys, unread badge; TABLE unchanged | `desk.py` |
| `dashcount/engine.py` | `veto_health` in the snapshot; extra fields on the `taken` journal event | — |
| `dashcount_ui/demo.py` | Demo orders and alerts, so DESK can be previewed without Alpaca | — |

## 6. Errors

- Missing keys or no network: the blotter shows the empty state, health turns
  red, and the other panels work normally.
- A stale or missing snapshot greys out the meters with "bot last seen…". An
  outside-session state is grey, not red.
- Malformed JSON (a partial write): keep the last good value, as the
  datasource already does.
- An exception inside one panel's update is logged and that panel shows
  "error: …". The window never crashes.

## 7. Testing

- **Unit tests:**
  - **Risk maths:** every meter, levels, the stale snapshot.
  - **Alert rules:** fire once, re-arm, levels, persistence round trip.
  - **Reconciliation:** slippage signs for long and short, entry vs legs,
    mismatch both ways, missing plan.
  - **Broker feed:** parsing and backoff with `httpx.MockTransport` (like
    `tests/test_alpaca.py`).
  - **Health:** levels from file ages, Ollama up/down via a mock transport.
- **UI smoke test:** build the DESK on demo data, render it offscreen, and
  assert that no panel shows "error". `--screenshot out.png --screen desk`
  saves it for review.
- **Packaging:** rebuild `DashCount.exe` (`DashCount.spec`), write
  `home.txt`, launch it and confirm it starts.

## 8. Out of scope (this spec)

- The LAB and MAP ROOM screens.
- Editing settings in the app.
- Placing or cancelling orders from the app.
- Phone push from the app.
- Multiple accounts.
