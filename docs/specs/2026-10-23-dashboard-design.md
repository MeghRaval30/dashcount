# DashCount dashboard — design spec

Date: 2026-10-23 · Status: approved in chat ("go, build it with option A").

A native Windows desktop app (PySide6/Qt), retro pixel style inspired by
Minecraft (stone panels, inventory slots, hotbar, XP bars, TNT) and Uncharted
(parchment journal, treasure-map equity path, compass). All art is drawn in
code; no game assets, fonts or logos are used. Fonts: Press Start 2P and VT323
(SIL Open Font License, bundled with their licences).

## Panels

| Panel | Shows |
|---|---|
| Header | grass/dirt strip, title, chips: LIVE/WAITING/IDLE, ARMED/HALTED/FUSE/PANIC, budget equity, today P&L + R, NY/IST clocks, countdown to open/close, sound toggle |
| The Level | watchlist: price, change %, centered count meter (sellers ← → buyers, gold at ≥3σ "go big"), hazard spike (spread blowout / stale), sword when in a trade |
| Inventory | open trades as item slots: side, qty, R now, bar from stop (lava) through entry to target (diamond), checkpoint flag |
| Journal | parchment feed of setups, vetoes (Kronos odds, Fin-R1 reason), entries, exits, checkpoints, guardian events |
| Treasure map | equity path on parchment (today intraday; all live days), X marks for trades, compass rose |
| Scoreboard | today W/L, avg R, all-time live stats, veto scorecards (profit saved), latest practice run |
| Hotbar | START, STOP, TNT PANIC, RE-ARM, PRACTICE, REPORT |

## Data flow (read-only, decoupled)

The bot writes `data/state/live_snapshot.json` every 2 s and appends
`data/state/journal-<day>.jsonl`; the Guardian writes `guardian.json`; records
live under `data/records/`. The app polls these (1 s / 30 s). If the app
crashes, the bot is unaffected. The app never places orders itself.

## Controls

* START: spawns `uv run dashcount run-day` (detached) unless a fresh heartbeat
  shows it is already running.
* STOP: writes `data/state/stop.flag`; the bot flattens, waits ≤30 s for exits,
  finishes the day and writes its report.
* PANIC (confirm): runs `dashcount panic` (cancel all, close all, block).
* RE-ARM (confirm): runs `dashcount arm`.
* PRACTICE: dialog (dates, vetoes) → spawns `dashcount practice`.
* REPORT: opens the newest HTML report.

## Extras

8-bit sounds generated in code (taken / win / loss / panic), off by default.
`--demo` replays a synthetic morning into a demo data folder for previews.
Packaged as `DashCount.exe` (PyInstaller, one-folder) with a pixel-pickaxe
icon and a desktop shortcut.
