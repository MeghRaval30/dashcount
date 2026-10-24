"""Scoreboard: what happened in a run, as console text and a self-contained HTML page."""

from __future__ import annotations

import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .recorder import read_table


def _bucket(tc: float) -> str:
    if tc is None or not np.isfinite(tc):
        return "n/a"
    return "3+ (go big)" if tc >= 3 else ("2-3" if tc >= 2 else ("1-2" if tc >= 1 else "<1"))


def summarize(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").exists() else {}
    att = read_table(run_dir, "attempts")
    out = read_table(run_dir, "outcomes")
    trd = read_table(run_dir, "trades")
    daily = read_table(run_dir, "daily")
    vc = read_table(run_dir, "veto_calls")
    gev = read_table(run_dir, "guardian_events")
    s: dict = {"run": run_dir.name, "meta": {k: meta.get(k) for k in ("kind", "synthetic", "start", "end", "feed", "vetoes", "mode", "version")}}

    filled = trd[trd["qty_filled"] > 0] if len(trd) else trd
    s["days"] = int(len(daily))
    if len(daily):
        daily = daily.sort_values("day")
        s["start_equity"] = float(daily["start_equity"].iloc[0])
        s["end_equity"] = float(daily["end_equity"].iloc[-1])
        eq = pd.concat([pd.Series([s["start_equity"]]), daily["end_equity"]], ignore_index=True)
        s["equity_curve"] = [("start", s["start_equity"])] + [(str(d), float(e)) for d, e in zip(daily["day"], daily["end_equity"])]
        peak = eq.cummax()
        s["max_drawdown_pct"] = float(((eq - peak) / peak).min() * 100)
    s["trades"] = int(len(filled))
    if len(filled):
        s["pnl"] = float(filled["pnl"].sum())
        s["pnl_realistic"] = float(filled["pnl_realistic"].sum())
        s["win_rate"] = float((filled["pnl"] > 0).mean())
        s["avg_r"] = float(filled["r"].mean())
        s["best_trade"] = float(filled["pnl"].max())
        s["worst_trade"] = float(filled["pnl"].min())
        s["by_pad"] = filled.groupby("pad").agg(n=("r", "size"), win=("pnl", lambda x: float((x > 0).mean())),
                                               avg_r=("r", "mean"), pnl=("pnl", "sum")).round(3).reset_index().to_dict("records")
        s["exit_reasons"] = filled["exit_reason"].value_counts().to_dict()
        s["deaths_by_cause"] = filled[filled["pnl"] < 0]["exit_reason"].value_counts().to_dict()
    if len(att):
        s["attempts"] = int(len(att))
        s["actions"] = att["action"].map(lambda a: str(a).split(":")[0] + (":" + str(a).split(":")[1] if ":" in str(a) else "")).value_counts().to_dict()
        s["hazards_fired"] = pd.Series([h for hs in att["hazards"].dropna() for h in json.loads(hs)]).value_counts().to_dict() if "hazards" in att else {}
    if len(att) and len(out):
        o = out.copy()
        if "r_net" in o:                  # ghosts never paid costs: score them after estimated costs
            o["r"] = o["r_net"].where(o["is_ghost"] == True, o["r"])  # noqa: E712
        j = att.merge(o[["attempt_id", "r", "is_ghost", "exit_reason"]].rename(columns={"exit_reason": "exit"}), on="attempt_id")
        j["count_bucket"] = j["tc"].map(_bucket)
        j["hour"] = j["time_et"].str.slice(11, 13)
        s["by_count_bucket"] = j.groupby("count_bucket").agg(n=("r", "size"), avg_r=("r", "mean"), win=("r", lambda x: float((x > 0).mean()))).round(3).reset_index().to_dict("records")
        s["by_hour"] = j.groupby("hour").agg(n=("r", "size"), avg_r=("r", "mean")).round(3).reset_index().to_dict("records")
        # death map: which skip/take decisions and conditions lead where
        j["mkt"] = pd.cut(j["market_z_dir"], [-99, -1, 0, 1, 99], labels=["mkt<-1", "mkt -1..0", "mkt 0..1", "mkt>1"]).astype(str)
        dm = j.groupby(["pad", "count_bucket", "mkt"]).agg(n=("r", "size"), avg_r=("r", "mean"), win=("r", lambda x: float((x > 0).mean())))
        dm = dm[dm["n"] >= 3].sort_values("avg_r").round(3).reset_index()
        s["death_map"] = dm.head(10).to_dict("records")
        s["sweet_spots"] = dm.sort_values("avg_r", ascending=False).head(5).to_dict("records")
        s["skipped_ghost_r"] = j[j["is_ghost"] == True].groupby(j["action"].map(lambda a: str(a).split(":")[0]))["r"].agg(["size", "mean"]).round(3).reset_index().to_dict("records")  # noqa: E712
        # veto scorecards from ghosts
        cards = []
        for src in ("kronos", "finr1"):
            calls = vc[vc["source"] == src] if len(vc) else vc
            if not len(calls):
                continue
            vetoed = j[j["action"] == f"vetoed:{src}"]
            cards.append({
                "source": src, "calls": int(len(calls)),
                "veto_rate": float((calls["verdict"] == "veto").mean()),
                "unavailable": int((calls["verdict"] == "unavailable").sum()),
                "avg_latency_s": float(calls["latency_s"].mean()),
                "vetoes_resolved": int(len(vetoed)),
                "ghost_avg_r_of_vetoed": float(vetoed["r"].mean()) if len(vetoed) else None,
                "profit_saved_r": float(-vetoed["r"].sum()) if len(vetoed) else 0.0,
            })
        s["veto_cards"] = cards
    if len(gev):
        s["guardian_events"] = gev[["kind", "reason"]].value_counts().reset_index().rename(columns={0: "n"}).to_dict("records")
    return s


def console(s: dict) -> str:
    L = [f"== {s['run']} ==", f"   {s['meta']}"]
    if "end_equity" in s:
        L.append(f"   equity {s['start_equity']:,.2f} -> {s['end_equity']:,.2f}  over {s['days']} day(s); max drawdown {s.get('max_drawdown_pct', 0):.1f}%")
    L.append(f"   trades {s['trades']}" + (f", win rate {s['win_rate']:.0%}, avg {s['avg_r']:+.2f}R, paper P&L {s['pnl']:+,.2f}, realistic {s['pnl_realistic']:+,.2f}" if s.get("trades") else ""))
    if s.get("actions"):
        L.append(f"   attempts {s['attempts']}: " + ", ".join(f"{k} {v}" for k, v in s["actions"].items()))
    for c in s.get("veto_cards", []):
        L.append(f"   veto {c['source']}: {c['calls']} calls, veto rate {c['veto_rate']:.0%}, avg {c['avg_latency_s']:.1f}s, "
                 f"profit saved {c['profit_saved_r']:+.1f}R over {c['vetoes_resolved']} vetoes")
    return "\n".join(L)


def _table(rows: list[dict]) -> str:
    if not rows:
        return "<p class=muted>none yet</p>"
    cols = list(rows[0].keys())
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(_fmt(r.get(c)))}</td>" for c in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:,.3f}" if abs(v) < 100 else f"{v:,.2f}"
    return str(v)


def _kv(d: dict) -> str:
    return _table([{"what": k, "count": v} for k, v in d.items()]) if d else "<p class=muted>none</p>"


def _curve(points: list[tuple[str, float]]) -> str:
    if len(points) < 2:
        return "<p class=muted>needs at least two days</p>"
    w, h, pad = 720, 220, 30
    ys = [p[1] for p in points]
    lo, hi = min(ys), max(ys)
    hi = hi if hi > lo else lo + 1
    xy = [(pad + i * (w - 2 * pad) / (len(ys) - 1), h - pad - (y - lo) / (hi - lo) * (h - 2 * pad)) for i, y in enumerate(ys)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="equity curve" style="width:100%;max-width:{w}px">'
            f'<polyline fill="none" stroke="var(--accent)" stroke-width="2.5" points="{path}"/>'
            f'<text x="{pad}" y="16" class="lbl">{hi:,.0f}</text><text x="{pad}" y="{h - 6}" class="lbl">{lo:,.0f}</text>'
            f'<text x="{w - pad}" y="{h - 6}" text-anchor="end" class="lbl">{html.escape(points[-1][0])}</text></svg>')


def render_html(s: dict) -> str:
    stat = lambda k, label, fmt: f"<div class=stat><div class=v>{fmt(s[k])}</div><div class=k>{label}</div></div>" if k in s else ""  # noqa: E731
    stats = "".join([
        stat("end_equity", "end equity", lambda v: f"${v:,.0f}"),
        stat("pnl", "paper P&L", lambda v: f"{v:+,.0f}"),
        stat("pnl_realistic", "realistic P&L", lambda v: f"{v:+,.0f}"),
        stat("trades", "trades", str),
        stat("win_rate", "win rate", lambda v: f"{v:.0%}"),
        stat("avg_r", "avg R", lambda v: f"{v:+.2f}"),
        stat("max_drawdown_pct", "max drawdown", lambda v: f"{v:.1f}%"),
    ])
    sec = lambda t, body: f"<section><h2>{t}</h2>{body}</section>"  # noqa: E731
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>DashCount scoreboard</title><style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a66;--line:#e4e1da;--accent:#1f6feb;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#131416;--fg:#ececea;--muted:#9a9a96;--line:#2a2c30;--accent:#6ea8ff;--card:#1b1d20}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:0;padding:24px 16px;max-width:1000px;margin:auto}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px}} .muted{{color:var(--muted)}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:16px 0}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}} .v{{font-size:20px;font-weight:600}} .k{{color:var(--muted);font-size:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px;background:var(--card)}} th,td{{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left}}
section{{overflow-x:auto}} .lbl{{fill:var(--muted);font-size:11px}}
</style></head><body>
<h1>DashCount scoreboard</h1><div class=muted>{html.escape(s['run'])} · {html.escape(json.dumps(s['meta'], default=str))}</div>
<div class=stats>{stats}</div>
{sec("Equity curve", _curve(s.get("equity_curve", [])))}
{sec("By jump pad", _table(s.get("by_pad", [])))}
{sec("By true count (all attempts incl. ghosts)", _table(s.get("by_count_bucket", [])))}
{sec("Veto scorecards (ghost-scored)", _table(s.get("veto_cards", [])))}
{sec("Death map: worst conditions (n ≥ 3)", _table(s.get("death_map", [])))}
{sec("Sweet spots", _table(s.get("sweet_spots", [])))}
{sec("What skipped setups would have done (ghosts)", _table(s.get("skipped_ghost_r", [])))}
{sec("Exit reasons", _kv(s.get("exit_reasons", {})))}
{sec("Deaths by cause", _kv(s.get("deaths_by_cause", {})))}
{sec("Decisions", _kv(s.get("actions", {})))}
{sec("Hazards fired", _kv(s.get("hazards_fired", {})))}
{sec("By hour (ET)", _table(s.get("by_hour", [])))}
{sec("Guardian events", _table(s.get("guardian_events", [])))}
<p class=muted>Paper P&amp;L is what the account shows; realistic P&amp;L subtracts spread, slippage and fees (practice runs already use the harsh simulator). Ghosts are setups the bot did not take, followed with the same exit rules.</p>
</body></html>"""


def write_report(run_dir: Path, out_dir: Path | None = None) -> tuple[Path, dict]:
    s = summarize(run_dir)
    out_dir = Path(out_dir) if out_dir else Path(run_dir).parents[2] / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{Path(run_dir).name}.html"
    p.write_text(render_html(s), encoding="utf-8")
    return p, s
